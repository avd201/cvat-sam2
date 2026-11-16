#!/usr/bin/env bash
# Runs every automated test layer of this repository and prints a summary table.
#
#   ./scripts/run_all_tests.sh                       # layers 1-2: Python tests + plugin unit tests
#   ./scripts/run_all_tests.sh --cvat ~/cvat/cvat    # + layers 3-4: real cvat-core harness + TypeScript check
#
# Layers 3-4 need a CVAT v2.78.0 checkout WITH node_modules (`yarn workspaces focus cvat-ui cvat-canvas cvat-core
# cvat-data` inside it) and network access for npx. They are SKIPPED (and reported as skipped) without --cvat:
# a green summary with skipped layers is NOT a full pass.
#
# Environment: PYTHON (default python3) = interpreter that has tests/requirements-dev.txt installed.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
CVAT=""

usage() { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --cvat) CVAT="${2:?--cvat needs the path of a CVAT checkout}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

NAMES=(); RESULTS=()
record() { NAMES+=("$1"); RESULTS+=("$2"); }
layer() { # layer "<name>" <command...>
  local name="$1"; shift
  echo; echo "=== $name"
  if "$@"; then record "$name" PASS; else record "$name" FAIL; fi
}
skip() { echo; echo "=== $1: skipped ($2)"; record "$1" "SKIP ($2)"; }

# ---------------------------------------------------------------- 1. Python
py_layer() {
  "$PYTHON" -c 'import fastapi, httpx, pytest, PIL, numpy, cv2, pydantic, multipart, uvicorn, requests, cvat_sdk' 2>/dev/null || {
    echo "Missing Python test dependencies for '$PYTHON'. Install them with:" >&2
    echo "  $PYTHON -m pip install -r $ROOT/tests/requirements-dev.txt" >&2
    return 1
  }
  (cd "$ROOT" && "$PYTHON" -m pytest tests -q -p no:cacheprovider -rs)
}
layer "1. Python tests (service, CLI, queue)" py_layer

# ---------------------------------------------------------------- 2. plugin unit tests
node_layer() {
  local out rc; out="$(mktemp)"
  (cd "$ROOT/plugin/sam2-propagate" && node --experimental-strip-types --test --test-reporter=tap test/*.test.mjs >"$out" 2>&1)
  rc=$?                                  # node's own exit status (NOT the status of the grep below)
  grep -E '^# (tests|pass|fail|skipped)|^not ok' "$out"
  [ "$rc" -ne 0 ] && tail -40 "$out"
  rm -f "$out"
  return "$rc"
}
if command -v node >/dev/null 2>&1; then
  layer "2. Plugin unit tests (node)" node_layer
else
  skip "2. Plugin unit tests (node)" "node not installed"
fi

# ---------------------------------------------------------------- 3-4. need a CVAT checkout
PLUGIN_DEST=""
cleanup() { [ -n "$PLUGIN_DEST" ] && rm -rf "$PLUGIN_DEST"; }
trap cleanup EXIT

if [ -z "$CVAT" ]; then
  skip "3. Plugin apply.ts against real cvat-core" "no --cvat given"
  skip "4. Plugin TypeScript check inside cvat-ui" "no --cvat given"
elif [ ! -d "$CVAT/node_modules" ]; then
  echo; echo "=== 3-4: $CVAT has no node_modules (run: yarn workspaces focus cvat-ui cvat-canvas cvat-core cvat-data)"
  record "3. Plugin apply.ts against real cvat-core" "FAIL"
  record "4. Plugin TypeScript check inside cvat-ui" "FAIL"
else
  PLUGIN_DEST="$CVAT/cvat-ui/plugins/sam2-propagate"
  layer "3. Plugin apply.ts against real cvat-core" bash "$ROOT/plugin/sam2-propagate/test/core-harness/run.sh" "$CVAT"

  ts_layer() {
    rm -rf "$PLUGIN_DEST" && mkdir -p "$(dirname "$PLUGIN_DEST")" && cp -r "$ROOT/plugin/sam2-propagate" "$PLUGIN_DEST"
    local out; out="$(mktemp)"
    (cd "$CVAT/cvat-ui" && npx --yes -p typescript@5.8.3 tsc -p tsconfig.json --noEmit --listFiles >"$out" 2>&1)
    # Proof that the plugin was really part of the compiled program (a tsc that never started prints nothing).
    if ! grep -q 'plugins/sam2-propagate/src/ts/ui.tsx' "$out"; then
      echo "tsc did not compile the plugin; output tail:"; tail -5 "$out"; rm -f "$out"; return 1
    fi
    # CVAT itself has a few unrelated baseline errors (canvas3d / fabric typings); only errors in OUR files count.
    if grep -E 'plugins/sam2-propagate/.*error TS' "$out"; then rm -f "$out"; return 1; fi
    echo "no TypeScript errors in the plugin ($(grep -c 'error TS' "$out") unrelated baseline errors in CVAT itself)"
    rm -f "$out"
  }
  layer "4. Plugin TypeScript check inside cvat-ui" ts_layer
fi

# ---------------------------------------------------------------- summary
echo; echo "==================== summary"
FAILED=0; SKIPPED=0
for i in "${!NAMES[@]}"; do
  printf '  %-48s %s\n' "${NAMES[$i]}" "${RESULTS[$i]}"
  [ "${RESULTS[$i]}" = FAIL ] && FAILED=$((FAILED + 1))
  case "${RESULTS[$i]}" in SKIP*) SKIPPED=$((SKIPPED + 1)) ;; esac
done
[ "$SKIPPED" -gt 0 ] && echo "  ($SKIPPED layer(s) skipped: this is not a full pass)"
[ "$FAILED" -eq 0 ] && echo "  ALL RUN LAYERS PASSED" || echo "  $FAILED LAYER(S) FAILED"
exit $((FAILED > 0))
