"""FR-12A 대시보드 조회 REST. 인증 → 인자 검사 → 조회 접점 → 형식 검사 순서다. 조회 접점과 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import copy
import json
import logging
import threading
from typing import Any, NamedTuple
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI

from _dashboard import NOW, OWNER, OWNER_TOKEN, VISITOR, VISITOR_TOKEN, FakeAccounts, FakeQueries, FakeSnapshot
from _dashboard import WsClient, authenticator, http_request, run, settings
from _mcp_client import FakeCore
from nearpilot.api.mcp.server import build_app
from nearpilot.api.ws.hub import MonitorHub
from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.router import create_dashboard_router
from nearpilot.api.ws.schema import SchemaError, render_event, render_response
from nearpilot.shared.enums import ReasonCode, Step, Verdict
from nearpilot.shared.models import MonitorEvent

REST_LOGGER = "nearpilot.api.rest.dashboard"
HUB_LOGGER = "nearpilot.api.ws.hub"
OWNER_HEADERS = {"authorization": f"Bearer {OWNER_TOKEN}"}
VISITOR_HEADERS = {"authorization": f"Bearer {VISITOR_TOKEN}"}
PERIOD = urlencode({"period": "오늘"})  # 한글은 퍼센트 인코딩으로 보낸다
ISO = "2026-10-23T14:03:05.120+09:00"  # NOW
UNENCODABLE = "x\ud800"  # 짝 없는 서로게이트. UTF-8 로 바꿀 수 없다


class Route(NamedTuple):
    kind: str  # 응답 kind
    path: str
    query: str  # 인자를 갖춘 질의 문자열
    method: str  # 부르는 접점 메서드
    args: tuple  # 접점에 넘기는 인자
    data: str  # 접점이 돌려주는 FakeQueries 표본 속성
    drop: tuple  # 표본에서 지우면 형식이 틀리는 필수 키의 자리
    bad_path: str  # 그때 형식 검사가 알리는 경로


ROUTES = [
    Route("place", "/dashboard/place", "", "place", (), "place_data", ("placeName",), "place.placeName"),
    Route("uses", "/dashboard/uses", "", "uses", (), "uses_data", (0, "nodeId"), "uses[0].nodeId"),
    Route("approvals", "/dashboard/approvals", "", "approvals", (), "approvals_data", ("pending",),
          "approvals.pending"),
    Route("audit", "/dashboard/audit", "limit=10", "audit", (10,), "audit_data", (0, "id"), "audit[0].id"),
    Route("history", "/dashboard/history", urlencode({"period": "오늘", "verdict": "OK", "nodeId": "locker-1"}),
          "history", ("오늘", Verdict.OK, "locker-1"), "history_data", (0, "requestId"), "history[0].requestId"),
    Route("request", "/dashboard/requests/req-7", "", "request", ("req-7",), "request_data", ("id",), "request.id"),
    Route("trust", "/dashboard/trust", "", "trust", (), "trust_data", ("live",), "trust.live"),
    Route("rental", "/dashboard/rentals/locker-9", "", "rental", ("locker-9",), "rental_data", ("nodeId",),
          "rental.nodeId"),
    Route("health", "/dashboard/health", "", "health", (), "health_data", ("devices",), "health.devices"),
]
MISSING_ROUTES = [route for route in ROUTES if route.kind in ("request", "rental")]  # 없을 수 있는 한 건
OTHER_ROUTES = [route for route in ROUTES if route not in MISSING_ROUTES]
TEXT_AT = {  # 종류 → (표본에서 문자열 값이 있는 자리, 그 값이 틀릴 때 형식 검사가 알리는 경로)
    "place": (("placeName",), "place.placeName"),
    "uses": ((0, "nodeId"), "uses[0].nodeId"),
    "approvals": (("pending", 0, "nodeId"), "approvals.pending[0].nodeId"),
    "audit": ((0, "text"), "audit[0].text"),
    "history": ((0, "outcome"), "history[0].outcome"),
    "request": (("id",), "request.id"),
    "trust": (("evaluation", "date"), "trust.evaluation.date"),
    "rental": (("timeline", 0, "text"), "rental.timeline[0].text"),
    "health": (("server", "model"), "health.server.model"),
}


def kind_of(route: Route) -> str:
    return route.kind


class Rig:
    """스트림과 조회를 한 라우터로 붙인 테스트 앱과 그 fake 들. 바꿀 설정만 넘긴다."""

    def __init__(self, **overrides) -> None:
        config = settings(**overrides)
        self.hub = MonitorHub(PersonNames(), config)
        self.queries = FakeQueries()
        self.accounts = FakeAccounts()
        self.app = FastAPI()
        self.app.include_router(create_dashboard_router(
            hub=self.hub, snapshot=FakeSnapshot(), queries=self.queries,
            authenticator=authenticator(self.accounts), settings=config))

    async def fetch(
        self, path: str, query: str = "", headers: dict[str, str] | None = None, method: str = "GET"
    ) -> tuple[int, Any]:
        """(상태 코드, JSON 본문). headers 를 주지 않으면 소유자 토큰이다. 헤더 없이 보내려면 {} 를 준다."""
        return await http_request(
            self.app, method, path, query=query, headers=OWNER_HEADERS if headers is None else headers)


class AssembledRig(Rig):
    """조립한 서버 앱처럼 대시보드 라우터를 먼저 붙이고 MCP 앱을 루트(`/`)에 마운트한다 (runtime/app.py, architecture.md §2)."""

    def __init__(self, **overrides) -> None:
        super().__init__(**overrides)
        self.app.mount("/", build_app(FakeCore(), authenticator(self.accounts), lambda: NOW))


class HeaderSpy:
    """ASGI 앱을 감싸서 마지막 응답의 헤더(소문자 이름)를 기록한다. http_request 는 헤더를 돌려주지 않는다."""

    def __init__(self, app) -> None:
        self.app = app
        self.headers: dict[str, str] = {}

    async def __call__(self, scope, receive, send) -> None:
        async def spy(message) -> None:
            if message["type"] == "http.response.start":
                self.headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message["headers"]}
            await send(message)

        await self.app(scope, receive, spy)


class ThreadLog(list):
    """append 한 스레드를 함께 쌓는다. fake 의 calls 를 대신해서 접점을 부른 스레드를 알아낸다."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[int] = []

    def append(self, item) -> None:
        self.threads.append(threading.get_ident())
        super().append(item)


