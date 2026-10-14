"""`AuditLog.append` (FR-22): 테이블에만 쓰고, 같은 event_id는 한 번만 저장한다."""

from __future__ import annotations

from nearpilot.db.audit import SqliteAuditLog
from nearpilot.shared.enums import Step
from nearpilot.shared.models import AuditEvent


def test_FR22_audit_append_writes_table(conn):
    audit = SqliteAuditLog(conn)

    audit.append(AuditEvent(event_id="evt-1", step=Step.DEDUP, payload={"a": 1}))

    row = conn.execute("SELECT step, payload_json FROM audit_log WHERE event_id = 'evt-1'").fetchone()
    assert row["step"] == "0"
    assert row["payload_json"] == '{"a": 1}'


def test_FR22_audit_duplicate_event(conn):
    audit = SqliteAuditLog(conn)

    audit.append(AuditEvent(event_id="evt-1", step=Step.DEDUP, payload={"a": 1}))
    audit.append(AuditEvent(event_id="evt-1", step=Step.DEDUP, payload={"a": 2}))

    rows = conn.execute("SELECT payload_json FROM audit_log WHERE event_id = 'evt-1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["payload_json"] == '{"a": 1}'


def test_FR22_audit_append_without_request(conn):
    audit = SqliteAuditLog(conn)

    audit.append(AuditEvent(event_id="evt-auth", step=Step.AUTH, payload={"reason": "INVALID_TOKEN"}))

    row = conn.execute("SELECT internal_request_id FROM audit_log WHERE event_id = 'evt-auth'").fetchone()
    assert row["internal_request_id"] is None
