"""`nearpilot seed` (FR-01, FR-12A, FR-13, FR-14, FR-16, specs/core-ops-cli/spec.md 5절·6절 "AdminService와 seed").

실제 SQLite 파일에 `main(argv)`를 같은 프로세스에서 불러 확인한다. 기기 행은 서버가 MQTT 발표로만 만들므로
`announce_nodes`로 같은 경로(`upsert_announced_node`)를 거쳐 만든다. 기기의 종류는 시드와 관계가 없다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli import seed as seed_cli
from nearpilot.cli.seed import (
    SeedAccount,
    SeedBeacon,
    SeedFile,
    SeedFileError,
    SeedNode,
    SeedPlace,
    SeedSummary,
    apply_seed,
    load_seed_file,
)
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import (
    AccessMode,
    AdminWriteOutcome,
    AdminWriteReason,
    ApprovalMode,
    Role,
    TrustState,
    Visibility,
)
from nearpilot.shared.models import (
    Account,
    AccountIssue,
    AccountRegistration,
    AdminWriteResult,
    BeaconKey,
    BeaconRegistration,
    PolicyRevision,
    PolicySettings,
)
from nearpilot.shared.tokens import hash_token

UUID = "11111111-2222-3333-4444-555555555555"
NODE_IDS = ("locker-1", "light-1", "locker-2")

# 전체 시드. 소유자가 둘이고 장소가 둘이라서 기기마다 장소의 소유자가 행위자로 쓰이는지 확인한다.
ACCOUNTS = """
[[accounts]]
user_id = "owner"
name = "소유자"
role = "owner"

[[accounts]]
user_id = "owner2"
name = "카페 주인"
role = "owner"

[[accounts]]
user_id = "visitor"
name = "방문자"
role = "visitor"
"""
BEACONS = f"""
[[beacons]]
beacon_id = "beacon-visitor"
user_id = "visitor"
uuid = "{UUID}"
major = 1
minor = 7

[[beacons]]
beacon_id = "beacon-owner-spare"
user_id = "owner"
uuid = "{UUID}"
major = 2
minor = 1
active = false
"""
PLACES = """
[[places]]
place_id = "lab"
owner = "owner"
name = "실험실"
nodes = ["locker-1", "light-1"]

[[places]]
place_id = "cafe"
owner = "owner2"
name = "카페"
nodes = ["locker-2"]
"""
NODES = """
[[nodes]]
node_id = "locker-1"
trust = "trusted"
release_policy = { check_criteria = { id = "locker-return", version = 1 } }

[[nodes]]
node_id = "light-1"
trust = "trusted"

[[nodes]]
node_id = "locker-2"
trust = "trusted"
"""
POLICIES = """
[[policies]]
node_id = "locker-1"
visibility = "public"
mode = "restricted"
approver = "owner"
approval_mode = "time_window"
approval_ttl_sec = 3600
max_uses = 3

[[policies]]
node_id = "light-1"
visibility = "public"
mode = "open"
approval_mode = "one_time"

