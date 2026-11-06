"""FR-04·FR-09·FR-19 스피커 음량과 스피커 제어 정보의 저장."""

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
from nearpilot.shared.models import LightControls, LightSetting, NodeAnnounce, SpeakerControls, SpeakerSetting

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
CAPPED = SpeakerControls(volume_max=20)


def speaker_node(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="speaker-1", capability="speaker", occupancy="shared")


def shared_request(conn):
    insert_request(conn, internal_request_id="req1", node_id="speaker-1", action="on")
    insert_policy(conn, policy_id="policy-speaker", node_id="speaker-1")
    insert_approval(conn, approval_id="approval-speaker", node_id="speaker-1", policy_id="policy-speaker",
                    internal_request_id="req1")
    insert_approval_usage(conn, usage_id="usage1", approval_id="approval-speaker", internal_request_id="req1")


def test_FR19_command_speaker_round_trip(conn, store):
    speaker_node(conn)
    shared_request(conn)
    quiet = SpeakerSetting(12)

    result = store.link_command("req1", "speaker-1", Action.ON, speaker=quiet)

    assert result.outcome is StoreOutcome.APPLIED and result.command.speaker == quiet
    row = conn.execute("SELECT speaker FROM node_command").fetchone()
    assert json.loads(row["speaker"]) == {"volume": 12}
    # 재전송·재시작은 저장한 명령 기록을 다시 읽는다. 값이 바뀌지 않는다.
    assert store.execution("req1").speaker == quiet
    again = store.link_command("req1", "speaker-1", Action.ON, speaker=SpeakerSetting(20))
    assert again.outcome is StoreOutcome.ALREADY_APPLIED and again.command.speaker == quiet
    assert json.loads(conn.execute("SELECT speaker FROM node_command").fetchone()["speaker"]) == {"volume": 12}


def test_FR09_restored_context_keeps_speaker(conn, store):
    speaker_node(conn)
    shared_request(conn)
    conn.execute("UPDATE request SET settings_json = ? WHERE internal_request_id = 'req1'",
                 (settings_to_json(Settings()),))
    quiet = SpeakerSetting(12)
    command = store.link_command("req1", "speaker-1", Action.ON, speaker=quiet).command

    # 기기 결과를 인계할 때 다시 만든 문맥이 판정이 넘긴 음량과 같아야 한다.
    assert store.execution_context(command.command_id).speaker == quiet


def test_FR19_command_without_speaker_stays_null(conn, store):
    speaker_node(conn)
    shared_request(conn)

    assert store.link_command("req1", "speaker-1", Action.ON).command.speaker is None
    assert conn.execute("SELECT speaker FROM node_command").fetchone()["speaker"] is None
    assert store.execution("req1").speaker is None

    # 조명 명령의 speaker 열도 비어 있다.
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")
    insert_request(conn, internal_request_id="req2", request_id="ext-2", node_id="light1", action="on")
    blue = LightSetting(30, "#0000FF")
    lit = store.link_command("req2", "light1", Action.ON, blue).command
    assert lit.light == blue and lit.speaker is None
    row = conn.execute("SELECT speaker FROM node_command WHERE internal_request_id = 'req2'").fetchone()
    assert row["speaker"] is None


def test_FR19_restore_pending_keeps_speaker(conn, repo, store):
    speaker_node(conn)
    shared_request(conn)
    repo.upsert_announced_node(announce(CAPPED))
    store.link_command("req1", "speaker-1", Action.ON, speaker=SpeakerSetting(12))  # PENDING 명령

    bundle = next(b for b in store.restore_pending() if b.node.node_id == "speaker-1")

    assert bundle.node.speaker_controls == CAPPED
    assert [c.speaker for c in bundle.commands] == [SpeakerSetting(12)]


def command(conn, cid, action="on", status="succeeded", sent=None, speaker=None):
    insert_node_command(conn, command_id=cid, internal_request_id=f"r-{cid}", node_id="speaker-1", action=action,
                        status=status, execution_kind="shared", send_started_at=sent, send_attempted=1)
    if speaker is not None:
        conn.execute("UPDATE node_command SET speaker = ? WHERE command_id = ?", (json.dumps(speaker), cid))


def test_FR09_last_speaker_setting_latest_succeeded_on(conn, repo):
    speaker_node(conn)
    for cid in ("c1", "c2", "c3", "c4", "c5"):
        insert_request(conn, internal_request_id=f"r-{cid}", request_id=cid, node_id="speaker-1", action="on")
    assert repo.last_speaker_setting("speaker-1") is None

    # 순서는 전송 시작 시각이다. 저장한 순서가 아니다.
    command(conn, "c2", sent="2026-10-29T00:00:02+00:00", speaker={"volume": 20})
    command(conn, "c1", sent="2026-10-29T00:00:01+00:00", speaker={"volume": 10})
    # 실패한 명령과 off 는 마지막 음량이 아니다.
    command(conn, "c3", status="failed", sent="2026-10-29T00:00:03+00:00", speaker={"volume": 5})
    command(conn, "c4", action="off", sent="2026-10-29T00:00:04+00:00")
    assert repo.last_speaker_setting("speaker-1") == SpeakerSetting(20)

    # 마지막 성공 on 에 음량이 없으면 기본값을 쓰도록 None 이다.
    command(conn, "c5", sent="2026-10-29T00:00:05+00:00")
    assert repo.last_speaker_setting("speaker-1") is None


def announce(controls):
    """스피커 v2 발표의 템플릿 선언 (iot-device-descriptor spec 결정 3)."""
    extra = {} if controls is None else dict(volume_max=controls.volume_max)
    return NodeAnnounce(
        node_id="speaker-1", descriptor=legacy_descriptor(Capability.SPEAKER, Occupancy.SHARED, **extra),
        received_at=NOW, session_id="s1", seq=1, sent_at=NOW,
    )


def test_FR04_announce_stores_speaker_controls(conn, repo):
    """스피커 제어 정보는 선언의 volume 상한으로 계산한다 (spec 결정 7)."""
    repo.upsert_announced_node(announce(CAPPED))

    row = conn.execute("SELECT descriptor FROM node WHERE node_id = 'speaker-1'").fetchone()
    on = json.loads(row["descriptor"])["actions"][0]
    assert on["input_schema"]["properties"]["volume"]["maximum"] == 20
    assert [n.speaker_controls for n in repo.nodes(Capability.SPEAKER)] == [CAPPED]


def test_FR04_speaker_controls_change_resets_trust(conn, repo):
    """음량 상한은 선언의 인자 스키마다. 바뀌면 선언 해시가 바뀐다 (FR-04, #245)."""
    speaker_node(conn)  # trusted, 선언 없는 행

    repo.upsert_announced_node(announce(CAPPED))
    repo.upsert_announced_node(announce(CAPPED))
    row = conn.execute("SELECT trust_state FROM node WHERE node_id = 'speaker-1'").fetchone()
    assert row["trust_state"] == "trusted"
    assert conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"] == 0

    repo.upsert_announced_node(announce(None))
    row = conn.execute("SELECT trust_state FROM node WHERE node_id = 'speaker-1'").fetchone()
    assert row["trust_state"] == "discovered"
    assert conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"] == 1


def test_FR04_speaker_columns_migrated(conn):
    """발표한 음량 제어 정보 열은 선언으로 옮겨 지웠다. 명령의 음량 열은 남는다 (spec 결정 9)."""
    node_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node)")}
    command_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_command)")}
    assert "speaker_controls" not in node_cols and "speaker" in command_cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
