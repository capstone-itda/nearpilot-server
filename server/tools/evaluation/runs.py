"""수집 회차 읽기 (specs/core-ops-cli/spec.md 6절 "collect", "split", "fit").

`list_runs`는 `collection_run`의 모든 회차를 연결된 관측 수와 장소와 함께 읽는다.
`load_labeled_runs`는 고른 회차를 연결된 프레임과 함께 적합용 `LabeledRun`으로 읽는다.
둘 다 읽기만 하고 DB를 바꾸지 않는다 (NFR-13).
SELECT만 쓰므로 어떤 연결이든 받지만, 호출자는 `db.open_readonly`로 연 연결을 넘긴다.
회차의 장소는 DB에 저장되지 않는다. 라벨이 기기면 그 기기가 속한 장소이고,
라벨이 `none`이면 회차에 연결된 관측의 기기들이 속한 장소다.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from nearpilot.core.proximity.fit import NONE_LABEL, LabeledRun
from nearpilot.shared.models import BeaconKey, RssiFrame

# 관측 수와 장소는 한 번에 센다. rssi_observation에는 collection_id 인덱스가 없어서 회차마다 세면 매번 훑는다.
_RUNS_SQL = "SELECT collection_id, beacon_id, label, started_at, ended_at FROM collection_run"
_OBSERVATIONS_SQL = (
    "SELECT o.collection_id, n.place_id, count(*) FROM rssi_observation o "
    "LEFT JOIN node n ON n.node_id = o.node_id "
    "WHERE o.collection_id IS NOT NULL GROUP BY o.collection_id, n.place_id"
)
_NODE_PLACES_SQL = "SELECT node_id, place_id FROM node"

# 고른 회차의 라벨, 비콘, 구간. 비콘은 회차의 beacon_id로 찾는다. `{marks}`는 회차 ID 수만큼의 `?`다.
_RUN_SQL = (
    "SELECT r.collection_id, r.label, r.started_at, r.ended_at, b.uuid, b.major, b.minor "
    "FROM collection_run r JOIN beacon b ON b.beacon_id = r.beacon_id "
    "WHERE r.collection_id IN ({marks})"
)
# 프레임도 고른 회차 전체를 한 번에 읽는다 (collection_id 인덱스가 없다). 비콘은 관측의 beacon_id로 찾고,
# 저장된 순서(rowid)로 읽어서 질의 계획이 바뀌어도 순서가 같다.
_FRAMES_SQL = (
    "SELECT o.collection_id, o.node_id, b.uuid, b.major, b.minor, o.rssi, o.frame_ts, o.received_at, o.sent_at, "
    "o.session_id, o.seq FROM rssi_observation o JOIN beacon b ON b.beacon_id = o.beacon_id "
    "WHERE o.collection_id IN ({marks}) ORDER BY o.rowid"
)


@dataclass(frozen=True)
class RunInfo:
    """수집 회차 하나. 장소는 `list_runs`가 정한다."""

    collection_id: str
    beacon_id: str
    label: str  # node_id 또는 "none"
    started_at: datetime
    ended_at: datetime | None  # 진행 중이면 None
    frame_count: int  # 회차에 연결된 관측 수
    place_id: str | None
    place_problem: str | None  # place_id 가 None 인 이유. 있으면 분할에서 뺀다


def list_runs(conn: sqlite3.Connection) -> list[RunInfo]:
    """모든 수집 회차를 시작 시각, 회차 ID 순으로 돌려준다.

    장소를 정할 수 없는 회차도 돌려주고 `place_problem`에 이유를 쓴다.
    - 라벨이 기기면 그 기기의 `node.place_id`다. 기기가 없거나 장소가 없으면 정할 수 없다.
    - 라벨이 `none`이면 연결된 관측의 기기들이 속한 장소(장소가 없는 기기는 뺀다)다. 하나가 아니면 정할 수 없다.
    """
    frames: defaultdict[str, int] = defaultdict(int)
    observed_places: defaultdict[str, set[str]] = defaultdict(set)
    for collection_id, place_id, count in conn.execute(_OBSERVATIONS_SQL):
        frames[collection_id] += count
        if place_id is not None:
            observed_places[collection_id].add(place_id)
    node_places = {node_id: place_id for node_id, place_id in conn.execute(_NODE_PLACES_SQL)}

    runs = []
    for collection_id, beacon_id, label, started_at, ended_at in conn.execute(_RUNS_SQL):
        if label == NONE_LABEL:
            place_id, problem = _none_run_place(observed_places.get(collection_id, set()))
        else:
            place_id = node_places.get(label)
            problem = None if place_id is not None else "라벨 기기가 장소에 없다"
        runs.append(RunInfo(
            collection_id=collection_id,
            beacon_id=beacon_id,
            label=label,
            started_at=datetime.fromisoformat(started_at),
            ended_at=None if ended_at is None else datetime.fromisoformat(ended_at),
            frame_count=frames.get(collection_id, 0),
            place_id=place_id,
            place_problem=problem,
        ))
    runs.sort(key=lambda run: (run.started_at, run.collection_id))
    return runs


def _none_run_place(places: set[str]) -> tuple[str | None, str | None]:
    """`none` 회차의 (장소, 정할 수 없는 이유). 관측에 나온 장소가 정확히 하나일 때만 정한다."""
    if len(places) == 1:
        return next(iter(places)), None
    if not places:
        return None, "관측이 없어 장소를 정할 수 없다"
    return None, "여러 장소의 관측이 섞였다"


class RunLoadError(ValueError):
    """회차를 `LabeledRun`으로 읽을 수 없다. 메시지에 DB에 없는 회차와 진행 중인 회차를 모두 쓴다."""


def load_labeled_runs(conn: sqlite3.Connection, collection_ids: Sequence[str]) -> list[LabeledRun]:
    """회차 ID마다 라벨, 비콘, 구간, 연결된 프레임을 읽어 `LabeledRun`으로 돌려준다. 순서는 `collection_ids` 그대로다.

    프레임은 회차에 연결된(`rssi_observation.collection_id`) 관측 전부다. 비콘이나 기기로 거르는 일은
    적합과 평가가 한다 (`run_frames`). DB에 없는 회차나 진행 중인(끝 시각이 없는) 회차가 있으면 모아서 `RunLoadError`다.
    """
    if not collection_ids:
        return []
    ids = list(collection_ids)
    marks = ", ".join("?" * len(ids))

    # collection_id → (라벨, 비콘, 시작, 끝)
    heads: dict[str, tuple[str, BeaconKey, datetime, datetime | None]] = {}
    for collection_id, label, started_at, ended_at, uuid, major, minor in conn.execute(
        _RUN_SQL.format(marks=marks), ids
    ):
        heads[collection_id] = (
            label, BeaconKey(uuid, major, minor), datetime.fromisoformat(started_at),
            None if ended_at is None else datetime.fromisoformat(ended_at),
        )
    wanted = list(dict.fromkeys(ids))  # 같은 ID를 두 번 알리지 않는다
    missing = [collection_id for collection_id in wanted if collection_id not in heads]
    in_progress = [
        collection_id for collection_id in wanted if collection_id in heads and heads[collection_id][3] is None
    ]
    problems = []
    if missing:
        problems.append(f"DB에 없는 회차 {', '.join(missing)}")
    if in_progress:
        problems.append(f"진행 중인 회차 {', '.join(in_progress)}")
    if problems:
        raise RunLoadError(f"수집 회차를 읽을 수 없다: {'; '.join(problems)}")

    frames: defaultdict[str, list[RssiFrame]] = defaultdict(list)
    rows = conn.execute(_FRAMES_SQL.format(marks=marks), ids)
    for collection_id, node_id, uuid, major, minor, rssi, frame_ts, received_at, sent_at, session_id, seq in rows:
        frames[collection_id].append(RssiFrame(
            node_id=node_id,
            beacon=BeaconKey(uuid, major, minor),
            rssi=rssi,
            frame_ts=datetime.fromisoformat(frame_ts),
            session_id=session_id,
            seq=seq,
            sent_at=datetime.fromisoformat(sent_at),
            received_at=datetime.fromisoformat(received_at),
        ))

    runs = []
    for collection_id in ids:
        label, beacon, started_at, ended_at = heads[collection_id]
        runs.append(LabeledRun(collection_id, label, beacon, started_at, ended_at, frames[collection_id]))
    return runs
