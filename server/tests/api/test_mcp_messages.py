"""FR-09, FR-11, FR-14, FR-21 호스트 안내 문장(`api/mcp/messages.py`)."""

from __future__ import annotations

import pytest

from nearpilot.api.mcp.messages import build_message, device_name
from nearpilot.api.mcp.response import host_response
from nearpilot.shared.enums import ReasonCode, Verdict
from nearpilot.shared.models import Decision, DeviceOutcome


def decision(reason: ReasonCode, **kw) -> Decision:
    verdict = Verdict.OK if reason is ReasonCode.OK else Verdict.DENY
    return Decision("internal-1", "r1", verdict, reason, **kw)


# ── FR-14 기기 이름 변환 ──────────────────────────────────────────────


@pytest.mark.parametrize(("node_id", "expected"), [
    ("light-2", "2번 조명"), ("locker-1", "1번 사물함"), ("speaker-3", "3번 스피커"),
    ("display-1", "1번 디스플레이"), ("fan-1", "1번 선풍기"),
])
def test_FR14_아는_kind는_번호와_한국어_이름으로_바꾼다(node_id, expected):
    name, _ = device_name(node_id)
    assert name == expected


def test_FR14_번호가_없으면_node_id_그대로():
    name, _ = device_name("display")  # "-숫자" 형식이 아니다
    assert name == "display"


def test_FR14_모르는_kind는_node_id_그대로():
    name, _ = device_name("box-7")
    assert name == "box-7"


def test_FR14_node_id가_없으면_기기():
    name, _ = device_name(None)
    assert name == "기기"
    name, _ = device_name("")
    assert name == "기기"


# ── FR-09 모든 ReasonCode에 문장이 있다 ──────────────────────────────


def test_FR09_모든_ReasonCode에_문장이_있다():
    for reason in ReasonCode:
        message = build_message(decision(reason, node_id="light-1"), action="on")
        assert isinstance(message, str) and message


# ── 합쇼체 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", list(ReasonCode))
def test_합쇼체로만_끝난다(reason):
    message = build_message(decision(reason, node_id="light-1"), action="on")
    assert not message.rstrip().endswith("요.")
    assert message.rstrip().endswith(("습니다.", "ㅂ니다.", "까?", "다.", "시오."))


# ── 주어진 예시(톤 기준) ──────────────────────────────────────────────


def test_FR09_OK_문장():
    assert build_message(decision(ReasonCode.OK, node_id="light-2"), action="on") == "2번 조명을 켰습니다."


def test_FR09_MOVE_CLOSER_문장():
    message = build_message(decision(ReasonCode.MOVE_CLOSER, node_id="light-2"), action="on")
    assert message == "2번 조명과의 거리가 멀어 실행하지 않았습니다. 2번 조명 가까이 오셔서 다시 요청해 주십시오."


def test_FR09_APPROVAL_REQUIRED_문장():
    message = build_message(decision(ReasonCode.APPROVAL_REQUIRED, node_id="light-2"), action="on")
    assert message == "2번 조명은 소유자 승인이 필요합니다. 승인 요청을 보내시겠습니까?"


def test_FR09_DEVICE_BUSY_문장():
    message = build_message(decision(ReasonCode.DEVICE_BUSY, node_id="light-2"), action="on")
    assert message == "2번 조명은 다른 사용자가 사용 중입니다. 잠시 후 다시 시도해 주십시오."


def test_FR09_PARAMETER_OUT_OF_RANGE_문장():
    message = build_message(decision(ReasonCode.PARAMETER_OUT_OF_RANGE, node_id="fan-1"), action="spin")
    assert message == "요청한 값이 1번 선풍기의 설정 범위를 벗어나 실행하지 않았습니다. 범위 안의 값으로 다시 요청해 주십시오."


