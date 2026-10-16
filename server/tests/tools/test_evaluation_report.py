"""평가 보고서 CSV·Markdown 형식과 Clopper-Pearson 신뢰구간 (specs/db-evaluation-foundation/spec.md)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tools.evaluation.report import resolve_git_commit, write_report, zero_failure_upper_bound

GENERATED_AT = datetime(2026, 2, 1, tzinfo=timezone.utc)


def _rows():
    return [
        {"metric": "지표 5", "trials": 30, "failures": 0},
        {"metric": "지표 6", "trials": 30, "failures": 2},
    ]


def test_NFR13_zero_failure_reports_confidence_bound(tmp_path):
    csv_path = str(tmp_path / "report.csv")
    md_path = str(tmp_path / "report.md")

    write_report(
        _rows(), db_path="eval.db", schema_version=5, sample_count=60, generated_at=GENERATED_AT,
        csv_path=csv_path, md_path=md_path, git_commit="abc123",
    )

    md_text = open(md_path, encoding="utf-8").read()
    assert "confidence_bound_95" in md_text
    assert "0.115" in md_text  # 지표 5 행의 상한


def test_NFR13_zero_failure_confidence_bound_matches_known_value():
    bound = zero_failure_upper_bound(30)
    assert abs(bound - 0.1157) < 0.001


def test_NFR13_nonzero_failure_omits_confidence_bound(tmp_path):
    csv_path = str(tmp_path / "report.csv")
    md_path = str(tmp_path / "report.md")

    write_report(
        _rows(), db_path="eval.db", schema_version=5, sample_count=60, generated_at=GENERATED_AT,
        csv_path=csv_path, md_path=md_path, git_commit="abc123",
    )

    with open(csv_path, encoding="utf-8") as f:
        text = f.read()
    lines = [line for line in text.splitlines() if line.startswith("지표 6")]
    assert len(lines) == 1
    assert lines[0].endswith(",")  # confidence_bound_95 칸이 비어 있다(값을 채우지 않았다)


def test_NFR13_report_header_has_required_fields(tmp_path):
    csv_path = str(tmp_path / "report.csv")
    md_path = str(tmp_path / "report.md")

    write_report(
        _rows(), db_path="/data/eval.db", schema_version=5, sample_count=60,
        generated_at=GENERATED_AT, csv_path=csv_path, md_path=md_path, git_commit="abc123",
    )

    for path in (csv_path, md_path):
        text = open(path, encoding="utf-8").read()
        assert "/data/eval.db" in text
        assert "schema_version" in text.lower() or "스키마 버전" in text
        assert "abc123" in text
        assert "60" in text
        assert GENERATED_AT.isoformat() in text


def test_NFR13_report_written_as_csv_and_markdown(tmp_path):
    csv_path = str(tmp_path / "report.csv")
    md_path = str(tmp_path / "report.md")

    write_report(
        _rows(), db_path="eval.db", schema_version=5, sample_count=60, generated_at=GENERATED_AT,
        csv_path=csv_path, md_path=md_path, git_commit="abc123",
    )

    assert "지표 5" in open(csv_path, encoding="utf-8").read()
    assert "지표 5" in open(md_path, encoding="utf-8").read()


def test_NFR13_existing_output_requires_force(tmp_path):
    csv_path = tmp_path / "report.csv"
    md_path = tmp_path / "report.md"
    csv_path.write_text("old")

    with pytest.raises(FileExistsError):
        write_report(
            _rows(), db_path="eval.db", schema_version=5, sample_count=60, generated_at=GENERATED_AT,
            csv_path=str(csv_path), md_path=str(md_path), git_commit="abc123",
        )
    assert not md_path.exists()  # 아무 파일도 새로 쓰지 않았다

    write_report(
        _rows(), db_path="eval.db", schema_version=5, sample_count=60, generated_at=GENERATED_AT,
        csv_path=str(csv_path), md_path=str(md_path), git_commit="abc123", force=True,
    )
    assert "지표 5" in csv_path.read_text(encoding="utf-8")


def test_NFR13_unwritable_output_dir_is_rejected_before_any_write(tmp_path):
    missing_dir = tmp_path / "no-such-dir"
    csv_path = str(missing_dir / "report.csv")
    md_path = str(tmp_path / "report.md")

    with pytest.raises(PermissionError):
        write_report(
            _rows(), db_path="eval.db", schema_version=5, sample_count=60, generated_at=GENERATED_AT,
            csv_path=csv_path, md_path=md_path, git_commit="abc123",
        )
    assert not (tmp_path / "report.md").exists()


def test_NFR13_report_header_marks_dirty_working_tree(tmp_path):
    csv_path = str(tmp_path / "report.csv")
    md_path = str(tmp_path / "report.md")

    def fake_run(args):
        if args[1] == "rev-parse":
            return "deadbee\n"
        return " M some_file.py\n"  # porcelain 출력이 비어 있지 않다

    commit = resolve_git_commit(run=fake_run)
    assert commit == "deadbee-dirty"

    write_report(
        _rows(), db_path="eval.db", schema_version=5, sample_count=60, generated_at=GENERATED_AT,
        csv_path=csv_path, md_path=md_path, git_commit=commit,
    )
    assert "deadbee-dirty" in open(md_path, encoding="utf-8").read()


def test_NFR13_report_header_falls_back_when_git_unavailable():
    def fake_run(args):
        raise FileNotFoundError("git 명령이 없다")

    assert resolve_git_commit(run=fake_run) == "unknown"
