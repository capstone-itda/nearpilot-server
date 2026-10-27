"""FR-12A·FR-12B 대시보드 시험의 시나리오 서버: 실제 조립 서버가 흐름을 만들고, 멈춘 뒤의 DB 를 남긴다.

실제 SQLite, `Runtime` 조립, fake Paho 위의 기기 시뮬레이터 위에서 `core.request_use`·`request_access` 를 부른다.
근접 추정만 fake 다 (`FakeProximity`). `DashboardWorld` 는 `with` 안에서 서버가 떠 있고 흐름을 하나씩 만든다
(`decide`, `access`). `build_dashboard_world` 는 다음 여섯 가지 흐름을 만들고 서버를 멈춘다. 이름이 `requests` 의 키다.

- `ok`: 방문자 A 가 locker-1 을 연다. 점유가 끝나지 않고 남는다.
- `busy`: 방문자 B 가 같은 locker-1 을 요청한다. 다른 사람이 점유 중이다.
- `ask`: B 가 locker-2 를 요청하지만 근접 확률이 모자라 다시 묻는다.
- `fault`: B 가 locker-3 을 열지만 기기가 응답하지 않아 결과 불명이 되고 기기를 고장으로 격리한다.
- `light`: B 가 공용 조명 light-1 을 켠다. 유지 대상자로 남는다.
- `access`: A 가 승인이 필요한 light-2 의 접근을 신청한다. light-2 는 보고하지 않는 기기다.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from _runtime import VISITOR_A, VISITOR_B, FakeProximity, SimBroker, make_config, query, running, seed, wait_until
from nearpilot.core.dashboard import DashboardQueryService, ModelVersions
from nearpilot.core.lifecycle.shared_light import SharedHolder
from nearpilot.core.proximity.model import load_models
from nearpilot.db.repositories._common import deferred_transaction
from nearpilot.db.repositories.dashboard_reads import SqliteDashboardReads
from nearpilot.db.schema import connect
from nearpilot.runtime import Runtime
from nearpilot.shared.enums import Action, Verdict
from nearpilot.shared.models import TargetSpec, UseRequest

# 근접 지정 임계값을 0.5 로 낮춘다. 0.97 은 넘고 0.3 은 모자란다. 응답하지 않는 기기는 1 초 뒤 결과 불명이 된다.
# 시나리오가 느린 환경에서도 비콘과 기기가 끊기지 않고 공용 조명의 유지 대상자가 이탈로 꺼지지 않게 시간을 길게 둔다.
SETTINGS = {
    "threshold_locker": 0.5, "threshold_light": 0.5, "exec_timeout_sec": 1.0,
    "beacon_recent_sec": 30.0, "online_ttl_sec": 300.0, "leave_absent_sec": 600.0,
}
REPORTING = ("locker-1", "locker-2", "locker-3", "light-1")  # 상태를 보고하는 기기
WEAK_RSSI_DBM = -85.0


class DashboardWorld:
    """실제 조립 서버 한 대, 기기 시뮬레이터, 서버가 남긴 DB.

    `with` 에 들어가면 서버를 띄우고 모든 기기가 실행할 수 있게 될 때까지 기다린다. 나오면 서버를 멈추고 `finished_at` 과
    `holders` (멈추기 전의 공용 조명 유지 대상자) 를 남긴다. 멈춘 뒤에도 `service` 로 DB 를 읽을 수 있다.
    `proximity.posteriors` 와 `broker` 의 보고로 서버 밖의 사건을 만든다.
    """

    def __init__(self, tmp: Path) -> None:
        self.cfg = make_config(tmp, settings=SETTINGS)
        self.settings = self.cfg.settings
        self.db = self.cfg.database.path
        self.audit_jsonl = self.cfg.database.audit_jsonl  # 서버가 내보낸 감사 JSONL (보존 정리가 읽는다)
        self.reports = tmp / "reports"  # 평가 리포트 폴더. 처음에는 없다
        self.models = {
            place_id: ModelVersions(model.model_version, model.calib_version)
            for place_id, model in load_models(self.cfg.proximity.model_root, self.cfg.proximity.versions).items()
        }
        seed(self.db, modes={"light-2": "restricted"})
        self.broker = SimBroker(tmp, REPORTING)
        self.proximity = FakeProximity({})
        self.runtime = Runtime(self.cfg, client_factory=self.broker.factory,
                               proximity_factory=lambda models, s: self.proximity)
        self.requests: dict[str, str] = {}  # 흐름 이름 → 내부 요청 ID
        self.holders: tuple[SharedHolder, ...] = ()
        self.started_at: datetime | None = None  # 서버를 시작하기 직전
        self.finished_at: datetime | None = None  # 서버를 멈춘 직후
        self.tz: tzinfo | None = None
        self._stack: ExitStack | None = None

    def __enter__(self) -> DashboardWorld:
        with ExitStack() as stack:
            stack.callback(self._finish)  # 맨 나중에: 시뮬레이터를 닫고 끝난 시각을 남긴다
            self.started_at = datetime.now(timezone.utc)
            # 서버를 시작한 때가 낮 열두 시인 시간대다. 이력의 "오늘"이 시험 도중에 바뀌지 않는다
            since_midnight = self.started_at - self.started_at.replace(hour=0, minute=0, second=0, microsecond=0)
            self.tz = timezone(timedelta(hours=12) - since_midnight)
            stack.enter_context(running(self.runtime))
            self._ready()
            self._stack = stack.pop_all()
        return self

    def __exit__(self, *exc_info) -> bool | None:
        self.holders = tuple(self.runtime.services.shared_light.holders())
        return self._stack.__exit__(*exc_info)

    def decide(self, name, user_id, target, posteriors, verdict, action=Action.OPEN):
        """근접 확률을 `posteriors` 로 고정하고 `request_use` 를 부른다. 판정이 `verdict` 가 아니면 `AssertionError`."""
        self.proximity.posteriors = posteriors
        decision = self.runtime.core.request_use(UseRequest(
            user_id, name, TargetSpec.parse(target), action, None, datetime.now(timezone.utc)))
        assert decision.verdict is verdict, f"{name}: {decision.verdict} {decision.reason}"
        self.requests[name] = decision.internal_request_id
        return decision

    def access(self, name, user_id, node_id, action):
        """`request_access` 를 부른다. 승인을 기다리는 판정이 아니면 `AssertionError`."""
        decision = self.runtime.core.request_access(user_id, node_id, action, name)
        assert decision.verdict is Verdict.NEED_APPROVAL, f"{name}: {decision.verdict} {decision.reason}"
        self.requests[name] = decision.internal_request_id
        return decision

    @contextmanager
    def open_reads(self, db: Path | None = None):
        """`db` (기본은 서버의 DB) 에 연결을 열고 읽기 트랜잭션 하나를 연 `SqliteDashboardReads` 를 준다."""
        conn = connect(str(db or self.db))
        try:
            with deferred_transaction(conn):
                yield SqliteDashboardReads(conn)
        finally:
            conn.close()

    def service(
        self, at: datetime, *, db: Path | None = None, reports: Path | None = None,
    ) -> DashboardQueryService:
        """`at` 을 지금으로 보는 조회 서비스. 조회마다 읽기 트랜잭션 하나를 연다. 유지 대상자는 서버를 멈출 때 본 값이다."""
        return DashboardQueryService(
            open_reads=lambda: self.open_reads(db), settings=lambda: self.settings, weak_rssi_dbm=WEAK_RSSI_DBM,
            models=lambda: self.models, mqtt_ready=lambda: True, holders=lambda: self.holders,
            reports_dir=reports or self.reports, clock=lambda: at, started_at=lambda: self.started_at, tz=self.tz,
        )

    def _ready(self) -> None:
        """기기마다 실행할 수 있고, 시뮬레이터가 보낸 RSSI 프레임을 모두 저장할 때까지 기다린다."""
        restore = self.runtime.services.restore
        wait_until(lambda: all(restore.can_execute(node_id) for node_id in self.broker.nodes))
        before = self._frames()
        sent = sum(len(node.scenario.beacons) for node in self.broker.nodes.values())
        self.broker.rssi()
        wait_until(lambda: self._frames() >= before + sent)

    def _frames(self) -> int:
        return query(self.db, "SELECT count(*) c FROM rssi_observation")[0]["c"]

    def _finish(self) -> None:
        self.broker.close()
        self.finished_at = datetime.now(timezone.utc)


def build_dashboard_world(tmp: Path) -> DashboardWorld:
    """서버를 띄워 여섯 가지 흐름을 만들고 멈춘다. 흐름이 기대한 판정이 아니면 `AssertionError` 다."""
    world = DashboardWorld(tmp)
    with world:
        world.decide("ok", VISITOR_A, "nearest:locker", {"locker-1": 0.97}, Verdict.OK)
        world.decide("busy", VISITOR_B, "nearest:locker", {"locker-1": 0.97}, Verdict.BUSY)
        world.decide("ask", VISITOR_B, "nearest:locker", {"locker-2": 0.3}, Verdict.ASK)
        world.broker.drop_commands = {"locker-3"}  # 결과가 오지 않는다
        world.decide("fault", VISITOR_B, "nearest:locker", {"locker-3": 0.97}, Verdict.DENY)
        world.decide("light", VISITOR_B, "nearest:light", {"light-1": 0.9}, Verdict.OK, Action.ON)
        world.access("access", VISITOR_A, "light-2", Action.ON)
        wait_until(world.runtime.services.shared_light.holders)  # 켠 사람이 유지 대상자로 보일 때까지
    return world
