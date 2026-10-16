"""서버 서비스 조립과 시작·종료 순서 (specs/core-server-assembly/spec.md).

조립만 각 계층의 실제 구현을 만든다. 서비스끼리는 `shared` Protocol 모양의 객체로 연결한다.
판정 결과를 바꾸지 않는다 (NFR-07). MQTT 실행 명령은 CORE 실행 서비스만 보낸다 (FR-06).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import paho.mqtt.client as mqtt

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.core.allocation.window import AllocationWindow
from nearpilot.core.decision.core_service import build_core_service
from nearpilot.core.decision.recheck import Rechecker
from nearpilot.core.lifecycle.execution import ExecutionService
from nearpilot.core.lifecycle.fault_recovery import FaultRecoveryService
from nearpilot.core.lifecycle.node_events import NodeEventService
from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.core.lifecycle.restore import RestoreService
from nearpilot.core.lifecycle.shared_light import SharedLightService
from nearpilot.core.policy.checker import RepositoryPolicyChecker
from nearpilot.core.proximity.estimator import HmmProximityEstimator
from nearpilot.core.proximity.model import ProximityModel, load_models
from nearpilot.iot.mqtt import ConnectionFailure, MqttCommandGateway, MqttConnection, ReportDelivery
from nearpilot.runtime.config import ServerConfig
from nearpilot.runtime.database import ThreadDatabase
from nearpilot.runtime.tasks import CallGate, Late, NodeLocks, PeriodicTask
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, ExecutionStatus
from nearpilot.shared.interfaces import CoreService, ProximityEstimator, RetentionStore
from nearpilot.shared.models import Decision, ExecutionContext, ExecutionResult, UseRequest

log = logging.getLogger(__name__)

ProximityFactory = Callable[[Mapping[str, ProximityModel], Settings], ProximityEstimator]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ShutdownError(RuntimeError):
    """종료 제한 안에 끝내지 못한 작업이 있다. 미완료 기록은 다음 복원에서 다시 확인한다."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("종료를 끝내지 못했다: " + ", ".join(problems))
        self.problems = tuple(problems)


@dataclass
class Services:
    """조립한 객체. 진단과 테스트가 읽는다. 다른 계층에 넘기지 않는다."""

    connection: MqttConnection
    delivery: ReportDelivery
    gateway: MqttCommandGateway
    proximity: ProximityEstimator
    window: AllocationWindow
    execution: ExecutionService
    release: ReleaseService
    restore: RestoreService
    recovery: FaultRecoveryService
    shared_light: SharedLightService
    node_events: NodeEventService
    retention: RetentionStore
    core: CoreService


class GuardedCore:
    """MCP 에 넘기는 `CoreService`. 접수 문을 지나 연결 범위 하나에서 CORE 를 부른다."""

    def __init__(self, gate: CallGate, db: ThreadDatabase) -> None:
        self._gate, self._db = gate, db
        self._inner: CoreService | None = None

    def bind(self, core: CoreService) -> None:
        if self._inner is not None:
            raise RuntimeError("CORE 서비스를 이미 연결했다")
        self._inner = core

    def _call(self, name: str, *args: Any) -> Any:
        with self._gate.enter(), self._db.scope():
            if self._inner is None:
                raise RuntimeError("CORE 서비스를 연결하지 않았다")
            return getattr(self._inner, name)(*args)

    def find_available_devices(self, user_id: str):
        return self._call("find_available_devices", user_id)

    def get_device_state(self, user_id: str, node_id: str):
        return self._call("get_device_state", user_id, node_id)

    def request_access(self, user_id: str, node_id: str, action: Action, request_id: str) -> Decision:
        return self._call("request_access", user_id, node_id, action, request_id)

    def request_use(self, req: UseRequest) -> Decision:
        return self._call("request_use", req)

    def release_use(self, user_id: str, use_id: str) -> Decision:
        return self._call("release_use", user_id, use_id)

    def get_use_status(self, user_id: str, use_id: str):
        return self._call("get_use_status", user_id, use_id)


