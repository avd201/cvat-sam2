"""CLI side of the region of interest: argument parsing, what is sent, and refusing a service that ignored it."""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cli"))

import cvat_sam2 as cli  # noqa: E402


@pytest.mark.parametrize("text, expected", [("0,0,10,0", [0, 0, 10, 0]), ("0,5,10,5,3,9", [0, 5, 10, 5, 3, 9]),
                                            (" 1.5,2,3,4 ", [1.5, 2, 3, 4])])
def test_parse_roi_line_accepts_both_forms(text, expected):
    assert cli.parse_roi_line(text) == expected


@pytest.mark.parametrize("text, msg", [("1,2,3", "needs 4 numbers"), ("a,b,c,d", "comma-separated numbers"),
                                       ("5,5,5,5", "coincide"), ("0,0,10,10,5,5", "lies on the line")])
def test_parse_roi_line_refuses_bad_input_with_the_reason(text, msg):
    with pytest.raises(argparse.ArgumentTypeError, match=msg):
        cli.parse_roi_line(text)


def test_the_option_is_repeatable_and_defaults_to_empty():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roi-line", action="append", type=cli.parse_roi_line, default=[])
    assert ap.parse_args([]).roi_line == []
    assert ap.parse_args(["--roi-line", "0,0,1,0", "--roi-line", "5,0,5,9,9,3"]).roi_line == [[0, 0, 1, 0], [5, 0, 5, 9, 9, 3]]


class FakeResponse:
    status_code = 200

    def __init__(self, msgs):
        self._msgs = msgs

    def iter_lines(self):
        return [json.dumps(m).encode() for m in self._msgs]


def run(monkeypatch, msgs, meta, from_frame=None):
    import requests
    sent = {}

    def fake_post(url, data, files, stream, timeout):
        sent["meta"] = json.loads(data["meta"])
        return FakeResponse(msgs)

    monkeypatch.setattr(requests, "post", fake_post)
    res = cli.run_propagation("http://svc", [b"x", b"y", b"z"], meta, 5, from_frame=from_frame)
    return res, sent


def test_roi_lines_are_sent_and_the_drop_is_reported_in_absolute_frames(monkeypatch, capsys):
    msgs = [{"frame": 0, "points": [1, 2]}, {"frame": 1, "points": None}, {"frame": 2, "points": None},
            {"done": True, "frames": 3, "output": "mask", "roi_lines": 1, "dropped": {"1": 1}}]
    res, sent = run(monkeypatch, msgs, {"output": "mask", "roi_lines": [[0, 0, 1, 0]]}, from_frame=40)
    assert sent["meta"]["roi_lines"] == [[0, 0, 1, 0]]
    assert res == {0: [1, 2], 1: None, 2: None}
    assert "left the region of interest at frame 41" in capsys.readouterr().err


def test_a_service_that_ignored_the_roi_is_refused(monkeypatch):
    msgs = [{"frame": 0, "points": [1, 2]}, {"done": True, "frames": 1, "output": "mask"}]        # no roi_lines echo
    with pytest.raises(cli.ServiceError, match="did not apply them"):
        run(monkeypatch, msgs, {"output": "mask", "roi_lines": [[0, 0, 1, 0]]})


def test_without_a_roi_nothing_is_demanded_of_the_service(monkeypatch):
    msgs = [{"frame": 0, "points": [1, 2]}, {"done": True, "frames": 1, "output": "mask"}]
    res, sent = run(monkeypatch, msgs, {"output": "mask"})
    assert "roi_lines" not in sent["meta"] and res == {0: [1, 2]}
