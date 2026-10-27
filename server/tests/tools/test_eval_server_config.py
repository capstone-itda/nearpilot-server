"""NFR-13 서버 지표 평가 설정 읽기와 검증 (specs/db-evaluation-server-metrics/spec.md 2절 "입력").

`bad` 설정은 최소 설정을 부분별로 바꿔서 만든다. 부분마다 TOML 위치(최상위 키는 표보다 앞)를 지켜야 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.evaluation.server_config import EvalConfigError, load_eval_config

SERVER = Path(__file__).resolve().parents[2]
EXAMPLE = SERVER / "config" / "eval" / "server-metrics.example.toml"

TOP = ('seed = 1\nmodel_root = "models"\nmodel_version = "v1"\n'
       'actuation_sec = 0.1\nstartup_timeout_sec = 30.0\n')
BEACON = '[beacon]\nuuid = "11111111-2222-3333-4444-555555555555"\nmajor = 1\n'
OWNER = '[[users]]\nuser_id = "owner"\nrole = "owner"\nminor = 1\n'
VISITOR = '[[users]]\nuser_id = "visitor-1"\nrole = "visitor"\nminor = 2\n'
LOCKER = '[[nodes]]\nnode_id = "locker-1"\ncapability = "locker"\noccupancy = "rental"\n'
COUNTS = "[counts]\nm3_requests = 10\n"
ACCEPTANCE = "[acceptance]\nm1_max_wrong_rate = 0.025\nm7_max_median_sec = 1.0\n"
RSSI = "[rssi]\nnear = -53.0\n"


def write_config(tmp_path: Path, **parts: str) -> Path:
    """최소 설정에서 `parts`로 이름이 같은 부분을 바꿔 쓴다. `extra`는 맨 끝에 붙는다."""
    base = {"top": TOP, "beacon": BEACON, "users": OWNER + "\n" + VISITOR, "nodes": LOCKER, "counts": COUNTS,
            "acceptance": ACCEPTANCE, "rssi": RSSI, "extra": ""}
    path = tmp_path / "eval.toml"
    path.write_text("\n".join({**base, **parts}.values()), encoding="utf-8")
    return path


def test_NFR13_eval_config_loads_example():
    config = load_eval_config(EXAMPLE)

    # 상대 경로는 설정 파일이 있는 폴더 기준이고, 예시는 합성 모델 파일을 가리킨다
    assert config.model_root == (SERVER / "tests" / "core" / "proximity" / "fixtures" / "models").resolve()
    assert (config.model_root / "place-a" / f"{config.model_version}.json").is_file()
    assert [(user.user_id, user.role) for user in config.users] == [
        ("owner", "owner"), ("visitor-1", "visitor"), ("visitor-2", "visitor"), ("visitor-3", "visitor")]
    assert len({user.minor for user in config.users}) == len(config.users)
    assert [node.node_id for node in config.nodes] == [
        "locker-1", "locker-2", "locker-3", "light-1", "light-2", "locker-4"]
    assert {node.node_id for node in config.nodes if node.mode == "restricted"} == {"light-2"}
    assert {node.node_id for node in config.nodes if not node.trusted} == {"locker-4"}
    # docs/requirements.md §4 표의 횟수와 통과 조건. 지표 3의 클라이언트 수는 미결이라 초기값만 둔다
    assert {"m1_utterances": 100, "m3_requests": 2000, "m3_retransmits": 100, "m3_clients": 10, "m4_each": 50,
            "m5_each": 30, "m6_each": 30, "m7_server_requests": 100, "m9_sets": 50}.items() <= config.counts.items()
    assert {"m1_max_wrong_rate": 0.025, "m3_max_double_occupancy": 0, "m3_max_duplicate_execution": 1,
            "m7_max_median_sec": 1.0, "m7_max_p95_sec": 2.0}.items() <= config.acceptance.items()
    assert config.rssi["near"] > config.rssi["far"]
    with pytest.raises(TypeError):  # 지표를 구현하는 쪽이 설정 값을 바꾸지 못한다
        config.counts["m3_requests"] = 1


@pytest.mark.parametrize(("parts", "key"), [
    pytest.param({"top": TOP + "users = []\n", "users": ""}, "users", id="empty-users"),
    pytest.param({"nodes": LOCKER + "\n" + LOCKER}, "nodes[1].node_id", id="duplicate-node-id"),
    pytest.param({"top": TOP.replace("actuation_sec = 0.1", "actuation_sec = -0.1")}, "actuation_sec",
                 id="negative-time"),
    pytest.param({"top": TOP.replace("startup_timeout_sec = 30.0", "startup_timeout_sec = 0")},
                 "startup_timeout_sec", id="zero-time"),
    pytest.param({"counts": "[counts]\nm3_requests = 0\n"}, "counts.m3_requests", id="zero-count"),
    pytest.param({"acceptance": ACCEPTANCE.replace("0.025", "1.5")}, "acceptance.m1_max_wrong_rate",
                 id="rate-above-one"),
    pytest.param({"acceptance": ACCEPTANCE.replace("m7_max_median_sec = 1.0", "m7_max_median_sec = 0")},
                 "acceptance.m7_max_median_sec", id="zero-seconds-criterion"),
    pytest.param({"top": TOP + "bogus = 1\n"}, "bogus", id="unknown-key"),
    pytest.param({"top": TOP.replace("seed = 1\n", "")}, "seed", id="missing-key"),
    pytest.param({"users": VISITOR}, "owner", id="no-owner"),
    pytest.param({"users": OWNER + "\n" + OWNER.replace("minor = 1", "minor = 3")}, "users[1].user_id",
                 id="duplicate-user-id"),
    pytest.param({"users": OWNER + "\n" + VISITOR.replace("minor = 2", "minor = 1")}, "users[1].minor",
                 id="duplicate-minor"),
    pytest.param({"extra": "[server_settings]\nbogus = 1\n"}, "server_settings", id="unknown-server-setting"),
    pytest.param({"nodes": LOCKER.replace('"locker"', '"door"')}, "nodes[0].capability", id="bad-capability"),
    pytest.param({"nodes": LOCKER.replace("locker-1", "locker 1")}, "nodes[0].node_id", id="bad-node-id"),
    pytest.param({"beacon": BEACON.replace("11111111-2222-3333-4444-555555555555", "not-a-uuid")}, "beacon.uuid",
                 id="bad-beacon-uuid"),
])
def test_NFR13_eval_config_rejects_bad_values(tmp_path, parts, key):
    load_eval_config(write_config(tmp_path))  # 바꾸지 않은 설정은 읽힌다. 아래 오류는 바꾼 값 때문이다

    with pytest.raises(EvalConfigError, match=re.escape(key)):
        load_eval_config(write_config(tmp_path, **parts))


def test_NFR13_eval_config_reports_every_problem_at_once(tmp_path):
    path = write_config(tmp_path, counts="[counts]\nm3_requests = 0\n",
                        acceptance=ACCEPTANCE.replace("0.025", "1.5"))

    with pytest.raises(EvalConfigError) as caught:
        load_eval_config(path)

    assert "counts.m3_requests" in str(caught.value)
    assert "acceptance.m1_max_wrong_rate" in str(caught.value)


def test_NFR13_eval_config_rejects_missing_file(tmp_path):
    with pytest.raises(EvalConfigError, match="missing.toml"):
        load_eval_config(tmp_path / "missing.toml")