class _ScopedSink:
    """IOT 인계 작업이 부르는 `NodeEventSink`. 보고 하나마다 연결 범위 하나다."""

    def __init__(self, db: ThreadDatabase) -> None:
        self._db = db
        self._inner = Late("node_event_sink")

    def bind(self, sink: Any) -> None:
        self._inner.bind(lambda name, report: getattr(sink, name)(report))

    @property
    def bound(self) -> bool:
        return self._inner.bound

    def _call(self, name: str, report: Any) -> None:
        with self._db.scope():
            self._inner(name, report)

    def on_announce(self, announce) -> None:
        self._call("on_announce", announce)

    def on_rssi(self, frame) -> None:
        self._call("on_rssi", frame)

    def on_status(self, status) -> None:
        self._call("on_status", status)

    def on_result(self, result) -> None:
        self._call("on_result", result)

    def on_safety_check_result(self, result) -> None:
        self._call("on_safety_check_result", result)


class _SharedLightSuccess:
    """사용자 요청의 실행 결과 중 저장을 확인한 공용 조명 성공을 유지 대상자 접점에 넘긴다.

    결과를 바꾸지 않는다. 인계 실패는 기록만 하고 호스트 응답은 실행 결과를 따른다.
    """

    def __init__(self, execution: ExecutionService, shared_light: SharedLightService) -> None:
        self._execution = execution
        self._shared_light = shared_light

    def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        result = self._execution.execute(ctx)
        if result.status is ExecutionStatus.SUCCEEDED:
            try:
                self._shared_light.on_execution_result(ctx, result)
            except Exception:  # noqa: BLE001
                log.exception("공용 조명 성공 인계 실패: %s", ctx.node_id)
        return result