def test_FR09_EXEC_FAILED_문장():
    message = build_message(decision(ReasonCode.EXEC_FAILED, node_id="light-2"), action="on")
    assert message == "2번 조명을 켜지 못했습니다. 기기에서 동작이 확인되지 않았습니다. 전원이나 연결 상태를 확인해 주십시오."


def test_FR09_BEACON_NOT_SEEN_문장():
    message = build_message(decision(ReasonCode.BEACON_NOT_SEEN))
    assert message == "주변 기기에서 휴대폰 신호가 확인되지 않습니다. 앱이 켜져 있는지 확인해 주십시오."


def test_FR09_모르는_동작은_일반_표현():
    assert build_message(decision(ReasonCode.OK, node_id="light-2"), action="spin") == \
        "2번 조명에서 요청한 동작을 실행했습니다."


# ── 거리 선택자·묶음 응답의 묶음 문장 ──────────────────────────────────


def test_FR09_거리_선택자_전부_성공():
    d = decision(
        ReasonCode.OK, devices=(
            DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK),
            DeviceOutcome("light-2", Verdict.OK, ReasonCode.OK)),
        selected_count=2, similar=False)
    assert build_message(d, action="off") == "1번, 2번 조명을 껐습니다."


def test_FR09_거리_선택자_일부_실패():
    d = decision(
        ReasonCode.OK, devices=(
            DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK),
            DeviceOutcome("light-2", Verdict.OK, ReasonCode.OK),
            DeviceOutcome("light-3", Verdict.DENY, ReasonCode.DEVICE_BUSY)),
        selected_count=3, similar=False)
    assert build_message(d, action="off") == \
        "1번, 2번 조명을 껐습니다. 3번 조명은 다른 사용자가 사용 중이라 끄지 못했습니다."


def test_FR09_거리_선택자_similar_덧붙인다():
    d = decision(
        ReasonCode.OK, devices=(DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK),),
        selected_count=1, similar=True)
    message = build_message(d, action="off")
    assert message.endswith("비슷한 거리의 기기가 있어 선택 결과가 다를 수 있습니다.")


def test_FR09_묶음_요청은_item으로_동작을_찾는다():
    # near:light:1:all 과 on (item 1), speaker-1 과 off (item 2)
    d = decision(
        ReasonCode.OK, devices=(
            DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK, item=1),
            DeviceOutcome("light-2", Verdict.OK, ReasonCode.OK, item=1),
            DeviceOutcome("speaker-1", Verdict.DENY, ReasonCode.EXEC_FAILED, item=2)),
        selected_count=3, similar=False)
    message = build_message(d, item_actions={1: "on", 2: "off"})
    assert message == "1번, 2번 조명을 켰습니다. 1번 스피커는 끄지 못했습니다."


# ── NFR-08: 금지 항목이 들어가지 않는다 ────────────────────────────────


def test_core가_채운_message는_덮지_않는다():
    d = decision(ReasonCode.APPROVAL_PENDING, node_id="locker-1", message="소유자의 승인을 기다립니다.")
    resp = host_response(d, action="open")
    assert resp["message"] == "소유자의 승인을 기다립니다."


@pytest.mark.parametrize("reason", list(ReasonCode))
def test_NFR08_사용자_ID나_점유_주체_근접_확률이_없다(reason):
    d = decision(reason, node_id="light-1", use_id="use-secret-7", p_max=0.123456)
    message = build_message(d, action="on")
    assert "use-secret-7" not in message and "0.123456" not in message and "internal-1" not in message


_INTERNAL_TERMS = (
    "request_id", "use_id", "node_id", "verdict", "reason_code",
    "request_use", "request_access", "release_use", "get_use_status",
    "find_available_devices", "get_device_state",
)


@pytest.mark.parametrize("reason", list(ReasonCode))
def test_문장에_내부_용어나_도구_이름이_없다(reason):
    message = build_message(decision(reason, node_id="light-1"), action="on")
    for term in _INTERNAL_TERMS:
        assert term not in message, (reason, term)
