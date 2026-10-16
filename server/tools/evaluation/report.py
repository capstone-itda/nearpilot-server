"""평가 보고서 CSV·Markdown 형식 (specs/db-evaluation-foundation/spec.md §6 "결과 형식").

지표 스크립트는 지표별 행(dict)과 메타데이터만 넘긴다. 머리말, CSV·Markdown 변환,
실패 0건일 때의 Clopper-Pearson 95% 신뢰구간 상한은 이 모듈이 맡는다.
"""

from __future__ import annotations

import csv
import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_CONFIDENCE_FIELD = "confidence_bound_95"


def zero_failure_upper_bound(trials: int) -> float:
    """실패 0건일 때 양측 95% Clopper-Pearson 신뢰구간의 상한: `1 - (0.025)^(1/trials)`.

    실패가 0건이면 하한은 항상 0이라 양측 구간이 상한 하나로 정리되는 닫힌 식이다. `scipy`를
    쓰지 않는다. 실패가 1건 이상인 경우의 신뢰구간은 이 함수의 범위 밖이다
    (specs/db-evaluation-foundation/spec.md §4 "만들지 않는 것").
    """
    if trials <= 0:
        raise ValueError(f"trials 는 1 이상이어야 한다: {trials}")
    return 1 - 0.025 ** (1 / trials)


def _run_git(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout


def resolve_git_commit(run: Callable[[list[str]], str] | None = None) -> str:
    """현재 git 커밋을 얻는다. 작업 디렉터리가 깨끗하지 않으면 `-dirty`를 붙인다.

    `rev-parse`·`status` 중 하나라도 실패하면(`.git` 없음, `git` 명령 없음 등) "unknown"을
    돌려주고 경고 로그를 남긴다. 예외는 올리지 않는다 — 보고서 생성을 막지 않는다.
    """
    runner = run or _run_git
    try:
        commit = runner(["git", "rev-parse", "HEAD"]).strip()
        status = runner(["git", "status", "--porcelain"])
    except Exception as error:  # noqa: BLE001 - git 을 못 구해도 보고서 생성은 계속한다
        logger.warning("git 커밋을 확인할 수 없다: %s", error)
        return "unknown"
    return f"{commit}-dirty" if status.strip() else commit


@dataclass(frozen=True)
class ReportHeader:
    db_path: str
    schema_version: int
    sample_count: int
    generated_at: datetime
    git_commit: str


def _augment_row(row: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(row)
    trials = row.get("trials")
    failures = row.get("failures")
    if trials is not None and failures == 0:
        row[_CONFIDENCE_FIELD] = zero_failure_upper_bound(trials)
    return row


def _fieldnames(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        for key in row:
            if key not in names:
                names.append(key)
    return names


def _ensure_writable(path: str, *, force: bool) -> None:
    if os.path.exists(path) and not force:
        raise FileExistsError(f"{path} 이 이미 있다. 덮어쓰려면 force=True를 준다")
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if not os.access(parent, os.W_OK):
        raise PermissionError(f"{parent} 에 쓸 수 없다")


def _write_csv(path: str, header: ReportHeader, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = _fieldnames(rows)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(f"# db_path: {header.db_path}\n")
        f.write(f"# schema_version: {header.schema_version}\n")
        f.write(f"# git_commit: {header.git_commit}\n")
        f.write(f"# sample_count: {header.sample_count}\n")
        f.write(f"# generated_at: {header.generated_at.isoformat()}\n")
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(path: str, header: ReportHeader, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = _fieldnames(rows)
    lines = [
        f"- 입력 DB 경로: `{header.db_path}`",
        f"- 스키마 버전: {header.schema_version}",
        f"- git 커밋: `{header.git_commit}`",
        f"- 표본 수: {header.sample_count}",
        f"- 생성 시각: {header.generated_at.isoformat()}",
        "",
        "| " + " | ".join(fieldnames) + " |",
        "|" + "|".join("---" for _ in fieldnames) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(name, "")) for name in fieldnames) + " |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    db_path: str,
    schema_version: int,
    sample_count: int,
    generated_at: datetime,
    csv_path: str,
    md_path: str,
    git_commit: str | None = None,
    force: bool = False,
) -> None:
    """지표 표 데이터로 CSV와 Markdown 보고서를 함께 만든다.

    `rows`의 각 행에 `trials`·`failures`가 있고 `failures == 0`이면, 그 행에
    `confidence_bound_95`(Clopper-Pearson 95% 상한)를 덧붙인다. 출력 경로에 이미 파일이 있으면
    `force=True`를 주지 않는 한 덮어쓰지 않는다. DB를 조회하기 전에(이 함수를 부르기 전에) 이미
    얻은 `rows`를 받으므로, 이 함수 자체는 출력 경로 쓰기 가능 여부만 먼저 확인한다.
    """
    _ensure_writable(csv_path, force=force)
    _ensure_writable(md_path, force=force)
    commit = git_commit if git_commit is not None else resolve_git_commit()
    header = ReportHeader(
        db_path=db_path, schema_version=schema_version, sample_count=sample_count,
        generated_at=generated_at, git_commit=commit,
    )
    rendered_rows = [_augment_row(row) for row in rows]
    _write_csv(csv_path, header, rendered_rows)
    _write_markdown(md_path, header, rendered_rows)