JUNK = {"userId": VISITOR.user_id, "user_id": VISITOR.user_id, "ownerUserId": OWNER.user_id, "personId": "p99"}
NUMMAPS = {"posteriors", "counts"}  # 키가 기기 ID 나 판정 값인 맵에는 다른 키를 끼울 수 없다


def with_junk(value: Any) -> Any:
    """모든 객체에 허용 목록 밖의 키를 끼운다. 원래 있던 키가 이긴다."""
    if isinstance(value, dict):
        return {**JUNK, **{key: item if key in NUMMAPS else with_junk(item) for key, item in value.items()}}
    if isinstance(value, list):
        return [with_junk(item) for item in value]
    return value


def without(data: Any, where: tuple) -> Any:
    """data 를 복사해서 where 가 가리키는 키를 지운다."""
    data = copy.deepcopy(data)
    holder = data
    for key in where[:-1]:
        holder = holder[key]
    del holder[where[-1]]
    return data


def with_value(data: Any, where: tuple, value: Any) -> Any:
    """data 를 복사해서 where 가 가리키는 자리를 value 로 바꾼다."""
    data = copy.deepcopy(data)
    holder = data
    for key in where[:-1]:
        holder = holder[key]
    holder[where[-1]] = value
    return data


@pytest.mark.parametrize("route", ROUTES, ids=kind_of)
def test_FR12A_조회_경로_9개가_접점_결과를_형식대로_돌려준다(route):
    rig = Rig()
    status, body = run(rig.fetch(route.path, route.query))
    assert status == 200
    assert body == render_response(route.kind, getattr(rig.queries, route.data), PersonNames())
    [(name, args)] = rig.queries.calls  # 접점을 한 번 불렀다
    assert (name, args) == (route.method, route.args)
    assert [type(arg) for arg in args] == [type(arg) for arg in route.args]  # verdict 는 문자열이 아니라 멤버로 넘긴다


