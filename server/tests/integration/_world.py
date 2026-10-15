"""통합 시나리오의 세계: 실제 SQLite와 SqliteRepository 위에 계정, 기기, 정책을 SQL로 넣는다.

`AdminRepository`가 아직 없어 SQL로 직접 넣는다 (tests/db 와 같다). 근접 추정, 실행, 감사는 fake 다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nearpilot.core.decision.access import AccessRequestService
from nearpilot.core.decision.pipeline import DecisionPipeline
from nearpilot.core.policy.checker import RepositoryPolicyChecker
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action
from nearpilot.shared.models import BeaconKey, ProximityResult, RssiFrame, TargetSpec, UseRequest

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
OWNER, VISITOR_A, VISITOR_B = "owner", "visitor-a", "visitor-b"
SETTINGS = Settings(version="v1", threshold_explicit_locker=0.5, threshold_explicit_light=0.5)


class FakeProximity:
    """기기별 확률을 고정해서 돌려준다. 후보에 없는 기기는 뺀다."""

    def __init__(self, posteriors):
        self.posteriors = posteriors

    def posterior(self, beacons, candidates, at, *, place_nodes):
        wanted = {n.node_id for n in candidates}
        return ProximityResult({k: v for k, v in self.posteriors.items() if k in wanted}, at, at, "m", "c")


class FakeReadiness:
    def can_execute(self, node_id):
        return True


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def beacon_of(user_id: str) -> BeaconKey:
    return BeaconKey("UUID", 1, {OWNER: 1, VISITOR_A: 2, VISITOR_B: 3}[user_id])


class World:
    def __init__(self, posteriors=None, *, db_path=":memory:", settings=SETTINGS, window=None):
        self.db_path = db_path
        self.settings = settings
        self.window = window
        self.conn = connect(db_path)
        self.repo = SqliteRepository(self.conn)
        self.policy = RepositoryPolicyChecker(self.repo)
        self.audit = FakeAudit()
        self.prox = FakeProximity(posteriors or {"locker-1": 0.97})
        self._seed()
        self.pipeline = DecisionPipeline(
            self.repo, self.prox, self.policy, self.audit, settings, FakeReadiness(), window=window)
        self.access = AccessRequestService(
            self.repo, self.policy, self.audit, settings, lambda: NOW)

    def fork(self):
        """같은 DB 파일에 연결을 하나 더 열고 그 위의 파이프라인을 만든다. 스레드마다 하나씩 쓴다.

        대기 구간, 근접 추정, 감사는 공유한다. 반환한 연결은 쓴 스레드가 닫는다. 파일 DB 에서만 쓴다.
        """
        conn = connect(self.db_path)
        repo = SqliteRepository(conn)
        pipeline = DecisionPipeline(
            repo, self.prox, RepositoryPolicyChecker(repo), self.audit, self.settings,
            FakeReadiness(), window=self.window)
        return conn, pipeline

    def close(self):
        self.conn.close()

    # ── 데이터 ──────────────────────────────────────────────────────

    def _seed(self):
        c = self.conn
        for user_id, role in ((OWNER, "owner"), (VISITOR_A, "visitor"), (VISITOR_B, "visitor")):
            c.execute("INSERT INTO user (user_id, name, token_hash, role) VALUES (?, ?, ?, ?)",
                      (user_id, user_id, f"hash-{user_id}", role))
            key = beacon_of(user_id)
            c.execute("INSERT INTO beacon (beacon_id, user_id, uuid, major, minor, is_active) "
                      "VALUES (?, ?, ?, ?, ?, 1)", (f"b-{user_id}", user_id, key.uuid, key.major, key.minor))
        c.execute("INSERT INTO place (place_id, owner_user_id, name) VALUES ('p1', ?, 'lab')", (OWNER,))
        seen = (NOW - timedelta(seconds=1)).isoformat()
        for node_id, visibility, mode in (
            ("locker-1", "public", "open"),         # 공개, 승인 불필요
            ("locker-2", "public", "restricted"),   # 공개, 승인 필요
            ("locker-3", "private", "open"),        # 비공개 (소유자만)
            ("locker-4", "public", "open"),         # 공개, 승인 불필요 (any:locker 의 두 번째 후보)
        ):
            c.execute(
                "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, "
                "exclusive_group, release_policy, last_seen_at) "
                "VALUES (?, 'p1', 'locker', 'rental', 'trusted', 'AVAILABLE', NULL, '{}', ?)",
                (node_id, seen))
            policy_id = f"pol-{node_id}"
            c.execute("INSERT INTO access_policy (policy_id, node_id, current_version) VALUES (?, ?, 1)",
                      (policy_id, node_id))
            c.execute(
                "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, "
                "approval_ttl_sec, approval_mode, max_uses) VALUES (?, 1, ?, ?, ?, 3600, 'one_time', 1)",
                (policy_id, visibility, mode, OWNER))
        # 모든 방문자가 네 기기를 최근에 봤다
        for user_id in (OWNER, VISITOR_A, VISITOR_B):
            for seq, node_id in enumerate(("locker-1", "locker-2", "locker-3", "locker-4"), start=1):
                self.repo.save_rssi(RssiFrame(
                    node_id, beacon_of(user_id), -60.0, NOW, f"s-{node_id}", seq, NOW, NOW))

    # ── 요청 ────────────────────────────────────────────────────────

    @staticmethod
    def request(user_id, target="locker-1", request_id="r1", action=Action.OPEN) -> UseRequest:
        return UseRequest(user_id, request_id, TargetSpec.parse(target), action, "claude", NOW)

    def count(self, sql: str, *params) -> int:
        return self.conn.execute(sql, params).fetchone()[0]
