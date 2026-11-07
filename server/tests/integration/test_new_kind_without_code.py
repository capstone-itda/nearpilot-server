"""FR-09·FR-23·NFR-07 설정만 추가한 새 종류의 기기를 서버 코드 수정 없이 쓴다 (specs/core-device-actions/spec.md).

새 종류 `fan` 의 v3 발표를 IOT 해석 → 실제 SQLite 저장 → 판정·조회 → `cmd` 인코딩까지 잇는다.
근접 추정, 실행, 감사만 fake 다 (`_world.World`).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from _world import NOW, OWNER, SETTINGS, VISITOR_A, FakeReadiness, World
from nearpilot.core.decision.query import DeviceQueryService
from nearpilot.iot.messages import decode_message, encode_command
from nearpilot.shared.enums import ExecutionKind, ReasonCode, TrustState, Verdict
from nearpilot.shared.models import NodeCommand, TargetSpec, UseRequest

SESSION = "12345678-1234-1234-1234-123456789abc"
_SIGNAL = {"kind": "signal_output"}
FAN = {
    "format": "nearpilot.device/1", "kind": "fan", "occupancy": "shared",
    "vendor": {"name": "acme", "model": "f1", "firmware": "1.0"},
    "actions": [
        {"name": "spin", "description": "팬을 돌린다", "result": _SIGNAL,
         "input_schema": {"type": "object", "additionalProperties": False, "required": [],
                          "properties": {"speed": {"type": "integer", "minimum": 1, "maximum": 5}}}},
        {"name": "stop", "description": "팬을 멈춘다", "role": "deactivate", "result": _SIGNAL,
         "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    ],
}
# 운영자가 새 종류의 설정만 추가했다. threshold_fan 과 any_allowed_kinds 는 추가하지 않았다.
FAN_SETTINGS = replace(SETTINGS, param_max={"fan": {"spin": {"speed": 4}}},
                       param_default={"fan": {"spin": {"speed": 2}}})


@pytest.fixture
def world():
    w = World(settings=FAN_SETTINGS)
    announce = {"schema_version": 3, "node_id": "fan-1", "session_id": SESSION, "seq": 1,
                "sent_at": NOW.isoformat().replace("+00:00", "Z"), "descriptor": FAN}
    report = decode_message("nearpilot/node/fan-1/announce", json.dumps(announce).encode(),
                            received_at=NOW)
    w.repo.upsert_announced_node(report)
    # 소유자가 발견된 기기를 장소에 두고 신뢰했다. 공개·승인 불필요 정책이다.
    w.conn.execute("UPDATE node SET place_id = 'p1', trust_state = 'trusted', last_seen_at = ? "
                   "WHERE node_id = 'fan-1'", ((NOW - timedelta(seconds=1)).isoformat(),))
    w.conn.execute("INSERT INTO access_policy (policy_id, node_id, current_version) VALUES ('pol-fan', 'fan-1', 1)")
    w.conn.execute(
        "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, approval_ttl_sec, "
        "approval_mode, max_uses) VALUES ('pol-fan', 1, 'public', 'open', ?, 3600, 'one_time', 1)", (OWNER,))
    yield w
    w.close()


def use(w, target, action, request_id, arguments=None):
    req = UseRequest(VISITOR_A, request_id, TargetSpec.parse(target), action, "claude", NOW, arguments=arguments)
    return w.pipeline.decide_for_execution(req)


def test_FR09_new_kind_announced_and_stored(world):
    (node,) = [n for n in world.repo.nodes() if n.node_id == "fan-1"]
    assert (node.kind, node.trust_state, node.descriptor.vendor.name) == ("fan", TrustState.TRUSTED, "acme")
    assert world.repo.nodes("fan") == [node]


def test_FR09_new_kind_explicit_target_executes(world):
    d, ctx = use(world, "fan-1", "spin", "r1", {"speed": 3})
    assert (d.verdict, d.reason, d.node_id, d.use_id) == (Verdict.OK, ReasonCode.OK, "fan-1", None)
    assert (ctx.execution_kind, ctx.arguments) == (ExecutionKind.SHARED, {"speed": 3})
    # 생략한 인자는 param_default 로 채운다.
    d, ctx = use(world, "fan-1", "spin", "r2")
    assert d.verdict is Verdict.OK and ctx.arguments == {"speed": 2}
    # IOT 는 판정이 정한 인자를 v3 `arguments` 로 그대로 보낸다.
    command = NodeCommand("c1", "fan-1", "spin", SESSION, NOW, NOW + timedelta(seconds=3), arguments=ctx.arguments)
    data = json.loads(encode_command(command, schema_version=3))
    assert (data["action"], data["arguments"]) == ("spin", {"speed": 2})


def test_FR09_new_kind_arguments_checked_by_declaration_and_settings(world):
    d, ctx = use(world, "fan-1", "spin", "r1", {"speed": 5})  # 선언은 5까지, param_max 는 4까지
    assert (d.verdict, d.reason, ctx) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE, None)
    d, _ = use(world, "fan-1", "spin", "r2", {"speed": "fast"})
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    d, _ = use(world, "fan-1", "on", "r3")  # 팬이 선언하지 않은 동작
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.UNSUPPORTED_ACTION)


def test_FR23_new_kind_without_kind_settings_only_explicit(world):
    # threshold_fan 이 없어 근접 지정을, any_allowed_kinds 에 없어 전역 할당을 허가하지 않는다 (§1.2).
    for target, request_id in (("nearest:fan", "n1"), ("any:fan", "a1")):
        d, _ = use(world, target, "spin", request_id)
        assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert world.count("SELECT COUNT(*) FROM node_command") == 0


def test_FR11_new_kind_query_shows_declared_actions(world):
    query = DeviceQueryService(world.repo, world.policy, FAN_SETTINGS, lambda: NOW, FakeReadiness())
    state = query.get_device_state(VISITOR_A, "fan-1")
    assert (state["kind"], state["occupancy"]) == ("fan", "shared")
    spin, stop = state["actions"]
    assert (spin["name"], spin["input_schema"]["properties"]["speed"]["maximum"]) == ("spin", 4)
    assert stop == {"name": "stop", "description": "팬을 멈춘다", "input_schema": FAN["actions"][1]["input_schema"]}
    assert "acme" not in json.dumps(state, ensure_ascii=False)  # 제작사 정보와 선언 해시는 넣지 않는다
    found = [d for d in query.find_available_devices(VISITOR_A) if d["node_id"] == "fan-1"]
    assert [d["access"] for d in found] == ["operable"]
