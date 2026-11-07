"""FR-09·FR-18 ③ 대상 확정 뒤 조명 `on` 인자 검사 (specs/core-device-actions/spec.md).

조명은 v2 템플릿 선언을 발표한 기기다. 인자는 선언과 `param_max`로 검사한다.
다른 계층은 판정 파이프라인 테스트의 fake 를 쓴다.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from test_decision_pipeline import FakeRepo, build, node, request

from nearpilot.core.decision.digest import input_digest
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import Occupancy, ReasonCode, Step, Verdict

RING = {"brightness_max": 64, "color": True}
SETTINGS = Settings(version="v1")


class LightRepo(FakeRepo):
    """조명 하나가 후보다. 같은 동작의 마지막 성공 인자를 돌려준다."""

    def __init__(self, controls=RING, last=None):
        super().__init__()
        descriptor = legacy_descriptor("light", Occupancy.SHARED, **(controls or {}))
        self.nodes_list = [node("light-1", Occupancy.SHARED, "light", descriptor=descriptor)]
        self.frames = [replace(self.frames[0], node_id="light-1")]
        self.reserve_use_id = None
        self.last = last
        self.last_calls = []

    def last_success_arguments(self, node_id, action):
        self.last_calls.append((node_id, action))
        return self.last


def decide(arguments=None, *, controls=RING, last=None, settings=SETTINGS, target="nearest:light",
           action="on"):
    pipe, _, _, _, audit = build(posteriors={"light-1": 0.99}, settings=settings)
    repo = LightRepo(controls, last)
    pipe._repo = repo
    decision, ctx = pipe.decide_for_execution(request(target, action=action, arguments=arguments))
    return decision, ctx, repo, audit


def argument_events(audit):
    return [e.payload["arguments"] for e in audit.events
            if e.step is Step.PROXIMITY and "arguments" in e.payload]


def test_FR09_given_setting_goes_to_handoff():
    d, ctx, repo, _ = decide({"brightness": 30, "color": "#0000FF"})
    assert (d.verdict, d.node_id) == (Verdict.OK, "light-1")
    assert ctx.arguments == {"brightness": 30, "color": "#0000FF"}
    assert repo.reserved  # 검사를 통과해야 예약한다


def test_FR09_brightness_boundary():
    d, ctx, _, _ = decide({"brightness": 64})
    assert d.verdict is Verdict.OK and ctx.arguments["brightness"] == 64
    d, ctx, repo, audit = decide({"brightness": 65})
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE, "light-1")
    assert ctx is None and repo.reserved == []  # 명령과 승인 사용분을 만들지 않는다
    assert argument_events(audit)[-1]["result"] == "PARAMETER_OUT_OF_RANGE"


def test_FR09_k_is_smaller_of_device_and_server_cap():
    small = Settings(version="v1", param_max={"light": {"on": {"brightness": 40}}},
                     param_default={"light": {"on": {"brightness": 40, "color": "#FFFFFF"}}})
    assert decide({"brightness": 40}, settings=small)[0].verdict is Verdict.OK
    assert decide({"brightness": 41}, settings=small)[0].reason is ReasonCode.PARAMETER_OUT_OF_RANGE
    device = {"brightness_max": 20, "color": True}
    assert decide({"brightness": 20}, controls=device)[0].verdict is Verdict.OK
    assert decide({"brightness": 21}, controls=device)[0].reason is ReasonCode.PARAMETER_OUT_OF_RANGE


def test_FR09_brightness_zero_is_on_not_off():
    d, ctx, repo, _ = decide({"brightness": 0})
    assert d.verdict is Verdict.OK and ctx.arguments["brightness"] == 0
    assert repo.reserved[0].action == "on"  # 서버는 동작을 off 로 바꾸지 않는다


def test_FR09_nearest_light_without_controls_denies():
    # 인자를 선언하지 않은 조명에 인자를 주면 스키마 위반이다 (FR-09).
    d, ctx, repo, _ = decide({"brightness": 30}, controls=None)
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.INVALID_INPUT, "light-1")
    assert ctx is None and repo.reserved == []


def test_FR09_color_unsupported_denies():
    white = {"brightness_max": 64, "color": False}
    d, _, repo, _ = decide({"color": "#0000FF"}, controls=white)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT) and repo.reserved == []
    # 색을 선언하지 않은 조명에는 색을 넣지 않는다 (spec core-device-actions 결정 14).
    d, ctx, _, _ = decide({"brightness": 30}, controls=white)
    assert d.verdict is Verdict.OK and ctx.arguments == {"brightness": 30}


def test_FR09_explicit_target_checks_after_target():
    d, _, _, _ = decide({"brightness": 65}, target="light-1")
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE)


def test_FR09_omitted_uses_defaults():
    d, ctx, repo, _ = decide(None)
    assert d.verdict is Verdict.OK and ctx.arguments == {"brightness": 64, "color": "#FFFFFF"}
    assert repo.last_calls == [("light-1", "on")]


def test_FR09_omitted_keeps_last_setting():
    last = {"brightness": 20, "color": "#00FF00"}
    assert decide(None, last=last)[1].arguments == last
    assert decide({"color": "#FF0000"}, last=last)[1].arguments == {"brightness": 20, "color": "#FF0000"}
    assert decide({"brightness": 5}, last=last)[1].arguments == {"brightness": 5, "color": "#00FF00"}


def test_FR09_default_above_device_cap_denies():
    device = {"brightness_max": 32, "color": True}
    d, _, _, _ = decide(None, controls=device)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE)


def test_FR09_lit_light_accepts_new_setting():
    # 마지막 성공 명령이 on 이어도 새 on 을 허가한다. 공용형은 점유 없이 새 명령이다.
    d, ctx, _, _ = decide({"brightness": 50, "color": "#FF00FF"}, last={"brightness": 64, "color": "#FFFFFF"})
    assert d.verdict is Verdict.OK and ctx.arguments == {"brightness": 50, "color": "#FF00FF"}


def test_FR09_light_without_controls_keeps_plain_commands():
    for action in ("on", "off"):
        d, ctx, repo, _ = decide(None, controls=None, action=action)
        assert d.verdict is Verdict.OK and ctx.arguments is None and repo.last_calls == []
    d, ctx, repo, _ = decide(None, action="off")
    assert d.verdict is Verdict.OK and ctx.arguments is None and repo.last_calls == []


def test_FR09_lowercase_color_normalized():
    d, ctx, _, _ = decide({"brightness": 30, "color": "#00ff0a"})
    assert d.verdict is Verdict.OK and ctx.arguments["color"] == "#00FF0A"


def test_FR22_digest_without_light_unchanged():
    # 인자 없는 요청의 정규화 입력은 이전 정의와 같다 (이전 커밋의 값).
    req = request("nearest:light", action="on")
    assert input_digest(req) == input_digest(replace(req, arguments=None))
    expected = hashlib.sha256(
        b'{"action":"on","parent_internal_request_id":null,'
        b'"target":{"capability":"light","kind":"nearest","node_id":null}}').hexdigest()
    assert input_digest(req) == expected
    explicit = hashlib.sha256(
        b'{"action":"on","parent_internal_request_id":null,'
        b'"target":{"capability":"light","kind":"explicit","node_id":"light-1"}}').hexdigest()
    assert input_digest(request("light-1", action="on")) == explicit


def test_FR22_same_request_id_other_light_conflicts():
    blue = request("nearest:light", action="on", arguments={"brightness": 30, "color": "#0000FF"})
    assert input_digest(blue) != input_digest(replace(blue, arguments={"brightness": 31, "color": "#0000FF"}))
    assert input_digest(blue) != input_digest(replace(blue, arguments=None))
    # 키 순서는 요약에 영향을 주지 않는다.
    assert input_digest(blue) == input_digest(replace(blue, arguments={"color": "#0000FF", "brightness": 30}))


@pytest.mark.parametrize(("value", "reason"), [
    (True, ReasonCode.INVALID_INPUT), (1.0, ReasonCode.INVALID_INPUT), ("30", ReasonCode.INVALID_INPUT),
    (-1, ReasonCode.PARAMETER_OUT_OF_RANGE), (256, ReasonCode.PARAMETER_OUT_OF_RANGE),
])
def test_FR09_bad_brightness_denies(value, reason):
    d, ctx, _, _ = decide({"brightness": value})
    assert (d.verdict, d.reason, ctx) == (Verdict.DENY, reason, None)
