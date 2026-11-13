"""묶음 요청의 중복 판단 요약 검사 (FR-22, #284)."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from nearpilot.core.decision.digest import input_digest
from nearpilot.shared.models import BATCH_ACTION, TargetSpec, UseItem, UseRequest

RECEIVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)

# 묶음 요청을 더하기 전 코드에서 얻은 단일 요청과 거리 선택자의 요약이다.
# 메인 checkout(같은 커밋의 바뀌지 않은 코드)의 input_digest 로 계산했다.
BEFORE_BATCH_DIGESTS = (
    ("nearest:light", "on", None, None, "19a1f8ee913b7c0da275b79c92217711f6c5b430a87b1ed8b28e2c9013480d77"),
    ("locker-1", "open", None, None, "c582b00f84ac0e0ced1041e97bea48099961eff487dae055b6f0828f0669b998"),
    ("any:locker", "open", None, "p-1", "8e1fbdf0063745f8bb8f7aac9648cb500f9b1c18160ae05b23ea81478baa0c22"),
    ("nearest:light", "on", {"brightness": 10, "color": "#FF0000"}, None,
     "a7a3d0bb83982295fad75fb00a9cc20a435ea00369f4a28011520f89f4d05be2"),
    ("light-1", "off", {"brightness": 5}, "p-1", "d4121919900adbc40eda2822c9537b3042b5b588de9eedd571a2e039d3c4e6be"),
    ("near:light:2:all", "off", None, None, "7d43c369fa21ae6e0422010a945169024702a98765aa0b342e740e8163ea4947"),
    ("far:speaker:1:3", "on", {"volume": 5}, "p-2", "f808c42983e72ab2961ed14267393b155c1bb37ce9dc6f5362b704b3f56f246e"),
)


def _request(target, action, arguments=None, parent=None):
    return UseRequest("u", "r", TargetSpec.parse(target), action, None, RECEIVED_AT, parent, arguments)


def _item(target, action, arguments=None):
    return UseItem(TargetSpec.parse(target), action, arguments)


def _batch(*items, parent=None, user_id="u", request_id="r", host_kind=None, received_at=RECEIVED_AT):
    return UseRequest(
        user_id, request_id, TargetSpec.parse("batch:"), BATCH_ACTION, host_kind, received_at, parent, None, tuple(items),
    )


def _sha(normalized):
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _target(capability, kind, node_id=None, **extra):
    return {"capability": capability, "kind": kind, "node_id": node_id, **extra}


LIGHTS_ON = ("near:light:1:all", "on", None)
SPEAKER_OFF = ("speaker-1", "off", None)


def test_FR22_batch_digest_depends_on_items_and_order():
    # 단일 요청과 거리 선택자의 요약은 묶음 요청을 더하기 전과 같다
    for target, action, arguments, parent, expected in BEFORE_BATCH_DIGESTS:
        assert input_digest(_request(target, action, arguments, parent)) == expected, (target, action)

    base = input_digest(_batch(_item(*LIGHTS_ON), _item(*SPEAKER_OFF)))
    assert len(base) == 64 and set(base) <= set("0123456789abcdef")

    # 같은 항목은 같은 요약이다. request_id, 계정, 호스트, 받은 시각은 넣지 않는다
    assert base == input_digest(_batch(_item(*LIGHTS_ON), _item(*SPEAKER_OFF)))
    same = _batch(
        _item(*LIGHTS_ON), _item(*SPEAKER_OFF),
        user_id="other", request_id="r-2", host_kind="claude", received_at=RECEIVED_AT + timedelta(minutes=5),
    )
    assert input_digest(same) == base

    # 항목 순서, 대상, 동작, 인자, 항목 수, 원 요청 연결 ID 가 다르면 다른 입력이다
    different = {
        "순서": _batch(_item(*SPEAKER_OFF), _item(*LIGHTS_ON)),
        "앞 항목의 대상": _batch(_item("near:light:1:2", "on"), _item(*SPEAKER_OFF)),
        "앞 항목의 거리 선택자 시작": _batch(_item("near:light:2:all", "on"), _item(*SPEAKER_OFF)),
        "앞 항목의 방향": _batch(_item("far:light:1:all", "on"), _item(*SPEAKER_OFF)),
        "뒤 항목의 대상": _batch(_item(*LIGHTS_ON), _item("speaker-2", "off")),
        "뒤 항목의 지정 방식": _batch(_item(*LIGHTS_ON), _item("nearest:speaker", "off")),
        "앞 항목의 동작": _batch(_item("near:light:1:all", "off"), _item(*SPEAKER_OFF)),
        "뒤 항목의 동작": _batch(_item(*LIGHTS_ON), _item("speaker-1", "on")),
        "앞 항목의 인자": _batch(_item("near:light:1:all", "on", {"brightness": 10}), _item(*SPEAKER_OFF)),
        "항목 하나 더": _batch(_item(*LIGHTS_ON), _item(*SPEAKER_OFF), _item("locker-1", "open")),
        "항목 하나 덜": _batch(_item(*LIGHTS_ON)),
        "항목 하나 덜(뒤 항목만)": _batch(_item(*SPEAKER_OFF)),
        "원 요청 연결 ID": _batch(_item(*LIGHTS_ON), _item(*SPEAKER_OFF), parent="p-1"),
    }
    digests = {name: input_digest(request) for name, request in different.items()}
    assert base not in digests.values()
    assert len(set(digests.values())) == len(digests), "서로 다른 요청의 요약이 겹친다"

    # 인자의 키 순서는 요약에 영향을 주지 않는다. 인자가 있고 없고는 영향을 준다
    ab = input_digest(_batch(_item("near:light:1:all", "on", {"brightness": 10, "color": "#FF0000"})))
    ba = input_digest(_batch(_item("near:light:1:all", "on", {"color": "#FF0000", "brightness": 10})))
    assert ab == ba
    assert ab != input_digest(_batch(_item("near:light:1:all", "on")))


def test_FR22_batch_digest_differs_from_single_request_with_same_content():
    # 같은 request_id 로 단일 요청을 보냈던 키에 묶음 요청이 오면 다른 입력이다 (REQUEST_ID_CONFLICT)
    for target, action, arguments in (
        LIGHTS_ON, SPEAKER_OFF, ("any:locker", "open", None), ("light-1", "on", {"brightness": 10}),
    ):
        single = input_digest(_request(target, action, arguments))
        one_item = input_digest(_batch(_item(target, action, arguments)))
        assert one_item != single, target
    parent = input_digest(_request("near:light:1:all", "on", None, "p-1"))
    assert input_digest(_batch(_item(*LIGHTS_ON), parent="p-1")) != parent


def test_FR22_batch_digest_normalizes_items_like_single_request():
    """항목마다 단일 요청의 target·action·arguments 규칙으로 정규화해 `batch` 에 순서대로 담는다 (spec 결정 4)."""
    request = _batch(
        _item("near:light:2:all", "on", {"brightness": 10}),
        _item("far:speaker:1:3", "off"),
        _item("locker-1", "open"),
        _item("nearest:light", "on"),
        _item("any:locker", "open"),
        parent="p-9",
    )
    expected = _sha({
        "batch": [
            {"action": "on", "target": _target("light", "near", start=2, count="all"), "arguments": {"brightness": 10}},
            {"action": "off", "target": _target("speaker", "far", start=1, count=3)},
            {"action": "open", "target": _target("locker", "explicit", "locker-1")},
            {"action": "on", "target": _target("light", "nearest")},
            {"action": "open", "target": _target("locker", "any")},
        ],
        "parent_internal_request_id": "p-9",
    })
    assert input_digest(request) == expected