@pytest.mark.parametrize("route", ROUTES, ids=kind_of)
def test_FR11_조회_응답에서_허용_목록_밖의_키와_사용자_ID를_버린다(route):
    rig = Rig()
    clean = getattr(rig.queries, route.data)
    noisy = with_junk(clean)
    setattr(rig.queries, route.data, noisy)
    first = noisy[0] if isinstance(noisy, list) else noisy
    assert JUNK.keys() <= first.keys()  # 접점이 돌려준 결과에 허용 목록 밖의 키가 실제로 있다
    status, body = run(rig.fetch(route.path, route.query))
    assert status == 200
    assert body == render_response(route.kind, clean, PersonNames())  # personId 는 가짜 값이 아니라 이름표의 값이다
    text = json.dumps(body, ensure_ascii=False)
    assert OWNER.user_id not in text and VISITOR.user_id not in text


def test_FR12B_같은_사람은_스트림과_조회에서_같은_이름():
    rig = Rig()

    async def main():
        async with WsClient(rig.app, "/dashboard/stream") as ws:
            await ws.send_json({"type": "auth", "token": OWNER_TOKEN})
            await ws.receive_json()  # 지금 상태. 비었다
            rig.hub.publish(MonitorEvent("beacon", NOW, {"seen": True}, VISITOR))
            [event] = (await ws.receive_json())["events"]
            return event, (await rig.fetch("/dashboard/uses"))[1], (await rig.fetch("/dashboard/health"))[1]

    event, uses, health = run(main())
    assert (event["personId"], event["pseudonym"]) == ("p1", "방문자 A")
    # 표본은 소유자 행이 먼저다. 조회가 따로 이름표를 쓰면 방문자가 p2 가 된다
    use = next(i for i, row in enumerate(rig.queries.uses_data) if row["subject"] == VISITOR)
    beacon = next(i for i, row in enumerate(rig.queries.health_data["beacons"]) if row["subject"] == VISITOR)
    assert uses[use]["personId"] == event["personId"]
    assert health["beacons"][beacon]["personId"] == event["personId"]
    assert health["beacons"][beacon]["label"] == event["pseudonym"]


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no_header"),
        pytest.param({"authorization": "Bearer wrong-token"}, id="wrong_token"),
        pytest.param({"authorization": f"Basic {OWNER_TOKEN}"}, id="basic_scheme"),
        pytest.param({"authorization": ""}, id="empty_header"),
    ],
)
@pytest.mark.parametrize("route", ROUTES, ids=kind_of)
def test_FR12A_토큰이_틀리면_401(route, headers):
    rig = Rig()
    assert run(rig.fetch(route.path, route.query, headers)) == (401, {"error": "INVALID_TOKEN"})
    assert rig.queries.calls == []


@pytest.mark.parametrize("route", ROUTES, ids=kind_of)
def test_FR12A_방문자_토큰은_403(route):
    rig = Rig()
    assert run(rig.fetch(route.path, route.query, VISITOR_HEADERS)) == (403, {"error": "POLICY_DENIED"})
    assert rig.queries.calls == []


@pytest.mark.parametrize("route", ROUTES, ids=kind_of)
def test_FR12A_계정_조회가_실패하면_500(route):
    rig = Rig()
    rig.accounts.error = RuntimeError(f"계정 조회 고장: {VISITOR.user_id}")
    assert run(rig.fetch(route.path, route.query)) == (500, {"error": "INTERNAL_ERROR"})
    assert rig.queries.calls == []


