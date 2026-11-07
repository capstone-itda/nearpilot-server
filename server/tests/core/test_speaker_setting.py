"""FR-09·FR-18 ③ 대상 확정 뒤 스피커 `on` 인자 검사 (specs/core-device-actions/spec.md).

스피커는 v2 템플릿 선언을 발표한 기기다. 근접 임계값은 기본이 None 이라 근접 지정은 허가하지 않는다.
판정 테스트는 명시 지정을 쓴다. 다른 계층은 판정 파이프라인 테스트의 fake 를 쓴다.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from test_decision_pipeline import NOW, FakeRepo, build, node, request

from nearpilot.core.dashboard.text import audit_text
from nearpilot.core.decision.digest import input_digest
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import Occupancy, ReasonCode, Step, Verdict
from nearpilot.shared.models import DashboardAudit

CONTROLS = {"volume_max": 20}
SETTINGS = Settings(version="v1")


def speaker(node_id="speaker-1", controls=CONTROLS, kind="speaker"):
    if kind == "speaker":
        descriptor = legacy_descriptor("speaker", Occupancy.SHARED, **(controls or {}))
    else:
        descriptor = legacy_descriptor(kind, Occupancy.SHARED)
    return node(node_id, Occupancy.SHARED, kind, descriptor=descriptor)


class SpeakerRepo(FakeRepo):
    """공용형 기기 하나가 후보다. 같은 동작의 마지막 성공 인자를 돌려준다."""

    def __init__(self, controls=CONTROLS, last=None, node_id="speaker-1", kind="speaker"):
        super().__init__()
        self.nodes_list = [speaker(node_id, controls, kind)]
        self.frames = [replace(self.frames[0], node_id=node_id)]
        self.reserve_use_id = None
        self.last = last
        self.last_calls = []

    def last_success_arguments(self, node_id, action):
        self.last_calls.append((node_id, action))
        return self.last


def decide(arguments=None, *, controls=CONTROLS, last=None, settings=SETTINGS, action="on",
           target="speaker-1", kind="speaker"):
    pipe, _, _, _, audit = build(posteriors={target: 0.99}, settings=settings)
    repo = SpeakerRepo(controls, last, target, kind)
    pipe._repo = repo
    decision, ctx = pipe.decide_for_execution(request(target, action=action, arguments=arguments))
    return decision, ctx, repo, audit


def argument_events(audit):
    return [e.payload["arguments"] for e in audit.events
            if e.step is Step.PROXIMITY and "arguments" in e.payload]


def test_FR09_speaker_volume_goes_to_handoff():
    d, ctx, repo, audit = decide({"volume": 12})
    assert (d.verdict, d.node_id) == (Verdict.OK, "speaker-1")
    assert ctx.arguments == {"volume": 12}
    assert ctx.request.arguments is None  # 방문자 입력은 인계에 남기지 않는다. 정한 값만 `arguments` 에 있다
    assert repo.reserved  # 검사를 통과해야 예약한다
    assert argument_events(audit) == [{"node_id": "speaker-1", "action": "on", "given": {"volume": 12},
                                       "param_max": {"volume": 20}, "resolved": {"volume": 12}}]


def test_FR09_volume_boundary():
    d, ctx, _, _ = decide({"volume": 20})
    assert d.verdict is Verdict.OK and ctx.arguments["volume"] == 20
    d, ctx, repo, audit = decide({"volume": 21})
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE, "speaker-1")
    assert ctx is None and repo.reserved == []  # 명령과 승인 사용분을 만들지 않는다
    assert argument_events(audit) == [{"node_id": "speaker-1", "action": "on", "given": {"volume": 21},
                                       "param_max": {"volume": 20}, "result": "PARAMETER_OUT_OF_RANGE"}]


def test_FR09_volume_cap_is_smaller_of_two():
    small = Settings(version="v1", param_max={"speaker": {"on": {"volume": 10}}},
                     param_default={"speaker": {"on": {"volume": 10}}})
    assert decide({"volume": 10}, settings=small)[0].verdict is Verdict.OK
    assert decide({"volume": 11}, settings=small)[0].reason is ReasonCode.PARAMETER_OUT_OF_RANGE
    device = {"volume_max": 8}
    assert decide({"volume": 8}, controls=device)[0].verdict is Verdict.OK
    assert decide({"volume": 9}, controls=device)[0].reason is ReasonCode.PARAMETER_OUT_OF_RANGE


def test_FR09_speaker_without_controls_denies():
    d, ctx, repo, audit = decide({"volume": 5}, controls=None)
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.INVALID_INPUT, "speaker-1")
    assert ctx is None and repo.reserved == []
    assert argument_events(audit)[-1]["result"] == "INVALID_INPUT"
    # 음량을 주지 않은 요청은 이전처럼 인자 없는 명령을 받는다.
    d, ctx, repo, _ = decide(None, controls=None)
    assert d.verdict is Verdict.OK and ctx.arguments is None and repo.last_calls == []


def test_FR18_unsupported_speaker_does_not_pick_another_device():
    # 음량을 지원하는 두 번째 스피커가 있어도 확정한 대상을 바꾸지 않는다.
    pipe, _, _, pol, _ = build(posteriors={"speaker-1": 0.9, "speaker-2": 0.05},
                               settings=Settings(version="v1", threshold_speaker=0.5))
    repo = SpeakerRepo(controls=None)
    repo.nodes_list.append(speaker("speaker-2"))
    pipe._repo = repo
    d, ctx = pipe.decide_for_execution(request("nearest:speaker", action="on", arguments={"volume": 5}))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.INVALID_INPUT, "speaker-1")
    assert ctx is None and repo.reserved == []
    assert pol.calls == []  # 인자 검사는 ④ 정책 검사 앞에서 멈춘다


def test_FR09_speaker_off_and_other_devices_keep_plain_commands():
    # 음량은 스피커 on 에만 정한다. off 와 다른 기기는 인자를 선언하지 않아 인자 없는 명령이다.
    for kwargs in ({"action": "off"}, {"target": "light-1", "kind": "light"}):
        d, ctx, repo, _ = decide(None, **kwargs)
        assert d.verdict is Verdict.OK and ctx.arguments is None and repo.last_calls == []
        d, ctx, repo, _ = decide({"volume": 5}, **kwargs)
        assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
        assert ctx is None and repo.reserved == []


def test_FR09_volume_omitted_uses_default():
    d, ctx, repo, _ = decide(None)
    assert d.verdict is Verdict.OK and ctx.arguments == {"volume": 20}
    assert repo.last_calls == [("speaker-1", "on")]
    quiet = Settings(version="v1", param_default={"speaker": {"on": {"volume": 5}}})  # 기본값은 설정에서 읽는다
    assert decide(None, settings=quiet)[1].arguments == {"volume": 5}


def test_FR09_volume_omitted_keeps_last_setting():
    assert decide(None, last={"volume": 7})[1].arguments == {"volume": 7}
    assert decide(None, last={"volume": 0})[1].arguments == {"volume": 0}  # 0 을 없는 값으로 보지 않는다


def test_FR09_default_volume_above_device_cap_denies():
    device = {"volume_max": 10}
    d, ctx, repo, _ = decide(None, controls=device)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE)
    assert ctx is None and repo.reserved == []
    # 앞선 값도 지금의 상한을 넘으면 그대로 거부한다. 서버가 낮춰서 실행하지 않는다.
    d, ctx, _, _ = decide(None, controls=device, last={"volume": 15})
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE) and ctx is None


def test_FR09_playing_speaker_accepts_new_volume():
    # 마지막 성공 명령이 on 이어도 새 on 을 허가한다. 공용형은 점유 없이 새 명령이다.
    d, ctx, _, _ = decide({"volume": 5}, last={"volume": 15})
    assert d.verdict is Verdict.OK and ctx.arguments == {"volume": 5}


def test_FR09_zero_volume_is_on_not_off():
    d, ctx, repo, _ = decide({"volume": 0}, last={"volume": 7})
    assert d.verdict is Verdict.OK and ctx.arguments == {"volume": 0}  # 앞선 값이나 기본값으로 바뀌지 않는다
    assert repo.reserved[0].action == "on"  # 서버는 동작을 off 로 바꾸지 않는다
    assert ctx.request.action == "on"


def test_FR09_dashboard_reason_text_is_device_neutral():
    # 모든 기기가 같은 사유 코드를 쓰므로 대시보드 문구는 기능을 가리지 않는다.
    for reason, text in ((ReasonCode.UNSUPPORTED_ACTION, "기기가 선언하지 않은 동작"),
                         (ReasonCode.UNSUPPORTED_PARAMETER, "기기가 받은 인자를 지원하지 않음"),
                         (ReasonCode.PARAMETER_OUT_OF_RANGE, "인자가 기기의 상한을 넘음")):
        row = DashboardAudit(
            seq=1, log_id="log-1", internal_request_id="i1", command_id=None, step=Step.PROXIMITY,
            payload={"verdict": "DENY", "reason": reason.value, "settings_version": "v1"}, created_at=NOW)
        assert audit_text(row, {}) == text


def test_FR22_digest_without_speaker_unchanged():
    # 인자 없는 요청의 정규화 입력은 `arguments` 필드를 더하기 전과 같다 (이전 커밋의 값).
    plain = request("nearest:light", action="on")
    assert input_digest(plain) == input_digest(replace(plain, arguments=None))
    assert input_digest(plain) == sha256(
        b'{"action":"on","parent_internal_request_id":null,'
        b'"target":{"capability":"light","kind":"nearest","node_id":null}}').hexdigest()
    play = request("speaker-1", action="on")
    assert input_digest(play) == sha256(
        b'{"action":"on","parent_internal_request_id":null,'
        b'"target":{"capability":"speaker","kind":"explicit","node_id":"speaker-1"}}').hexdigest()
    # 인자가 있는 요청은 `arguments` 필드가 더해지고, 값이 다르면 digest 가 다르다.
    quiet = replace(play, arguments={"volume": 10})
    assert input_digest(quiet) == sha256(
        b'{"action":"on","arguments":{"volume":10},"parent_internal_request_id":null,'
        b'"target":{"capability":"speaker","kind":"explicit","node_id":"speaker-1"}}').hexdigest()
    assert input_digest(quiet) != input_digest(replace(play, arguments={"volume": 11}))


def test_FR22_same_request_id_other_volume_conflicts():
    quiet = request("speaker-1", action="on", arguments={"volume": 10})
    assert input_digest(quiet) != input_digest(replace(quiet, arguments={"volume": 11}))
    assert input_digest(quiet) != input_digest(replace(quiet, arguments={"volume": 0}))
    assert input_digest(quiet) != input_digest(replace(quiet, arguments=None))
    assert input_digest(quiet) == input_digest(replace(quiet, arguments={"volume": 10}))
