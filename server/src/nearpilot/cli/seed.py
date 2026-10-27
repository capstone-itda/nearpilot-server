"""`nearpilot seed`: 시드 파일(TOML)로 계정, 비콘, 장소, 기기 신뢰, 종료 정책, 접근 정책을 넣는다
(specs/core-ops-cli/spec.md 6절 "AdminService와 seed").

SQL을 직접 쓰지 않는다. 모든 쓰기는 `AdminService`를 거쳐 `AdminRepository`로 간다 (#30).
파일 전체를 검사한 뒤에만 쓴다. 쓰는 중에 항목 하나가 거부되면 사유를 쓰고 나머지를 계속한다.
토큰 원문은 발급한 줄 바로 아래에 stdout으로 한 번만 쓰고, 로그나 파일에는 남기지 않는다 (FR-01, #29).

표 배열은 모두 선택이다. 키는 아래만 허용하고, 모르는 키와 빠진 필수 키는 오류다.
  [[accounts]]  user_id, name, role(owner|visitor)
  [[beacons]]   beacon_id, user_id, uuid, major, minor, active(선택, 기본 true)
  [[places]]    place_id, owner, name, nodes(기기 ID 배열)
  [[nodes]]     node_id, trust(discovered|trusted|revoked, 선택), release_policy(표, 선택), name(선택)
  [[policies]]  node_id, visibility, mode, approval_mode, approver·approval_ttl_sec·max_uses(선택)
기기 행은 서버가 MQTT 발표로 만든다. `[[nodes]]`와 `[[policies]]`의 기기는 어느 장소의 `nodes`에 있어야 하고,
그 기기에 쓰는 행위자는 장소의 `owner`다. `approval_mode`가 one_time 인 정책은 `max_uses`를 주면 1 이어야 한다.
"""

from __future__ import annotations

import argparse
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import CliError, dirs_for, load_cli_config, open_write_db, resolve_input
from nearpilot.core.admin import DefaultAdminService
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.shared.enums import (
    AccessMode,
    AdminWriteOutcome,
    AdminWriteReason,
    ApprovalMode,
    Role,
    TrustState,
    Visibility,
)
from nearpilot.shared.interfaces import AdminService, Repository
from nearpilot.shared.models import (
    AdminWriteResult,
    BeaconKey,
    BeaconRegistration,
    PlaceAssignment,
    PolicySettings,
)


class SeedFileError(ValueError):
    """시드 파일을 쓸 수 없다. 메시지에 잘못된 필드를 모두 "; "로 잇는다."""


@dataclass(frozen=True)
class SeedAccount:
    user_id: str
    name: str
    role: Role


@dataclass(frozen=True)
class SeedBeacon:
    beacon_id: str
    user_id: str
    beacon: BeaconKey
    active: bool


@dataclass(frozen=True)
class SeedPlace:
    place_id: str
    owner: str
    name: str
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class SeedNode:
    node_id: str
    trust: TrustState | None  # None 이면 신뢰 상태를 건드리지 않는다
    release_policy: Mapping[str, Any] | None  # None 이면 건드리지 않는다. 빈 표는 비우라는 뜻이다
    name: str | None = None  # 대시보드에 보이는 기기 이름. None 이면 건드리지 않는다


@dataclass(frozen=True)
class SeedFile:
    accounts: tuple[SeedAccount, ...]
    beacons: tuple[SeedBeacon, ...]
    places: tuple[SeedPlace, ...]
    nodes: tuple[SeedNode, ...]
    policies: tuple[PolicySettings, ...]


@dataclass(frozen=True)
class SeedSummary:
    applied: int  # 쓰기가 일어난 항목
    unchanged: int  # 이미 있거나 바뀐 것이 없는 항목
    rejected: int


