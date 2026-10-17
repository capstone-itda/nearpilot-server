"""`server/config/`의 예시 파일 (FR-16, specs/core-ops-cli/spec.md 4절 "고치는 곳").

예시 파일은 `nearpilot`이 읽는 로더로 그대로 읽혀야 하고, 서로 같은 장소·기기·비콘을 가리켜야 한다.
`server/config/README.md`의 명령 흐름(`nearpilot seed example.toml`, `nearpilot collect run example.json`, …)이
예시 파일만으로 이어지려면 둘이 모두 맞아야 한다. 값의 좋고 나쁨은 확인하지 않는다 (예시 값이다).
"""

from __future__ import annotations

import tomllib
from collections import Counter
from pathlib import Path

from nearpilot.cli.seed import load_seed_file
from nearpilot.core.proximity.files import load_fit_file, load_layout
from tools.data_collection.plan import load_plan

CONFIG = Path(__file__).resolve().parents[2] / "config"
SEED, PLAN, LAYOUT, FIT = (
    CONFIG / "seed" / "example.toml",
    CONFIG / "plans" / "example.json",
    CONFIG / "layouts" / "example.json",
    CONFIG / "fit" / "example.toml",
)


def test_FR16_config_examples_load():
    seed = load_seed_file(SEED)
    plan = load_plan(PLAN)
    layout = load_layout(LAYOUT)
    fit = load_fit_file(FIT)

    # 비어 있는 파일이 읽히는 것은 아니다
    assert seed.accounts and seed.beacons and seed.places and seed.nodes and seed.policies
    assert plan.runs and layout.nodes and layout.none_points
    assert fit.fit.taus and fit.fit.stays and fit.fit.window_secs


def test_FR16_config_examples_agree():
    seed = load_seed_file(SEED)
    plan = load_plan(PLAN)
    layout = load_layout(LAYOUT)
    fit = load_fit_file(FIT)

    # 같은 장소다. 배치의 기기는 시드가 그 장소에 놓은 기기다
    assert plan.place_id == layout.place_id
    place = next(place for place in seed.places if place.place_id == layout.place_id)
    assert set(layout.nodes) <= set(place.nodes)
    # 수집하는 사람의 비콘은 시드가 등록한 활성 비콘이다
    assert plan.beacon in {beacon.beacon for beacon in seed.beacons if beacon.active}
    # 라벨은 배치의 기기와 none뿐이고, 한 번 돌리면 라벨마다 분할에 필요한 회차 수가 모인다
    counts = Counter(run.label for run in plan.runs)
    assert set(counts) == {*layout.nodes, "none"}
    assert min(counts.values()) >= fit.split.min_runs_per_label
    # 서버 설정 예시가 이 기기들의 시각 오차를 정해 둔다. 없는 기기의 관측은 쓰지 않는다
    server = tomllib.loads((CONFIG / "server.example.toml").read_text(encoding="utf-8"))
    assert set(layout.nodes) <= set(server["delivery"]["clock_skew_sec"])
