"""기기 관리 서비스: 원격 재시작 (FR-12A, NFR-04, #235, specs/iot-node-restart).

판정을 거치지 않는 관리 기능이다. 장소 소유권만 검사하고 점유, 명령 기록, 복구 건을 바꾸지 않는다.
요청자, 기기, 결과는 서버 로그에만 남긴다. 결과에는 사용자 ID와 기기 연결 세션 ID가 없다 (NFR-08).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import RestartOutcome
from nearpilot.shared.interfaces import NodeRestarter, Repository
from nearpilot.shared.models import RestartResult

log = logging.getLogger(__name__)


class NodeAdminService:
    """`shared.interfaces.NodeAdminService` 구현."""

    def __init__(self, *, repository: Repository, restarter: NodeRestarter, clock: Callable[[], datetime],
                 settings: Callable[[], Settings] = Settings,
                 new_id: Callable[[], str] = lambda: str(uuid4())) -> None:
        self._repository = repository
        self._restarter = restarter
        self._clock = clock
        self._settings = settings
        self._new_id = new_id

    def restart(self, user_id: str, node_id: str) -> RestartResult:
        if not self._repository.is_place_owner(user_id, node_id):
            log.info("기기 재시작 거부: user=%s node=%s outcome=%s", user_id, node_id, RestartOutcome.NOT_OWNER.value)
            return RestartResult(node_id, RestartOutcome.NOT_OWNER)
        restart_id = self._new_id()
        # IOT 는 이 시각 뒤에 issued_at 을 정한다. 기기의 실제 만료는 이 값보다 늦거나 같다 (spec 6절).
        expires_at = self._clock() + timedelta(seconds=self._settings().exec_timeout_sec)
        outcome = self._restarter.restart(node_id, restart_id)
        log.info("기기 재시작 요청: user=%s node=%s restart_id=%s outcome=%s",
                 user_id, node_id, restart_id, outcome.value)
        if outcome is not RestartOutcome.SENT:
            return RestartResult(node_id, outcome)
        return RestartResult(node_id, outcome, restart_id, expires_at)
