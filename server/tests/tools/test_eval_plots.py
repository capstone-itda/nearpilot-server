"""NFR-13 평가 그래프 세 장 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

그래프를 만드는 시험은 matplotlib이 있을 때만 돈다 (`pytest.importorskip`). 파일이 PNG인지만 보지 않고, `Figure.savefig`를 감싸
저장 직전의 그림을 모아서 축의 이름, 선과 막대의 값, 막대의 색을 본다. matplotlib이 없는 경우는 가져오기를 막아 시험하므로
matplotlib이 있는 PC에서도 돈다. 없는 PC에서는 이 파일의 시험 가운데 그래프를 만드는 둘이 건너뛰어진다.
"""

from __future__ import annotations

import logging
import sys

import pytest

from tools.evaluation.order import BoundaryStat
from tools.evaluation.plots import write_order_plot, write_plots
from tools.evaluation.reask import CurvePoint
from tools.evaluation.rows import metric_row

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
LATENCIES = [0.10, 0.12, 0.15, 0.15, 0.20, 0.35]
CURVE = [  # 비용 비율 순이 아니다
    CurvePoint(19.0, 0.95, 0.9, 0.0), CurvePoint(1.0, 0.5, 0.1, 0.4), CurvePoint(3.0, 0.75, 0.4, 0.1)]
ROWS = [
    metric_row("m3_double_occupancy", value=0, limit=0, passed=True, trials=100, failures=0),
    metric_row("m4_success_rate", value=0.9, limit=0.95, passed=False, trials=150, failures=15),
    metric_row("m7_host_latency", note="미측정"),  # passed가 빈 값이다
    metric_row("m6_auto_release", value=0, limit=0, passed=True, trials=30, failures=0),
]
ORDER_VALIDATION = [  # BoundaryStat(기준 확률 후보, 경계 수, 통과, 통과 중 틀림, 통과 비율, 오류 비율)
    BoundaryStat(0.5, 10, 10, 3, 1.0, 0.3),
    BoundaryStat(0.7, 10, 6, 1, 0.6, 1 / 6),
    BoundaryStat(0.9, 10, 0, 0, 0.0, None),  # 통과한 경계가 없어 오류 비율이 없다
]
ORDER_TEST = [
    BoundaryStat(0.5, 8, 8, 2, 1.0, 0.25),
    BoundaryStat(0.7, 8, 5, 0, 0.625, 0.0),
    BoundaryStat(0.9, 8, 1, 0, 0.125, 0.0),
]


@pytest.fixture
def saved_figures(monkeypatch):
    """`write_plots`가 저장한 그림을 저장한 순서대로 모은다. matplotlib이 없으면 시험을 건너뛴다."""
    figure_module = pytest.importorskip("matplotlib.figure")
    figures = []
    real = figure_module.Figure.savefig

    def record(self, *args, **kwargs):
        figures.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(figure_module.Figure, "savefig", record)
    return figures


def test_NFR13_plots_written(tmp_path, saved_figures):
    paths = write_plots(tmp_path, "run1", latencies=LATENCIES, curve=CURVE, rows=ROWS)

    assert paths == [tmp_path / "run1-latency.png", tmp_path / "run1-reask.png", tmp_path / "run1-pass.png"]
    for path in paths:
        assert path.read_bytes().startswith(PNG_SIGNATURE)
    latency, reask, passed = (figure.axes[0] for figure in saved_figures)

    # 지연: 히스토그램 막대의 높이를 모으면 요청 수다
    assert sum(patch.get_height() for patch in latency.patches) == len(LATENCIES)
    # 곡선: x는 재질문률, y는 오작동률이고, 입력 순서와 상관없이 비용 비율 순(r = 1, 3, 19)으로 이어진다
    (line,) = reask.lines
    assert list(line.get_xdata()) == [0.1, 0.4, 0.9]
    assert list(line.get_ydata()) == [0.4, 0.1, 0.0]
    # 통과: 행마다 막대 하나가 행 순서대로 있고, 통과·실패·판정 안 함(빈 값)의 색이 서로 다르다
    assert [text.get_text() for text in passed.get_yticklabels()] == [row["metric"] for row in ROWS]
    colors = {round(patch.get_y() + patch.get_height() / 2): patch.get_facecolor() for patch in passed.patches}
    assert sorted(colors) == [0, 1, 2, 3]
    assert colors[0] == colors[3]
    assert len({colors[0], colors[1], colors[2]}) == 3
    assert [text.get_text() for text in passed.get_legend().get_texts()] == ["passed", "failed", "not judged"]
    # 한글 글꼴이 없는 PC가 있어서 제목과 축 이름은 영어다
    texts = [axes.get_title() for axes in (latency, reask, passed)]
    texts += [label for axes in (latency, reask) for label in (axes.get_xlabel(), axes.get_ylabel())]
    assert all(text and text.isascii() for text in texts)


def test_NFR13_plots_write_only_what_has_data(tmp_path):
    pytest.importorskip("matplotlib")

    assert write_plots(tmp_path, "a", latencies=None, curve=None, rows=ROWS) == [tmp_path / "a-pass.png"]
    assert write_plots(tmp_path, "b", latencies=LATENCIES, curve=[], rows=[]) == [tmp_path / "b-latency.png"]
    assert write_plots(tmp_path, "c", latencies=[], curve=CURVE, rows=[]) == [tmp_path / "c-reask.png"]
    assert write_plots(tmp_path, "d", latencies=None, curve=None, rows=[]) == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a-pass.png", "b-latency.png", "c-reask.png"]