# 표 배열 이름 → ({키: 값 종류}, 선택 키). 선택이 아닌 키는 필수다.
# 값 종류: str 은 빈 문자열이 아닌 문자열, int 는 bool 이 아닌 정수, bool, dict 는 표,
# list 는 빈 문자열이 아닌 문자열의 배열, Enum 클래스는 그 값 중 하나.
_SECTIONS: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {
    "accounts": ({"user_id": str, "name": str, "role": Role}, ()),
    "beacons": (
        {"beacon_id": str, "user_id": str, "uuid": str, "major": int, "minor": int, "active": bool},
        ("active",),
    ),
    "places": ({"place_id": str, "owner": str, "name": str, "nodes": list}, ()),
    "nodes": (
        {"node_id": str, "trust": TrustState, "release_policy": dict, "name": str},
        ("trust", "release_policy", "name"),
    ),
    "policies": (
        {
            "node_id": str, "visibility": Visibility, "mode": AccessMode, "approver": str,
            "approval_mode": ApprovalMode, "approval_ttl_sec": int, "max_uses": int,
        },
        ("approver", "approval_ttl_sec", "max_uses"),
    ),
}
# 같은 값이 두 번 나오면 안 되는 키 (표 배열 이름, 키)
_UNIQUE_KEYS = (
    ("accounts", "user_id"), ("beacons", "beacon_id"), ("places", "place_id"),
    ("nodes", "node_id"), ("policies", "node_id"),
)

_Rows = dict[str, list[tuple[str, dict[str, Any]]]]  # 표 배열 이름 → [(경로, 검사를 통과한 값)]

# 항목 결과의 분류. `SeedSummary`의 필드 이름과 같다.
_APPLIED, _UNCHANGED, _REJECTED = "applied", "unchanged", "rejected"