@pytest.mark.parametrize("route", MISSING_ROUTES, ids=kind_of)
def test_FR12A_없는_요청과_대여는_404(route):
    rig = Rig()
    setattr(rig.queries, route.data, None)
    assert run(rig.fetch(route.path, route.query)) == (404, {"error": "USE_NOT_FOUND"})
    assert rig.queries.calls == [(route.method, route.args)]  # 접점에 물어본 뒤의 결과다


@pytest.mark.parametrize("route", OTHER_ROUTES, ids=kind_of)
def test_FR12A_요청과_대여가_아닌_조회가_None이면_500(route):
    rig = Rig()
    setattr(rig.queries, route.data, None)
    assert run(rig.fetch(route.path, route.query)) == (500, {"error": "INTERNAL_ERROR"})  # 없는 것이 아니라 틀린 결과다


@pytest.mark.parametrize("failure", ["raises", "malformed"])
@pytest.mark.parametrize("route", ROUTES, ids=kind_of)
def test_FR12A_조회가_실패하면_500(route, failure, caplog):
    rig = Rig()
    if failure == "raises":
        rig.queries.error = RuntimeError(f"조회 고장: {VISITOR.user_id}")
        logged = "예외=RuntimeError"
    else:
        setattr(rig.queries, route.data, without(getattr(rig.queries, route.data), route.drop))
        logged = f"경로={route.bad_path}"
    assert run(rig.fetch(route.path, route.query)) == (500, {"error": "INTERNAL_ERROR"})
    [record] = [r for r in caplog.records if r.name == REST_LOGGER]
    assert record.levelno == logging.ERROR
    assert f"kind={route.kind} " in record.getMessage() and logged in record.getMessage()  # 종류와 경로나 예외 종류만 남는다
    assert record.exc_info is None  # 트레이스백에는 예외 메시지가 있다
    assert VISITOR.user_id not in caplog.text


@pytest.mark.parametrize("route", ROUTES, ids=kind_of)
def test_FR12A_인코딩할_수_없는_조회_결과는_500(route, caplog):
    rig = Rig()
    where, path = TEXT_AT[route.kind]
    setattr(rig.queries, route.data, with_value(getattr(rig.queries, route.data), where, UNENCODABLE))
    # 형식 검사가 거르지 않으면 JSON 본문을 UTF-8 로 만들다 예외가 앱 밖으로 나가 기본 500 본문이 된다
    assert run(rig.fetch(route.path, route.query)) == (500, {"error": "INTERNAL_ERROR"})
    [record] = [r for r in caplog.records if r.name == REST_LOGGER]
    assert record.levelno == logging.ERROR
    assert f"kind={route.kind} 경로={path}" in record.getMessage()  # 종류와 경로만 남는다
    assert "\ud800" not in caplog.text