def block_matplotlib(monkeypatch: pytest.MonkeyPatch) -> None:
    """matplotlib이 설치돼 있어도 없는 것처럼 만든다."""
    try:
        import matplotlib.figure  # 하위 모듈을 이미 가져온 프로세스에서도 없다고 봐야 한다
    except ImportError:
        pass
    monkeypatch.setitem(sys.modules, "matplotlib", None)


def test_NFR13_plots_skip_without_matplotlib(monkeypatch, tmp_path, caplog):
    block_matplotlib(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="tools.evaluation.plots"):
        paths = write_plots(tmp_path, "run1", latencies=LATENCIES, curve=CURVE, rows=ROWS)

    # 그래프만 건너뛰고 경고한다. 파일은 하나도 쓰지 않는다
    assert paths == [] and list(tmp_path.iterdir()) == []
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert "matplotlib" in caplog.text and "3장" in caplog.text and '".[eval]"' in caplog.text


def test_NFR13_plots_nothing_to_draw_does_not_warn_without_matplotlib(monkeypatch, tmp_path, caplog):
    block_matplotlib(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="tools.evaluation.plots"):
        paths = write_plots(tmp_path, "run1", latencies=None, curve=[], rows=[])

    assert paths == [] and caplog.records == []  # 건너뛴 그래프가 없으니 알릴 것도 없다


def line_data(axes) -> dict[str, tuple[list[float], list[float]]]:
    """축의 선을 그린 순서대로 범례 이름 → (x 값, y 값)."""
    return {line.get_label(): (list(line.get_xdata()), list(line.get_ydata())) for line in axes.lines}


def test_FR09_order_plot_written(tmp_path, saved_figures):
    path = write_order_plot(tmp_path, "run1", validation=ORDER_VALIDATION, test=ORDER_TEST, max_error=0.05, chosen=0.7)

    assert path == tmp_path / "run1-order.png"
    assert path.read_bytes().startswith(PNG_SIGNATURE)
    (axes,) = saved_figures[0].axes
    # 곡선 넷은 검증·시험의 통과 비율과 오류 비율이고 x는 기준 확률 후보다. 오류 비율이 None인 후보(검증의 0.9)는 건너뛴다.
    # 나머지 둘은 수평 목표선(y = 목표 오류 비율)과 고른 기준 확률의 수직선이다.
    assert line_data(axes) == {
        "validation pass rate": ([0.5, 0.7, 0.9], [1.0, 0.6, 0.0]),
        "validation error rate": ([0.5, 0.7], [0.3, 1 / 6]),
        "test pass rate": ([0.5, 0.7, 0.9], [1.0, 0.625, 0.125]),
        "test error rate": ([0.5, 0.7, 0.9], [0.25, 0.0, 0.0]),
        "target error rate = 0.05": ([0.0, 1.0], [0.05, 0.05]),
        "chosen min prob = 0.7": ([0.7, 0.7], [0.0, 1.0]),
    }
    assert [text.get_text() for text in axes.get_legend().get_texts()] == list(line_data(axes))
    bottom, top = axes.get_ylim()  # 세로축은 비율 0~1이다 (선이 가장자리에서 잘리지 않을 만큼만 여백이 있다)
    assert -0.1 < bottom <= 0.0 and 1.0 <= top < 1.1
    # 한글 글꼴이 없는 PC가 있어서 제목과 축 이름은 영어다
    texts = [axes.get_title(), axes.get_xlabel(), axes.get_ylabel()]
    assert all(text and text.isascii() for text in texts)


def test_FR09_order_plot_without_chosen_or_test_table(tmp_path, saved_figures):
    path = write_order_plot(tmp_path, "run2", validation=ORDER_VALIDATION, test=[], max_error=0.05, chosen=None)

    assert path == tmp_path / "run2-order.png"
    (axes,) = saved_figures[0].axes
    # 고른 값이 없으면 수직선이 없고, 표가 빈 쪽의 곡선은 없다
    assert list(line_data(axes)) == ["validation pass rate", "validation error rate", "target error rate = 0.05"]


def test_FR09_order_plot_skipped_without_matplotlib(monkeypatch, tmp_path, caplog):
    block_matplotlib(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="tools.evaluation.plots"):
        path = write_order_plot(tmp_path, "run1", validation=ORDER_VALIDATION, test=ORDER_TEST, max_error=0.05, chosen=0.7)

    # 그래프만 건너뛰고 경고한다. 파일은 쓰지 않는다
    assert path is None and list(tmp_path.iterdir()) == []
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert "matplotlib" in caplog.text and "1장" in caplog.text and '".[eval]"' in caplog.text


def test_FR09_order_plot_nothing_to_draw_does_not_warn(monkeypatch, tmp_path, caplog):
    block_matplotlib(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="tools.evaluation.plots"):
        path = write_order_plot(tmp_path, "run1", validation=[], test=[], max_error=0.05, chosen=None)

    assert path is None and list(tmp_path.iterdir()) == [] and caplog.records == []  # 두 표가 모두 비면 그릴 것도 알릴 것도 없다
