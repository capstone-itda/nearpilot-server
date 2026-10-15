"""`AdminRepository.register_account` (specs/db-admin-repository/spec.md, FR-01, NFR-08)."""

from __future__ import annotations

from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason, Role
from nearpilot.shared.models import AccountRegistration


def _account(user_id="u1", name="tester", role=Role.VISITOR, token_hash="hash-1"):
    return AccountRegistration(user_id=user_id, name=name, role=role, token_hash=token_hash)


def test_FR01_register_account_applied(admin, conn):
    result = admin.register_account(_account())

    assert result.outcome == AdminWriteOutcome.APPLIED
    assert result.reason == AdminWriteReason.OK
    row = conn.execute("SELECT user_id, name, role, token_hash FROM user WHERE user_id = 'u1'").fetchone()
    assert tuple(row) == ("u1", "tester", "visitor", "hash-1")
    assert conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"] == 1


def test_FR01_register_account_duplicate(admin, conn):
    admin.register_account(_account())

    result = admin.register_account(_account(name="other", token_hash="hash-2"))

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.ACCOUNT_ALREADY_EXISTS
    row = conn.execute("SELECT name, token_hash FROM user WHERE user_id = 'u1'").fetchone()
    assert tuple(row) == ("tester", "hash-1")  # 기존 행을 바꾸지 않는다


def test_FR01_register_account_token_hash_taken(admin, conn):
    admin.register_account(_account(user_id="u1", token_hash="hash-1"))

    result = admin.register_account(_account(user_id="u2", token_hash="hash-1"))

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.ACCOUNT_ALREADY_EXISTS
    assert conn.execute("SELECT COUNT(*) AS c FROM user WHERE user_id = 'u2'").fetchone()["c"] == 0


def test_NFR08_admin_audit_has_no_token(admin, conn):
    admin.register_account(_account())

    row = conn.execute("SELECT payload_json FROM audit_log").fetchone()
    assert "hash-1" not in row["payload_json"]
    assert "token_hash" not in row["payload_json"]