@pytest.mark.parametrize(
    ("bad", "path"),
    [
        pytest.param(
            MonitorEvent("step", NOW, {"requestId": "r1", "step": Step.PROXIMITY, "result": "stop",
                                       "detail": UNENCODABLE}),
            "step.detail", id="detail"),
        pytest.param(
            MonitorEvent("audit", NOW, {"row": {"id": "a-1", "at": NOW, "step": Step.EXEC, "text": UNENCODABLE,
                                                "code": ReasonCode.OK}}),
            "audit.row.text", id="audit_text"),
        pytest.param(MonitorEvent("rssi", NOW, {"rssi": {UNENCODABLE: -60}}, VISITOR), "rssi.rssi", id="rssi_key"),
    ],
)
def test_FR12B_인코딩할_수_없는_문자열은_형식_오류다(bad, path, caplog):
    caplog.set_level(logging.WARNING, logger=HUB_LOGGER)
    with pytest.raises(SchemaError) as error:
        render_event(bad, PersonNames())
    assert str(error.value) == path  # 경로만 있다. 값이 없다
    rig = Rig()
    after = MonitorEvent("mcp_result", NOW, {"requestId": "after"})

    async def main():
        async with WsClient(rig.app, "/dashboard/stream") as ws:
            await ws.send_json({"type": "auth", "token": OWNER_TOKEN})
            assert (await ws.receive_json())["events"] == []  # 지금 상태. 비었다
            rig.hub.publish(bad)  # 그 이벤트만 버려진다. 대기열에 들어가면 보내다 연결이 1011 로 닫힌다
            rig.hub.publish(after)
            return await ws.receive_json()

    assert run(main()) == {"v": 1, "events": [{"type": "mcp_result", "at": ISO, "requestId": "after"}]}
    [record] = [r for r in caplog.records if r.name == HUB_LOGGER]
    assert record.levelno == logging.WARNING and f"경로={path}" in record.getMessage()
    assert "\ud800" not in caplog.text


@pytest.mark.parametrize("big", [2**53, -(2**53), 10**5000], ids=["safe_max_plus_1", "safe_min_minus_1", "huge"])
def test_FR12B_범위_밖_정수는_형식_오류다(big):
    for edge in (2**53 - 1, -(2**53 - 1)):  # 경계 안은 받는다
        render_event(MonitorEvent("posterior", NOW, {"requestId": "r1", "posteriors": {}, "threshold": edge}),
                     PersonNames())
    bad = MonitorEvent("posterior", NOW, {"requestId": "r1", "posteriors": {}, "threshold": big})
    with pytest.raises(SchemaError) as error:
        render_event(bad, PersonNames())
    assert str(error.value) == "posterior.threshold"
    rig = Rig()
    after = MonitorEvent("mcp_result", NOW, {"requestId": "after"})

    async def main():
        async with WsClient(rig.app, "/dashboard/stream") as ws:
            await ws.send_json({"type": "auth", "token": OWNER_TOKEN})
            assert (await ws.receive_json())["events"] == []
            rig.hub.publish(bad)  # 그 이벤트만 버려진다. 대기열에 들어가면 보내다 연결이 1011 로 닫힌다
            rig.hub.publish(after)
            return await ws.receive_json()

    assert run(main()) == {"v": 1, "events": [{"type": "mcp_result", "at": ISO, "requestId": "after"}]}
    rig.queries.place_data = {**rig.queries.place_data, "weakRssiDbm": big}
    assert run(rig.fetch("/dashboard/place")) == (500, {"error": "INTERNAL_ERROR"})


@pytest.mark.parametrize(
    ("path", "query"),
    [
        pytest.param("/dashboard/audit", "limit=abc", id="limit_not_number"),
        pytest.param("/dashboard/audit", "limit=0", id="limit_zero"),
        pytest.param("/dashboard/audit", "limit=00", id="limit_only_zeros"),
        pytest.param("/dashboard/audit", "limit=-1", id="limit_negative"),
        pytest.param("/dashboard/audit", "limit=", id="limit_empty"),
        pytest.param("/dashboard/audit", "limit=1.5", id="limit_decimal"),
        pytest.param("/dashboard/audit", "limit=1e2", id="limit_exponent"),
        pytest.param("/dashboard/audit", "limit=0x10", id="limit_hex"),
        pytest.param("/dashboard/audit", "limit=1_0", id="limit_underscore"),
        pytest.param("/dashboard/audit", "limit=%2B5", id="limit_plus_sign"),
        pytest.param("/dashboard/audit", "limit=%205", id="limit_leading_space"),
        pytest.param("/dashboard/audit", "limit=5%20", id="limit_trailing_space"),
        pytest.param("/dashboard/audit", "limit=%EF%BC%95", id="limit_fullwidth_digit"),
        pytest.param("/dashboard/audit", "limit=%C2%B2", id="limit_superscript_digit"),
        pytest.param("/dashboard/history", "", id="period_missing_without_query"),
        pytest.param("/dashboard/history", "verdict=OK&nodeId=locker-1", id="period_missing"),
        pytest.param("/dashboard/history", "period=", id="period_empty"),
        pytest.param("/dashboard/history", "period=%20", id="period_space"),
        pytest.param("/dashboard/history", "period=%09%20", id="period_tab_and_space"),
        pytest.param("/dashboard/history", "period=%E3%80%80", id="period_ideographic_space"),
        pytest.param("/dashboard/history", f"{PERIOD}&verdict=MAYBE", id="verdict_unknown"),
        pytest.param("/dashboard/history", f"{PERIOD}&verdict=ok", id="verdict_lowercase"),
        pytest.param("/dashboard/history", f"{PERIOD}&verdict=OK%20", id="verdict_trailing_space"),
        pytest.param("/dashboard/history", f"{PERIOD}&verdict=", id="verdict_empty"),
    ],
)
def test_FR12A_조회_인자가_틀리면_400(path, query):
    rig = Rig()
    assert run(rig.fetch(path, query)) == (400, {"error": "INVALID_INPUT"})
    assert rig.queries.calls == []


