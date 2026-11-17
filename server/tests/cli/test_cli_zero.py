"""`nearpilot zero` (FR-16, NFR-08, #305, #307, spec 결정 23, 24).

HTTP 처리(`_send`)는 `nearpilot recover`와 같고 `test_cli_recover.py`가 확인한다. 여기서는 `_send`를 가짜로 바꾼다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK

TOKEN = "tok-3f9a1c7e5b2d4a60"
URL = "http://127.0.0.1:9"


class FakeSend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.reply: tuple[int, dict | None] = (200, {})

    def __call__(self, method, base, path, token, *, timeout=10):
        self.calls.append((method, base, path, token, timeout))
        return self.reply


@pytest.fixture
def send(monkeypatch):
    fake = FakeSend()
    monkeypatch.setattr("nearpilot.cli.zero._send", fake)
    return fake


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "owner.token"
    path.write_text(f"{TOKEN}\n", encoding="utf-8")
    return path


def server(token_file):
    return ["--server", URL, "--token-file", str(token_file)]


def test_FR16_cli_zero_measure_and_reset(send, token_file, run_cli, tmp_path):
    send.reply = (200, {"node_id": "light-1", "frames": 9, "median_dbm": -49.0, "zero_db": -5.0, "revision": 8,
                        "distance_m": 0.5, "reference_dbm": -44.0})
    code, out, err = run_cli(["zero", "measure", "light/1", *server(token_file)], cwd=tmp_path)
    assert code == EXIT_OK and err == ""
    # 설정 파일이 없어 측정 시간의 기본값 10초에 여유 20초를 더해 기다린다
    assert send.calls == [("POST", URL, "/admin/nodes/light%2F1/zero/measure", TOKEN, 30.0)]
    assert "측정 영점: -5.00 dB (개정 8)" in out and "측정 거리: 0.5 m" in out
    assert TOKEN not in out

    send.reply = (200, {"nodes": ["light-1"], "revision": 9})
    code, out, _ = run_cli(["zero", "reset", "light-1", *server(token_file)], cwd=tmp_path)
    assert code == EXIT_OK and "영점을 버렸다: light-1 (개정 9)" in out
    code, out, _ = run_cli(["zero", "reset", "--place", "place-a", *server(token_file)], cwd=tmp_path)
    assert code == EXIT_OK
    assert [call[2] for call in send.calls[1:]] == ["/admin/nodes/light-1/zero/reset",
                                                    "/admin/places/place-a/zero/reset"]


def test_FR16_cli_zero_measure_not_enough_frames(send, token_file, run_cli, tmp_path):
    send.reply = (422, {"error": "NOT_ENOUGH_FRAMES", "frames": 2})
    code, _, err = run_cli(["zero", "measure", "light-1", *server(token_file)], cwd=tmp_path)
    assert code == EXIT_FAILED and "프레임 2개" in err


@pytest.mark.parametrize("status,reason", [(403, "NOT_OWNER"), (409, "NO_BEACON"), (503, "NOT_SAVED")])
def test_FR16_cli_zero_rejected(send, token_file, run_cli, tmp_path, status, reason):
    send.reply = (status, {"error": reason})
    code, _, err = run_cli(["zero", "measure", "light-1", *server(token_file)], cwd=tmp_path)
    assert code == EXIT_FAILED and f"HTTP {status}" in err and reason in err


def test_FR16_cli_zero_reset_needs_one_target(send, token_file, run_cli, tmp_path):
    code, _, _ = run_cli(["zero", "reset", *server(token_file)], cwd=tmp_path)
    assert code == EXIT_INVALID
    assert send.calls == []
