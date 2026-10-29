"""FR-09·FR-18 ③ 대상 확정 뒤 스피커 음량 검사. 다른 계층은 판정 파이프라인 테스트의 fake 를 쓴다.

스피커의 근접 임계값은 기본이 None 이라 근접 지정은 허가하지 않는다. 판정 테스트는 명시 지정을 쓴다.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from test_decision_pipeline import NOW, FakeRepo, build, node, request

from nearpilot.core.dashboard.text import audit_text
from nearpilot.core.decision.digest import input_digest
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, Capability, Occupancy, ReasonCode, Step, Verdict
from nearpilot.shared.models import DashboardAudit, LightSetting, SpeakerControls, SpeakerSetting

CONTROLS = SpeakerControls(volume_max=20)
SETTINGS = Settings(version="v1")


class SpeakerRepo(FakeRepo):
    """공용형 기기 하나가 후보다. 마지막 스피커 음량을 돌려준다."""

    def __init__(self, controls=CONTROLS, last=None, node_id="speaker-1", capability=Capability.SPEAKER):
        super().__init__()
        self.nodes_list = [node(node_id, Occupancy.SHARED, capability, speaker_controls=controls)]
        self.frames = [replace(self.frames[0], node_id=node_id)]
        self.reserve_use_id = None
        self.last = last
        self.last_calls = []

    def last_speaker_setting(self, node_id):
        self.last_calls.append(node_id)
        return self.last


def decide(speaker=None, *, controls=CONTROLS, last=None, settings=SETTINGS, action=Action.ON,
           target="speaker-1", capability=Capability.SPEAKER):
    pipe, _, _, _, audit = build(posteriors={target: 0.99}, settings=settings)
    repo = SpeakerRepo(controls, last, target, capability)
    pipe._repo = repo
    decision, ctx = pipe.decide_for_execution(request(target, action=action, speaker=speaker))
    return decision, ctx, repo, audit


def speaker_events(audit):
    return [e.payload["speaker"] for e in audit.events if e.step is Step.PROXIMITY and "speaker" in e.payload]


def test_FR09_speaker_volume_goes_to_handoff():
    d, ctx, repo, audit = decide(SpeakerSetting(12))
    assert (d.verdict, d.node_id) == (Verdict.OK, "speaker-1")
    assert ctx.speaker == SpeakerSetting(12)
    assert ctx.request.speaker is None  # 방문자 입력은 인계에 남기지 않는다. 정한 값만 `speaker` 에 있다
    assert ctx.light is None
    assert repo.reserved  # 검사를 통과해야 예약한다
    assert speaker_events(audit) == [{"node_id": "speaker-1", "given": {"volume": 12}, "last": None,
                                      "volume_max": 20, "resolved": {"volume": 12}}]


def test_FR09_volume_boundary():
    d, ctx, _, _ = decide(SpeakerSetting(20))
    assert d.verdict is Verdict.OK and ctx.speaker.volume == 20
    d, ctx, repo, audit = decide(SpeakerSetting(21))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE, "speaker-1")
    assert ctx is None and repo.reserved == []  # 명령과 승인 사용분을 만들지 않는다
    assert speaker_events(audit) == [{"node_id": "speaker-1", "given": {"volume": 21}, "last": None,
                                      "volume_max": 20, "resolved": {"volume": 21},
                                      "result": "PARAMETER_OUT_OF_RANGE"}]


def test_FR09_volume_cap_is_smaller_of_two():
    small = Settings(version="v1", speaker_volume_max=10, speaker_default_volume=10)
    assert decide(SpeakerSetting(10), settings=small)[0].verdict is Verdict.OK
    assert decide(SpeakerSetting(11), settings=small)[0].reason is ReasonCode.PARAMETER_OUT_OF_RANGE
    device = SpeakerControls(8)
    assert decide(SpeakerSetting(8), controls=device)[0].verdict is Verdict.OK
    assert decide(SpeakerSetting(9), controls=device)[0].reason is ReasonCode.PARAMETER_OUT_OF_RANGE


def test_FR09_speaker_without_controls_denies():
    d, ctx, repo, audit = decide(SpeakerSetting(5), controls=None)
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.UNSUPPORTED_PARAMETER, "speaker-1")
    assert ctx is None and repo.reserved == []
    assert speaker_events(audit) == [{"controls": None, "result": "UNSUPPORTED_PARAMETER"}]
    # 음량을 주지 않은 요청은 이전처럼 인자 없는 명령을 받는다.
    d, ctx, repo, _ = decide(None, controls=None)
    assert d.verdict is Verdict.OK and ctx.speaker is None and repo.last_calls == []


def test_FR18_unsupported_speaker_does_not_pick_another_device():
    # 음량을 지원하는 두 번째 스피커가 있어도 확정한 대상을 바꾸지 않는다.
    pipe, _, _, pol, _ = build(posteriors={"speaker-1": 0.9, "speaker-2": 0.05},
                               settings=Settings(version="v1", threshold_speaker=0.5))
    repo = SpeakerRepo(controls=None)
    repo.nodes_list.append(node("speaker-2", Occupancy.SHARED, Capability.SPEAKER, speaker_controls=CONTROLS))
    pipe._repo = repo
    d, ctx = pipe.decide_for_execution(request("nearest:speaker", action=Action.ON, speaker=SpeakerSetting(5)))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.UNSUPPORTED_PARAMETER, "speaker-1")
    assert ctx is None and repo.reserved == []
    assert pol.calls == []  # 음량 검사는 ④ 정책 검사 앞에서 멈춘다


def test_FR09_speaker_off_and_other_devices_keep_plain_commands():
    # 음량은 스피커 on 에만 정한다. off 와 스피커가 아닌 기기는 제어 정보가 있어도 인자 없는 명령이다.
    other = {"target": "light-1", "capability": Capability.LIGHT}
    for kwargs in ({"action": Action.OFF}, other):
        d, ctx, repo, _ = decide(None, **kwargs)
        assert d.verdict is Verdict.OK and ctx.speaker is None and repo.last_calls == []
        d, ctx, repo, _ = decide(SpeakerSetting(5), **kwargs)
        assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.UNSUPPORTED_PARAMETER)
        assert ctx is None and repo.reserved == []


def test_FR09_volume_omitted_uses_default():
    d, ctx, repo, _ = decide(None)
    assert d.verdict is Verdict.OK and ctx.speaker == SpeakerSetting(20)
    assert repo.last_calls == ["speaker-1"]
    quiet = Settings(version="v1", speaker_default_volume=5)  # 기본값은 설정에서 읽는다
    assert decide(None, settings=quiet)[1].speaker == SpeakerSetting(5)


def test_FR09_volume_omitted_keeps_last_setting():
    assert decide(None, last=SpeakerSetting(7))[1].speaker == SpeakerSetting(7)
    assert decide(None, last=SpeakerSetting(0))[1].speaker == SpeakerSetting(0)  # 0 을 없는 값으로 보지 않는다


def test_FR09_default_volume_above_device_cap_denies():
    device = SpeakerControls(10)
    d, ctx, repo, _ = decide(None, controls=device)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE)
    assert ctx is None and repo.reserved == []
    # 앞선 값도 지금의 상한을 넘으면 그대로 거부한다. 서버가 낮춰서 실행하지 않는다.
    d, ctx, _, _ = decide(None, controls=device, last=SpeakerSetting(15))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.PARAMETER_OUT_OF_RANGE) and ctx is None


def test_FR09_playing_speaker_accepts_new_volume():
    # 마지막 성공 명령이 on 이어도 새 on 을 허가한다. 공용형은 점유 없이 새 명령이다.
    d, ctx, _, _ = decide(SpeakerSetting(5), last=SpeakerSetting(15))
    assert d.verdict is Verdict.OK and ctx.speaker == SpeakerSetting(5)


def test_FR09_zero_volume_is_on_not_off():
    d, ctx, repo, _ = decide(SpeakerSetting(0), last=SpeakerSetting(7))
    assert d.verdict is Verdict.OK and ctx.speaker == SpeakerSetting(0)  # 앞선 값이나 기본값으로 바뀌지 않는다
    assert repo.reserved[0].action is Action.ON  # 서버는 동작을 off 로 바꾸지 않는다
    assert ctx.request.action is Action.ON


def test_FR09_dashboard_reason_text_is_device_neutral():
    # 조명과 스피커가 같은 사유 코드를 쓰므로 대시보드 문구는 기능을 가리지 않는다.
    for reason, text in ((ReasonCode.UNSUPPORTED_PARAMETER, "기기가 받은 인자를 지원하지 않음"),
                         (ReasonCode.PARAMETER_OUT_OF_RANGE, "인자가 기기의 상한을 넘음")):
        row = DashboardAudit(
            seq=1, log_id="log-1", internal_request_id="i1", command_id=None, step=Step.PROXIMITY,
            payload={"verdict": "DENY", "reason": reason.value, "settings_version": "v1"}, created_at=NOW)
        assert audit_text(row, {}) == text


def test_FR22_digest_without_speaker_unchanged():
    # 인자 없는 요청과 조명 요청의 정규화 입력은 `speaker` 필드를 더하기 전과 같다 (이전 커밋의 값).
    plain = request("nearest:light", action=Action.ON)
    assert input_digest(plain) == input_digest(replace(plain, speaker=None))
    assert input_digest(plain) == sha256(
        b'{"action":"on","parent_internal_request_id":null,'
        b'"target":{"capability":"light","kind":"nearest","node_id":null}}').hexdigest()
    blue = replace(plain, light=LightSetting(30, "#0000FF"))
    assert input_digest(blue) == sha256(
        b'{"action":"on","light":{"brightness":30,"color":"#0000FF"},"parent_internal_request_id":null,'
        b'"target":{"capability":"light","kind":"nearest","node_id":null}}').hexdigest()
    play = request("speaker-1", action=Action.ON)
    assert input_digest(play) == sha256(
        b'{"action":"on","parent_internal_request_id":null,'
        b'"target":{"capability":"speaker","kind":"explicit","node_id":"speaker-1"}}').hexdigest()
    # 음량이 있는 요청은 `speaker` 필드가 더해지고, 음량이 다르면 digest 가 다르다.
    quiet = replace(play, speaker=SpeakerSetting(10))
    assert input_digest(quiet) == sha256(
        b'{"action":"on","parent_internal_request_id":null,"speaker":{"volume":10},'
        b'"target":{"capability":"speaker","kind":"explicit","node_id":"speaker-1"}}').hexdigest()
    assert input_digest(quiet) != input_digest(replace(play, speaker=SpeakerSetting(11)))


def test_FR22_same_request_id_other_volume_conflicts():
    quiet = request("speaker-1", action=Action.ON, speaker=SpeakerSetting(10))
    assert input_digest(quiet) != input_digest(replace(quiet, speaker=SpeakerSetting(11)))
    assert input_digest(quiet) != input_digest(replace(quiet, speaker=SpeakerSetting(0)))
    assert input_digest(quiet) != input_digest(replace(quiet, speaker=None))
    assert input_digest(quiet) == input_digest(replace(quiet, speaker=SpeakerSetting(10)))
