"""수집 회차 목록과 회차의 장소 (FR-16, NFR-13, specs/core-ops-cli/spec.md 6절 "collect", "split").

회차 표와 관측 표를 SQL로 직접 채워서 `list_runs`가 읽는 값을 확인한다. 라벨의 기기가 DB에 없는 회차처럼
`AdminRepository`로는 만들 수 없는 상태도 만들어야 해서 쓰기 경로를 거치지 않는다.
`list_runs`에는 평가 도구가 쓰는 읽기 전용 연결(`open_readonly`)을 넘긴다.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nearpilot.db.schema import connect
from nearpilot.shared.enums import Capability, DeviceState, Occupancy, Role, TrustState

from tools.evaluation.db import open_readonly
from tools.evaluation.runs import RunInfo, list_runs

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)
UUID = "11111111-2222-3333-4444-555555555555"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class RunsDb:
    """회차와 관측을 SQL로 넣는 테스트용 DB. 쓰기 연결(`write`)로 채우고 읽기 전용 연결(`read`)로 읽는다."""

    def __init__(self, path: Path) -> None:
        self.write = connect(str(path))
        self.write.execute(
            "INSERT INTO user (user_id, name, token_hash, role) VALUES (?, ?, ?, ?)",
            ("owner", "소유자", "hash-owner", Role.OWNER.value))
        self.write.execute(
            "INSERT INTO beacon (beacon_id, user_id, uuid, major, minor, is_active) VALUES (?, ?, ?, 1, 1, 1)",
            ("beacon-1", "owner", UUID))
        self.read = open_readonly(str(path))
        self._seq = 0

    def node(self, node_id: str, place_id: str | None = None) -> None:
        """기기 행을 넣는다. 장소가 있으면 장소 행도 (없을 때만) 넣는다."""
        if place_id is not None:
            self.write.execute(
                "INSERT OR IGNORE INTO place (place_id, owner_user_id, name) VALUES (?, 'owner', ?)",
                (place_id, place_id))
        self.write.execute(
            "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, place_id, Capability.LOCKER.value, Occupancy.RENTAL.value, TrustState.TRUSTED.value,
             DeviceState.AVAILABLE.value))

    def run(self, collection_id: str, label: str, started_at: datetime, ended_at: datetime | None = None,
            frames: dict[str, int] | None = None) -> None:
        """회차 행과, 기기마다 `frames`개의 연결된 관측을 넣는다."""
        self.write.execute(
            "INSERT INTO collection_run (collection_id, beacon_id, label, started_at, ended_at) "
            "VALUES (?, 'beacon-1', ?, ?, ?)",
            (collection_id, label, started_at.isoformat(), None if ended_at is None else ended_at.isoformat()))
        for node_id, count in (frames or {}).items():
            self.observations(node_id, count, collection_id)

    def observations(self, node_id: str, count: int, collection_id: str | None = None) -> None:
        """관측을 `count`개 넣는다. `collection_id`가 없으면 어느 회차에도 연결되지 않은 관측이다."""
        for _ in range(count):
            self._seq += 1
            self.write.execute(
                "INSERT INTO rssi_observation (obs_id, node_id, beacon_id, rssi, frame_ts, received_at, sent_at, "
                "session_id, seq, collection_id) VALUES (?, ?, 'beacon-1', -60.0, ?, ?, ?, 'session', ?, ?)",
                (f"obs-{self._seq}", node_id, T0.isoformat(), T0.isoformat(), T0.isoformat(), self._seq,
                 collection_id))

    def close(self) -> None:
        self.read.close()
        self.write.close()


@pytest.fixture
def db(tmp_path):
    runs_db = RunsDb(tmp_path / "nearpilot.db")
    yield runs_db
    runs_db.close()


def test_FR16_list_runs_device_label_takes_the_device_place(db):
    db.node("locker-1", "place-a")
    db.node("locker-2", "place-a")
    db.node("locker-9", "place-b")
    db.run("c1", "locker-1", at(0), at(30), {"locker-1": 5, "locker-2": 2})  # 다른 기기가 본 관측도 회차의 프레임이다
    db.run("c2", "locker-9", at(60), at(90), {"locker-9": 3})

    assert list_runs(db.read) == [
        RunInfo("c1", "beacon-1", "locker-1", at(0), at(30), 7, "place-a", None),
        RunInfo("c2", "beacon-1", "locker-9", at(60), at(90), 3, "place-b", None),
    ]


def test_FR16_list_runs_device_label_without_place_is_excluded(db):
    db.node("locker-1", "place-a")
    db.node("locker-2")  # 발표만 받고 장소에 놓지 않은 기기
    db.run("c1", "locker-2", at(0), at(30), {"locker-1": 2})
    # 라벨의 기기가 DB에 없다. 회차 쓰기는 이런 라벨을 거부하지만 읽는 쪽은 죽지 않고 제외로 센다.
    db.run("c2", "ghost-1", at(60), at(90))

    runs = list_runs(db.read)

    assert [(run.collection_id, run.place_id, run.place_problem) for run in runs] == [
        ("c1", None, "라벨 기기가 장소에 없다"),
        ("c2", None, "라벨 기기가 장소에 없다"),
    ]
    assert [run.frame_count for run in runs] == [2, 0]  # 제외하는 회차도 프레임 수는 그대로 센다


def test_FR16_list_runs_none_run_takes_the_observed_place(db):
    db.node("locker-1", "place-a")
    db.node("locker-2", "place-a")
    db.node("locker-9", "place-b")
    db.node("locker-8")  # 장소가 없는 기기의 관측은 장소를 정하는 데 쓰지 않는다
    db.run("c1", "none", at(0), at(30), {"locker-1": 4, "locker-2": 3, "locker-8": 2})
    db.run("c2", "none", at(60), at(90), {"locker-9": 1})

    runs = list_runs(db.read)

    assert runs == [
        RunInfo("c1", "beacon-1", "none", at(0), at(30), 9, "place-a", None),  # 같은 장소의 두 기기는 한 장소다
        RunInfo("c2", "beacon-1", "none", at(60), at(90), 1, "place-b", None),
    ]


def test_FR16_list_runs_none_run_without_observations_has_no_place(db):
    db.node("locker-1", "place-a")
    db.node("locker-8")
    db.run("c1", "none", at(0), at(30))
    db.run("c2", "none", at(60), at(90), {"locker-8": 2})  # 장소 없는 기기의 관측뿐이어도 장소를 정할 수 없다

    runs = list_runs(db.read)

    assert [(run.place_id, run.place_problem, run.frame_count) for run in runs] == [
        (None, "관측이 없어 장소를 정할 수 없다", 0),
        (None, "관측이 없어 장소를 정할 수 없다", 2),
    ]


def test_FR16_list_runs_none_run_with_two_places_has_no_place(db):
    db.node("locker-1", "place-a")
    db.node("locker-9", "place-b")
    db.run("c1", "none", at(0), at(30), {"locker-1": 3, "locker-9": 1})

    assert list_runs(db.read) == [
        RunInfo("c1", "beacon-1", "none", at(0), at(30), 4, None, "여러 장소의 관측이 섞였다"),
    ]


def test_FR16_list_runs_open_run_has_no_end(db):
    db.node("locker-1", "place-a")
    db.run("c1", "locker-1", at(0), at(30), {"locker-1": 3})
    db.run("c2", "locker-1", at(60), None, {"locker-1": 2})  # 진행 중. 지금까지 연결된 관측만 센다

    runs = list_runs(db.read)

    assert [(run.collection_id, run.ended_at, run.frame_count, run.place_id) for run in runs] == [
        ("c1", at(30), 3, "place-a"),
        ("c2", None, 2, "place-a"),
    ]


def test_FR16_list_runs_counts_only_observations_linked_to_the_run(db):
    db.node("locker-1", "place-a")
    db.node("locker-9", "place-b")
    db.run("c1", "locker-1", at(0), at(30), {"locker-1": 3})
    db.run("c2", "none", at(60), at(90), {"locker-1": 1})
    db.observations("locker-1", 5)  # 어느 회차에도 연결되지 않은 운영 관측
    db.observations("locker-9", 4)  # 장소 b의 기기가 본 연결되지 않은 관측은 none 회차의 장소를 흐리지 않는다

    runs = list_runs(db.read)

    assert [(run.collection_id, run.frame_count, run.place_id) for run in runs] == [
        ("c1", 3, "place-a"),
        ("c2", 1, "place-a"),
    ]


def test_FR16_list_runs_orders_by_start_then_collection_id(db):
    db.node("locker-1", "place-a")
    # 일부러 시작 시각의 역순으로 넣는다. 시작이 같으면 회차 ID 순이다.
    db.run("c-late", "locker-1", at(120), at(150))
    db.run("c-b", "locker-1", at(60), at(90))
    db.run("c-a", "locker-1", at(60), at(90))
    db.run("c-early", "locker-1", at(0), at(30))

    assert [run.collection_id for run in list_runs(db.read)] == ["c-early", "c-a", "c-b", "c-late"]


def test_FR16_list_runs_reads_stored_utc_times(db):
    db.node("locker-1", "place-a")
    started = datetime(2026, 10, 17, 9, 0, 0, 123456, tzinfo=timezone.utc)
    db.run("c1", "locker-1", started, started + timedelta(seconds=30.5))

    [run] = list_runs(db.read)

    assert run.started_at == started and run.started_at.utcoffset() == timedelta(0)
    assert run.ended_at == started + timedelta(seconds=30.5)
    assert run.beacon_id == "beacon-1" and run.label == "locker-1"


def test_FR16_list_runs_empty_database(db):
    db.node("locker-1", "place-a")
    db.observations("locker-1", 2)

    assert list_runs(db.read) == []


def test_FR16_list_runs_does_not_depend_on_row_factory(db, tmp_path):
    db.node("locker-1", "place-a")
    db.run("c1", "locker-1", at(0), at(30), {"locker-1": 2})
    plain = sqlite3.connect(f"file:{tmp_path / 'nearpilot.db'}?mode=ro", uri=True)  # row_factory가 없는 연결
    try:
        assert list_runs(plain) == list_runs(db.read)
    finally:
        plain.close()


def test_NFR13_list_runs_only_reads(db):
    db.node("locker-1", "place-a")
    db.run("c1", "locker-1", at(0), at(30), {"locker-1": 2})
    db.run("c2", "none", at(60), None)

    def snapshot() -> list[list[tuple]]:
        return [
            [tuple(row) for row in db.write.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("user", "beacon", "place", "node", "collection_run", "rssi_observation")
        ]

    before = snapshot()
    list_runs(db.read)

    assert snapshot() == before
    # 넘긴 연결은 읽기 전용이다. 쓰기 문장이 있었다면 sqlite가 거부했을 것이다.
    with pytest.raises(sqlite3.OperationalError):
        db.read.execute("DELETE FROM collection_run")
