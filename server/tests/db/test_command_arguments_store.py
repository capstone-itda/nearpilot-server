"""FR-09·FR-22 명령 인자 저장, 이전, 설정 스냅샷 (specs/core-device-actions/spec.md)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import nearpilot.db.schema as schema_module
from nearpilot.db.repositories._common import settings_from_json, settings_to_json
from nearpilot.db.repositories.execution_store import SqliteExecutionStore
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.config import Settings, settings_to_data
from nearpilot.shared.enums import InternalRequestCause, StoreOutcome
from nearpilot.shared.models import InternalRequest

from conftest import insert_node, insert_node_command, insert_place, insert_request, insert_user

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)
_OLD_KEYS = {"light_brightness_max": 64, "light_default_brightness": 64, "light_default_color": "#FFFFFF",
             "speaker_volume_max": 20, "speaker_default_volume": 20}


def _old_snapshot(**changes) -> str:
    """조명·스피커 필드가 있던 때 저장한 설정 스냅샷 (`asdict(Settings)`)."""
    data = settings_to_data(Settings(version="old"))
    for name in ("thresholds", "param_max", "param_default", "any_allowed_kinds"):
        del data[name]
    return json.dumps({**data, **_OLD_KEYS, **changes}, ensure_ascii=False)


def _v9_db(tmp_path: Path) -> str:
    """0009까지의 스키마. 명령 기록에 조명·스피커 열이 있다."""
    db_path = str(tmp_path / "v9.db")
    files = sorted((Path(schema_module.__file__).parent / "sql").glob("*.sql"))
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    for path in files[:9]:
        conn.executescript(path.read_text(encoding="utf-8"))
    conn.execute("PRAGMA user_version = 9")
    insert_user(conn)
    insert_place(conn)
    for node_id, capability in (("light-1", "light"), ("speaker-1", "speaker"), ("locker-1", "locker")):
        occupancy = "rental" if capability == "locker" else "shared"
        conn.execute(
            "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, "
            "release_policy) VALUES (?, 'place1', ?, ?, 'trusted', 'AVAILABLE', '{}')",
            (node_id, capability, occupancy))
    rows = [
        ("c-light", "light-1", "on", '{"brightness": 30, "color": "#0000FF"}', None),
        ("c-speaker", "speaker-1", "on", None, '{"volume": 12}'),
        ("c-open", "locker-1", "open", None, None),
    ]
    for cid, node_id, action, light, speaker in rows:
        insert_request(conn, internal_request_id=f"r-{cid}", request_id=cid, node_id=node_id, action=action)
        kind = "rental_initial" if node_id == "locker-1" else "shared"
        insert_node_command(conn, command_id=cid, internal_request_id=f"r-{cid}", node_id=node_id, action=action,
                            execution_kind=kind, send_started_at="2026-11-07T00:00:01+00:00", send_attempted=1)
        conn.execute("UPDATE node_command SET light = ?, speaker = ? WHERE command_id = ?", (light, speaker, cid))
    conn.close()
    return db_path


def test_FR09_migration_moves_command_arguments(tmp_path):
    conn = connect(_v9_db(tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        command_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_command)")}
        node_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node)")}
        assert "arguments" in command_cols and not {"light", "speaker"} & command_cols
        assert "kind" in node_cols and "capability" not in node_cols
        store, repo = SqliteExecutionStore(conn), SqliteRepository(conn)
        assert store.execution("r-c-light").arguments == {"brightness": 30, "color": "#0000FF"}
        assert store.execution("r-c-speaker").arguments == {"volume": 12}
        assert store.execution("r-c-open").arguments is None
        # 판정은 옮긴 값을 마지막 성공 인자로 읽는다 (FR-09).
        assert repo.last_success_arguments("light-1", "on") == {"brightness": 30, "color": "#0000FF"}
        assert {n.node_id: n.kind for n in repo.nodes()} == {
            "light-1": "light", "speaker-1": "speaker", "locker-1": "locker"}
        assert [n.node_id for n in repo.nodes("speaker")] == ["speaker-1"]
    finally:
        conn.close()


def test_FR09_arguments_for_any_declared_action(conn, store, repo):
    """인자는 동작 이름과 종류에 관계없이 저장하고 같은 동작끼리만 이어 쓴다 (FR-09, #245)."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="fan-1", capability="fan", occupancy="shared")
    insert_request(conn, internal_request_id="spin-1", node_id="fan-1", action="spin")
    linked = store.link_command("spin-1", "fan-1", "spin", {"speed": 3, "mode": "calm", "label": "한글"})
    assert linked.outcome is StoreOutcome.APPLIED
    assert json.loads(conn.execute("SELECT arguments FROM node_command").fetchone()["arguments"]) == {
        "label": "한글", "mode": "calm", "speed": 3}
    conn.execute("UPDATE node_command SET status = 'succeeded', send_started_at = ?", (NOW.isoformat(),))
    assert repo.last_success_arguments("fan-1", "spin") == {"speed": 3, "mode": "calm", "label": "한글"}
    assert repo.last_success_arguments("fan-1", "stop") is None


def test_FR22_legacy_settings_snapshot_restored_from_db(conn):
    """옛 형식 스냅샷을 새 형식으로 옮겨 복원한다. 현재 설정으로 채우지 않는다 (FR-22)."""
    restored = settings_from_json(_old_snapshot(speaker_volume_max=15, speaker_default_volume=15))
    assert restored.version == "old"
    assert restored.param_caps("speaker", "on") == {"volume": 15}
    assert restored.param_defaults("light", "on") == {"brightness": 64, "color": "#FFFFFF"}
    assert settings_from_json(settings_to_json(restored)) == restored


def test_FR22_internal_request_retry_with_legacy_snapshot(conn, store):
    """옛 형식 스냅샷으로 저장한 서버 내부 요청도 같은 값의 재저장은 같은 요청이다 (spec 결정 13)."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light-1", capability="light", occupancy="shared")
    settings = Settings(version="old")
    request = InternalRequest("internal-1", "light-1", "off", InternalRequestCause.LEAVE_OFF, settings, NOW)
    assert store.link_internal_request(request).outcome is StoreOutcome.APPLIED
    conn.execute("UPDATE request SET settings_json = ? WHERE internal_request_id = 'internal-1'",
                 (_old_snapshot(),))
    assert store.link_internal_request(request).outcome is StoreOutcome.ALREADY_APPLIED