def load_seed_file(path: Path) -> SeedFile:
    """시드 TOML을 읽고 검사한다. 잘못된 필드를 모두 모아 `SeedFileError`로 낸다."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SeedFileError(f"읽을 수 없다: {path}") from error
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise SeedFileError(f"TOML 이 아니다: {error}") from None
    problems = [f"{key}: 알 수 없는 키" for key in sorted(set(raw) - set(_SECTIONS))]
    rows = {name: _read_rows(raw, name, problems) for name in _SECTIONS}
    _check_links(rows, problems)
    _check_policies(rows, problems)
    if problems:
        raise SeedFileError("; ".join(problems))
    return _build(rows)


def _read_rows(raw: Mapping[str, Any], name: str, problems: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """`[[name]]` 표 배열을 검사한다. (경로, 통과한 값) 목록을 돌려주고 문제는 `problems`에 모은다."""
    kinds, optional = _SECTIONS[name]
    table = raw.get(name, [])
    if not isinstance(table, list) or not all(isinstance(row, dict) for row in table):
        problems.append(f"{name}: [[{name}]] 표 배열이어야 한다")
        return []
    rows = []
    for index, row in enumerate(table):
        where = f"{name}[{index}]"
        problems.extend(f"{where}.{key}: 알 수 없는 키" for key in sorted(set(row) - set(kinds)))
        values: dict[str, Any] = {}
        for key, kind in kinds.items():
            if key not in row:
                if key not in optional:
                    problems.append(f"{where}.{key}: 필수 키가 없다")
                continue
            try:
                values[key] = _convert(row[key], kind)
            except ValueError as error:
                problems.append(f"{where}.{key}: {error}")
        rows.append((where, values))
    return rows


def _convert(value: Any, kind: Any) -> Any:
    """값이 `kind`에 맞으면 (Enum은 멤버로 바꿔서) 돌려주고, 아니면 사유를 담은 `ValueError`를 낸다."""
    if kind is str:
        if isinstance(value, str) and value.strip():
            return value
        raise ValueError("빈 문자열이 아닌 문자열이어야 한다")
    if kind is int:
        if type(value) is int:  # bool 은 int 의 하위 형이라 isinstance 로는 거르지 못한다
            return value
        raise ValueError("정수여야 한다")
    if kind is bool:
        if type(value) is bool:
            return value
        raise ValueError("true 나 false 여야 한다")
    if kind is dict:
        if isinstance(value, dict):
            return value
        raise ValueError("표여야 한다")
    if kind is list:
        if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
            return value
        raise ValueError("빈 문자열이 아닌 문자열의 배열이어야 한다")
    try:
        return kind(value)
    except ValueError:
        raise ValueError(f"{', '.join(member.value for member in kind)} 중 하나여야 한다") from None


def _check_links(rows: _Rows, problems: list[str]) -> None:
    """검사를 통과한 값끼리 겹치는지, 기기가 장소에 속하는지 본다."""
    for name, key in _UNIQUE_KEYS:
        _report_duplicates(
            ((f"{where}.{key}", values[key]) for where, values in rows[name] if key in values), problems)
    # 한 기기는 한 장소에만 있다. 장소에 속하지 않은 기기는 쓸 행위자(장소 소유자)가 없다.
    placed = [(f"{where}.nodes[{index}]", node_id)
              for where, values in rows["places"] for index, node_id in enumerate(values.get("nodes", ()))]
    _report_duplicates(placed, problems)
    known = {node_id for _, node_id in placed}
    for name in ("nodes", "policies"):
        for where, values in rows[name]:
            if "node_id" in values and values["node_id"] not in known:
                problems.append(f"{where}.node_id: 어떤 [[places]] 의 nodes 에도 없다 ({values['node_id']})")


def _check_policies(rows: _Rows, problems: list[str]) -> None:
    """one_time 승인은 한 번만 쓰이므로 max_uses 를 주면 1 이어야 한다 (FR-14)."""
    for where, values in rows["policies"]:
        if values.get("approval_mode") is ApprovalMode.ONE_TIME and values.get("max_uses", 1) != 1:
            problems.append(f"{where}.max_uses: approval_mode 가 one_time 이면 1 이어야 한다 ({values['max_uses']})")


def _report_duplicates(items: Iterable[tuple[str, str]], problems: list[str]) -> None:
    """(경로, 값) 중 앞에서 이미 나온 값을 문제로 모은다."""
    first: dict[str, str] = {}
    for where, value in items:
        if value in first:
            problems.append(f"{where}: 중복이다 ({first[value]} 와 같다: {value})")
        else:
            first[value] = where


def _build(rows: _Rows) -> SeedFile:
    """검사를 통과한 값으로 `SeedFile`을 만든다."""
    return SeedFile(
        accounts=tuple(SeedAccount(v["user_id"], v["name"], v["role"]) for _, v in rows["accounts"]),
        beacons=tuple(
            SeedBeacon(v["beacon_id"], v["user_id"], BeaconKey(v["uuid"], v["major"], v["minor"]),
                       v.get("active", True))
            for _, v in rows["beacons"]),
        places=tuple(SeedPlace(v["place_id"], v["owner"], v["name"], tuple(v["nodes"])) for _, v in rows["places"]),
        nodes=tuple(
            SeedNode(v["node_id"], v.get("trust"), v.get("release_policy"), v.get("name"))
            for _, v in rows["nodes"]),
        policies=tuple(
            PolicySettings(v["node_id"], v["visibility"], v["mode"], v.get("approver"), v["approval_mode"],
                           v.get("approval_ttl_sec"), v.get("max_uses"))
            for _, v in rows["policies"]),
    )


def apply_seed(seed: SeedFile, service: AdminService, repository: Repository,
               write: Callable[[str], None]) -> SeedSummary:
    """시드를 계정 → 비콘 → 장소 → 기기 → 정책 순서로 반영한다. 항목마다 한 줄을 `write`로 쓴다.

    `load_seed_file`이 검사한 파일을 받는다. 항목 하나가 거부되면 사유를 쓰고 나머지를 계속한다.
    DB에 없는 기기는 거부로 세고, 그 기기의 신뢰·종료 정책·이름·정책은 건너뛴다.
    """
    counts = {_APPLIED: 0, _UNCHANGED: 0, _REJECTED: 0}

    def run(results: Iterable[tuple[str | None, str]]) -> None:
        for kind, line in results:  # 항목을 반영한 직후에 쓴다. 토큰은 이 줄이 유일한 기회다
            if kind is not None:
                counts[kind] += 1
            write(line)

    run(_accounts(seed.accounts, service))
    run(_beacons(seed.beacons, service))
    stored = {node.node_id for node in repository.nodes()}
    owner_of = {node_id: place.owner for place in seed.places for node_id in place.nodes}
    run(_places(seed.places, stored, service))
    run(_nodes(seed.nodes, stored, owner_of, service))
    run(_policies(seed.policies, stored, owner_of, service))
    summary = SeedSummary(**counts)
    write(f"완료: 반영 {summary.applied}, 변경 없음 {summary.unchanged}, 거부 {summary.rejected}")
    return summary


# 아래 반영 함수는 항목을 하나 반영할 때마다 (분류, 줄)을 낸다. 분류가 None인 줄은 항목이 아니라서 세지 않는다.


def _written(result: AdminWriteResult, subject: str, done: str) -> tuple[str, str]:
    """쓰기 결과를 (분류, 줄)로 바꾼다. 반영됐으면 `subject: done`, 아니면 거부 사유를 쓴다."""
    if result.outcome == AdminWriteOutcome.APPLIED:
        return _APPLIED, f"{subject}: {done}"
    return _REJECTED, f"{subject}: 거부 ({result.reason})"


def _accounts(accounts: Iterable[SeedAccount], service: AdminService) -> Iterator[tuple[str | None, str]]:
    for account in accounts:
        issue = service.register_account(account.user_id, account.name, account.role)
        subject = f"계정 {account.user_id}"
        if issue.result.outcome == AdminWriteOutcome.APPLIED:
            yield _APPLIED, f"{subject}: 등록"
            yield None, f"  토큰: {issue.token}  (다시 보여 주지 않는다)"
        elif issue.result.reason == AdminWriteReason.ACCOUNT_ALREADY_EXISTS:
            yield _UNCHANGED, f"{subject}: 이미 있음"
        else:
            yield _REJECTED, f"{subject}: 거부 ({issue.result.reason})"


def _beacons(beacons: Iterable[SeedBeacon], service: AdminService) -> Iterator[tuple[str | None, str]]:
    for beacon in beacons:
        result = service.register_beacon(
            BeaconRegistration(beacon.beacon_id, beacon.user_id, beacon.beacon, beacon.active))
        yield _written(result, f"비콘 {beacon.beacon_id} ({beacon.user_id})", "등록")


def _places(places: Iterable[SeedPlace], stored: set[str],
            service: AdminService) -> Iterator[tuple[str | None, str]]:
    for place in places:
        for node_id in place.nodes:
            if node_id not in stored:
                yield _REJECTED, f"기기 {node_id}: DB에 없다 (서버가 발표를 받은 뒤 다시 실행한다)"
        present = tuple(node_id for node_id in place.nodes if node_id in stored)
        result = service.assign_place(
            place.owner, PlaceAssignment(place.place_id, place.owner, place.name, present))
        yield _written(result, f"장소 {place.place_id}", f"배치 (기기 {len(present)}대)")


def _nodes(nodes: Iterable[SeedNode], stored: set[str], owner_of: Mapping[str, str],
           service: AdminService) -> Iterator[tuple[str | None, str]]:
    for node in nodes:
        if node.node_id not in stored:
            continue  # 장소 단계에서 이미 거부로 셌다
        owner = owner_of[node.node_id]
        if node.trust is not None:
            result = service.set_node_trust(owner, node.node_id, node.trust)
            yield _written(result, f"기기 {node.node_id} 신뢰", node.trust.value)
        if node.release_policy is not None:
            result = service.set_release_policy(owner, node.node_id, node.release_policy)
            yield _written(result, f"기기 {node.node_id} 종료 정책", "반영")
        if node.name is not None:
            result = service.set_node_name(owner, node.node_id, node.name)
            yield _written(result, f"기기 {node.node_id} 이름", node.name)


def _policies(policies: Iterable[PolicySettings], stored: set[str], owner_of: Mapping[str, str],
              service: AdminService) -> Iterator[tuple[str | None, str]]:
    for settings in policies:
        if settings.node_id not in stored:
            continue
        written = service.set_policy(owner_of[settings.node_id], settings)
        subject = f"정책 {settings.node_id}"
        if written.result.outcome != AdminWriteOutcome.APPLIED:
            yield _REJECTED, f"{subject}: 거부 ({written.result.reason})"
        elif written.changed:
            yield _APPLIED, f"{subject}: 버전 {written.version}"
        else:
            yield _UNCHANGED, f"{subject}: 변경 없음 (버전 {written.version})"


def _plan_lines(seed: SeedFile) -> Iterator[str]:
    """`--dry-run`이 쓸 줄. DB를 열지 않으므로 이미 있는지는 모르고, 파일에 있는 항목을 반영 순서대로 쓴다."""
    for account in seed.accounts:
        yield f"(dry-run) 계정 {account.user_id} 등록"
    for beacon in seed.beacons:
        yield f"(dry-run) 비콘 {beacon.beacon_id} ({beacon.user_id}) 등록"
    for place in seed.places:
        yield f"(dry-run) 장소 {place.place_id} 배치 (기기 {len(place.nodes)}대)"
    for node in seed.nodes:
        if node.trust is not None:
            yield f"(dry-run) 기기 {node.node_id} 신뢰 {node.trust.value}"
        if node.release_policy is not None:
            yield f"(dry-run) 기기 {node.node_id} 종료 정책 반영"
        if node.name is not None:
            yield f"(dry-run) 기기 {node.node_id} 이름 {node.name}"
    for settings in seed.policies:
        yield f"(dry-run) 정책 {settings.node_id} 반영"


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "seed", parents=[parent], help="시드 파일로 계정, 비콘, 장소, 기기, 정책을 넣는다",
        description="시드 파일(TOML)을 모두 검사한 뒤 반영한다. 항목이 거부되면 사유를 쓰고 나머지를 계속하며, "
                    "거부가 하나라도 있으면 종료 코드 1이다. 계정 토큰은 발급할 때 한 번만 보여 준다.")
    parser.add_argument("file", metavar="FILE",
                        help="시드 파일 이름(설정 파일 옆 seed/ 폴더에서 찾는다) 또는 경로(/ 나 \\ 포함, 실행 폴더 기준)")
    parser.add_argument("--dry-run", action="store_true", help="DB를 열지 않고 반영할 항목만 쓴다")
    parser.set_defaults(handler=_seed)


def _seed(args: argparse.Namespace) -> int:
    config = load_cli_config(args.config)
    path = resolve_input(args.file, dirs_for(config.source).seed, kind="시드")
    try:
        seed = load_seed_file(path)
    except SeedFileError as error:  # DB를 열기 전에 막는다
        raise CliError(f"시드 파일 오류: {error}", EXIT_INVALID) from None
    if args.dry_run:
        for line in _plan_lines(seed):
            print(line)
        return EXIT_OK
    conn = open_write_db(config)
    try:
        repository = SqliteRepository(conn, config.settings)
        service = DefaultAdminService(repository, SqliteAdminRepository(conn))
        summary = apply_seed(seed, service, repository, write=print)
    finally:
        conn.close()
    return EXIT_FAILED if summary.rejected else EXIT_OK
