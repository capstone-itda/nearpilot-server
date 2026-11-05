"""`nearpilot restart` (FR-12A, NFR-08, #235, specs/iot-node-restart/spec.md).

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
        self.reply: tuple[int, dict | None] = (202, {"node_id": "locker-1", "restart_id": "r-1",
                                                      "expires_at": "2026-11-05T03:00:03+00:00"})

    def __call__(self, method, base, path, token):
        self.calls.append((method, base, path, token))
        return self.reply


@pytest.fixture
def send(monkeypatch):
    fake = FakeSend()
    monkeypatch.setattr("nearpilot.cli.restart._send", fake)
    return fake


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "owner.token"
    path.write_text(f"{TOKEN}\n", encoding="utf-8")
    return path


def argv(token_file, node="locker-1"):
    return ["restart", node, "--server", URL, "--token-file", str(token_file)]


def test_FR12A_restart_cli_sends(send, token_file, run_cli, tmp_path):
    code, out, err = run_cli(argv(token_file, "locker/1"), cwd=tmp_path)
    assert code == EXIT_OK and err == ""
    assert send.calls == [("POST", URL, "/admin/nodes/locker%2F1/restart", TOKEN)]
    assert "재시작 ID: r-1" in out and "재시작 완료가 아니다" in out
    assert TOKEN not in out


@pytest.mark.parametrize("status,reason", [(403, "NOT_OWNER"), (409, "NODE_OFFLINE"), (503, "NOT_SENT")])
def test_FR12A_restart_cli_rejected(send, token_file, run_cli, tmp_path, status, reason):
    send.reply = (status, {"error": reason})
    code, _, err = run_cli(argv(token_file), cwd=tmp_path)
    assert code == EXIT_FAILED
    assert f"HTTP {status}" in err and reason in err


def test_FR12A_restart_cli_unreadable(send, token_file, run_cli, tmp_path):
    send.reply = (202, {"node_id": "locker-1"})
    assert run_cli(argv(token_file), cwd=tmp_path)[0] == EXIT_FAILED


def test_FR12A_restart_cli_token_file_required(send, run_cli, tmp_path):
    empty = tmp_path / "empty.token"
    empty.write_text("", encoding="utf-8")
    assert run_cli(argv(empty), cwd=tmp_path)[0] == EXIT_INVALID
    assert send.calls == []
