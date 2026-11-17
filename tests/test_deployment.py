"""Deployment contract checks: render real Compose config without a daemon/GPU.

Set CVAT_CHECKOUT to a v2.78.0 checkout to also validate CVAT's actual overlays.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / 'plugin/VERSION').read_text().strip()
IMAGE = f'cvat/ui:sam2-v2.78.0-{VERSION}'


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / 'sam2 checkout'
    root.mkdir()
    shutil.copytree(ROOT / 'scripts', root / 'scripts')
    shutil.copytree(ROOT / 'deployment', root / 'deployment')
    for path in ROOT.glob('docker-compose*.yml'):
        shutil.copy(path, root / path.name)
    (root / '.env').write_text(
        'CVAT_HOST=annotate.example.org\nACME_EMAIL=ops@example.org\n'
        'CVAT_VERSION=wrong\nMAX_PROPAGATION_FRAMES=32\nMAX_OBJECTS=2\n'
        'MAX_QUEUE=3\nEMBEDDING_CACHE_SIZE=1\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CVAT_', 'MAX_', 'EMBEDDING_'))}
    return root, env


def render(deployment, project, https):
    if shutil.which('docker') is None:
        pytest.skip('Docker Compose CLI unavailable')
    root, env = deployment
    args = [str(root / 'scripts/compose.sh')]
    if https:
        args.append('--https')
    args += [project, 'config', '--format', 'json']
    return json.loads(subprocess.check_output(args, env=env, cwd='/tmp', text=True))


@pytest.mark.parametrize('https', [False, True])
def test_sam_configuration(deployment, https):
    config = render(deployment, 'sam2', https)
    svc = config['services']['sam2-service']
    assert svc['environment']['CVAT_HOST_HEADER'] == 'annotate.example.org'
    assert svc['environment']['MAX_PROPAGATION_FRAMES'] == '32'
    assert svc['environment']['MAX_OBJECTS'] == '2'
    assert svc['environment']['MAX_QUEUE'] == '3'
    assert svc['environment']['EMBEDDING_CACHE_SIZE'] == '1'
    assert svc['ports'][0]['host_ip'] == '127.0.0.1'
    assert svc['ports'][0]['published'] == '8000'
    assert svc['volumes'][0]['source'] == str(deployment[0] / 'checkpoints')
    assert config['networks']['cvat_cvat']['external'] is True
    labels = svc['labels']
    assert labels['traefik.http.routers.sam2.rule'] == (
        'Host(`annotate.example.org`) && Path(`/sam2/v1/video/propagate-job`)'
    )
    assert labels['traefik.http.routers.sam2.entrypoints'] == ('websecure' if https else 'web')
    assert ('traefik.http.routers.sam2.tls.certresolver' in labels) is https


@pytest.mark.parametrize('https', [False, True])
def test_actual_cvat_configuration(deployment, https):
    checkout = os.environ.get('CVAT_CHECKOUT')
    if not checkout:
        pytest.skip('set CVAT_CHECKOUT to test actual CVAT v2.78.0 Compose files')
    deployment[1]['CVAT_DIR'] = checkout
    config = render(deployment, 'cvat', https)
    services = config['services']
    assert config['name'] == 'cvat'
    assert config['networks']['cvat']['name'] == 'cvat_cvat'
    assert services['cvat_ui']['image'] == IMAGE
    assert services['cvat_ui']['pull_policy'] == 'never'
    assert services['cvat_server']['image'] == 'cvat/server:v2.78.0'
    assert str(services['cvat_server']['environment']['CVAT_SERVERLESS']) == '1'
    assert services['cvat_server']['environment']['DJANGO_SETTINGS_MODULE'] == 'cvat_accounts.settings'
    accounts = next(v for v in services['cvat_server']['volumes']
                    if v['target'] == '/home/django/cvat_accounts')
    assert accounts['source'] == str(deployment[0] / 'deployment/cvat_accounts')
    assert accounts['read_only'] is True
    assert accounts['bind'].get('create_host_path', False) is False
    assert services['nuclio']['image'] == 'quay.io/nuclio/dashboard:1.16.3-amd64'
    for name in ['cvat_server', 'cvat_worker_annotation']:
        assert services[name]['environment']['CVAT_NUCLIO_INVOKE_METHOD'] == 'dashboard'
    assert not services['nuclio'].get('ports')
    assert services['nuclio']['labels']['traefik.enable'] == 'false'
    for name in ['cvat', 'cvat-ui']:
        svc = services['cvat_server' if name == 'cvat' else 'cvat_ui']
        assert svc['labels'][f'traefik.http.routers.{name}.entrypoints'] == ('websecure' if https else 'web')
        assert 'annotate.example.org' in svc['labels'][f'traefik.http.routers.{name}.rule']


def test_nuclio_function_has_no_host_port():
    function = yaml.safe_load((ROOT / 'nuclio/sam2-proxy-interactor/function.yaml').read_text())
    triggers = function['spec']['triggers']
    http_triggers = [trigger for trigger in triggers.values() if trigger['kind'] == 'http']
    assert http_triggers
    for trigger in http_triggers:
        assert trigger['attributes']['disablePortPublishing'] is True
        # An explicit port takes precedence over disablePortPublishing in Nuclio 1.16.3.
        assert not trigger['attributes'].get('port')


def test_security_overlay_removes_existing_dashboard_publication(tmp_path):
    if shutil.which('docker') is None:
        pytest.skip('Docker Compose CLI unavailable')
    base = tmp_path / 'compose.yml'
    base.write_text('''services:
  cvat_server:
    image: cvat/server:v2.78.0
    environment:
      CVAT_NUCLIO_INVOKE_METHOD: direct
  cvat_worker_annotation:
    image: cvat/server:v2.78.0
    environment:
      CVAT_NUCLIO_INVOKE_METHOD: direct
  nuclio:
    image: quay.io/nuclio/dashboard:1.16.3-amd64
    ports: ["8070:8070"]
    labels:
      traefik.enable: "true"
''')
    config = json.loads(subprocess.check_output([
        'docker', 'compose', '-f', str(base),
        '-f', str(ROOT / 'docker-compose.cvat-serverless.yml'),
        'config', '--format', 'json',
    ], text=True))
    services = config['services']
    assert not services['nuclio'].get('ports')
    assert services['nuclio']['labels']['traefik.enable'] == 'false'
    for name in ['cvat_server', 'cvat_worker_annotation']:
        assert services[name]['environment']['CVAT_NUCLIO_INVOKE_METHOD'] == 'dashboard'


def test_missing_configuration_fails_before_docker(deployment):
    root, env = deployment
    (root / '.env').unlink()
    result = subprocess.run([str(root / 'scripts/compose.sh'), 'sam2', 'up', '-d'],
                            env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert 'Copy .env.example' in result.stderr


def test_build_tag_matches_compose_and_rejects_wrong_checkout(tmp_path):
    cvat = tmp_path / 'cvat checkout'
    cvat.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(cvat), *args], text=True)
    git('init', '-q')
    git('config', 'user.name', 'Deployment test')
    git('config', 'user.email', 'test@example.org')
    (cvat / 'Dockerfile.ui').write_text('FROM scratch\n')
    git('add', '.')
    git('commit', '-qm', 'fixture')
    git('tag', 'v2.78.0')
    commands = tmp_path / 'commands.json'
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    docker = fake_bin / 'docker'
    docker.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                      'open(os.environ["COMMANDS"],"w").write(json.dumps(sys.argv[1:]))\n')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f'{fake_bin}:{os.environ["PATH"]}', COMMANDS=str(commands))
    script = ROOT / 'plugin/build_ui.sh'
    subprocess.run([str(script), str(cvat)], env=env, check=True, capture_output=True)
    args = json.loads(commands.read_text())
    assert args[args.index('-t') + 1] == IMAGE
    assert 'CLIENT_PLUGINS=plugins/sam2-propagate' in args
    assert VERSION in (cvat / 'cvat-ui/plugins/sam2-propagate/src/ts/version.ts').read_text()
    commands.unlink()
    result = subprocess.run([str(script), str(cvat), 'v2.79.0'], env=env, capture_output=True)
    assert result.returncode != 0
    assert not commands.exists()
