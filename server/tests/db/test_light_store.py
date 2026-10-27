"""FR-04·FR-09·FR-19 조명 설정과 조명 제어 정보의 저장."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from conftest import (
    insert_approval,
    insert_approval_usage,
    insert_node,
    insert_node_command,
    insert_place,
    insert_policy,
    insert_request,
    insert_user,
)

from nearpilot.shared.enums import Action, Capability, Occupancy, StoreOutcome
from nearpilot.shared.models import LightControls, LightSetting, NodeAnnounce

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
RING = LightControls(brightness_max=64, color=True)


def light_node(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")


def shared_request(conn):
    insert_request(conn, internal_request_id="req1", node_id="light1", action="on")
    insert_policy(conn, policy_id="policy-light", node_id="light1")
    insert_approval(conn, approval_id="approval-light", node_id="light1", policy_id="policy-light",
                    internal_request_id="req1")
    insert_approval_usage(conn, usage_id="usage1", approval_id="approval-light", internal_request_id="req1")


def test_FR19_command_light_round_trip(conn, store):
    light_node(conn)
    shared_request(conn)
    blue = LightSetting(30, "#0000FF")

    result = store.link_command("req1", "light1", Action.ON, blue)

    assert result.outcome is StoreOutcome.APPLIED and result.command.light == blue
    row = conn.execute("SELECT light FROM node_command").fetchone()
    assert json.loads(row["light"]) == {"brightness": 30, "color": "#0000FF"}
    # 재전송·재시작은 저장한 명령 기록을 다시 읽는다. 값이 바뀌지 않는다.
    assert store.execution("req1").light == blue
    again = store.link_command("req1", "light1", Action.ON, LightSetting(64, "#FFFFFF"))
    assert again.outcome is StoreOutcome.ALREADY_APPLIED and again.command.light == blue


def test_FR19_command_without_light_stays_null(conn, store):
    light_node(conn)
    shared_request(conn)

    assert store.link_command("req1", "light1", Action.ON).command.light is None
    assert conn.execute("SELECT light FROM node_command").fetchone()["light"] is None
    assert store.execution("req1").light is None


def command(conn, cid, action="on", status="succeeded", sent=None, light=None):
    insert_node_command(conn, command_id=cid, internal_request_id=f"r-{cid}", node_id="light1", action=action,
                        status=status, execution_kind="shared", send_started_at=sent, send_attempted=1)
    if light is not None:
        conn.execute("UPDATE node_command SET light = ? WHERE command_id = ?", (json.dumps(light), cid))


def test_FR09_last_light_setting_latest_succeeded_on(conn, repo):
    light_node(conn)
    for cid in ("c1", "c2", "c3", "c4", "c5"):
        insert_request(conn, internal_request_id=f"r-{cid}", request_id=cid, node_id="light1", action="on")
    assert repo.last_light_setting("light1") is None

    command(conn, "c1", sent="2026-10-27T00:00:01+00:00", light={"brightness": 10, "color": "#FF0000"})
    command(conn, "c2", sent="2026-10-27T00:00:02+00:00", light={"brightness": 20, "color": "#00FF00"})
    # 실패한 명령과 off 는 마지막 설정이 아니다.
    command(conn, "c3", status="failed", sent="2026-10-27T00:00:03+00:00",
            light={"brightness": 30, "color": "#0000FF"})
    command(conn, "c4", action="off", sent="2026-10-27T00:00:04+00:00")
    assert repo.last_light_setting("light1") == LightSetting(20, "#00FF00")

    # 마지막 성공 on 에 설정이 없으면 기본값을 쓰도록 None 이다.
    command(conn, "c5", sent="2026-10-27T00:00:05+00:00")
    assert repo.last_light_setting("light1") is None


def announce(controls, capability=Capability.LIGHT):
    return NodeAnnounce(
        node_id="light1", capability=capability, occupancy=Occupancy.SHARED,
        received_at=NOW, session_id="s1", seq=1, sent_at=NOW, light_controls=controls,
    )


def test_FR04_announce_stores_controls(conn, repo):
    repo.upsert_announced_node(announce(RING))

    row = conn.execute("SELECT light_controls FROM node WHERE node_id = 'light1'").fetchone()
    assert json.loads(row["light_controls"]) == {"brightness_max": 64, "color": True}
    assert [n.light_controls for n in repo.nodes(Capability.LIGHT)] == [RING]


def test_FR04_controls_change_keeps_trust(conn, repo):
    light_node(conn)  # trusted, controls 없음

    repo.upsert_announced_node(announce(RING))
    repo.upsert_announced_node(announce(RING))  # 같은 값은 감사를 남기지 않는다

    row = conn.execute("SELECT trust_state, light_controls FROM node WHERE node_id = 'light1'").fetchone()
    assert row["trust_state"] == "trusted" and json.loads(row["light_controls"])["brightness_max"] == 64
    audits = conn.execute("SELECT step, payload_json FROM audit_log").fetchall()
    assert [a["step"] for a in audits] == ["announce"]
    payload = json.loads(audits[0]["payload_json"])
    assert payload["previous_light_controls"] is None and payload["light_controls"]["brightness_max"] == 64

    repo.upsert_announced_node(announce(None))
    row = conn.execute("SELECT trust_state, light_controls FROM node WHERE node_id = 'light1'").fetchone()
    assert row["trust_state"] == "trusted" and row["light_controls"] is None
    assert conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"] == 2


def test_FR04_light_control_columns_migrated(conn):
    node_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node)")}
    command_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_command)")}
    assert "light_controls" in node_cols and "light" in command_cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