@pytest.mark.parametrize(
    ("headers", "status", "reason"),
    [
        pytest.param({}, 401, "INVALID_TOKEN", id="no_token"),
        pytest.param(VISITOR_HEADERS, 403, "POLICY_DENIED", id="visitor"),
    ],
)
@pytest.mark.parametrize(
    ("path", "query"),
    [pytest.param("/dashboard/audit", "limit=abc", id="audit"),
     pytest.param("/dashboard/history", "verdict=MAYBE", id="history")],
)
def test_FR12A_인증에_실패하면_인자가_틀려도_401_403(path, query, headers, status, reason):
    rig = Rig()
    assert run(rig.fetch(path, query, headers)) == (status, {"error": reason})  # 인증이 먼저다
    assert rig.queries.calls == []


@pytest.mark.parametrize(
    ("query", "limit"),
    [
        pytest.param("limit=500", 50, id="over_max"),
        pytest.param("", 50, id="missing"),
        pytest.param("limit=10", 10, id="under_max"),
        pytest.param("limit=50", 50, id="at_max"),
        pytest.param("limit=51", 50, id="just_over_max"),
        pytest.param("limit=1", 1, id="one"),
        pytest.param("limit=007", 7, id="leading_zeros"),
        pytest.param("limit=0050", 50, id="leading_zeros_at_max"),
        pytest.param("limit=" + "9" * 5000, 50, id="huge_number"),  # int() 가 거부하는 길이
    ],
)
def test_FR12A_감사_조회는_한도로_줄인다(query, limit):
    rig = Rig(audit_limit_max=50)
    status, _ = run(rig.fetch("/dashboard/audit", query))
    assert status == 200
    assert rig.queries.calls == [("audit", (limit,))]


@pytest.mark.parametrize(
    ("query", "args"),
    [
        pytest.param(urlencode({"period": "오늘", "nodeId": "locker-1"}), ("오늘", None, "locker-1"), id="no_verdict"),
        pytest.param(urlencode({"period": "오늘", "verdict": "DENY"}), ("오늘", Verdict.DENY, None), id="no_node"),
        pytest.param(f"{PERIOD}&nodeId=", ("오늘", None, None), id="empty_node"),
        pytest.param(urlencode({"period": " 오늘 "}), (" 오늘 ", None, None), id="period_as_is"),  # 고치지 않고 넘긴다
        pytest.param(f"{PERIOD}&other=1", ("오늘", None, None), id="unknown_param"),
    ],
)
def test_FR12A_이력_조회는_선택_인자가_없으면_None으로_부른다(query, args):
    rig = Rig()
    status, _ = run(rig.fetch("/dashboard/history", query))
    assert status == 200
    assert rig.queries.calls == [("history", args)]
    assert [type(arg) for arg in rig.queries.calls[0][1]] == [type(arg) for arg in args]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("route", ROUTES, ids=kind_of)