[[policies]]
node_id = "locker-2"
visibility = "private"
mode = "restricted"
approver = "owner2"
approval_mode = "one_time"
approval_ttl_sec = 600
"""
FULL_SEED = ACCOUNTS + BEACONS + PLACES + NODES + POLICIES
# 기기 이름이 있는 전체 시드. locker-1과 light-1에만 이름을 정하고 locker-2는 이름을 정하지 않는다.
NAMED_SEED = FULL_SEED.replace(
    'node_id = "locker-1"\ntrust = "trusted"\n', 'node_id = "locker-1"\ntrust = "trusted"\nname = "사물함 1"\n', 1
).replace(
    'node_id = "light-1"\ntrust = "trusted"\n', 'node_id = "light-1"\ntrust = "trusted"\nname = "조명 1"\n', 1)

GHOST_NODE = '\n[[nodes]]\nnode_id = "ghost"\n'
GHOST_POLICY = '\n[[policies]]\nnode_id = "ghost"\nvisibility = "public"\nmode = "open"\napproval_mode = "one_time"\n'

TOKEN_LINE = re.compile(r"^  토큰: (\S+)  \(다시 보여 주지 않는다\)$")
TOKEN_MASK = "  토큰: <토큰>  (다시 보여 주지 않는다)"
STATE_TABLES = ("user", "beacon", "place", "node", "access_policy", "policy_revision")


def write_seed(server: Path, content: str | bytes, name: str = "demo.toml") -> Path:
    """`config/seed/<name>`을 쓰고 경로를 돌려준다."""
    folder = server / "config" / "seed"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


@contextmanager
def opened(server: Path):
    conn = connect(str(server / "var" / "nearpilot.db"))
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def repository(server: Path):
    with opened(server) as conn:
        yield SqliteRepository(conn)


def query(server: Path, sql: str, *params) -> list[tuple]:
    with opened(server) as conn:
        return [tuple(row) for row in conn.execute(sql, params)]


def state(server: Path) -> dict[str, list[tuple]]:
    """시드가 쓰는 표의 내용 전체 (user.token_hash 포함)."""
    return {table: query(server, f"SELECT * FROM {table} ORDER BY 1, 2") for table in STATE_TABLES}


def everything(server: Path) -> dict[str, list[tuple]]:
    """DB의 모든 표의 내용 전체 (감사 로그 포함)."""
    names = [name for (name,) in query(
        server, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: query(server, f"SELECT * FROM {name} ORDER BY rowid") for name in names}


def printed_tokens(out: str) -> dict[str, str]:
    """`계정 X: 등록` 바로 아래의 토큰 줄을 `{user_id: 토큰}`으로 읽는다."""
    lines = out.splitlines()
    tokens = {}
    for line, following in zip(lines, lines[1:]):
        account, token = re.fullmatch(r"계정 (\S+): 등록", line), TOKEN_LINE.match(following)
        if account and token:
            tokens[account[1]] = token[1]
    return tokens


def masked_lines(out: str) -> list[str]:
    """토큰 원문은 매번 달라서 토큰 줄의 값을 가린다."""
    return [TOKEN_MASK if TOKEN_LINE.match(line) else line for line in out.splitlines()]


INVALID_CASES = [
    pytest.param(FULL_SEED + "\n[extra]\nvalue = 1\n", ("extra: 알 수 없는 키",), id="unknown-top-level-key"),
    pytest.param(FULL_SEED.replace('name = "소유자"\n', 'name = "소유자"\nnickname = "o"\n', 1),
                 ("accounts[0].nickname: 알 수 없는 키",), id="unknown-row-key"),
    pytest.param(FULL_SEED.replace('role = "owner"\n', "", 1),
                 ("accounts[0].role: 필수 키가 없다",), id="missing-required-key"),
    pytest.param(FULL_SEED.replace('role = "owner"', 'role = "admin"', 1),
                 ("accounts[0].role: ", "중 하나여야 한다"), id="invalid-role"),
    pytest.param(FULL_SEED + GHOST_NODE,
                 ("nodes[3].node_id: 어떤 [[places]] 의 nodes 에도 없다 (ghost)",), id="node-not-in-places"),
    pytest.param(FULL_SEED + GHOST_POLICY,
                 ("policies[3].node_id: 어떤 [[places]] 의 nodes 에도 없다 (ghost)",), id="policy-node-not-in-places"),
    pytest.param(FULL_SEED.replace("major = 1", "major = true", 1),
                 ("beacons[0].major: 정수여야 한다",), id="bool-major"),
    pytest.param(FULL_SEED.replace("approval_ttl_sec = 3600", "approval_ttl_sec = true", 1),
                 ("policies[0].approval_ttl_sec: 정수여야 한다",), id="bool-approval-ttl"),
    pytest.param(FULL_SEED.replace('user_id = "owner"', 'user_id = "  "', 1),
                 ("accounts[0].user_id: 빈 문자열이 아닌 문자열이어야 한다",), id="blank-user-id"),
    pytest.param(FULL_SEED.replace("minor = 7", 'minor = 7\nactive = "yes"', 1),
                 ("beacons[0].active: true 나 false 여야 한다",), id="active-not-bool"),
    pytest.param(FULL_SEED.replace('release_policy = { check_criteria = { id = "locker-return", version = 1 } }',
                                   'release_policy = "x"', 1),
                 ("nodes[0].release_policy: 표여야 한다",), id="release-policy-not-table"),
    pytest.param(FULL_SEED.replace('node_id = "light-1"\ntrust = "trusted"\n',
                                   'node_id = "light-1"\ntrust = "trusted"\nname = 7\n', 1),
                 ("nodes[1].name: 빈 문자열이 아닌 문자열이어야 한다",), id="node-name-not-string"),
    pytest.param(FULL_SEED.replace('node_id = "light-1"\ntrust = "trusted"\n',
                                   'node_id = "light-1"\ntrust = "trusted"\nname = "  "\n', 1),
                 ("nodes[1].name: 빈 문자열이 아닌 문자열이어야 한다",), id="node-name-blank"),
    pytest.param(FULL_SEED.replace('nodes = ["locker-2"]', 'nodes = ["locker-2", 3]', 1),
                 ("places[1].nodes: 빈 문자열이 아닌 문자열의 배열이어야 한다",), id="node-id-not-string"),
    pytest.param(FULL_SEED.replace('visibility = "private"', 'visibility = "secret"', 1),
                 ("policies[2].visibility: ", "중 하나여야 한다"), id="invalid-visibility"),
    pytest.param(FULL_SEED.replace('nodes = ["locker-1", "light-1"]', 'nodes = "locker-1"', 1),
                 ("places[0].nodes: 빈 문자열이 아닌 문자열의 배열이어야 한다",), id="nodes-not-array"),
    pytest.param(FULL_SEED + '\n[[accounts]]\nuser_id = "owner"\nname = "또"\nrole = "visitor"\n',
                 ("accounts[3].user_id: 중복이다 (accounts[0].user_id 와 같다: owner)",), id="duplicate-user-id"),
    pytest.param(FULL_SEED + '\n[[beacons]]\nbeacon_id = "beacon-visitor"\nuser_id = "owner"\nuuid = "x"\nmajor = 3\nminor = 3\n',
                 ("beacons[2].beacon_id: 중복이다 (beacons[0].beacon_id 와 같다: beacon-visitor)",),
                 id="duplicate-beacon-id"),
    pytest.param(FULL_SEED + '\n[[places]]\nplace_id = "lab"\nowner = "owner"\nname = "또"\nnodes = []\n',
                 ("places[2].place_id: 중복이다 (places[0].place_id 와 같다: lab)",), id="duplicate-place-id"),
    pytest.param(FULL_SEED + '\n[[nodes]]\nnode_id = "light-1"\n',
                 ("nodes[3].node_id: 중복이다 (nodes[1].node_id 와 같다: light-1)",), id="duplicate-node-id-in-nodes"),
    pytest.param(FULL_SEED + GHOST_POLICY.replace("ghost", "light-1"),
                 ("policies[3].node_id: 중복이다 (policies[1].node_id 와 같다: light-1)",),
                 id="duplicate-node-id-in-policies"),
    pytest.param(FULL_SEED.replace('nodes = ["locker-2"]', 'nodes = ["locker-2", "locker-1"]', 1),
                 ("places[1].nodes[1]: 중복이다 (places[0].nodes[0] 와 같다: locker-1)",), id="node-in-two-places"),
    pytest.param(FULL_SEED.replace('nodes = ["locker-1", "light-1"]', 'nodes = ["locker-1", "locker-1"]', 1),
                 ("places[0].nodes[1]: 중복이다 (places[0].nodes[0] 와 같다: locker-1)",),
                 id="node-twice-in-one-place"),
    pytest.param("accounts = [1]\n", ("accounts: [[accounts]] 표 배열이어야 한다",), id="accounts-not-array-of-tables"),
    pytest.param("[[accounts]\n", ("TOML 이 아니다",), id="toml-syntax"),
    pytest.param(b"\xff\xfe[[accounts]]\n", ("TOML 이 아니다",), id="not-utf8"),
]


@pytest.mark.parametrize("content, expected", INVALID_CASES)
def test_FR12A_seed_invalid_file_writes_nothing(server_dir, run_cli, content, expected):
    write_seed(server_dir, content)

    for options in ([], ["--dry-run"]):  # 검사는 반영과 dry-run이 같다
        code, out, err = run_cli(["seed", "demo.toml", *options], cwd=server_dir)

        assert code == EXIT_INVALID == 2
        assert out == ""
        assert err.startswith("시드 파일 오류: ")
        for fragment in expected:
            assert fragment in err
        assert not (server_dir / "var").exists()  # DB를 열기 전에 막았다


def test_FR12A_seed_invalid_file_lists_every_problem(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, *NODE_IDS)  # DB가 이미 있어도 아무것도 쓰지 않는다
    before = everything(server_dir)
    content = (FULL_SEED.replace('role = "owner"', 'role = "admin"', 1).replace("major = 1", "major = true", 1)
               + GHOST_NODE)
    write_seed(server_dir, content)

    code, out, err = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_INVALID and out == ""
    problems = err.strip().removeprefix("시드 파일 오류: ").split("; ")
    assert [problem.split(": ")[0] for problem in problems] == ["accounts[0].role", "beacons[0].major", "nodes[3].node_id"]
    assert everything(server_dir) == before


def test_FR12A_seed_is_rerunnable(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, *NODE_IDS)
    write_seed(server_dir, FULL_SEED)

    code, first_out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)
    assert code == EXIT_OK
    assert len(printed_tokens(first_out)) == 3
    assert first_out.splitlines()[-1] == "완료: 반영 14, 변경 없음 0, 거부 0"
    after_first = state(server_dir)
    assert len(after_first["user"]) == 3 and len(after_first["policy_revision"]) == 3
    audit_first = audit_count(server_dir)

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_OK
    assert "토큰" not in out  # 새 토큰을 만들지도, 보여 주지도 않는다
    lines = out.splitlines()
    for user_id in ("owner", "owner2", "visitor"):
        assert f"계정 {user_id}: 이미 있음" in lines
    for node_id in NODE_IDS:
        assert f"정책 {node_id}: 변경 없음 (버전 1)" in lines
    # 비콘, 장소, 기기 신뢰, 종료 정책은 같은 값을 다시 쓴다 (반영으로 센다)
    assert lines[-1] == "완료: 반영 8, 변경 없음 6, 거부 0"
    assert state(server_dir) == after_first  # user(token_hash 포함), policy_revision 행 수와 내용이 같다
    assert audit_count(server_dir) == audit_first + 8  # 다시 반영한 8개만 감사 행을 남긴다


def audit_count(server: Path) -> int:
    conn = sqlite3.connect(server / "var" / "nearpilot.db")
    try:
        return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        conn.close()


def test_FR12A_seed_dry_run_writes_nothing(server_dir, run_cli, announce_nodes):
    write_seed(server_dir, FULL_SEED)

    code, out, err = run_cli(["seed", "demo.toml", "--dry-run"], cwd=server_dir)

    assert code == EXIT_OK and err == ""
    expected = [
        "(dry-run) 계정 owner 등록",
        "(dry-run) 계정 owner2 등록",
        "(dry-run) 계정 visitor 등록",
        "(dry-run) 비콘 beacon-visitor (visitor) 등록",
        "(dry-run) 비콘 beacon-owner-spare (owner) 등록",
        "(dry-run) 장소 lab 배치 (기기 2대)",
        "(dry-run) 장소 cafe 배치 (기기 1대)",
        "(dry-run) 기기 locker-1 신뢰 trusted",
        "(dry-run) 기기 locker-1 종료 정책 반영",
        "(dry-run) 기기 light-1 신뢰 trusted",
        "(dry-run) 기기 locker-2 신뢰 trusted",
        "(dry-run) 정책 locker-1 반영",
        "(dry-run) 정책 light-1 반영",
        "(dry-run) 정책 locker-2 반영",
    ]
    assert out.splitlines() == expected
    assert not (server_dir / "var").exists()  # DB 파일도 폴더도 만들지 않는다

    # DB가 이미 있어도 쓰지 않고, DB를 보지 않으므로 같은 줄을 쓴다
    announce_nodes(server_dir, *NODE_IDS)
    before = everything(server_dir)
    code, second_out, _ = run_cli(["seed", "demo.toml", "--dry-run"], cwd=server_dir)
    assert code == EXIT_OK and second_out.splitlines() == expected
    assert everything(server_dir) == before


def test_FR13_seed_unknown_node_reports_and_continues(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, "locker-1", "locker-2")  # light-1은 서버가 아직 발표를 받지 못했다
    write_seed(server_dir, FULL_SEED)

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_FAILED
    lines = out.splitlines()
    assert lines.count("기기 light-1: DB에 없다 (서버가 발표를 받은 뒤 다시 실행한다)") == 1
    assert "장소 lab: 배치 (기기 1대)" in lines and "장소 cafe: 배치 (기기 1대)" in lines
    # light-1은 장소, 신뢰, 정책 세 곳에 있지만 거부는 한 번만 센다
    assert lines[-1] == "완료: 반영 12, 변경 없음 0, 거부 1"
    assert not any("기기 light-1 신뢰" in line or "정책 light-1" in line for line in lines)
    # 앞서 반영한 항목과 나머지 기기는 유지한다
    assert query(server_dir, "SELECT user_id FROM user ORDER BY 1") == [("owner",), ("owner2",), ("visitor",)]
    assert query(server_dir, "SELECT place_id FROM place ORDER BY 1") == [("cafe",), ("lab",)]
    assert query(server_dir, "SELECT node_id, place_id, trust_state FROM node ORDER BY 1") == [
        ("locker-1", "lab", "trusted"), ("locker-2", "cafe", "trusted")]
    assert query(server_dir, "SELECT node_id FROM access_policy ORDER BY 1") == [("locker-1",), ("locker-2",)]


def test_FR13_seed_rerun_after_node_announced_completes(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, "locker-1", "locker-2")
    write_seed(server_dir, FULL_SEED)
    assert run_cli(["seed", "demo.toml"], cwd=server_dir)[0] == EXIT_FAILED
    announce_nodes(server_dir, "light-1")

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_OK
    assert "토큰" not in out
    lines = out.splitlines()
    assert "장소 lab: 배치 (기기 2대)" in lines
    assert "기기 light-1 신뢰: trusted" in lines
    assert "정책 light-1: 버전 1" in lines
    assert lines[-1] == "완료: 반영 9, 변경 없음 5, 거부 0"
    assert query(server_dir, "SELECT node_id, place_id, trust_state FROM node ORDER BY 1") == [
        ("light-1", "lab", "trusted"), ("locker-1", "lab", "trusted"), ("locker-2", "cafe", "trusted")]
    assert query(server_dir, "SELECT COUNT(*) FROM access_policy") == [(3,)]


def test_FR13_seed_places_are_created_before_nodes_are_announced(server_dir, run_cli, announce_nodes):
    write_seed(server_dir, FULL_SEED)  # 서버가 아직 어떤 기기의 발표도 받지 못했고, DB 파일도 아직 없다

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_FAILED
    lines = out.splitlines()
    for node_id in NODE_IDS:
        assert f"기기 {node_id}: DB에 없다 (서버가 발표를 받은 뒤 다시 실행한다)" in lines
    assert "장소 lab: 배치 (기기 0대)" in lines and "장소 cafe: 배치 (기기 0대)" in lines
    assert lines[-1] == "완료: 반영 7, 변경 없음 0, 거부 3"  # 계정 3 + 비콘 2 + 장소 2, 기기 3대는 한 번씩만 센다
    assert query(server_dir, "SELECT place_id FROM place ORDER BY 1") == [("cafe",), ("lab",)]
    assert query(server_dir, "SELECT COUNT(*) FROM node") == [(0,)]
    assert query(server_dir, "SELECT COUNT(*) FROM access_policy") == [(0,)]

    announce_nodes(server_dir, *NODE_IDS)
    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_OK and "토큰" not in out
    assert out.splitlines()[-1] == "완료: 반영 11, 변경 없음 3, 거부 0"
    assert query(server_dir, "SELECT node_id, place_id, trust_state FROM node ORDER BY 1") == [
        ("light-1", "lab", "trusted"), ("locker-1", "lab", "trusted"), ("locker-2", "cafe", "trusted")]
    assert query(server_dir, "SELECT COUNT(*) FROM access_policy") == [(3,)]


def test_FR01_seed_duplicate_beacon_rejected(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, *NODE_IDS)
    with opened(server_dir) as conn:  # 다른 계정이 같은 (uuid, major, minor)를 이미 쓰고 있다
        admin = SqliteAdminRepository(conn)
        admin.register_account(AccountRegistration("other", "다른 계정", Role.VISITOR, hash_token("other-token")))
        admin.register_beacon(BeaconRegistration("beacon-other", "other", BeaconKey(UUID, 1, 7), True))
    write_seed(server_dir, FULL_SEED)

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_FAILED
    lines = out.splitlines()
    assert "비콘 beacon-visitor (visitor): 거부 (BEACON_ALREADY_REGISTERED)" in lines
    assert lines[-1] == "완료: 반영 13, 변경 없음 0, 거부 1"  # 거부된 항목만 빠지고 나머지는 계속한다
    with repository(server_dir) as repo:
        assert repo.beacons_of("other") == [BeaconKey(UUID, 1, 7)]  # 기존 활성 비콘을 유지한다
        assert repo.beacons_of("visitor") == []
    assert query(server_dir, "SELECT beacon_id, user_id, is_active FROM beacon ORDER BY 1") == [
        ("beacon-other", "other", 1), ("beacon-owner-spare", "owner", 0)]
    assert query(server_dir, "SELECT COUNT(*) FROM user") == [(4,)]  # other + 시드의 세 계정


def test_FR01_seed_token_printed_once_hash_stored(server_dir, run_cli, announce_nodes, caplog):
    announce_nodes(server_dir, *NODE_IDS)
    write_seed(server_dir, FULL_SEED)

    code, first_out, first_err = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_OK
    tokens = printed_tokens(first_out)
    assert set(tokens) == {"owner", "owner2", "visitor"} and len(set(tokens.values())) == 3
    assert first_out.count("토큰:") == 3
    hashes = dict(query(server_dir, "SELECT user_id, token_hash FROM user"))
    for user_id, token in tokens.items():
        assert hashes[user_id] == hash_token(token)
    # 원문은 stdout에만 있다: stderr, 로그, DB(감사 포함), 서버 폴더의 어떤 파일에도 없다
    audit = [payload for (payload,) in query(server_dir, "SELECT payload_json FROM audit_log")]
    assert any('"register_account"' in payload for payload in audit)  # 아래 검사가 빈 목록을 보는 것이 아니다
    for token in tokens.values():
        assert token not in first_err and token not in caplog.text
        assert not any(token in payload for payload in audit)
        for path in server_dir.rglob("*"):
            if path.is_file():
                assert token.encode("utf-8") not in path.read_bytes(), path

    code, second_out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_OK
    assert "토큰" not in second_out and printed_tokens(second_out) == {}
    assert dict(query(server_dir, "SELECT user_id, token_hash FROM user")) == hashes  # 해시도 그대로다


def test_FR12A_seed_applies_all_sections(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, *NODE_IDS)
    write_seed(server_dir, FULL_SEED)

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_OK
    assert masked_lines(out) == [
        "계정 owner: 등록", TOKEN_MASK,
        "계정 owner2: 등록", TOKEN_MASK,
        "계정 visitor: 등록", TOKEN_MASK,
        "비콘 beacon-visitor (visitor): 등록",
        "비콘 beacon-owner-spare (owner): 등록",
        "장소 lab: 배치 (기기 2대)",
        "장소 cafe: 배치 (기기 1대)",
        "기기 locker-1 신뢰: trusted",
        "기기 locker-1 종료 정책: 반영",
        "기기 light-1 신뢰: trusted",
        "기기 locker-2 신뢰: trusted",
        "정책 locker-1: 버전 1",
        "정책 light-1: 버전 1",
        "정책 locker-2: 버전 1",
        "완료: 반영 14, 변경 없음 0, 거부 0",
    ]
    tokens = printed_tokens(out)
    assert query(server_dir, "SELECT user_id, name, role FROM user ORDER BY 1") == [
        ("owner", "소유자", "owner"), ("owner2", "카페 주인", "owner"), ("visitor", "방문자", "visitor")]
    assert query(server_dir, "SELECT place_id, owner_user_id, name FROM place ORDER BY 1") == [
        ("cafe", "owner2", "카페"), ("lab", "owner", "실험실")]
    assert query(server_dir, "SELECT beacon_id, user_id, uuid, major, minor, is_active FROM beacon ORDER BY 1") == [
        ("beacon-owner-spare", "owner", UUID, 2, 1, 0), ("beacon-visitor", "visitor", UUID, 1, 7, 1)]
    with repository(server_dir) as repo:
        nodes = {node.node_id: node for node in repo.nodes()}
        assert {node_id: (node.trust_state, node.place_id) for node_id, node in nodes.items()} == {
            "locker-1": (TrustState.TRUSTED, "lab"),
            "light-1": (TrustState.TRUSTED, "lab"),
            "locker-2": (TrustState.TRUSTED, "cafe"),
        }
        assert nodes["locker-1"].release_policy == {"check_criteria": {"id": "locker-return", "version": 1}}
        assert nodes["light-1"].release_policy == {} and nodes["locker-2"].release_policy == {}
        assert repo.current_policy("locker-1") == PolicyRevision(
            "pol-locker-1", 1, "locker-1", Visibility.PUBLIC, AccessMode.RESTRICTED, "owner",
            ApprovalMode.TIME_WINDOW, 3600, 3)
        assert repo.current_policy("light-1") == PolicyRevision(
            "pol-light-1", 1, "light-1", Visibility.PUBLIC, AccessMode.OPEN, None, ApprovalMode.ONE_TIME, None, None)
        assert repo.current_policy("locker-2") == PolicyRevision(
            "pol-locker-2", 1, "locker-2", Visibility.PRIVATE, AccessMode.RESTRICTED, "owner2",
            ApprovalMode.ONE_TIME, 600, None)
        # 출력한 토큰으로 계정을 알아볼 수 있고, 기기마다 장소의 소유자가 소유자로 기록된다
        assert repo.account_by_token_hash(hash_token(tokens["owner"])) == Account("owner", Role.OWNER)
        assert repo.account_by_token_hash(hash_token(tokens["visitor"])) == Account("visitor", Role.VISITOR)
        assert repo.beacons_of("visitor") == [BeaconKey(UUID, 1, 7)]
        assert repo.is_place_owner("owner", "locker-1") and repo.is_place_owner("owner", "light-1")
        assert repo.is_place_owner("owner2", "locker-2") and not repo.is_place_owner("owner", "locker-2")


def test_FR12A_seed_node_name_applies_and_summarizes(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, *NODE_IDS)
    write_seed(server_dir, NAMED_SEED)

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_OK
    lines = masked_lines(out)
    # 이름은 같은 기기의 신뢰·종료 정책 다음 줄이다. 이름을 정하지 않은 기기는 건드리지 않는다
    assert lines[lines.index("기기 locker-1 종료 정책: 반영") + 1] == "기기 locker-1 이름: 사물함 1"
    assert lines[lines.index("기기 light-1 신뢰: trusted") + 1] == "기기 light-1 이름: 조명 1"
    assert not any("locker-2 이름" in line for line in lines)
    assert lines[-1] == "완료: 반영 16, 변경 없음 0, 거부 0"
    assert query(server_dir, "SELECT node_id, name FROM node ORDER BY 1") == [
        ("light-1", "조명 1"), ("locker-1", "사물함 1"), ("locker-2", None)]
    audited = [json.loads(payload) for (payload,) in query(server_dir, "SELECT payload_json FROM audit_log ORDER BY rowid")
               if "set_node_name" in payload]
    assert audited == [
        {"event": "set_node_name", "node_id": "locker-1", "name": "사물함 1"},
        {"event": "set_node_name", "node_id": "light-1", "name": "조명 1"},
    ]

    # 다시 실행해도 신뢰나 종료 정책처럼 같은 값을 다시 쓰고 반영으로 센다
    code, second_out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)
    assert code == EXIT_OK
    assert second_out.splitlines()[-1] == "완료: 반영 10, 변경 없음 6, 거부 0"
    assert query(server_dir, "SELECT node_id, name FROM node ORDER BY 1") == [
        ("light-1", "조명 1"), ("locker-1", "사물함 1"), ("locker-2", None)]


def test_FR12A_seed_node_name_dry_run_writes_a_line(server_dir, run_cli):
    write_seed(server_dir, FULL_SEED, name="plain.toml")
    write_seed(server_dir, NAMED_SEED)

    code, plain, _ = run_cli(["seed", "plain.toml", "--dry-run"], cwd=server_dir)
    assert code == EXIT_OK
    code, named, _ = run_cli(["seed", "demo.toml", "--dry-run"], cwd=server_dir)

    assert code == EXIT_OK
    expected = plain.splitlines()
    expected.insert(expected.index("(dry-run) 기기 locker-1 종료 정책 반영") + 1, "(dry-run) 기기 locker-1 이름 사물함 1")
    expected.insert(expected.index("(dry-run) 기기 light-1 신뢰 trusted") + 1, "(dry-run) 기기 light-1 이름 조명 1")
    assert named.splitlines() == expected
    assert not (server_dir / "var").exists()  # DB 파일도 폴더도 만들지 않는다


def test_FR13_seed_node_name_unknown_node_is_rejected_once(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, "locker-1", "locker-2")  # light-1은 서버가 아직 발표를 받지 못했다
    write_seed(server_dir, NAMED_SEED)

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_FAILED
    lines = out.splitlines()
    assert lines.count("기기 light-1: DB에 없다 (서버가 발표를 받은 뒤 다시 실행한다)") == 1
    assert "기기 locker-1 이름: 사물함 1" in lines
    assert not any("light-1 이름" in line for line in lines)  # light-1의 이름은 건너뛰고 거부는 한 번만 센다
    assert lines[-1] == "완료: 반영 13, 변경 없음 0, 거부 1"
    assert query(server_dir, "SELECT node_id, name FROM node ORDER BY 1") == [("locker-1", "사물함 1"), ("locker-2", None)]


def test_FR12A_seed_apply_node_name_rejection_is_reported_like_other_keys():
    """DB가 이름을 거부하면 신뢰나 종료 정책과 같은 줄 모양으로 쓰고 거부로 센다."""

    class RejectingNames:
        def assign_place(self, actor_user_id, req):
            return AdminWriteResult(AdminWriteOutcome.APPLIED, AdminWriteReason.OK)

        def set_node_name(self, actor_user_id, node_id, name):
            return AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.NODE_NOT_FOUND)

    class OneNode:
        def nodes(self):
            return [SimpleNamespace(node_id="locker-1")]

    lines: list[str] = []
    seed = SeedFile((), (), (SeedPlace("lab", "owner", "실험실", ("locker-1",)),),
                    (SeedNode("locker-1", None, None, "사물함 1"),), ())

    summary = apply_seed(seed, RejectingNames(), OneNode(), lines.append)

    assert lines == ["장소 lab: 배치 (기기 1대)", "기기 locker-1 이름: 거부 (NODE_NOT_FOUND)",
                     "완료: 반영 1, 변경 없음 0, 거부 1"]
    assert summary == SeedSummary(applied=1, unchanged=0, rejected=1)


def test_FR14_seed_changes_apply_on_rerun(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, *NODE_IDS)
    write_seed(server_dir, FULL_SEED)
    assert run_cli(["seed", "demo.toml"], cwd=server_dir)[0] == EXIT_OK
    # 정책의 max_uses를 바꾸고, 종료 정책을 빈 표로 바꾼다 (빈 표는 "비우라"는 뜻이다)
    changed = FULL_SEED.replace("max_uses = 3", "max_uses = 5").replace(
        'release_policy = { check_criteria = { id = "locker-return", version = 1 } }', "release_policy = {}")
    write_seed(server_dir, changed)

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_OK
    lines = out.splitlines()
    assert "정책 locker-1: 버전 2" in lines
    assert "정책 light-1: 변경 없음 (버전 1)" in lines and "정책 locker-2: 변경 없음 (버전 1)" in lines
    assert "기기 locker-1 종료 정책: 반영" in lines
    assert query(server_dir, "SELECT policy_id, version, max_uses FROM policy_revision ORDER BY 1, 2") == [
        ("pol-light-1", 1, None), ("pol-locker-1", 1, 3), ("pol-locker-1", 2, 5), ("pol-locker-2", 1, None)]
    assert query(server_dir, "SELECT current_version FROM access_policy WHERE node_id = 'locker-1'") == [(2,)]
    with repository(server_dir) as repo:
        assert [node.release_policy for node in repo.nodes() if node.node_id == "locker-1"] == [{}]


def test_FR12A_seed_rejects_taking_over_another_owners_node(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, *NODE_IDS)
    write_seed(server_dir, FULL_SEED)
    assert run_cli(["seed", "demo.toml"], cwd=server_dir)[0] == EXIT_OK
    before = state(server_dir)
    # owner2가 owner의 장소에 있는 locker-1을 자기 장소에 넣고 신뢰와 정책을 바꾸려 한다
    write_seed(server_dir, """
