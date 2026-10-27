"""승인 한 건이 정해진 횟수만 쓰인다: one_time 은 한 번, time_window 는 max_uses 번 (FR-14, FR-19).

실제 SQLite·runtime 조립·IOT 위에서 시뮬레이터를 fake Paho 로 연결한다. 제한 정책(restricted)인 사물함
locker-1 에서 승인 신청 → 소유자 승인 → 사용 → 반납을 되풀이하면서, 판정과 기기 구동(명령 전송과 결과)을
함께 본다. 근접 추정은 fake 로 고정한다.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from _runtime import OWNER, VISITOR_A, FakeProximity, SimBroker, make_config, query, running, seed, wait_until
from nearpilot.runtime import Runtime, ShutdownError
from nearpilot.shared.enums import Action, ApprovalStatus, ReasonCode, Verdict
from nearpilot.shared.models import TargetSpec, UseRequest


class LockerWorld:
    """제한 정책인 사물함 locker-1 하나를 쓰는 서버. 승인 방식과 max_uses 는 시드 뒤에 정한다."""

    def __init__(self, tmp, approval_mode, max_uses):
        self.cfg = make_config(tmp, settings={"threshold_explicit_locker": 0.5})
        seed(self.cfg.database.path, modes={"locker-1": "restricted"})
        conn = sqlite3.connect(str(self.cfg.database.path))
        try:
            conn.execute("UPDATE policy_revision SET approval_mode = ?, max_uses = ? WHERE policy_id = 'pol-locker-1'",
                         (approval_mode, max_uses))
            conn.commit()
        finally:
            conn.close()
        self.broker = SimBroker(tmp, ("locker-1",), beacons={"locker-1": (VISITOR_A,)})
        proximity = FakeProximity({"locker-1": 0.97})
        self.runtime = Runtime(self.cfg, client_factory=self.broker.factory,
                               proximity_factory=lambda models, settings: proximity)

    @property
    def db(self):
        return self.cfg.database.path

    def ready(self):
        restore = self.runtime.services.restore
        wait_until(lambda: restore.can_execute("locker-1"))
        self.refresh_rssi()

    def refresh_rssi(self):
        """① 비콘은 최근 수신만 인정한다. 요청마다 새 프레임을 저장하고 나서 요청한다."""
        before = query(self.db, "SELECT count(*) c FROM rssi_observation")[0]["c"]
        self.broker.rssi()
        wait_until(lambda: query(self.db, "SELECT count(*) c FROM rssi_observation")[0]["c"] > before)

    def approve(self, request_id):
        """방문자 A 가 승인을 신청하고 소유자가 승인한다."""
        access = self.runtime.core.request_access(VISITOR_A, "locker-1", Action.OPEN, request_id)
        assert (access.verdict, access.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING)
        [pending] = query(self.db, "SELECT approval_id FROM approval WHERE status = 'pending'")
        assert self.runtime.approvals.decide(OWNER, pending["approval_id"], ApprovalStatus.APPROVED).succeeded

    def use(self, request_id):
        self.refresh_rssi()
        return self.runtime.core.request_use(UseRequest(
            VISITOR_A, request_id, TargetSpec.parse("locker-1"), Action.OPEN, None, datetime.now(timezone.utc)))

    def use_and_release(self, request_id, commands):
        """사용이 OK 이고 기기를 구동해서 명령이 `commands` 가 된 뒤에 반납한다."""
        used = self.use(request_id)
        assert used.verdict is Verdict.OK
        self.wait_commands(commands)
        assert self.runtime.core.release_use(VISITOR_A, used.use_id).verdict is Verdict.OK

    def commands(self):
        return [row["status"] for row in query(self.db, "SELECT status FROM node_command")]

    def wait_commands(self, expected):
        try:
            wait_until(lambda: self.commands() == expected)
        except AssertionError:
            raise AssertionError(f"명령 상태 {self.commands()} 가 {expected} 가 되지 않았다") from None

    def close(self):
        self.broker.close()


@pytest.fixture
def world(tmp_path):
    made = []

    def make(approval_mode, max_uses):
        made.append(LockerWorld(tmp_path, approval_mode, max_uses))
        return made[-1]

    yield make
    for w in made:
        try:
            w.runtime.stop()
        except ShutdownError:
            pass
        w.close()


def _runs_once(w):
    """승인 하나로 첫 사용은 OK 이고 구동하며, 반납 뒤 두 번째 요청은 승인 소진으로 막힌다."""
    with running(w.runtime):
        w.ready()
        w.approve("access-1")
        w.use_and_release("use-1", ["succeeded"])
        second = w.use("use-2")
        assert (second.verdict, second.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED)
        assert w.commands() == ["succeeded"]  # 두 번째는 구동하지 않는다


def test_FR14_one_time_approval_runs_once(world):
    _runs_once(world("one_time", None))  # max_uses 가 없어도 한 번이다


def test_FR14_one_time_with_max_uses_one_runs_once(world):
    _runs_once(world("one_time", 1))  # 재검사가 자기 사용분을 세면 첫 사용이 막힌다


def test_FR14_time_window_max_uses_counts_exactly(world):
    w = world("time_window", 2)
    with running(w.runtime):
        w.ready()
        w.approve("access-1")
        w.use_and_release("use-1", ["succeeded"])
        w.use_and_release("use-2", ["succeeded", "succeeded"])
        third = w.use("use-3")
        assert (third.verdict, third.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED)
        assert w.commands() == ["succeeded", "succeeded"]  # 정확히 두 번 구동했다