class Runtime:
    """서버 한 번의 시작과 종료. 다시 시작하려면 새 객체를 만든다.

    `collect_only` 는 수집 전용이다. 근접 모델을 읽지 않고, `create_app` 이 MCP 를 열지 않는다.
    """

    def __init__(
        self, config: ServerConfig, *, clock: Callable[[], datetime] = utc_now,
        client_factory: Callable[..., Any] = mqtt.Client,
        proximity_factory: ProximityFactory = HmmProximityEstimator,
        monotonic: Callable[[], float] = time.monotonic,
        collect_only: bool = False,
    ) -> None:
        self.config = config
        self.settings = config.settings
        self._clock = clock
        self._client_factory = client_factory
        self._proximity_factory = proximity_factory
        self._monotonic = monotonic
        self._collect_only = collect_only
        self.gate = CallGate()
        self.db = ThreadDatabase(
            config.database.path, settings=config.settings, audit_jsonl=config.database.audit_jsonl,
            busy_timeout_ms=config.runtime.sqlite_busy_timeout_ms)
        self.core = GuardedCore(self.gate, self.db)
        self.authenticator = Authenticator(self.db.accounts())
        self.services: Services | None = None
        self.tasks: list[PeriodicTask] = []
        self._started: list[str] = []
        self._state = "new"

    @property
    def clock(self) -> Callable[[], datetime]:
        return self._clock

    @property
    def collect_only(self) -> bool:
        return self._collect_only

    # ── 시작 ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._state != "new":
            raise RuntimeError("Runtime 은 한 번만 시작한다")
        self._state = "starting"
        try:
            self._start()
        except BaseException:
            log.error("서버 시작 실패. 요청을 받지 않고 획득한 자원을 정리한다")
            problems = self._shutdown(self._monotonic() + self.config.runtime.shutdown_timeout_sec)
            if problems:
                log.error("시작 실패 정리를 끝내지 못했다: %s", ", ".join(problems))
            self._state = "failed"
            raise
        self._state = "running"
        log.info("서버 시작 완료. 요청을 받는다 (settings %s)", self.settings.version)

    def _start(self) -> None:
        cfg, s = self.config, self.settings
        # 1. 모델 — 설정은 load_config 가 이미 검증했다. 수집 전용이면 읽지 않고 빈 모델로 만든다
        if self._collect_only:
            log.info("수집 전용 모드: 근접 모델을 읽지 않는다. MCP 를 열지 않는다")
            models: dict[str, ProximityModel] = {}
        else:
            models = load_models(cfg.proximity.model_root, cfg.proximity.versions)
        for place_id, model in models.items():
            log.info("근접 모델: place=%s model=%s calib=%s", place_id, model.model_version,
                     model.calib_version)
        proximity = self._proximity_factory(models, s)
        # 2. DB — 연결이 스키마를 최신으로 맞춘다
        with self.db.scope():
            pass
        self._started.append("db")
        self.db.export_jsonl()
        # 3. 근접 추정 복원
        self._restore_proximity(proximity, models)
        # 4. 서비스와 콜백
        svc = self.services = self._build(proximity)
        # 5. MQTT 구독. SUBACK 전후의 보고는 보고 작업 시작 전까지 수신 큐에 남는다
        self._started.append("mqtt")
        svc.connection.start()
        # 6. 최초 복원 호출
        with self.db.scope():
            svc.restore.restore()
        # 7. 시작 정리
        with self.db.scope():
            svc.retention.cleanup(self._clock())
        # 8. 보고 검증·인계 작업과 주기 작업
        self._started.append("delivery")
        svc.delivery.start()
        for task in self.tasks:
            task.start()
        # 9. 요청 접수
        self.gate.open()

    def _restore_proximity(self, proximity: ProximityEstimator, models: Mapping[str, ProximityModel]) -> None:
        restore = getattr(proximity, "restore", None)
        if restore is None or not models:
            return
        window = max(model.window_sec for model in models.values()) + self.settings.rssi_frame_sec
        since = self._clock() - timedelta(seconds=window)
        with self.db.scope() as bound:
            beacons = self.db.active_beacons()
            frames = bound.repo.rssi_since(beacons, since) if beacons else []
            nodes = bound.repo.nodes()
        restore(frames, nodes)
        log.info("근접 추정 복원: 관측 %d건", len(frames))

    def _build(self, proximity: ProximityEstimator) -> Services:
        cfg, s, rt, clock, db = self.config, self.settings, self.config.runtime, self._clock, self.db
        repo, store, audit = db.repository(), db.execution_store(), db.audit_log()
        retention = db.retention_store()
        policy = RepositoryPolicyChecker(repo)
        locks = NodeLocks()

        def settings() -> Settings:
            return s

        # IOT. 연결 상실과 CORE 인계는 서비스를 만든 뒤 연결한다.
        sink = _ScopedSink(db)
        delivery = ReportDelivery(sink, settings=settings, config=cfg.delivery, clock=clock)
        lost = Late("mqtt_connection_lost")
        connection = MqttConnection(cfg.mqtt, on_report=delivery.submit, on_connection_lost=lost,
                                    client_factory=self._client_factory, clock=clock)
        gateway = MqttCommandGateway(connection, delivery, clock=clock)
        lost.bind(gateway.on_connection_lost)

        # 상태 관리. 모든 서비스가 같은 잠금과 복구 서비스를 쓴다 (NFR-04).
        recovery = FaultRecoveryService(repository=repo, store=store, clock=clock, lock_for=locks,
                                        gateway=gateway, settings=settings)
        release = ReleaseService(repository=repo, store=store, gateway=gateway, settings=settings,
                                 clock=clock, lock_for=locks, record_fault=recovery.record_fault)
        execute_off = Late("shared_light_execute")
        shared_light = SharedLightService(repository=repo, policy=policy, settings=settings,
                                          clock=clock, lock_for=locks, execute=execute_off)
        restore = RestoreService(repository=repo, store=store, release=release, recovery=recovery,
                                 shared_light=shared_light, settings=settings, clock=clock,
                                 lock_for=locks)
        rechecker = Rechecker(repo, proximity, policy, audit, clock)
        execution = ExecutionService(
            repository=repo, store=store, rechecker=rechecker, gateway=gateway, clock=clock,
            lock_for=locks, can_execute=restore.can_execute,
            record_isolated=recovery.record_isolated,
            confirm_internal=lambda ctx: shared_light.can_request_off(ctx.node_id))
        execute_off.bind(execution.execute)

        # 판정. 할당 대기창은 프로세스에서 하나다 (FR-23).
        window = AllocationWindow(s.allocation_window_ms)
        core = build_core_service(
            repo=repo, proximity=proximity, policy=policy, audit=audit, settings=s, clock=clock,
            readiness=restore, execution=_SharedLightSuccess(execution, shared_light),
            store=store, release=release, window=window)
        node_events = NodeEventService(
            repository=repo, store=store, proximity=proximity, execution=execution,
            release=release, restore=restore, recovery=recovery, shared_light=shared_light,
            context_for=store.execution_context, lock_for=locks)
        sink.bind(node_events)
        self.core.bind(core)
        if not (lost.bound and execute_off.bound and sink.bound):
            raise RuntimeError("연결하지 않은 콜백이 있다")

        self.tasks = [
            PeriodicTask("execution", rt.execution_tick_sec, execution.tick, scope=db.scope),
            PeriodicTask("release", rt.release_tick_sec, release.tick, scope=db.scope),
            PeriodicTask("restore", rt.restore_tick_sec, restore.tick, scope=db.scope),
            PeriodicTask("recovery", rt.recovery_tick_sec, recovery.tick, scope=db.scope),
            PeriodicTask("shared_light", rt.shared_light_tick_sec, shared_light.tick, scope=db.scope),
            PeriodicTask("retention", s.retention_cleanup_interval_sec,
                         lambda: retention.cleanup(clock()), scope=db.scope),
        ]
        return Services(connection, delivery, gateway, proximity, window, execution, release,
                        restore, recovery, shared_light, node_events, retention, core)

    # ── 종료 ────────────────────────────────────────────────────────

    def stop(self) -> None:
        """종료 순서를 따른다. 제한을 넘기면 `ShutdownError` 다. 미완료 상태는 정산하지 않는다."""
        if self._state != "running":
            return
        self._state = "stopping"
        problems = self._shutdown(self._monotonic() + self.config.runtime.shutdown_timeout_sec)
        self._state = "stopped"
        if problems:
            log.error("종료 제한 초과: %s. 미완료 기록은 다음 복원에서 다시 확인한다",
                      ", ".join(problems))
            raise ShutdownError(problems)
        log.info("서버 종료 완료")

    def _shutdown(self, deadline: float) -> list[str]:
        def remaining() -> float:
            return max(0.0, deadline - self._monotonic())

        problems: list[str] = []
        # 1. 새 요청 접수를 막는다. 2. 새 주기 호출을 멈춘다
        self.gate.close()
        started_tasks = [task for task in self.tasks if task.started]
        for task in started_tasks:
            task.signal_stop()
        # 3. 진행 중인 호출을 제한 시간까지 기다린다. 끊지 않는다
        if not self.gate.wait_idle(remaining()):
            problems.append("진행 중인 요청")
        for task in started_tasks:
            if not task.join(remaining()):
                problems.append(f"주기 작업 {task.name}")
        # 4. MQTT 와 보고 작업을 멈춘다
        svc = self.services
        if svc is not None and "mqtt" in self._started:
            try:
                svc.connection.stop()
            except ConnectionFailure:
                problems.append("MQTT 종료")
        if svc is not None and "delivery" in self._started:
            try:
                svc.delivery.stop()
            except RuntimeError:
                problems.append("보고 인계 작업 종료")
        # 5. 각 연결은 소유 스레드가 닫는다. 남은 범위는 강제로 닫지 않는다
        if "db" in self._started:
            self.db.export_jsonl()
        if self.db.open_connections:
            problems.append(f"사용 중인 DB 연결 {self.db.open_connections}개")
        return problems