[[places]]
place_id = "lab2"
owner = "owner2"
name = "다른 실험실"
nodes = ["locker-1"]

[[nodes]]
node_id = "locker-1"
trust = "revoked"

[[policies]]
node_id = "locker-1"
visibility = "private"
mode = "restricted"
approver = "owner2"
approval_mode = "one_time"
""", name="takeover.toml")

    code, out, _ = run_cli(["seed", "takeover.toml"], cwd=server_dir)

    assert code == EXIT_FAILED
    assert out.splitlines() == [
        "장소 lab2: 거부 (NOT_PLACE_OWNER)",
        "기기 locker-1 신뢰: 거부 (NOT_PLACE_OWNER)",
        "정책 locker-1: 거부 (NOT_PLACE_OWNER)",
        "완료: 반영 0, 변경 없음 0, 거부 3",
    ]
    assert state(server_dir) == before


def test_FR12A_seed_unknown_account_reference_is_rejected_by_db(server_dir, run_cli, announce_nodes):
    """계정 참조는 파일 안에서 검사하지 않는다 (이미 DB에 있는 계정을 쓸 수 있다). 없는 계정은 DB가 거부한다."""
    announce_nodes(server_dir, "locker-1", "light-1")
    write_seed(server_dir, """
[[accounts]]
user_id = "owner"
name = "소유자"
role = "owner"

