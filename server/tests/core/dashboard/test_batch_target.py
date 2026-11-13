"""묶음 요청의 상위 요청이 대시보드에 나가는 대상 표기 (FR-09, FR-12B, #284)."""

from datetime import datetime, timezone

import pytest

from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import render_event
from nearpilot.core.dashboard import target_of
from nearpilot.shared.enums import Role, TargetKind
from nearpilot.shared.models import MonitorEvent, Subject


def test_FR09_batch_dashboard_target_kind():
    # 상위 요청의 대상은 종류만 있고 종류 이름과 기기 ID 는 없다
    assert target_of("batch:") == {"kind": "batch"}
    assert set(target_of("batch:")) == {"kind"}

    # 기존 형식의 결과는 그대로다
    assert target_of("nearest:locker") == {"kind": TargetKind.NEAREST, "deviceKind": "locker"}
    assert target_of("any:locker") == {"kind": TargetKind.ANY, "deviceKind": "locker"}
    assert target_of("light-1") == {"kind": TargetKind.EXPLICIT, "nodeId": "light-1"}
    assert target_of("near:light:1:all") == {"kind": TargetKind.NEAR, "deviceKind": "light"}
    assert target_of("far:speaker:2:3") == {"kind": TargetKind.FAR, "deviceKind": "speaker"}

    # 콜론이 없는 batch 는 기기 ID 다. 콜론 뒤에 글자가 있으면 형식 오류다
    assert target_of("batch") == {"kind": TargetKind.EXPLICIT, "nodeId": "batch"}
    for raw in ("batch:x", "batch:light:1:1"):
        with pytest.raises(ValueError):
            target_of(raw)


def test_FR09_batch_dashboard_target_passes_stream_schema():
    # 스트림 형식은 대상 종류를 TargetKind 에서 읽는다. 묶음 상위 요청의 도구 호출이 형식 검사를 통과한다
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = MonitorEvent(
        "mcp_call", at,
        {"requestId": "i1", "tool": "request_use", "target": target_of("batch:"), "action": "batch"},
        Subject("u-1", Role.VISITOR),
    )
    rendered = render_event(event, PersonNames())
    assert rendered["target"] == {"kind": "batch"}
    assert rendered["action"] == "batch"
