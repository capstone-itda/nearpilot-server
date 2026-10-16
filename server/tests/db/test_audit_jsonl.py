"""`AuditLog` JSONL 내보내기 (specs/db-audit-jsonl/spec.md, #34, FR-22, NFR-03)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nearpilot.db.audit import SqliteAuditLog
from nearpilot.db.repositories._common import immediate_transaction, iso, new_id
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import AdminWriteOutcome, Role, Step
from nearpilot.shared.models import AccountRegistration, AuditEvent


def _account(user_id, name="t", token_hash=None):
    return AccountRegistration(
        user_id=user_id, name=name, role=Role.VISITOR, token_hash=token_hash or f"hash-{user_id}"
    )


def test_FR22_jsonl_appends_committed_row(conn, admin, tmp_path):
    """`AdminRepository`가 `AuditLog.append()`를 거치지 않고 직접 쓰는 경로도 덮인다."""
    path = tmp_path / "audit.jsonl"
    SqliteAuditLog(conn, jsonl_path=str(path))

    admin.register_account(_account("u1"))

    lines = path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["step"] == "admin"
    assert record["payload"]["event"] == "register_account"
    assert record["payload"]["user_id"] == "u1"


def test_FR22_jsonl_skips_rolled_back(conn, tmp_path):
    path = tmp_path / "audit.jsonl"
    SqliteAuditLog(conn, jsonl_path=str(path))

    with pytest.raises(RuntimeError):
        with immediate_transaction(conn):
            conn.execute(
                "INSERT INTO audit_log (log_id, event_id, internal_request_id, command_id, step, "
                "payload_json, created_at) VALUES (?, ?, NULL, NULL, ?, '{}', ?)",
                (new_id(), "evt-rollback", Step.ADMIN.value, iso(datetime.now(timezone.utc))),
            )
            raise RuntimeError("boom")

    assert not path.exists()


def test_FR22_jsonl_commit_order(conn, admin, tmp_path):
    path = tmp_path / "audit.jsonl"
    SqliteAuditLog(conn, jsonl_path=str(path))

    admin.register_account(_account("u1"))
    admin.register_account(_account("u2"))
    admin.register_account(_account("u3"))

    lines = path.read_text().splitlines()
    seqs = [json.loads(line)["seq"] for line in lines]
    assert seqs == sorted(seqs)
    assert len(seqs) == 3


def test_FR22_jsonl_no_duplicate_event(conn, tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = SqliteAuditLog(conn, jsonl_path=str(path))

    audit.append(AuditEvent(event_id="evt-1", step=Step.DEDUP, payload={"a": 1}))
    audit.append(AuditEvent(event_id="evt-1", step=Step.DEDUP, payload={"a": 2}))

    lines = path.read_text().splitlines()
    assert len(lines) == 1


def test_FR22_jsonl_failure_does_not_block_store(conn, admin, tmp_path, caplog):
    # 디렉터리 자체를 경로로 줘서 쓰기가 항상 실패하게 한다.
    SqliteAuditLog(conn, jsonl_path=str(tmp_path))

    result = admin.register_account(_account("u1"))

    assert result.outcome == AdminWriteOutcome.APPLIED
    assert conn.execute("SELECT COUNT(*) AS c FROM user").fetchone()["c"] == 1
    assert any("JSONL" in message for message in caplog.messages)


def test_FR22_jsonl_resumes_after_failure(conn, admin, tmp_path):
    missing_dir = tmp_path / "missing"
    path = missing_dir / "audit.jsonl"
    SqliteAuditLog(conn, jsonl_path=str(path))

    admin.register_account(_account("u1"))
    assert not missing_dir.exists()  # 디렉터리가 없어 실패했다. 커서는 그대로다.

    missing_dir.mkdir()
    admin.register_account(_account("u2"))

    lines = path.read_text().splitlines()
    users = [json.loads(line)["payload"]["user_id"] for line in lines]
    assert users == ["u1", "u2"]  # 빠진 u1부터 이어 썼다.


def test_FR22_jsonl_truncated_last_line_recovers(conn, admin, tmp_path):
    path = tmp_path / "audit.jsonl"
    SqliteAuditLog(conn, jsonl_path=str(path))
    admin.register_account(_account("u1"))

    content = path.read_text()
    path.write_text(content[:-5])  # 마지막 줄을 중간에서 끊는다.

    admin.register_account(_account("u2"))

    lines = path.read_text().splitlines()
    users = [json.loads(line)["payload"]["user_id"] for line in lines]
    assert users == ["u1", "u2"]  # 끊긴 u1 줄을 버리고 u1부터 온전히 다시 썼다.


def test_FR22_jsonl_resume_after_restart(tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    jsonl_path = str(tmp_path / "audit.jsonl")

    conn1 = connect(db_path)
    SqliteAuditLog(conn1, jsonl_path=jsonl_path)
    SqliteAdminRepository(conn1).register_account(_account("u1"))
    conn1.close()

    conn2 = connect(db_path)  # "재시작" — 새 연결, 새 내보내기 객체.
    SqliteAuditLog(conn2, jsonl_path=jsonl_path)
    SqliteAdminRepository(conn2).register_account(_account("u2"))
    conn2.close()

    lines = Path(jsonl_path).read_text().splitlines()
    users = [json.loads(line)["payload"]["user_id"] for line in lines]
    assert users == ["u1", "u2"]


def test_NFR03_jsonl_concurrent_writers(tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    jsonl_path = str(tmp_path / "audit.jsonl")
    connect(db_path).close()

    barrier = threading.Barrier(5)

    def attempt(i: int) -> None:
        conn = connect(db_path)
        try:
            SqliteAuditLog(conn, jsonl_path=jsonl_path)
            admin = SqliteAdminRepository(conn)
            barrier.wait()
            admin.register_account(_account(f"u{i}"))
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = Path(jsonl_path).read_text().splitlines()
    seqs = [json.loads(line)["seq"] for line in lines]
    assert len(lines) == 5
    assert len(set(seqs)) == 5
    assert seqs == sorted(seqs)


def test_FR22_jsonl_rebuild_from_table(conn, admin, tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = SqliteAuditLog(conn, jsonl_path=str(path))
    admin.register_account(_account("u1"))
    admin.register_account(_account("u2"))
    path.write_text("이건 jsonl이 아니다\n")

    audit._exporter.rebuild()

    lines = path.read_text().splitlines()
    users = [json.loads(line)["payload"]["user_id"] for line in lines]
    assert users == ["u1", "u2"]


def test_FR22_jsonl_disabled_without_path(conn, admin):
    audit = SqliteAuditLog(conn)

    result = admin.register_account(_account("u1"))

    assert audit._exporter is None
    assert result.outcome == AdminWriteOutcome.APPLIED


def test_FR22_jsonl_append_without_transaction_still_exports(conn, tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = SqliteAuditLog(conn, jsonl_path=str(path))

    audit.append(AuditEvent(event_id="evt-1", step=Step.AUTH, payload={"reason": "INVALID_TOKEN"}))

    lines = path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_id"] == "evt-1"
    assert record["step"] == "auth"
