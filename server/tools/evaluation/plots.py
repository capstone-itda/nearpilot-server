"""평가 그래프 세 장을 PNG로 쓴다 (specs/db-evaluation-server-metrics/spec.md 6절 "결정", NFR-13).

`write_plots`는 지표 7 서버 판정 지연 분포(히스토그램), 지표 2 재질문 곡선(x는 재질문률, y는 오작동률), 지표별 통과 여부 막대를 그린다.
데이터가 없는 그래프는 만들지 않는다.
matplotlib은 선택 의존성 `eval`이라 모듈을 가져올 때가 아니라 그릴 때 함수 안에서 가져온다. 없으면 경고만 하고 그래프를 건너뛴다.
`pyplot` 없이 `Figure`에 Agg 캔버스를 달아 그린다. 화면이 없어도 되고, 프로세스 전체의 백엔드 설정을 바꾸지 않는다.
한글 글꼴이 없는 PC가 있어서 제목과 축 이름은 영어로 쓴다.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tools.evaluation.reask import CurvePoint

if TYPE_CHECKING:
    from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

_HISTOGRAM_BINS = 20
# (통과 여부, 범례, 색). 통과 여부가 True·False가 아니면 판정하지 않은 것이다. Okabe-Ito 팔레트라 색각 이상에서도 구분된다.
_STATUSES = ((True, "passed", "#0072B2"), (False, "failed", "#D55E00"), ("", "not judged", "#999999"))


def write_plots(
    out_dir: Path, name: str, *, latencies: Sequence[float] | None,
    curve: Sequence[CurvePoint] | None, rows: Sequence[Mapping[str, Any]],
) -> list[Path]:
    """`out_dir`에 `{name}-latency.png`, `{name}-reask.png`, `{name}-pass.png` 가운데 데이터가 있는 것만 쓰고, 쓴 경로를 이 순서로 돌려준다.

    - latency: `latencies`(초. 지표 7의 `raw["latencies"]`)의 히스토그램. 없거나 비면 그리지 않는다.
    - reask: `curve`의 점을 비용 비율 순으로 이은 선. x는 재질문률, y는 오작동률이다. 없거나 비면 그리지 않는다.
    - pass: `rows`의 행마다 막대 하나. `passed`가 True, False, 그 밖(빈 값)이면 색이 다르다. `rows`가 비면 그리지 않는다.

    `out_dir`은 이미 있어야 한다. 같은 이름의 파일은 덮어쓰니, 덮어써도 되는지는 부르는 쪽이 정한다.
    matplotlib이 없으면 경고하고 아무것도 쓰지 않은 채 빈 목록을 돌려준다. 그릴 것이 없으면 matplotlib이 없어도 경고하지 않는다.
    """
    draws = []  # (파일 이름 꼬리, 그리는 함수, 그릴 데이터)
    if latencies:
        draws.append(("latency", _draw_latency, latencies))
    if curve:
        draws.append(("reask", _draw_reask, curve))
    if rows:
        draws.append(("pass", _draw_pass, rows))
    if not draws:
        return []
    # 하위 모듈을 이미 가져온 프로세스에서도 없다고 보도록 최상위 패키지를 본다 (`sys.modules["matplotlib"] = None`도 없는 것이다)
    if importlib.util.find_spec("matplotlib") is None:
        logger.warning('matplotlib이 없어 그래프 %d장을 만들지 않았다. pip install -e ".[eval]"로 설치한다', len(draws))
        return []

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    paths = []
    for suffix, draw, data in draws:
        figure = Figure(figsize=(7.0, 4.5), layout="constrained")
        FigureCanvasAgg(figure)
        draw(figure, data)
        path = out_dir / f"{name}-{suffix}.png"
        figure.savefig(path)
        paths.append(path)
    return paths


def _draw_latency(figure: Figure, latencies: Sequence[float]) -> None:
    axes = figure.add_subplot()
    axes.hist(latencies, bins=_HISTOGRAM_BINS)
    axes.set_title("Metric 7: server decision latency")
    axes.set_xlabel("Latency (s)")
    axes.set_ylabel("Requests")


def _draw_reask(figure: Figure, curve: Sequence[CurvePoint]) -> None:
    axes = figure.add_subplot()
    points = sorted(curve, key=lambda point: point.cost_ratio)
    axes.plot([point.reask_rate for point in points], [point.misoperation_rate for point in points], marker="o")
    for point in points:  # 점마다 비용 비율을 적어야 곡선의 어디가 어느 비율인지 읽는다
        axes.annotate(
            f"r={point.cost_ratio:g}", (point.reask_rate, point.misoperation_rate),
            textcoords="offset points", xytext=(5, 5), fontsize=8,
        )
    axes.margins(x=0.1, y=0.1)  # 가장자리 점의 마커와 라벨이 잘리지 않게 한다
    axes.set_title("Metric 2: re-ask curve by cost ratio r")
    axes.set_xlabel("Re-ask rate")
    axes.set_ylabel("Misoperation rate")


def _draw_pass(figure: Figure, rows: Sequence[Mapping[str, Any]]) -> None:
    figure.set_size_inches(8.0, max(2.5, 0.3 * len(rows) + 1.5))  # 막대가 겹치지 않게 행 수에 맞춘다
    axes = figure.add_subplot()
    statuses = [row["passed"] if isinstance(row["passed"], bool) else "" for row in rows]
    for status, label, color in _STATUSES:
        members = [index for index, value in enumerate(statuses) if value == status]
        if members:
            axes.barh(members, [1] * len(members), color=color, label=label)
    axes.set_yticks(range(len(rows)), labels=[row["metric"] for row in rows])
    axes.invert_yaxis()  # 행 순서대로 위에서 아래로
    axes.set_xticks([])
    axes.set_xlim(0, 1)
    axes.set_title("Pass status by metric")
    axes.legend(loc="upper center", bbox_to_anchor=(0.5, -0.02), ncol=len(_STATUSES))