def test_FR12A_조회_경로는_GET만_받는다(route, method):
    rig = Rig()
    spy = HeaderSpy(rig.app)
    status, body = run(http_request(spy, method, route.path, query=route.query, headers=OWNER_HEADERS))
    assert (status, body, spy.headers.get("allow")) == (405, {"error": "INVALID_INPUT"}, "GET")
    assert rig.queries.calls == []


@pytest.mark.parametrize("headers", [OWNER_HEADERS, {}], ids=["owner", "no_token"])
def test_FR12A_MCP_앱을_루트에_붙여도_쓰기는_405이고_없는_경로는_404(headers):
    rig = AssembledRig()
    spy = HeaderSpy(rig.app)
    not_get = ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]  # 조회 경로가 받지 않는 메서드
    any_method = ["GET", "HEAD", *not_get]
    query_paths = ["/dashboard/place", "/dashboard/requests/r1"]  # 조회 경로 (경로 인자 자리 포함)
    missing_paths = ["/dashboard/nope", "/dashboard/place/extra", "/dashboard/requests",
                     "/dashboard/requests/r1/extra", "/dashboard/"]
    refused = (405, {"error": "INVALID_INPUT"}, "GET")  # (상태, 본문, Allow 헤더)
    not_found = (404, {"error": "USE_NOT_FOUND"}, None)
    invalid_token = (401, {"error": "INVALID_TOKEN"}, None)  # MCP 앱의 미들웨어가 낸다
    place = (200, render_response("place", rig.queries.place_data, PersonNames()), None)

    async def call(method: str, path: str, headers: dict[str, str] = headers) -> tuple[int, Any, str | None]:
        status, body = await http_request(spy, method, path, headers=headers)
        return status, body, spy.headers.get("allow")

    async def main():
        seen = {(method, path): await call(method, path) for path in query_paths for method in not_get}
        seen |= {(method, path): await call(method, path) for path in missing_paths for method in any_method}
        seen["GET", "/dashboard/place"] = await call("GET", "/dashboard/place")  # 조회는 그대로 된다
        # 받이 경로는 `/dashboard/` 아래만 받는다. 그 밖의 요청은 MCP 앱으로 간다
        seen["GET", "/dashboard"] = await call("GET", "/dashboard", {})
        seen["POST", "/mcp"] = await call("POST", "/mcp", {})
        async with WsClient(rig.app, "/dashboard/stream") as ws:  # 스트림 연결은 받이 경로에 가로채이지 않는다
            await ws.send_json({"type": "auth", "token": OWNER_TOKEN})
            return seen, await ws.receive_json()

    seen, first = run(main())
    expected = {(method, path): refused for path in query_paths for method in not_get}  # 토큰이 없어도 같다
    expected |= {(method, path): not_found for path in missing_paths for method in any_method}
    expected["GET", "/dashboard/place"] = place if headers else invalid_token
    expected["GET", "/dashboard"] = expected["POST", "/mcp"] = invalid_token
    assert seen == expected
    assert first == {"v": 1, "events": []}
    assert rig.queries.calls == ([("place", ())] if headers else [])  # 조회 접점은 GET 조회 하나만 불렀다


def test_FR12A_동기_접점은_이벤트_루프_밖의_스레드에서_부른다():
    rig = Rig()
    rig.queries.calls, rig.accounts.calls = ThreadLog(), ThreadLog()

    async def main():
        for route in ROUTES:
            assert (await rig.fetch(route.path, route.query))[0] == 200
        return threading.get_ident()

    loop_thread = run(main())
    assert len(rig.queries.calls.threads) == len(rig.accounts.calls.threads) == len(ROUTES)
    assert loop_thread not in rig.queries.calls.threads + rig.accounts.calls.threads
