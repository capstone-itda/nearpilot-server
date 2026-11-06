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

from nearpilot.db.repositories._common import settings_to_json
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import legacy_descriptor
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


def test_FR09_execution_context_keeps_light(conn, store):
    light_node(conn)
    shared_request(conn)
    conn.execute("UPDATE request SET settings_json = ? WHERE internal_request_id = 'req1'",
                 (settings_to_json(Settings()),))
    blue = LightSetting(30, "#0000FF")
    command = store.link_command("req1", "light1", Action.ON, blue).command

    # 기기 결과를 인계할 때 다시 만든 문맥이 판정이 넘긴 조명 설정과 같아야 한다.
    assert store.execution_context(command.command_id).light == blue


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


def announce(controls):
    """조명 v2 발표의 템플릿 선언 (iot-device-descriptor spec 결정 3)."""
    extra = {} if controls is None else dict(brightness_max=controls.brightness_max, color=controls.color)
    return NodeAnnounce(
        node_id="light1", descriptor=legacy_descriptor(Capability.LIGHT, Occupancy.SHARED, **extra),
        received_at=NOW, session_id="s1", seq=1, sent_at=NOW,
    )


def test_FR04_announce_stores_controls(conn, repo):
    """조명 제어 정보는 선언의 brightness 상한과 color 로 계산한다 (spec 결정 7)."""
    repo.upsert_announced_node(announce(RING))

    row = conn.execute("SELECT descriptor FROM node WHERE node_id = 'light1'").fetchone()
    on = json.loads(row["descriptor"])["actions"][0]
    assert on["input_schema"]["properties"]["brightness"]["maximum"] == 64
    assert [n.light_controls for n in repo.nodes(Capability.LIGHT)] == [RING]


def test_FR04_controls_change_resets_trust(conn, repo):
    """제어 정보는 선언의 인자 스키마다. 바뀌면 선언 해시가 바뀐다 (FR-04, #245)."""
    light_node(conn)  # trusted, 선언 없는 행

    repo.upsert_announced_node(announce(RING))  # 종류·점유 방식이 같아 신뢰를 유지한다 (spec 결정 10)
    repo.upsert_announced_node(announce(RING))  # 같은 선언은 감사를 남기지 않는다
    row = conn.execute("SELECT trust_state FROM node WHERE node_id = 'light1'").fetchone()
    assert row["trust_state"] == "trusted"
    assert conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"] == 0

    repo.upsert_announced_node(announce(None))
    row = conn.execute("SELECT trust_state FROM node WHERE node_id = 'light1'").fetchone()
    assert row["trust_state"] == "discovered"
    payload = json.loads(conn.execute("SELECT payload_json FROM audit_log").fetchone()["payload_json"])
    assert payload["previous_descriptor_hash"] != payload["descriptor_hash"]


def test_FR04_light_control_columns_migrated(conn):
    """발표한 제어 정보 열은 선언으로 옮겨 지웠다. 명령의 조명 설정 열은 남는다 (spec 결정 9)."""
    node_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node)")}
    command_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_command)")}
    assert "light_controls" not in node_cols and {"descriptor", "descriptor_hash"} <= node_cols
    assert "light" in command_cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 10
