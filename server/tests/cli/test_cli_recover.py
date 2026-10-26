"""`nearpilot recover` (FR-12A, NFR-08, specs/api-rest-recovery/spec.md 5절·6절 "복구 명령").

`ThreadingHTTPServer` fake가 복구 REST 두 경로를 대신하고, 받은 요청(메서드·경로·`Authorization`·본문)을 기록한다.
실제 NearPilot 서버는 띄우지 않는다. 명령은 `main(argv)`를 같은 프로세스에서 불러 확인한다.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from argparse import Namespace
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import CliError
from nearpilot.cli.recover import server_url

TOKEN = "tok-3f9a1c7e5b2d4a60"
NODE = "locker-1"
RECOVERY = "rec-0001"
COMPLETED_AT = "2026-10-27T09:00:00+00:00"
COMMANDS = ["status", "approve"]


def readiness(status: str, recovery_id: str | None, blockers: list[str]) -> dict[str, Any]:
    """서버의 준비 조회 응답. 키는 `api/rest/recovery.py`의 `readiness_json`과 같다."""
    return {
        "node_id": NODE, "recovery_id": recovery_id, "status": status, "blockers": blockers,
        "evidence": [{"observed_at": "2026-10-27T08:59:00+00:00", "execution_outcome": None, "command_idle": True}],
        "checked_at": "2026-10-27T09:00:00+00:00",
    }


def approval(outcome: str, reason_code: str, completed_at: str | None, after: dict[str, Any]) -> dict[str, Any]:
    """서버의 승인 성공 응답(`completed` 200, `pending` 202)."""
    return {"recovery_id": RECOVERY, "outcome": outcome, "reason_code": reason_code,
            "completed_at": completed_at, "readiness": after}


COMPLETED = approval("completed", "OK", COMPLETED_AT, readiness("recovered", RECOVERY, []))
PENDING = approval("pending", "STORE_OUTCOME_UNKNOWN", None,
                   readiness("recovering", RECOVERY, ["STORE_OUTCOME_UNKNOWN"]))


@dataclass(frozen=True)
class Seen:
    method: str
    path: str
    authorization: str | None
    body: bytes


class FakeServer:
    """`ThreadingHTTPServer` fake. 받은 요청을 `seen`에 쌓고, `reply`로 정한 상태 코드와 본문을 돌려준다."""

    def __init__(self) -> None:
        self.seen: list[Seen] = []
        self.hold: threading.Event | None = None  # 있으면 응답하기 전에 이 이벤트를 기다린다 (응답이 늦는 서버)
        self.short_by = 0  # 본문 길이 헤더를 실제 본문보다 이만큼 크게 쓴다 (본문이 잘린 응답)
        self._status = 200
        self._body = b"{}"
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                fake.seen.append(Seen(self.command, self.path, self.headers.get("Authorization"),
                                      self.rfile.read(length)))
                if fake.hold is not None:
                    fake.hold.wait(timeout=5)
                try:
                    self.send_response(fake._status)
                    self.send_header("Content-Length", str(len(fake._body) + fake.short_by))
                    self.end_headers()
                    self.wfile.write(fake._body)
                except OSError:  # 기다리다 연결을 끊은 클라이언트
                    pass

            do_GET = do_POST = _respond

            def log_message(self, format: str, *args: Any) -> None:  # 테스트 출력에 요청 로그를 쓰지 않는다
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        # shutdown()이 폴링 간격(기본 0.5초)만큼 기다리지 않게 줄인다
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def reply(self, status: int, body: Mapping[str, Any] | bytes) -> None:
        self._status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def close(self) -> None:
        if self.hold is not None:
            self.hold.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


@pytest.fixture
def fake():
    server = FakeServer()
    yield server
    server.close()


@pytest.fixture
def closed() -> FakeServer:
    """서버를 만들었다가 닫은 것. 주소와 포트는 남고 아무도 듣지 않는다."""
    server = FakeServer()
    server.close()
    return server


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "owner.token"
    path.write_text(f"  {TOKEN}\n", encoding="utf-8")  # 앞뒤 공백과 줄바꿈은 지운다
    return path


def argv(command: str, url: str, token_file: Path | None) -> list[str]:
    """`recover status|approve` 명령줄. 주소는 `--server`로 준다. `token_file`이 None이면 `--token-file`을 쓰지 않는다."""
    ids = [NODE] if command == "status" else [NODE, RECOVERY]
    return ["recover", command, *ids, "--server", url, *(["--token-file", str(token_file)] if token_file else [])]


@pytest.mark.parametrize("body, shown, ready", [
    pytest.param(readiness("ready", RECOVERY, []),
                 ["상태: ready", f"복구 ID: {RECOVERY}", "부족한 조건: 없음"], True, id="ready"),
    pytest.param(readiness("not_ready", None, ["NO_RECOVERY_REQUIRED"]),
                 ["상태: not_ready", "복구 ID: 없음", "부족한 조건: NO_RECOVERY_REQUIRED"], False, id="no-recovery"),
    pytest.param(readiness("not_ready", RECOVERY, ["COMMAND_STATE_UNCONFIRMED", "OFFLINE"]),
                 ["상태: not_ready", f"복구 ID: {RECOVERY}", "부족한 조건: COMMAND_STATE_UNCONFIRMED, OFFLINE"], False,
                 id="blocked"),
])
def test_FR12A_recover_cli_status(fake, token_file, run_cli, tmp_path, body, shown, ready):
    fake.reply(200, body)

    code, out, err = run_cli(argv("status", fake.url, token_file), cwd=tmp_path)

    assert code == EXIT_OK == 0 and err == ""
    # ready일 때만 마지막 줄에 이어서 실행할 approve 명령을 쓴다. 토큰 파일 경로는 준 그대로다
    next_command = f"nearpilot recover approve {NODE} {RECOVERY} --token-file {token_file}"
    assert out.splitlines() == ([*shown, next_command] if ready else shown)
    # 파일 내용(앞뒤 공백을 지운 토큰)이 Bearer 토큰이다
    assert fake.seen == [Seen("GET", f"/admin/nodes/{NODE}/recovery", f"Bearer {TOKEN}", b"")]
    assert TOKEN not in out + err


@pytest.mark.parametrize("status, body, code, shown, reported", [
    pytest.param(200, COMPLETED, EXIT_OK, ["복구를 완료했다", f"완료 시각: {COMPLETED_AT}"], [], id="completed"),
    pytest.param(202, PENDING, EXIT_FAILED, [], ["아직 완료가 아니다", "STORE_OUTCOME_UNKNOWN"], id="pending"),
])
def test_FR12A_recover_cli_approve(fake, token_file, run_cli, tmp_path, status, body, code, shown, reported):
    fake.reply(status, body)

    actual_code, out, err = run_cli(argv("approve", fake.url, token_file), cwd=tmp_path)

    assert actual_code == code
    assert out.splitlines() == shown
    if reported:  # 저장 확인 중이다. 완료를 안내하지 않고 사유 코드를 표준 오류에 쓴다
        assert all(fragment in err for fragment in reported) and "복구를 완료했다" not in out + err
    else:
        assert err == ""
    assert fake.seen == [Seen("POST", f"/admin/nodes/{NODE}/recovery/{RECOVERY}/approve", f"Bearer {TOKEN}", b"")]
    assert TOKEN not in out + err


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("status, body, shown", [
    pytest.param(403, {"error": "NOT_OWNER"}, "NOT_OWNER", id="403-not-owner"),
    pytest.param(409, {"error": "NOT_READY"}, "NOT_READY", id="409-not-ready"),
    pytest.param(401, {"error": "INVALID_TOKEN"}, "INVALID_TOKEN", id="401-invalid-token"),
    pytest.param(500, b"<html>Internal Server Error</html>", "", id="500-not-json"),
    pytest.param(404, {"detail": "Not Found"}, "", id="404-without-reason"),
])
def test_FR12A_recover_cli_rejected(fake, token_file, run_cli, tmp_path, command, status, body, shown):
    fake.reply(status, body)

    code, out, err = run_cli(argv(command, fake.url, token_file), cwd=tmp_path)

    assert code == EXIT_FAILED == 1 and out == ""
    assert len(err.splitlines()) == 1  # 한 줄이다
    assert f"HTTP {status}" in err and shown in err  # 사유 코드가 없으면 상태 코드만 쓴다
    assert "<html>" not in err and "Not Found" not in err  # 본문을 옮겨 쓰지 않는다
    assert TOKEN not in err


@pytest.mark.parametrize("command, status, body", [
    pytest.param("status", 200, b"<html>ok</html>", id="status-not-json"),
    pytest.param("status", 200, b"[]", id="status-not-object"),
    pytest.param("status", 202, readiness("ready", RECOVERY, []), id="status-unexpected-code"),
    pytest.param("approve", 200, b"", id="approve-empty-body"),
    pytest.param("approve", 204, b"", id="approve-unexpected-code"),
])
def test_FR12A_recover_cli_unreadable_response(fake, token_file, run_cli, tmp_path, command, status, body):
    fake.reply(status, body)

    code, out, err = run_cli(argv(command, fake.url, token_file), cwd=tmp_path)

    assert code == EXIT_FAILED and out == ""
    assert f"서버 응답을 해석하지 못했다 (HTTP {status})" in err


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("content", [
    pytest.param(None, id="missing"),
    pytest.param("", id="empty"),
    pytest.param(" \n\t\n", id="whitespace-only"),
    pytest.param(f"{TOKEN}\nother-{TOKEN}\n", id="two-lines"),
    pytest.param(f"{TOKEN} {TOKEN}", id="inner-space"),
    pytest.param(f"{TOKEN}-토큰", id="non-ascii"),
    pytest.param(b"\xff\xfe" + TOKEN.encode("utf-16-le"), id="not-utf8"),
])
def test_FR12A_recover_cli_token_file_errors(fake, run_cli, tmp_path, command, content):
    path = tmp_path / "bad.token"
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        path.write_text(content, encoding="utf-8")

    code, out, err = run_cli(argv(command, fake.url, path), cwd=tmp_path)

    assert code == EXIT_INVALID == 2 and out == ""
    assert str(path) in err and TOKEN not in err  # 토큰이 든 파일도 내용을 문구에 쓰지 않는다
    assert fake.seen == []  # 요청을 보내지 않았다


@pytest.mark.parametrize("words, fragments", [
    pytest.param(["recover", "--help"], ["status", "approve"], id="recover"),
    pytest.param(["recover", "status", "--help"], ["NODE_ID", "--token-file", "--server", "--config"], id="status"),
    pytest.param(["recover", "approve", "--help"],
                 ["NODE_ID", "RECOVERY_ID", "--token-file", "--server", "--config"], id="approve"),
])
def test_FR12A_recover_cli_help(run_cli, tmp_path, words, fragments):
    code, out, err = run_cli(words, cwd=tmp_path)

    assert code == EXIT_OK and err == ""
    assert all(fragment in out for fragment in fragments)


def test_FR12A_recover_cli_requires_action(run_cli, tmp_path):
    code, out, err = run_cli(["recover"], cwd=tmp_path)

    assert code == EXIT_INVALID and out == ""
    assert "{status,approve}" in err  # 사용법에 고를 수 있는 동작이 나온다


@pytest.mark.parametrize("command", COMMANDS)
def test_FR12A_recover_cli_requires_token_file_option(fake, run_cli, tmp_path, command):
    code, out, err = run_cli(argv(command, fake.url, None), cwd=tmp_path)

    assert code == EXIT_INVALID and out == ""
    assert "--token-file" in err
    assert fake.seen == []


def test_FR12A_recover_cli_accepts_bom_token_file(fake, run_cli, tmp_path):
    path = tmp_path / "bom.token"
    path.write_text(TOKEN + "\r\n", encoding="utf-8-sig")  # 메모장의 UTF-8(BOM) 저장
    fake.reply(200, readiness("not_ready", None, ["NO_RECOVERY_REQUIRED"]))

    code, _, _ = run_cli(argv("status", fake.url, path), cwd=tmp_path)

    assert code == EXIT_OK
    assert [seen.authorization for seen in fake.seen] == [f"Bearer {TOKEN}"]


@pytest.mark.parametrize("command", COMMANDS)
def test_FR12A_recover_cli_connection_error(closed, token_file, run_cli, tmp_path, command):
    code, out, err = run_cli(argv(command, closed.url, token_file), cwd=tmp_path)

    assert code == EXIT_FAILED == 1 and out == ""
    assert "서버에 연결하지 못했다" in err and closed.url in err
    assert TOKEN not in err


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("status", [200, 500])
def test_FR12A_recover_cli_truncated_response(fake, token_file, run_cli, tmp_path, command, status):
    fake.reply(status, readiness("ready", RECOVERY, []))
    fake.short_by = 50  # 본문을 다 보내지 않고 연결을 닫는다

    code, out, err = run_cli(argv(command, fake.url, token_file), cwd=tmp_path)

    assert code == EXIT_FAILED and out == ""
    assert "서버에 연결하지 못했다" in err and fake.url in err and TOKEN not in err


def test_FR12A_recover_cli_request_timeout(fake, token_file, run_cli, tmp_path, monkeypatch):
    """요청 시간 제한은 10초다. 응답이 오지 않으면 연결 실패로 끝낸다 (실제로 10초를 기다리지 않게 줄여서 부른다)."""
    timeouts = []
    real_urlopen = urllib.request.urlopen

    def short_urlopen(request, *, timeout):
        timeouts.append(timeout)
        return real_urlopen(request, timeout=0.3)

    monkeypatch.setattr(urllib.request, "urlopen", short_urlopen)
    fake.hold = threading.Event()

    code, out, err = run_cli(argv("status", fake.url, token_file), cwd=tmp_path)

    assert timeouts == [10]
    assert code == EXIT_FAILED and out == ""
    assert "서버에 연결하지 못했다" in err and fake.url in err and TOKEN not in err


def test_FR12A_recover_cli_quotes_ids_in_path(fake, token_file, run_cli, tmp_path):
    fake.reply(200, COMPLETED)

    code, _, _ = run_cli(["recover", "approve", "node/1 a", "rec?x#1", "--server", fake.url,
                          "--token-file", str(token_file)], cwd=tmp_path)

    assert code == EXIT_OK
    assert [seen.path for seen in fake.seen] == ["/admin/nodes/node%2F1%20a/recovery/rec%3Fx%231/approve"]


@pytest.mark.parametrize("server", ["127.0.0.1:8000", "localhost", "ftp://127.0.0.1", "file:///etc/hosts", ""])
def test_FR12A_recover_cli_server_option_must_be_http_url(token_file, run_cli, tmp_path, server):
    code, out, err = run_cli(["recover", "status", NODE, "--server", server, "--token-file", str(token_file)],
                             cwd=tmp_path)

    assert code == EXIT_INVALID and out == ""
    assert "--server" in err and TOKEN not in err


def test_FR12A_recover_cli_server_from_config(fake, server_dir, write_config, token_file, run_cli):
    # --server가 없고 --config도 없으면 실행 폴더의 config/server.toml의 [http]로 요청이 간다
    write_config(server_dir, http={"host": "127.0.0.1", "port": fake.port})
    fake.reply(200, readiness("ready", RECOVERY, []))

    code, out, _ = run_cli(["recover", "status", NODE, "--token-file", str(token_file)], cwd=server_dir)

    assert code == EXIT_OK and f"복구 ID: {RECOVERY}" in out
    assert [(seen.method, seen.path) for seen in fake.seen] == [("GET", f"/admin/nodes/{NODE}/recovery")]
    assert not (server_dir / "var").exists()  # 설정만 읽고 DB를 열지 않는다


@pytest.mark.parametrize("place", [
    pytest.param(lambda config, rest: ["--config", config, "recover", *rest], id="before-recover"),
    pytest.param(lambda config, rest: ["recover", "--config", config, *rest], id="after-recover"),
    pytest.param(lambda config, rest: ["recover", *rest, "--config", config], id="after-action"),
])
def test_FR12A_recover_cli_config_option_places(fake, closed, server_dir, write_config, token_file, run_cli, place):
    # 기본 설정은 닫힌 포트를 가리킨다. --config로 준 설정을 읽어야 fake에 닿는다
    write_config(server_dir, http={"host": "127.0.0.1", "port": closed.port})
    chosen = write_config(server_dir, name="chosen.toml", http={"host": "127.0.0.1", "port": fake.port})
    fake.reply(200, readiness("ready", RECOVERY, []))

    code, _, _ = run_cli(place(str(chosen), ["status", NODE, "--token-file", str(token_file)]), cwd=server_dir)

    assert code == EXIT_OK
    assert len(fake.seen) == 1


def test_FR12A_recover_cli_server_from_config_any_address(fake, server_dir, write_config, token_file, run_cli):
    # 설정의 host가 0.0.0.0이면 127.0.0.1로 부른다. 0.0.0.0은 받는 주소라서 접속에는 쓰지 않는다
    write_config(server_dir, http={"host": "0.0.0.0", "port": fake.port})
    fake.reply(200, readiness("not_ready", None, ["NO_RECOVERY_REQUIRED"]))

    code, _, _ = run_cli(["recover", "status", NODE, "--token-file", str(token_file)], cwd=server_dir)

    assert code == EXIT_OK
    assert len(fake.seen) == 1


def test_FR12A_recover_cli_config_error_sends_nothing(fake, token_file, run_cli, tmp_path):
    code, out, err = run_cli(["--config", str(tmp_path / "missing.toml"), "recover", "status", NODE,
                              "--token-file", str(token_file)], cwd=tmp_path)

    assert code == EXIT_INVALID and out == ""
    assert err.startswith("설정 오류: ")
    assert fake.seen == []


@pytest.mark.parametrize("host, expected", [
    ("0.0.0.0", "http://127.0.0.1:8123"),
    ("127.0.0.1", "http://127.0.0.1:8123"),
    ("192.168.4.1", "http://192.168.4.1:8123"),
])
def test_FR12A_recover_server_url_from_config(server_dir, write_config, host, expected):
    config = write_config(server_dir, http={"host": host, "port": 8123})

    assert server_url(Namespace(server=None, config=str(config))) == expected


@pytest.mark.parametrize("server, expected", [
    ("http://127.0.0.1:9", "http://127.0.0.1:9"),
    ("http://127.0.0.1:9/", "http://127.0.0.1:9"),
    ("https://nearpilot.example:8443//", "https://nearpilot.example:8443"),
    ("http://0.0.0.0:9", "http://0.0.0.0:9"),  # --server는 준 그대로다. 0.0.0.0을 바꾸는 것은 설정에서 만든 주소뿐이다
])
def test_FR12A_recover_server_url_from_option(tmp_path, server, expected):
    # --server가 있으면 설정 파일을 읽지 않는다. 없는 파일을 가리켜도 된다
    assert server_url(Namespace(server=server, config=str(tmp_path / "missing.toml"))) == expected


def test_FR12A_recover_server_url_config_error(tmp_path):
    with pytest.raises(CliError) as raised:
        server_url(Namespace(server=None, config=str(tmp_path / "missing.toml")))

    assert raised.value.exit_code == EXIT_INVALID and raised.value.message.startswith("설정 오류: ")
