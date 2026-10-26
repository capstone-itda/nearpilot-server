"""FR-12A·FR-12B 대시보드 조회를 실제 조립 서버가 만든 DB 로 확인한다 (specs/core-dashboard-queries/spec.md).

판정, 점유, 실행, 감사는 실제 코드가 남긴 행이고 근접 추정만 fake 다. 서버는 `_dashboard_world.py` 가 모듈마다 한 번 띄워
여섯 가지 흐름 (제어 신호 출력 완료, 점유 중, 다시 질문, 결과 불명 고장, 공용 조명, 접근 신청) 을 만들고 멈춘다. 그 DB 를
`DashboardQueryService` 로 읽고 `api/ws/schema.py` 의 조회 검사에 넣는다.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest

from _dashboard_world import WEAK_RSSI_DBM, build_dashboard_world
from _runtime import TOKENS, VISITOR_A, VISITOR_B, query
from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import RESPONSE_KINDS, render_event, render_response
from nearpilot.core.proximity.files import AcceptanceSettings
from nearpilot.db.repositories.retention import SqliteRetentionStore
from nearpilot.db.schema import connect
from nearpilot.shared.enums import DeviceState, ExecutionStatus, Occupancy, ReasonCode, SessionState, Verdict
from tools.evaluation.proximity import Metrics, metric_rows
from tools.evaluation.report import write_report

FLOWS = ("ok", "busy", "ask", "fault", "light")  # 이력에 오는 흐름. 접근 신청(access)은 request_use 가 아니다
EVALUATION = [
    {"id": 1, "name": "앞 기기 오선택률", "pass": True, "label": "통과", "sample": "100"},
    {"id": 2, "name": "보정 오차", "pass": True, "label": "통과", "sample": "250"},
    {"id": 3, "name": "전환 시간", "pass": None, "label": "미측정", "sample": "—", "sampleNote": "미측정"},
    {"id": 4, "name": "흔들림", "pass": None, "label": "미측정", "sample": "—", "sampleNote": "미측정"},
]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return build_dashboard_world(tmp_path_factory.mktemp("dashboard"))


def write_evaluation(world, reports):
    """평가 도구가 쓰는 그대로의 리포트 (CSV 와 Markdown) 를 `reports` 에 쓴다."""
    reports.mkdir()
    rows = metric_rows(Metrics(0.04, 100, 4, 0.049, 250), AcceptanceSettings(max_misselection=0.05, max_ece=0.05))
    write_report(
        rows, db_path="np.db", schema_version=1, sample_count=250, generated_at=world.finished_at,
        csv_path=str(reports / "lab.csv"), md_path=str(reports / "lab.md"), git_commit="0123abc",
    )


def results(world, service):
    """조회 9종의 결과. 키는 응답 종류다."""
    return {
        "places": service.places(), "uses": service.uses(), "approvals": service.approvals(),
        "audit": service.audit(100), "history": service.history("today", None, None),
        "request": service.request(world.requests["ok"]), "trust": service.trust(),
        "rental": service.rental("locker-1"), "health": service.health(),
    }


def test_FR12A_조회_9종이_형식_검사를_통과한다(world, tmp_path):
    write_evaluation(world, tmp_path / "reports")
    service = world.service(world.finished_at + timedelta(seconds=1), reports=tmp_path / "reports")
    data = results(world, service)
    names = PersonNames()

    assert data.keys() == RESPONSE_KINDS
    for kind, value in data.items():
        assert render_response(kind, value, names) is not None, kind
    for event in service.current_events():
        assert render_event(event, names), event.type
    # 평가 도구가 쓴 리포트에는 confusion 이 없다. 그래도 신뢰도 응답은 형식 검사를 통과한다
    assert data["trust"]["evaluation"] == {"date": world.finished_at.date().isoformat(), "metrics": EVALUATION}


def test_FR12A_평가_리포트가_없으면_신뢰도에_evaluation이_없다(world):
    trust = world.service(world.finished_at + timedelta(seconds=1)).trust()

    assert "evaluation" not in trust
    assert render_response("trust", trust, PersonNames()) is not None


def test_FR11_조회_결과와_이벤트에_사용자_ID와_토큰이_없다(world):
    service = world.service(world.finished_at + timedelta(seconds=1))
    names = PersonNames()
    rendered = [render_response(kind, value, names) for kind, value in results(world, service).items()]
    rendered += [render_event(event, names) for event in service.current_events()]

    text = repr(rendered)
    assert "방문자" in text  # 사람은 이름표로만 나온다
    for secret in (VISITOR_A, VISITOR_B, *TOKENS.values()):
        assert secret not in text


def test_FR12A_실제_흐름의_이력과_요청_한_건(world):
    service = world.service(world.finished_at + timedelta(seconds=1))
    ids = world.requests
    history = service.history("today", None, None)

    # 최근 것부터이고, 접근 신청은 이력에 없다
    assert [row["requestId"] for row in history] == [ids[name] for name in reversed(FLOWS)]
    assert {
        name: (row["verdict"], row["outcome"], row.get("code"))
        for name, row in zip(reversed(FLOWS), history, strict=True)
    } == {
        "ok": (Verdict.OK, "제어 신호 출력 완료", None),
        "busy": (Verdict.BUSY, "실행 안 함", ReasonCode.DEVICE_BUSY),
        "ask": (Verdict.ASK, "다시 질문", ReasonCode.MOVE_CLOSER),
        "fault": (Verdict.OK, "결과 불명 → 고장 격리", ReasonCode.EXEC_UNKNOWN),  # 판정은 OK 였고 실행이 불명이었다
        "light": (Verdict.OK, "제어 신호 출력 완료", None),
    }
    assert [row["requestId"] for row in service.history("today", Verdict.BUSY, None)] == [ids["busy"]]
    assert [row["requestId"] for row in service.history("today", None, "locker-1")] == [ids["busy"], ids["ok"]]

    pass_, stop, skip = "pass", "stop", "skip"
    expected = {  # 단계 일곱 칸의 결과와 실행 상태
        "ok": ([pass_] * 7, ExecutionStatus.SUCCEEDED),
        "busy": ([*[pass_] * 5, stop, skip], None),
        "ask": ([*[pass_] * 3, stop, skip, skip, skip], None),
        "fault": ([pass_] * 7, ExecutionStatus.UNKNOWN),
        "light": ([*[pass_] * 5, skip, skip], ExecutionStatus.SUCCEEDED),  # 공용형은 점유 검사가 없다
    }
    for name, (steps, execution) in expected.items():
        detail = service.request(ids[name])
        assert [step["result"] for step in detail["steps"]] == steps, name
        assert detail.get("execution") == execution, name
        assert detail["id"] == ids[name] and detail["tool"] == "request_use" and "respondedAt" in detail, name
    ok = service.request(ids["ok"])
    assert (ok["posteriors"], ok["threshold"], ok["nodeId"]) == ({"locker-1": 0.97}, 0.5, "locker-1")
    assert service.request(ids["busy"])["reason"] is ReasonCode.DEVICE_BUSY
    assert service.request(ids["ask"])["reason"] is ReasonCode.MOVE_CLOSER
    assert service.request(ids["access"]) is None  # 접근 신청은 request_use 가 아니다
    assert service.request("없는 요청") is None


def test_FR12A_신뢰도는_서버가_시작한_뒤의_판정을_센다(world):
    live = world.service(world.finished_at + timedelta(seconds=1)).trust()["live"]

    assert live["since"] == world.started_at and live["decisions"] == len(FLOWS)
    assert live["counts"] == {
        Verdict.OK: 3, Verdict.BUSY: 1, Verdict.ASK: 1, Verdict.DENY: 0, Verdict.NEED_APPROVAL: 0,
    }
    assert live["doubleOccupancy"] == 0
    # 가장 늦은 응답은 결과를 기다린 요청이다. 나머지는 바로 돌려줬다
    timeout = world.settings.exec_timeout_sec
    assert live["latencyP95Sec"] >= timeout > live["latencyMedianSec"] > 0


def test_FR12A_장소_점유_승인_점검은_실제_행에서_만든다(world):
    service = world.service(world.finished_at + timedelta(seconds=1))

    places = service.places()
    [place] = places["places"]
    assert places["weakRssiDbm"] == WEAK_RSSI_DBM
    assert (place["placeId"], place["placeName"], place["modelVersion"], place["calibrationVersion"]) == (
        "place-a", "lab", "syn-a-1", "none-1")
    assert [device["nodeId"] for device in place["devices"]] == [
        "light-1", "light-2", "locker-1", "locker-2", "locker-3", "speaker-1"]

    uses = service.uses()
    assert [use["occupancy"] for use in uses] == [Occupancy.RENTAL, Occupancy.RENTAL, Occupancy.SHARED]  # 대여형이 먼저
    assert {
        use["nodeId"]: (use["subject"].user_id, use["session"], use.get("sessionNote")) for use in uses
    } == {
        "locker-1": (VISITOR_A, SessionState.ACTIVE, None),
        "locker-3": (VISITOR_B, SessionState.RESERVED, "고장으로 보존"),
        "light-1": (VISITOR_B, SessionState.ACTIVE, None),  # 공용 조명의 유지 대상자
    }
    assert all("overdueMinutes" not in use for use in uses)

    approvals = service.approvals()
    assert approvals["active"] == [] and approvals["ended"] == []
    [waiting] = approvals["pending"]
    assert (waiting["subject"].user_id, waiting["nodeId"], waiting["uses"]) == (VISITOR_A, "light-2", 0)
    assert waiting["remainingMinutes"] == 9  # 대기 기한 10 분에서 몇 초 지났다

    health = service.health()
    assert {device["nodeId"]: device["connected"] for device in health["devices"]} == {
        "light-1": True, "light-2": False, "locker-1": True, "locker-2": True, "locker-3": True, "speaker-1": False,
    }  # 보고하지 않은 기기는 연결되지 않았다
    beacons = {row["subject"].user_id: row for row in health["beacons"]}
    assert beacons["owner"]["lastSeenSec"] is None and beacons["owner"]["note"] == "최근 신호 없음"
    for user_id in (VISITOR_A, VISITOR_B):
        assert 0 <= beacons[user_id]["lastSeenSec"] < world.settings.beacon_recent_sec
        assert "note" not in beacons[user_id]
    assert health["server"] == {"database": True, "mqtt": True, "model": "syn-a-1 · none-1"}


def test_FR12A_시간이_지나면_대여_초과_승인_만료_연결_끊김이_보인다(world):
    service = world.service(world.finished_at + timedelta(hours=2))  # 대여는 한 시간이 기본이다

    uses = {use["nodeId"]: use for use in service.uses()}
    assert 60 <= uses["locker-1"]["overdueMinutes"] < 61 and uses["locker-1"]["sessionNote"] == "대여 시간 초과"
    assert uses["locker-3"]["sessionNote"] == "고장으로 보존" and "overdueMinutes" not in uses["locker-3"]
    assert "overdueMinutes" not in uses["light-1"]

    approvals = service.approvals()
    assert approvals["pending"] == [] and approvals["active"] == []
    [ended] = approvals["ended"]
    assert ended["endReason"] is ReasonCode.APPROVAL_REQUEST_EXPIRED and "remainingMinutes" not in ended

    rental = service.rental("locker-1")
    assert [item["text"] for item in rental["timeline"]] == ["예약 · RESERVED", "제어 신호 출력 완료 · ACTIVE", "대여 시간 초과"]
    assert [item.get("now") for item in rental["timeline"]] == [None, None, True]
    assert 60 <= rental["overdueMinutes"] < 61
    stuck = service.rental("locker-3")  # 결과 불명으로 멈춘 대여
    assert [item["text"] for item in stuck["timeline"]] == ["예약 · RESERVED", "결과 불명 · 고장 격리"]
    assert stuck["overdueMinutes"] == 0
    assert service.rental("locker-2") is None and service.rental("light-1") is None  # 끝나지 않은 대여가 없다

    health = service.health()
    assert not any(device["connected"] for device in health["devices"])
    assert all(row["note"] == "최근 신호 없음" for row in health["beacons"])


def test_FR12B_지금_상태는_기기_비콘_승인이다(world):
    events = world.service(world.finished_at + timedelta(seconds=1)).current_events()

    assert [event.type for event in events] == ["transition"] * 6 + ["beacon"] * 3 + ["approval"]  # 진행 중인 요청은 없다
    transitions = {event.fields["nodeId"]: event.fields for event in events[:6]}
    assert {node_id: fields["to"] for node_id, fields in transitions.items()} == {
        "light-1": DeviceState.AVAILABLE, "light-2": DeviceState.AVAILABLE, "locker-1": DeviceState.ACTIVE,
        "locker-2": DeviceState.AVAILABLE, "locker-3": DeviceState.FAULT, "speaker-1": DeviceState.AVAILABLE,
    }
    assert transitions["locker-3"]["fault"] == {"cause": ReasonCode.EXEC_UNKNOWN}  # 격리 근거는 결과 불명이다
    assert not [node_id for node_id, fields in transitions.items() if node_id != "locker-3" and "fault" in fields]
    assert not [fields for fields in transitions.values() if "overdue" in fields]
    assert {event.subject.user_id: event.fields["seen"] for event in events[6:9]} == {
        "owner": False, VISITOR_A: True, VISITOR_B: True,
    }
    approval = events[9]
    assert approval.fields == {"requestId": world.requests["access"], "nodeId": "light-2", "kind": "requested"}
    assert approval.subject.user_id == VISITOR_A


def test_FR12B_시간이_지난_뒤의_지금_상태(world):
    events = world.service(world.finished_at + timedelta(hours=2)).current_events()

    assert [event.type for event in events] == ["transition"] * 6 + ["beacon"] * 3  # 만료된 승인 대기는 없다
    transitions = {event.fields["nodeId"]: event.fields for event in events[:6]}
    assert [node_id for node_id, fields in transitions.items() if fields.get("overdue")] == ["locker-1"]
    assert not any(event.fields["seen"] for event in events[6:])


def test_FR12A_정리된_행은_조회에_없다(world, tmp_path):
    at = world.finished_at + timedelta(seconds=1)
    cleaned = tmp_path / "cleaned.db"
    with closing(sqlite3.connect(world.db)) as source, closing(sqlite3.connect(cleaned)) as target:
        source.backup(target)
    conn = connect(str(cleaned))
    try:
        # 서버를 멈출 때 내보낸 감사 JSONL 이 있으니 보존기간이 지난 감사 행을 지울 수 있다
        report = SqliteRetentionStore(conn, world.settings, str(world.audit_jsonl)).cleanup(
            world.finished_at + timedelta(days=40))
    finally:
        conn.close()

    before = {row["log_id"] for row in query(world.db, "SELECT log_id FROM audit_log")}
    after = {row["log_id"] for row in query(cleaned, "SELECT log_id FROM audit_log")}
    purged = before - after
    assert purged and len(purged) == report.deleted_audit_rows
    whole, trimmed = world.service(at), world.service(at, db=cleaned)
    assert {row["id"] for row in whole.audit(1000)} == before
    assert {row["id"] for row in trimmed.audit(1000)} == after  # 지운 감사 행은 목록에 없다

    # 감사 행이 남지 않은 요청은 이력과 한 건 조회에서 빠지고 신뢰도 판정 수에서도 빠진다 (요청 행은 남아 있다)
    kept_audit = {row["internal_request_id"] for row in query(cleaned, "SELECT internal_request_id FROM audit_log")}
    kept = {name for name in FLOWS if world.requests[name] in kept_audit}
    gone = set(FLOWS) - kept
    assert kept and gone
    count = "SELECT count(*) c FROM request"
    assert query(cleaned, count)[0]["c"] == query(world.db, count)[0]["c"]
    assert {row["requestId"] for row in trimmed.history("today", None, None)} == {world.requests[name] for name in kept}
    assert trimmed.trust()["live"]["decisions"] == len(kept)
    for name in gone:
        assert trimmed.request(world.requests[name]) is None, name
    for name in kept:
        assert trimmed.request(world.requests[name]) == whole.request(world.requests[name]), name
