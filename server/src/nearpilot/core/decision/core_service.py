"""FR-09 MCP 기능 여섯 개의 CORE 서비스 (specs/core-service/spec.md).

기존 기능 서비스에 위임만 한다. 입력, 판정, 사유와 소유권 검사 결과를 바꾸지 않는다 (NFR-07).
도메인 상태를 따로 저장하지 않고, 전역 잠금으로 동시 요청을 직렬화하지 않는다 (NFR-03).
위임한 서비스의 예외는 그대로 올린다. 오류 변환은 MCP 도구가 한다.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from nearpilot.core.decision.access import AccessRequestService
from nearpilot.core.decision.pipeline import BatchAllocator, DecisionPipeline
from nearpilot.core.decision.query import DeviceQueryService
from nearpilot.core.decision.service import RequestUseService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action
from nearpilot.shared.interfaces import (
    AuditLog,
    ExecutionReadiness,
    ExecutionService,
    ExecutionStore,
    PolicyChecker,
    ProximityEstimator,
    Repository,
)
from nearpilot.shared.models import Decision, UseRequest


class UseRequester(Protocol):
    def request_use(self, req: UseRequest) -> Decision: ...


class AccessRequester(Protocol):
    def request_access(self, user_id: str, node_id: str, action: Action, request_id: str) -> Decision: ...


class DeviceQuery(Protocol):
    def find_available_devices(self, user_id: str) -> Sequence[Mapping[str, Any]]: ...

    def get_device_state(self, user_id: str, node_id: str) -> Mapping[str, Any]: ...


class UseRelease(Protocol):
    """상태 관리의 `ReleaseService`. 본인 소유권 검사는 이 서비스가 한다 (FR-21)."""

    def release_use(self, user_id: str, use_id: str) -> Decision: ...

    def get_use_status(self, user_id: str, use_id: str) -> Mapping[str, Any]: ...


class DelegatingCoreService:
    """`shared.interfaces.CoreService` 구현. 호출마다 맞는 서비스를 한 번 부른다."""

    def __init__(
        self, *, use: UseRequester, access: AccessRequester, query: DeviceQuery, release: UseRelease
    ) -> None:
        self._use = use
        self._access = access
        self._query = query
        self._release = release

    def find_available_devices(self, user_id: str) -> Sequence[Mapping[str, Any]]:
        return self._query.find_available_devices(user_id)

    def get_device_state(self, user_id: str, node_id: str) -> Mapping[str, Any]:
        return self._query.get_device_state(user_id, node_id)

    def request_access(self, user_id: str, node_id: str, action: Action, request_id: str) -> Decision:
        return self._access.request_access(user_id, node_id, action, request_id)

    def request_use(self, req: UseRequest) -> Decision:
        return self._use.request_use(req)

    def release_use(self, user_id: str, use_id: str) -> Decision:
        return self._release.release_use(user_id, use_id)

    def get_use_status(self, user_id: str, use_id: str) -> Mapping[str, Any]:
        return self._release.get_use_status(user_id, use_id)


def build_core_service(
    *,
    repo: Repository,
    proximity: ProximityEstimator,
    policy: PolicyChecker,
    audit: AuditLog,
    settings: Settings,
    clock: Callable[[], datetime],
    readiness: ExecutionReadiness,
    execution: ExecutionService,
    store: ExecutionStore,
    release: UseRelease,
    window: BatchAllocator | None = None,
    new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> DelegatingCoreService:
    """판정 서비스를 만들고 CORE 서비스로 묶는다.

    실제 구현체, 공유할 `release`와 할당 대기창 `window`는 서버 조립이 만들어 넘긴다.
    판정 모듈은 다른 계층의 구현을 불러오지 않는다 (AGENTS.md 의존 규칙 4).
    """
    pipeline = DecisionPipeline(
        repo, proximity, policy, audit, settings, readiness, new_id=new_id, window=window,
        store=store)
    return DelegatingCoreService(
        use=RequestUseService(pipeline, execution, store),
        access=AccessRequestService(repo, policy, audit, settings, clock, new_id=new_id),
        query=DeviceQueryService(repo, proximity, policy, settings, clock, readiness),
        release=release,
    )
