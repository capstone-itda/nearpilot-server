"""FR-09·FR-17·FR-23 기기 선언으로 하는 동작·인자 판정 (specs/core-device-actions/spec.md).

판정은 선언의 `occupancy`와 동작 `role`로만 뜻을 구분한다. 종류와 동작 이름으로 분기하지 않는다 (NFR-07).
다른 계층은 판정 파이프라인 테스트의 fake 를 쓴다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from test_decision_pipeline import FakeRepo, FakeWindow, build, frame, node, request

from nearpilot.core.allocation import AllocationRequest, AllocationWindow
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import parse_descriptor
from nearpilot.shared.enums import Occupancy, ReasonCode, ReserveOutcome, Step, Verdict

_SIGNAL = {"kind": "signal_output"}
_NO_ARGUMENTS = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}

# 서버가 모르는 공용형 종류. 켜기 spin(속도·모드), 꺼짐 stop.
FAN = parse_descriptor({
    "format": "nearpilot.device/1", "kind": "fan", "occupancy": "shared",
    "vendor": {"name": "acme", "model": "f1", "firmware": "1.0"},
    "actions": [
        {"name": "spin", "description": "팬을 돌린다", "result": _SIGNAL,
         "input_schema": {"type": "object", "additionalProperties": False, "required": [], "properties": {
             "speed": {"type": "integer", "minimum": 1, "maximum": 5},
             "mode": {"type": "string", "enum": ["calm", "turbo"]}}}},
        {"name": "stop", "description": "팬을 멈춘다", "role": "deactivate", "input_schema": _NO_ARGUMENTS,
         "result": _SIGNAL},
    ]})
# 서버가 모르는 대여형 종류. 재조작은 relock 이다. 이름이 close 가 아니다.
BOX = parse_descriptor({
    "format": "nearpilot.device/1", "kind": "box", "occupancy": "rental",
    "vendor": {"name": "acme", "model": "b1", "firmware": "1.0"},
    "actions": [
        {"name": "unlock", "description": "상자를 연다", "input_schema": _NO_ARGUMENTS, "result": _SIGNAL},
        {"name": "relock", "description": "상자를 다시 잠근다", "role": "reoperate",
         "input_schema": _NO_ARGUMENTS, "result": _SIGNAL},
        {"name": "close", "description": "덮개를 닫는다", "input_schema": _NO_ARGUMENTS, "result": _SIGNAL},
    ]})
FAN_SETTINGS = Settings(version="v1", param_max={"fan": {"spin": {"speed": 4}}},
                        param_default={"fan": {"spin": {"speed": 2}}})


class DeviceRepo(FakeRepo):
    """기기 하나가 후보다. 같은 동작의 마지막 성공 인자를 돌려준다."""

    def __init__(self, target, last=None):
        super().__init__()
        self.nodes_list = [target]
        self.frames = [replace(self.frames[0], node_id=target.node_id)]
        self.reserve_use_id = None if target.occupancy is Occupancy.SHARED else "use-1"
        self.last = last
        self.last_calls = []

    def last_success_arguments(self, node_id, action):
        self.last_calls.append((node_id, action))
        return self.last


def fan(descriptor=FAN):
    return node("fan-1", Occupancy.SHARED, "fan", descriptor=descriptor)


def box():
    return node("box-1", Occupancy.RENTAL, "box", descriptor=BOX)


def decide(target_node, action, arguments=None, *, last=None, settings=FAN_SETTINGS, target=None,
           posteriors=None):
    pipe, _, _, pol, audit = build(posteriors=posteriors or {target_node.node_id: 0.99}, settings=settings)
    repo = DeviceRepo(target_node, last)
    pipe._repo = repo
    decision, ctx = pipe.decide_for_execution(
        request(target or target_node.node_id, action=action, arguments=arguments))
    return decision, ctx, repo, pol, audit


def argument_events(audit):
    return [e.payload["arguments"] for e in audit.events
            if e.step is Step.PROXIMITY and "arguments" in e.payload]


def test_FR09_undeclared_action_unsupported():
    d, ctx, repo, pol, audit = decide(fan(), "on")  # 조명의 동작 이름이어도 팬은 선언하지 않았다
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.UNSUPPORTED_ACTION, "fan-1")
    assert ctx is None and repo.reserved == [] and pol.calls == []  # ④ 정책과 ⑤⑥ 예약 앞에서 멈춘다
    assert argument_events(audit)[-1] == {"node_id": "fan-1", "action": "on", "result": "UNSUPPORTED_ACTION"}


def test_FR09_node_without_descriptor_unsupported():
    # 이전하지 못한 행(선언 없음)은 어떤 동작도 실행하지 않는다.
    d, ctx, repo, _, _ = decide(node("locker-1", descriptor=None), "open")
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.UNSUPPORTED_ACTION)
    assert ctx is None and repo.reserved == []


def test_FR09_argument_over_param_max_out_of_range():
    assert decide(fan(), "spin", {"speed": 4})[0].verdict is Verdict.OK
    d, ctx, repo, _, audit = decide(fan(), "spin", {"speed": 5})  # 선언은 5까지, 설정은 4까지
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE)
    assert ctx is None and repo.reserved == []
    assert argument_events(audit)[-1]["param_max"] == {"speed": 4}
    assert decide(fan(), "spin", {"speed": 0})[0].reason is ReasonCode.PARAMETER_OUT_OF_RANGE  # minimum
    # 설정이 없으면 선언의 범위만 쓴다.
    assert decide(fan(), "spin", {"speed": 5}, settings=Settings(version="v1"))[0].verdict is Verdict.OK


@pytest.mark.parametrize("arguments", [
    {"speed": "2"}, {"speed": 2.5}, {"speed": True}, {"mode": "storm"}, {"mode": 1}, {"power": 3},
    {"speed": 9, "mode": "storm"},  # 범위와 형식 오류가 함께 있으면 형식 오류다
])
def test_FR09_argument_schema_violation_invalid(arguments):
    d, ctx, repo, _, _ = decide(fan(), "spin", arguments)
    assert (d.verdict, d.reason, ctx) == (Verdict.DENY, ReasonCode.INVALID_INPUT, None)
    assert repo.reserved == []


def test_FR09_deactivate_takes_no_arguments():
    assert decide(fan(), "stop")[0].verdict is Verdict.OK
    d, _, repo, _, _ = decide(fan(), "stop", {"speed": 1})
    assert (d.reason, repo.last_calls) == (ReasonCode.INVALID_INPUT, [])


def test_FR09_omitted_argument_uses_last_success():
    d, ctx, repo, _, _ = decide(fan(), "spin", {"mode": "turbo"}, last={"speed": 3, "mode": "calm"})
    assert d.verdict is Verdict.OK and ctx.arguments == {"speed": 3, "mode": "turbo"}
    assert repo.last_calls == [("fan-1", "spin")]  # 같은 기기·같은 동작의 기록을 읽는다


def test_FR09_omitted_argument_uses_param_default():
    d, ctx, _, _, _ = decide(fan(), "spin", {"mode": "calm"})
    assert d.verdict is Verdict.OK and ctx.arguments == {"speed": 2, "mode": "calm"}


def test_FR09_omitted_argument_without_default_left_out():
    # mode 는 마지막 성공값도 기본값도 없다. 생략한 채 보낸다. 출력은 기기가 정한다.
    d, ctx, _, _, _ = decide(fan(), "spin")
    assert d.verdict is Verdict.OK and ctx.arguments == {"speed": 2}
    d, ctx, _, _, _ = decide(fan(), "spin", settings=Settings(version="v1"))
    assert d.verdict is Verdict.OK and ctx.arguments is None


def test_FR09_filled_argument_out_of_range():
    # 앞선 값이 지금 설정의 상한을 넘으면 낮춰서 실행하지 않고 거부한다.
    d, ctx, repo, _, audit = decide(fan(), "spin", last={"speed": 5})
    assert (d.verdict, d.reason, ctx) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE, None)
    assert argument_events(audit)[-1]["resolved"] == {"speed": 5}


def test_FR09_reoperate_role_without_rental_use_not_found():
    pipe, _, _, pol, _ = build(posteriors={"box-1": 0.99}, settings=FAN_SETTINGS)
    repo = DeviceRepo(box())
    repo.reserve_outcome = ReserveOutcome.USE_NOT_FOUND  # DB 가 본인 대여를 찾지 못했다
    pipe._repo = repo
    d, ctx = pipe.decide_for_execution(request("box-1", action="relock"))
    assert (d.verdict, d.reason, ctx) == (Verdict.DENY, ReasonCode.USE_NOT_FOUND, None)
    assert repo.reserved[0].reoperate_only is True  # 새 점유를 만들지 않도록 DB 에 역할을 넘긴다


def test_FR09_reoperate_role_by_declaration_not_name():
    captured = []

    class Policy:
        def check(self, user_id, node_id, action, now, *, internal_request_id=None, reoperate=False):
            captured.append((action, reoperate))
            from nearpilot.shared.models import PolicyCheckResult
            return PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1)

    for action, reoperate in (("relock", True), ("close", False), ("unlock", False)):
        pipe, _, _, _, _ = build(posteriors={"box-1": 0.99}, settings=FAN_SETTINGS, policy=Policy())
        repo = DeviceRepo(box())
        pipe._repo = repo
        d = pipe.decide(request("box-1", action=action))
        assert d.verdict is Verdict.OK
        assert repo.reserved[0].reoperate_only is reoperate
        assert captured[-1] == (action, reoperate)  # 정책도 이름이 아니라 역할로 승인 면제를 정한다


def test_FR17_kind_without_threshold_denied():
    # threshold_fan 이 없다. 근접 지정은 허가하지 않고 명시 지정은 쓴다 (§1.2, FR-17).
    d, _, repo, _, _ = decide(fan(), "spin", target="nearest:fan")
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT) and repo.reserved == []
    with_threshold = replace(FAN_SETTINGS, thresholds={"fan": 0.5})
    d, _, _, _, _ = decide(fan(), "spin", target="nearest:fan", settings=with_threshold)
    assert (d.verdict, d.node_id) == (Verdict.OK, "fan-1")
    d, _, _, _, _ = decide(fan(), "spin", target="nearest:fan", settings=with_threshold,
                           posteriors={"fan-1": 0.4})
    assert (d.verdict, d.reason) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE)
    assert decide(fan(), "spin")[0].verdict is Verdict.OK  # 명시 지정


def test_FR23_any_kind_not_allowed_denied():
    settings = replace(FAN_SETTINGS, thresholds={"fan": 0.5})
    pipe, _, _, _, audit = build(posteriors={"fan-1": 0.99}, settings=settings, window=FakeWindow())
    repo = DeviceRepo(fan())
    pipe._repo = repo
    d = pipe.decide(request("any:fan", action="spin"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT) and repo.reserved == []
    allowed = replace(settings, any_allowed_kinds=("locker", "fan"))
    window = FakeWindow()
    pipe, _, _, _, _ = build(posteriors={"fan-1": 0.99}, settings=allowed, window=window)
    pipe._repo = DeviceRepo(fan())
    d = pipe.decide(request("any:fan", action="spin"))
    assert (d.verdict, d.node_id) == (Verdict.OK, "fan-1") and len(window.requests) == 1


def test_FR23_window_groups_by_kind():
    """다른 종류의 any: 요청은 같은 시간에 와도 다른 구간이다 (FR-23)."""
    gate, batches = threading.Event(), []

    def wait(seconds):
        assert gate.wait(5)

    def allocate_fn(requests):
        batches.append(sorted(r.request_id for r in requests))
        return {r.request_id: next(iter(r.candidates)) for r in requests}

    window = AllocationWindow(500, wait=wait, allocate_fn=allocate_fn)
    results = {}
    jobs = [("l1", "locker", {"locker-1": 0.9}), ("f1", "fan", {"fan-1": 0.9}), ("l2", "locker", {"locker-2": 0.9})]
    threads = [threading.Thread(target=lambda rid=rid, g=g, c=c: results.__setitem__(
        rid, window.submit(AllocationRequest(rid, c), group=g))) for rid, g, c in jobs]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 2
    while window.pending() < 3:
        assert time.monotonic() < deadline
        time.sleep(0.002)
    gate.set()
    for t in threads:
        t.join(5)
    assert sorted(batches) == [["f1"], ["l1", "l2"]]
    assert results == {"l1": "locker-1", "f1": "fan-1", "l2": "locker-2"}
    assert window.pending() == 0


def test_NFR07_new_kind_decided_by_occupancy_and_role():
    # 공용형 새 종류는 세션 없이 실행하고, 대여형 새 종류는 점유 세션을 만든다.
    d, ctx, _, _, _ = decide(fan(), "spin", {"speed": 1})
    assert (d.verdict, d.use_id, ctx.execution_kind.value) == (Verdict.OK, None, "shared")
    d, ctx, _, _, _ = decide(box(), "unlock")
    assert (d.verdict, d.use_id, ctx.execution_kind.value) == (Verdict.OK, "use-1", "rental_initial")
    assert frame("box-1").node_id == "box-1"