[[beacons]]
beacon_id = "beacon-ghost"
user_id = "ghost"
uuid = "x"
major = 9
minor = 9

[[places]]
place_id = "lab"
owner = "owner"
name = "실험실"
nodes = ["locker-1"]

[[places]]
place_id = "typo"
owner = "ownr"
name = "오타"
nodes = ["light-1"]

[[nodes]]
node_id = "light-1"
trust = "trusted"

[[policies]]
node_id = "locker-1"
visibility = "public"
mode = "restricted"
approver = "ghost"
approval_mode = "one_time"
""")

    code, out, _ = run_cli(["seed", "demo.toml"], cwd=server_dir)

    assert code == EXIT_FAILED
    assert masked_lines(out) == [
        "계정 owner: 등록", TOKEN_MASK,
        "비콘 beacon-ghost (ghost): 거부 (ACCOUNT_NOT_FOUND)",
        "장소 lab: 배치 (기기 1대)",
        "장소 typo: 거부 (ACCOUNT_NOT_FOUND)",
        "기기 light-1 신뢰: 거부 (NOT_PLACE_OWNER)",  # 장소가 만들어지지 않아서 소유자가 아니다
        "정책 locker-1: 거부 (ACCOUNT_NOT_FOUND)",
        "완료: 반영 2, 변경 없음 0, 거부 4",
    ]


def test_FR12A_seed_place_redeclared_with_other_name_is_rejected(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, *NODE_IDS)
    write_seed(server_dir, FULL_SEED)
    assert run_cli(["seed", "demo.toml"], cwd=server_dir)[0] == EXIT_OK
    before = state(server_dir)
    # 이미 있는 장소를 다른 이름으로 다시 선언한다. DB가 충돌로 거부하고, 기존 장소는 그대로다.
    write_seed(server_dir, """
