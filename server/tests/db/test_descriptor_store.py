"""FR-04 기기 선언의 저장, 해시·vendor 규칙, 감사와 이전 (specs/iot-device-descriptor/spec.md)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import nearpilot.db.schema as schema_module
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.descriptor import legacy_descriptor, parse_descriptor
from nearpilot.shared.enums import Occupancy, TrustState
from nearpilot.shared.models import NodeAnnounce

from conftest import insert_node, insert_place, insert_user

NOW = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
RING = legacy_descriptor("light", Occupancy.SHARED, brightness_max=64, color=True)


def announce(descriptor=RING, node_id="light1", seq=1):
    return NodeAnnounce(node_id=node_id, descriptor=descriptor, received_at=NOW, session_id="s1",
                        seq=seq, sent_at=NOW)


def node(conn, node_id="light1"):
    return conn.execute("SELECT * FROM node WHERE node_id = ?", (node_id,)).fetchone()


def audits(conn):
    return [json.loads(r["payload_json"]) for r in conn.execute("SELECT payload_json FROM audit_log ORDER BY rowid")]


def trusted_light(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")
    repo.upsert_announced_node(announce())


def test_FR04_new_node_stored_with_descriptor(conn, repo):
    repo.upsert_announced_node(announce())
    row = node(conn)
    assert row["trust_state"] == "discovered"
    assert (row["kind"], row["occupancy"]) == ("light", "shared")
    assert parse_descriptor(json.loads(row["descriptor"])) == RING
    assert row["descriptor_hash"] == RING.hash
    info, = repo.nodes("light")
    assert (info.descriptor, info.descriptor_hash) == (RING, RING.hash)
    assert info.kind == "light" and info.descriptor.action("on").properties["brightness"]["maximum"] == 64


def test_FR04_same_hash_keeps_trust(conn, repo):
    trusted_light(conn, repo)
    repo.upsert_announced_node(announce(seq=2))
    assert node(conn)["trust_state"] == "trusted"
    assert audits(conn) == []


def test_FR04_vendor_change_keeps_trust(conn, repo):
    trusted_light(conn, repo)
    upgraded = replace(RING, vendor=replace(RING.vendor, firmware="3.0.0"))
    repo.upsert_announced_node(announce(upgraded, seq=2))
    row = node(conn)
    assert row["trust_state"] == "trusted" and row["descriptor_hash"] == RING.hash
    assert json.loads(row["descriptor"])["vendor"]["firmware"] == "3.0.0"
    payload, = audits(conn)
    assert payload["previous_vendor"]["firmware"] == "v2" and payload["vendor"]["firmware"] == "3.0.0"


def test_FR04_description_change_resets_trust(conn, repo):
    trusted_light(conn, repo)
    on = replace(RING.actions[0], description="조명을 켠다. 밝기와 색을 받는다")
    changed = replace(RING, actions=(on, *RING.actions[1:]))
    repo.upsert_announced_node(announce(changed, seq=2))
    row = node(conn)
    assert row["trust_state"] == "discovered" and row["descriptor_hash"] == changed.hash
    payload, = audits(conn)
    assert (payload["previous_descriptor_hash"], payload["descriptor_hash"]) == (RING.hash, changed.hash)
    assert payload["trust_state"] == "discovered"


def test_FR04_kind_change_updates_kind_and_occupancy(conn, repo):
    trusted_light(conn, repo)
    box = legacy_descriptor("locker", Occupancy.RENTAL)
    repo.upsert_announced_node(announce(box, seq=2))
    row = node(conn)
    assert (row["kind"], row["occupancy"], row["trust_state"]) == ("locker", "rental", "discovered")
    assert audits(conn)[0]["previous_kind"] == "light"


def test_FR04_row_without_descriptor_kind_change_resets_trust(conn, repo):
    """선언 없는 행은 종류·점유 방식으로 비교한다 (spec 결정 10)."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")
    assert repo.nodes()[0].descriptor is None
    repo.upsert_announced_node(announce(legacy_descriptor("speaker", Occupancy.SHARED)))
    row = node(conn)
    assert row["trust_state"] == "discovered" and audits(conn)[0]["previous_descriptor_hash"] is None


def _legacy_db(tmp_path: Path) -> str:
    """0009까지의 스키마와 v2 제어 정보가 있는 기존 DB."""
    db_path = str(tmp_path / "legacy.db")
    files = sorted((Path(schema_module.__file__).parent / "sql").glob("*.sql"))
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    for path in files[:9]:
        conn.executescript(path.read_text(encoding="utf-8"))
    conn.execute("PRAGMA user_version = 9")
    conn.execute("INSERT INTO user (user_id, name, token_hash, role) VALUES ('u1', 'owner', 'h', 'owner')")
    conn.execute("INSERT INTO place (place_id, owner_user_id, name) VALUES ('p1', 'u1', 'room')")
    rows = [
        ("locker-1", "locker", "rental", "trusted", None, None),
        ("light-1", "light", "shared", "trusted", '{"brightness_max": 64, "color": true}', None),
        ("light-2", "light", "shared", "revoked", None, None),
        ("speaker-1", "speaker", "shared", "trusted", None, '{"volume_max": 20}'),
        ("odd-1", "locker", "shared", "trusted", None, None),  # 역할 규칙에 맞지 않는 조합
    ]
    for node_id, capability, occupancy, trust, light, speaker in rows:
        conn.execute(
            "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, "
            "release_policy, light_controls, speaker_controls) VALUES (?, 'p1', ?, ?, ?, 'AVAILABLE', '{}', ?, ?)",
            (node_id, capability, occupancy, trust, light, speaker))
    conn.close()
    return db_path


def test_FR04_migration_backfills_descriptor(tmp_path):
    conn = connect(_legacy_db(tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(node)")}
        assert {"descriptor", "descriptor_hash"} <= columns
        assert not {"light_controls", "speaker_controls"} & columns
        nodes = {n.node_id: n for n in SqliteRepository(conn).nodes()}
        assert nodes["locker-1"].descriptor == legacy_descriptor("locker", Occupancy.RENTAL)
        assert nodes["light-1"].descriptor_hash == RING.hash
        assert nodes["light-1"].descriptor == RING
        assert nodes["speaker-1"].descriptor == legacy_descriptor("speaker", Occupancy.SHARED, volume_max=20)
        assert [nodes[n].trust_state for n in ("locker-1", "light-1", "light-2", "speaker-1")] == [
            TrustState.TRUSTED, TrustState.TRUSTED, TrustState.REVOKED, TrustState.TRUSTED]
        assert nodes["odd-1"].descriptor is None  # 다음 발표를 기다린다
    finally:
        conn.close()


def test_FR04_migrated_node_v2_reannounce_keeps_trust(tmp_path):
    conn = connect(_legacy_db(tmp_path))
    try:
        repo = SqliteRepository(conn)
        repo.upsert_announced_node(announce(RING, node_id="light-1"))
        v3 = replace(RING, vendor=replace(RING.vendor, model="light-ring", firmware="3.0.0"))
        repo.upsert_announced_node(announce(v3, node_id="light-1", seq=2))  # 펌웨어를 v3로 올렸다
        row = node(conn, "light-1")
        assert row["trust_state"] == "trusted" and row["descriptor_hash"] == RING.hash
    finally:
        conn.close()
