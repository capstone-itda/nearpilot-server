"""FR-09·FR-18 ③ 대상 확정 뒤 조명 설정 검사. 다른 계층은 판정 파이프라인 테스트의 fake 를 쓴다."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_decision_pipeline import FakeRepo, build, node, request

from nearpilot.core.decision.digest import input_digest
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, Capability, Occupancy, ReasonCode, Step, Verdict
from nearpilot.shared.models import LightControls, LightSetting

RING = LightControls(brightness_max=64, color=True)
SETTINGS = Settings(version="v1")


class LightRepo(FakeRepo):
    """조명 하나가 후보다. 마지막 조명 설정을 돌려준다."""

    def __init__(self, controls=RING, last=None):
        super().__init__()
        self.nodes_list = [node("light-1", Occupancy.SHARED, Capability.LIGHT, light_controls=controls)]
        self.frames = [replace(self.frames[0], node_id="light-1")]
        self.reserve_use_id = None
        self.last = last
        self.last_calls = []

    def last_light_setting(self, node_id):
        self.last_calls.append(node_id)
        return self.last


def decide(light=None, *, controls=RING, last=None, settings=SETTINGS, target="nearest:light",
           action=Action.ON):
    pipe, _, _, _, audit = build(posteriors={"light-1": 0.99}, settings=settings)
    repo = LightRepo(controls, last)
    pipe._repo = repo
    decision, ctx = pipe.decide_for_execution(request(target, action=action, light=light))
    return decision, ctx, repo, audit


def light_events(audit):
    return [e.payload["light"] for e in audit.events if e.step is Step.PROXIMITY and "light" in e.payload]


def test_FR09_given_setting_goes_to_handoff():
    d, ctx, repo, _ = decide(LightSetting(30, "#0000FF"))
    assert (d.verdict, d.node_id) == (Verdict.OK, "light-1")
    assert ctx.light == LightSetting(30, "#0000FF")
    assert repo.reserved  # 검사를 통과해야 예약한다


def test_FR09_brightness_boundary():
    d, ctx, _, _ = decide(LightSetting(64, None))
    assert d.verdict is Verdict.OK and ctx.light.brightness == 64
    d, ctx, repo, audit = decide(LightSetting(65, None))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE, "light-1")
    assert ctx is None and repo.reserved == []  # 명령과 승인 사용분을 만들지 않는다
    assert light_events(audit)[-1]["result"] == "PARAMETER_OUT_OF_RANGE"


def test_FR09_k_is_smaller_of_device_and_server_cap():
    small = Settings(version="v1", light_brightness_max=40, light_default_brightness=40)
    assert decide(LightSetting(40, None), settings=small)[0].verdict is Verdict.OK
    assert decide(LightSetting(41, None), settings=small)[0].reason is ReasonCode.PARAMETER_OUT_OF_RANGE
    device = LightControls(brightness_max=20, color=True)
    assert decide(LightSetting(20, None), controls=device)[0].verdict is Verdict.OK
    assert decide(LightSetting(21, None), controls=device)[0].reason is ReasonCode.PARAMETER_OUT_OF_RANGE


def test_FR09_brightness_zero_is_on_not_off():
    d, ctx, repo, _ = decide(LightSetting(0, None))
    assert d.verdict is Verdict.OK and ctx.light.brightness == 0
    assert repo.reserved[0].action is Action.ON  # 서버는 동작을 off 로 바꾸지 않는다


def test_FR09_nearest_light_without_controls_denies():
    d, ctx, repo, _ = decide(LightSetting(30, None), controls=None)
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.UNSUPPORTED_PARAMETER, "light-1")
    assert ctx is None and repo.reserved == []


def test_FR09_color_unsupported_denies():
    white = LightControls(brightness_max=64, color=False)
    d, _, repo, _ = decide(LightSetting(None, "#0000FF"), controls=white)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.UNSUPPORTED_PARAMETER) and repo.reserved == []
    d, ctx, _, _ = decide(LightSetting(30, None), controls=white)
    assert d.verdict is Verdict.OK and ctx.light == LightSetting(30, "#FFFFFF")


def test_FR09_explicit_target_checks_after_target():
    d, _, _, _ = decide(LightSetting(65, None), target="light-1")
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE)


def test_FR09_omitted_uses_defaults():
    d, ctx, repo, _ = decide(None)
    assert d.verdict is Verdict.OK and ctx.light == LightSetting(64, "#FFFFFF")
    assert repo.last_calls == ["light-1"]


def test_FR09_omitted_keeps_last_setting():
    last = LightSetting(20, "#00FF00")
    assert decide(None, last=last)[1].light == last
    assert decide(LightSetting(None, "#FF0000"), last=last)[1].light == LightSetting(20, "#FF0000")
    assert decide(LightSetting(5, None), last=last)[1].light == LightSetting(5, "#00FF00")


def test_FR09_default_above_device_cap_denies():
    device = LightControls(brightness_max=32, color=True)
    d, _, _, _ = decide(None, controls=device)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE)


def test_FR09_lit_light_accepts_new_setting():
    # 마지막 성공 명령이 on 이어도 새 on 을 허가한다. 공용형은 점유 없이 새 명령이다.
    d, ctx, _, _ = decide(LightSetting(50, "#FF00FF"), last=LightSetting(64, "#FFFFFF"))
    assert d.verdict is Verdict.OK and ctx.light == LightSetting(50, "#FF00FF")


def test_FR09_light_without_controls_keeps_plain_commands():
    for action in (Action.ON, Action.OFF):
        d, ctx, repo, _ = decide(None, controls=None, action=action)
        assert d.verdict is Verdict.OK and ctx.light is None and repo.last_calls == []
    d, ctx, repo, _ = decide(None, action=Action.OFF)
    assert d.verdict is Verdict.OK and ctx.light is None and repo.last_calls == []


def test_FR22_digest_without_light_unchanged():
    # 인자 없는 요청의 정규화 입력은 이전 정의와 같다 (이전 커밋의 값).
    req = request("nearest:light", action=Action.ON)
    assert input_digest(req) == input_digest(replace(req, light=None))
    expected = __import__("hashlib").sha256(
        b'{"action":"on","parent_internal_request_id":null,'
        b'"target":{"capability":"light","kind":"nearest","node_id":null}}').hexdigest()
    assert input_digest(req) == expected


def test_FR22_same_request_id_other_light_conflicts():
    blue = request("nearest:light", action=Action.ON, light=LightSetting(30, "#0000FF"))
    assert input_digest(blue) != input_digest(replace(blue, light=LightSetting(31, "#0000FF")))
    assert input_digest(blue) != input_digest(replace(blue, light=None))
    # 대소문자만 다른 색은 같은 입력이다.
    assert input_digest(blue) == input_digest(replace(blue, light=LightSetting(30, "#0000ff")))


@pytest.mark.parametrize("value", [True, 1.0, "30", -1, 256])
def test_FR09_light_setting_rejects_bad_brightness(value):
    with pytest.raises(ValueError):
        LightSetting(value, None)