[[places]]
place_id = "lab"
owner = "owner"
name = "다른 이름"
nodes = ["locker-1"]
""", name="rename.toml")

    code, out, _ = run_cli(["seed", "rename.toml"], cwd=server_dir)

    assert code == EXIT_FAILED
    assert out.splitlines() == ["장소 lab: 거부 (STATE_CONFLICT)", "완료: 반영 0, 변경 없음 0, 거부 1"]
    assert state(server_dir) == before


def test_FR12A_seed_apply_reports_unexpected_account_rejection():
    """실제 DB는 계정을 이미 있다는 사유로만 거부한다. 그 밖의 사유도 거부로 쓰고 센다."""

    class RejectingService:
        def register_account(self, user_id, name, role):
            return AccountIssue(AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.STATE_CONFLICT), None)

    class NoNodes:
        def nodes(self):
            return []

    lines: list[str] = []
    seed = SeedFile((SeedAccount("owner", "소유자", Role.OWNER),), (), (), (), ())

    summary = apply_seed(seed, RejectingService(), NoNodes(), lines.append)

    assert lines == ["계정 owner: 거부 (STATE_CONFLICT)", "완료: 반영 0, 변경 없음 0, 거부 1"]
    assert summary == SeedSummary(applied=0, unchanged=0, rejected=1)


def test_FR12A_seed_load_file_parses_all_sections(tmp_path):
    seed = load_seed_file(write_seed(tmp_path, FULL_SEED))

    assert seed.accounts == (
        SeedAccount("owner", "소유자", Role.OWNER),
        SeedAccount("owner2", "카페 주인", Role.OWNER),
        SeedAccount("visitor", "방문자", Role.VISITOR),
    )
    assert seed.beacons == (
        SeedBeacon("beacon-visitor", "visitor", BeaconKey(UUID, 1, 7), True),  # active 기본값
        SeedBeacon("beacon-owner-spare", "owner", BeaconKey(UUID, 2, 1), False),
    )
    assert seed.places == (
        SeedPlace("lab", "owner", "실험실", ("locker-1", "light-1")),
        SeedPlace("cafe", "owner2", "카페", ("locker-2",)),
    )
    assert seed.nodes == (
        SeedNode("locker-1", TrustState.TRUSTED, {"check_criteria": {"id": "locker-return", "version": 1}}),
        SeedNode("light-1", TrustState.TRUSTED, None),
        SeedNode("locker-2", TrustState.TRUSTED, None),
    )
    assert seed.policies == (
        PolicySettings("locker-1", Visibility.PUBLIC, AccessMode.RESTRICTED, "owner", ApprovalMode.TIME_WINDOW, 3600, 3),
        PolicySettings("light-1", Visibility.PUBLIC, AccessMode.OPEN, None, ApprovalMode.ONE_TIME, None, None),
        PolicySettings("locker-2", Visibility.PRIVATE, AccessMode.RESTRICTED, "owner2", ApprovalMode.ONE_TIME, 600, None),
    )


def test_FR12A_seed_load_file_parses_node_name(tmp_path):
    seed = load_seed_file(write_seed(tmp_path, NAMED_SEED))

    assert seed.nodes == (
        SeedNode("locker-1", TrustState.TRUSTED, {"check_criteria": {"id": "locker-return", "version": 1}}, "사물함 1"),
        SeedNode("light-1", TrustState.TRUSTED, None, "조명 1"),
        SeedNode("locker-2", TrustState.TRUSTED, None, None),  # name 이 없으면 None 이다
    )


def test_FR12A_seed_load_file_defaults(tmp_path):
    seed = load_seed_file(write_seed(tmp_path, """
