"""FR-04·FR-09·FR-19 조명 명령 인자와 조명 선언의 저장 (specs/core-device-actions/spec.md)."""

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
from nearpilot.shared.enums import Occupancy, StoreOutcome
from nearpilot.shared.models import NodeAnnounce

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
RING = {"brightness_max": 64, "color": True}
BLUE = {"brightness": 30, "color": "#0000FF"}


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

    result = store.link_command("req1", "light1", "on", BLUE)

    assert result.outcome is StoreOutcome.APPLIED and result.command.arguments == BLUE
    row = conn.execute("SELECT arguments FROM node_command").fetchone()
    assert json.loads(row["arguments"]) == BLUE
    # 재전송·재시작은 저장한 명령 기록을 다시 읽는다. 값이 바뀌지 않는다.
    assert store.execution("req1").arguments == BLUE
    again = store.link_command("req1", "light1", "on", {"brightness": 64, "color": "#FFFFFF"})
    assert again.outcome is StoreOutcome.ALREADY_APPLIED and again.command.arguments == BLUE


def test_FR09_execution_context_keeps_light(conn, store):
    light_node(conn)
    shared_request(conn)
    conn.execute("UPDATE request SET settings_json = ? WHERE internal_request_id = 'req1'",
                 (settings_to_json(Settings()),))
    command = store.link_command("req1", "light1", "on", BLUE).command

    # 기기 결과를 인계할 때 다시 만든 문맥이 판정이 넘긴 인자와 같아야 한다.
    assert store.execution_context(command.command_id).arguments == BLUE


def test_FR19_command_without_light_stays_null(conn, store):
    light_node(conn)
    shared_request(conn)

    assert store.link_command("req1", "light1", "on").command.arguments is None
    assert conn.execute("SELECT arguments FROM node_command").fetchone()["arguments"] is None
    assert store.execution("req1").arguments is None


def command(conn, cid, action="on", status="succeeded", sent=None, arguments=None):
    insert_node_command(conn, command_id=cid, internal_request_id=f"r-{cid}", node_id="light1", action=action,
                        status=status, execution_kind="shared", send_started_at=sent, send_attempted=1)
    if arguments is not None:
        conn.execute("UPDATE node_command SET arguments = ? WHERE command_id = ?", (json.dumps(arguments), cid))


def test_FR09_last_light_setting_latest_succeeded_on(conn, repo):
    light_node(conn)
    for cid in ("c1", "c2", "c3", "c4", "c5"):
        insert_request(conn, internal_request_id=f"r-{cid}", request_id=cid, node_id="light1", action="on")
    assert repo.last_success_arguments("light1", "on") is None

    command(conn, "c1", sent="2026-10-27T00:00:01+00:00", arguments={"brightness": 10, "color": "#FF0000"})
    command(conn, "c2", sent="2026-10-27T00:00:02+00:00", arguments={"brightness": 20, "color": "#00FF00"})
    # 실패한 명령과 다른 동작은 마지막 인자가 아니다.
    command(conn, "c3", status="failed", sent="2026-10-27T00:00:03+00:00", arguments=BLUE)
    command(conn, "c4", action="off", sent="2026-10-27T00:00:04+00:00")
    assert repo.last_success_arguments("light1", "on") == {"brightness": 20, "color": "#00FF00"}
    assert repo.last_success_arguments("light1", "off") is None  # 같은 동작의 명령만 본다

    # 마지막 성공 on 에 인자가 없으면 기본값을 쓰도록 None 이다.
    command(conn, "c5", sent="2026-10-27T00:00:05+00:00")
    assert repo.last_success_arguments("light1", "on") is None


def announce(controls):
    """조명 v2 발표의 템플릿 선언 (iot-device-descriptor spec 결정 3)."""
    return NodeAnnounce(
        node_id="light1", descriptor=legacy_descriptor("light", Occupancy.SHARED, **(controls or {})),
        received_at=NOW, session_id="s1", seq=1, sent_at=NOW,
    )


def test_FR04_announce_stores_controls(conn, repo):
    """조명의 인자는 선언에 저장한다. 판정은 저장한 선언을 읽는다 (FR-04, FR-09)."""
    repo.upsert_announced_node(announce(RING))

    row = conn.execute("SELECT descriptor FROM node WHERE node_id = 'light1'").fetchone()
    on = json.loads(row["descriptor"])["actions"][0]
    assert on["input_schema"]["properties"]["brightness"]["maximum"] == 64
    (node,) = repo.nodes("light")
    assert set(node.descriptor.action("on").properties) == {"brightness", "color"}


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
    """발표한 제어 정보 열은 선언으로, 명령의 조명 설정 열은 `arguments` 로 옮겨 지웠다 (spec 결정 12)."""
    node_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node)")}
    command_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_command)")}
    assert "light_controls" not in node_cols and {"descriptor", "descriptor_hash", "kind"} <= node_cols
    assert "light" not in command_cols and "arguments" in command_cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 11