[[beacons]]
beacon_id = "b1"
user_id = "u1"
uuid = "x"
major = 0
minor = 0
active = false

[[places]]
place_id = "p1"
owner = "u1"
name = "n"
nodes = ["n1", "n2", "n3"]

[[nodes]]
node_id = "n1"
release_policy = {}

[[nodes]]
node_id = "n2"
trust = "revoked"
"""))

    assert seed.accounts == () and seed.policies == ()  # 표 배열은 모두 선택이다
    assert seed.beacons == (SeedBeacon("b1", "u1", BeaconKey("x", 0, 0), False),)  # 0도 정수다
    assert seed.nodes == (SeedNode("n1", None, {}), SeedNode("n2", TrustState.REVOKED, None))  # 빈 표와 없음은 다르다
    assert load_seed_file(write_seed(tmp_path, "", name="empty.toml")) == SeedFile((), (), (), (), ())


@pytest.mark.parametrize("fails", [False, True], ids=["success", "apply-raises"])
def test_FR12A_seed_closes_database_connection(server_dir, run_cli, announce_nodes, monkeypatch, fails):
    announce_nodes(server_dir, *NODE_IDS)
    write_seed(server_dir, FULL_SEED)
    connections = []
    real_open = seed_cli.open_write_db

    def spy(config):
        conn = real_open(config)
        connections.append(conn)
        return conn

    monkeypatch.setattr(seed_cli, "open_write_db", spy)
    if fails:
        def explode(*args, **kwargs):
            raise RuntimeError("반영 도중 실패")

        monkeypatch.setattr(seed_cli, "apply_seed", explode)
        with pytest.raises(RuntimeError):
            run_cli(["seed", "demo.toml"], cwd=server_dir)
    else:
        assert run_cli(["seed", "demo.toml"], cwd=server_dir)[0] == EXIT_OK

    [conn] = connections
    with pytest.raises(sqlite3.ProgrammingError):  # 닫힌 연결이다
        conn.execute("SELECT 1")


def test_FR16_seed_file_argument_rules(server_dir, run_cli):
    write_seed(server_dir, "", name="empty.toml")
    (server_dir / "elsewhere").mkdir()
    (server_dir / "elsewhere" / "custom.toml").write_text("", encoding="utf-8")

    # 이름만 주면 설정 파일 옆 seed/ 폴더에서 찾고, 없으면 그 폴더의 파일을 알려 준다
    code, out, err = run_cli(["seed", "missing.toml"], cwd=server_dir)
    assert code == EXIT_INVALID and out == ""
    assert err.startswith("시드 파일이 없다: ") and "missing.toml" in err and "empty.toml" in err
    assert run_cli(["seed", "empty.toml"], cwd=server_dir) == (EXIT_OK, "완료: 반영 0, 변경 없음 0, 거부 0\n", "")

    # 구분자가 있으면 실행 폴더 기준 경로다. 항목이 없는 시드도 유효하다
    assert run_cli(["seed", "elsewhere/custom.toml"], cwd=server_dir) == (
        EXIT_OK, "완료: 반영 0, 변경 없음 0, 거부 0\n", "")


def test_FR12A_seed_load_file_unreadable_is_seed_file_error(tmp_path):
    with pytest.raises(SeedFileError, match="읽을 수 없다"):
        load_seed_file(tmp_path / "missing.toml")


def test_FR16_seed_uses_config_option_after_subcommand(server_dir, run_cli, write_config):
    other = write_config(server_dir, name="other.toml",
                         database={"path": "../var/other.db", "audit_jsonl": "../var/other.jsonl"})
    write_seed(server_dir, ACCOUNTS)

    code, out, _ = run_cli(["seed", "demo.toml", "--config", str(other)], cwd=server_dir)

    assert code == EXIT_OK and len(printed_tokens(out)) == 3
    assert (server_dir / "var" / "other.db").exists() and not (server_dir / "var" / "nearpilot.db").exists()


def test_FR16_seed_help_describes_arguments(server_dir, run_cli):
    code, out, err = run_cli(["seed", "--help"], cwd=server_dir)

    assert code == EXIT_OK and err == ""
    assert "FILE" in out and "--dry-run" in out and "--config" in out
    code, out, _ = run_cli(["--help"], cwd=server_dir)
    assert code == EXIT_OK and "seed" in out and "serve" in out
