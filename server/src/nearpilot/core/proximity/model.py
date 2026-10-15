"""장소별 근접 모델 파일 읽기 (specs/core-proximity-estimator/spec.md 6절).

모델 파일의 경로는 `{모델 루트}/{place_id}/{model_version}.json`이다. 읽을 때 형식, 값, 경로를
모두 검사하고 잘못된 파일은 `ModelFileError`로 거부한다. 읽은 모델은 바꿀 수 없다.
이 모듈은 파일을 읽어 검사할 뿐 사후확률을 계산하지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from math import inf, isfinite
from pathlib import Path
from typing import Any


class ModelFileError(ValueError):
    """모델 파일의 내용이나 경로가 잘못됐다."""


class ModelMismatchError(LookupError):
    """요청한 장소의 모델이 없거나, 장소의 trusted 기기가 모델에 없다."""


class _ReadOnlyDict(dict):
    """바꿀 수 없는 dict. JSON 직렬화, dataclass 변환, deepcopy는 dict와 같다.

    `shared.models`의 스냅샷 매핑과 같은 동작이다. 그쪽은 비공개라서 여기에 따로 둔다.
    """

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("모델의 매핑은 바꿀 수 없다")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation
    __ior__ = _reject_mutation

    def __deepcopy__(self, memo: dict[int, Any]) -> _ReadOnlyDict:
        return _ReadOnlyDict((deepcopy(key, memo), deepcopy(value, memo)) for key, value in self.items())


def _freeze(value: Any) -> Any:
    """매핑은 `_ReadOnlyDict`로, list와 tuple은 tuple로 바꿔 안쪽 JSON 컨테이너까지 불변으로 만든다."""
    if isinstance(value, Mapping):
        return _ReadOnlyDict((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class NodeModel:
    """모델 파일의 기기 하나. 좌표는 장소 평면의 위치이고 단위는 미터다."""

    node_id: str
    x: float  # 앵커(기기) 위치, 미터
    y: float
    front_x: float  # 기기 앞 위치, 미터
    front_y: float
    A: float  # 1 m 기준 RSSI, dBm
    n: float  # 경로손실 지수
    sigma: float  # 기기 앞 상태의 RSSI 표준편차, dB
    none_rssi: float  # "어느 기기 앞도 아님" 상태의 RSSI 평균, dBm
    none_sigma: float  # "어느 기기 앞도 아님" 상태의 RSSI 표준편차, dB


@dataclass(frozen=True, slots=True)
class ProximityModel:
    """장소 하나의 근접 모델. 파일 내용을 그대로 담고, 읽은 뒤에는 바뀌지 않는다."""

    place_id: str
    model_version: str
    calib_version: str
    window_sec: float  # 관측 구간 길이, 초
    stay: float  # 한 시간 칸에서 같은 상태에 머무를 확률
    min_distance_m: float  # 거리 계산의 최솟값, 미터
    calibration_method: str
    nodes: Mapping[str, NodeModel]  # node_id → NodeModel
    validation: Mapping[str, Any]  # 검증에 쓴 임계값과 결과. 계산에 쓰지 않는다 (#40)

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", _freeze(self.nodes))
        object.__setattr__(self, "validation", _freeze(self.validation))


_TOP_KEYS = (
    "place_id", "model_version", "calib_version", "window_sec", "transition",
    "min_distance_m", "calibration", "nodes",
)
_OPTIONAL_TOP_KEYS = ("validation",)
_NODE_KEYS = ("x", "y", "front_x", "front_y", "A", "n", "sigma", "none_rssi", "none_sigma")


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelFileError(f"{where}: JSON 객체여야 한다")
    return value


def _fixed_object(
    value: Any, where: str, required: tuple[str, ...], optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    """키가 정해진 JSON 객체다. 빠진 키와 모르는 키를 모두 거부한다 (오타를 막는다)."""
    obj = _object(value, where)
    missing = [key for key in required if key not in obj]
    unknown = [key for key in obj if key not in required and key not in optional]
    if missing or unknown:
        raise ModelFileError(f"{where}: 빠진 키 {missing}, 모르는 키 {unknown}")
    return obj


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelFileError(f"{where}: 비어 있지 않은 문자열이어야 한다 ({value!r})")
    return value


def _number(value: Any, where: str, *, gt: float | None = None, lt: float | None = None) -> float:
    """bool이 아닌 유한한 int 또는 float를 float로 바꾼다. gt보다 크고 lt보다 작아야 한다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelFileError(f"{where}: 숫자여야 한다 ({value!r})")
    try:
        number = float(value)
    except OverflowError:  # float로 바꿀 수 없을 만큼 큰 int
        number = inf
    if not isfinite(number):
        raise ModelFileError(f"{where}: 유한한 숫자여야 한다 ({value!r:.40})")  # 큰 int가 메시지를 채우지 않게 자른다
    if gt is not None and not number > gt:
        raise ModelFileError(f"{where}: {gt:g}보다 커야 한다 ({value!r})")
    if lt is not None and not number < lt:
        raise ModelFileError(f"{where}: {lt:g}보다 작아야 한다 ({value!r})")
    return number


def _node(node_id: str, raw: Any, where: str) -> NodeModel:
    _string(node_id, where)
    entry = _fixed_object(raw, where, _NODE_KEYS)
    return NodeModel(
        node_id=node_id,
        x=_number(entry["x"], f"{where}.x"),
        y=_number(entry["y"], f"{where}.y"),
        front_x=_number(entry["front_x"], f"{where}.front_x"),
        front_y=_number(entry["front_y"], f"{where}.front_y"),
        A=_number(entry["A"], f"{where}.A"),
        n=_number(entry["n"], f"{where}.n", gt=0.0),
        sigma=_number(entry["sigma"], f"{where}.sigma", gt=0.0),
        none_rssi=_number(entry["none_rssi"], f"{where}.none_rssi"),
        none_sigma=_number(entry["none_sigma"], f"{where}.none_sigma", gt=0.0),
    )


def load_model(path: Path) -> ProximityModel:
    """모델 파일 하나를 읽어 검사한다. 형식, 값, 경로가 맞지 않으면 `ModelFileError`.

    파일이 없는 경우처럼 읽기 자체가 실패하면 `OSError`가 그대로 올라간다.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:  # JSONDecodeError, UnicodeDecodeError
        raise ModelFileError(f"{path}: UTF-8 JSON으로 읽을 수 없다 ({error})") from error

    top = _fixed_object(raw, str(path), _TOP_KEYS, _OPTIONAL_TOP_KEYS)
    place_id = _string(top["place_id"], f"{path}: place_id")
    model_version = _string(top["model_version"], f"{path}: model_version")
    if path.parent.name != place_id:
        raise ModelFileError(
            f"{path}: place_id는 디렉터리 이름과 같아야 한다 (place_id={place_id!r}, 디렉터리={path.parent.name!r})"
        )
    if path.stem != model_version:
        raise ModelFileError(
            f"{path}: model_version은 파일 이름과 같아야 한다 (model_version={model_version!r}, 파일={path.stem!r})"
        )

    transition = _fixed_object(top["transition"], f"{path}: transition", ("stay",))
    calibration = _fixed_object(top["calibration"], f"{path}: calibration", ("method",))
    method = _string(calibration["method"], f"{path}: calibration.method")
    if method != "none":
        raise ModelFileError(f'{path}: calibration.method는 "none"만 지원한다 ({method!r})')

    raw_nodes = _object(top["nodes"], f"{path}: nodes")
    if not raw_nodes:
        raise ModelFileError(f"{path}: nodes: 기기가 하나 이상 있어야 한다")
    nodes = {
        node_id: _node(node_id, entry, f"{path}: nodes.{node_id}")
        for node_id, entry in raw_nodes.items()
    }

    return ProximityModel(
        place_id=place_id,
        model_version=model_version,
        calib_version=_string(top["calib_version"], f"{path}: calib_version"),
        window_sec=_number(top["window_sec"], f"{path}: window_sec", gt=0.0),
        stay=_number(transition["stay"], f"{path}: transition.stay", gt=0.0, lt=1.0),
        min_distance_m=_number(top["min_distance_m"], f"{path}: min_distance_m", gt=0.0),
        calibration_method=method,
        nodes=nodes,
        validation=_object(top.get("validation", {}), f"{path}: validation"),
    )


def load_models(root: Path, versions: Mapping[str, str]) -> dict[str, ProximityModel]:
    """`place_id → model_version`마다 모델 파일을 읽는다. 파일이 없으면 `ModelMismatchError`."""
    models: dict[str, ProximityModel] = {}
    for place_id, model_version in versions.items():
        path = root / place_id / f"{model_version}.json"
        if not path.is_file():
            raise ModelMismatchError(f"장소 {place_id!r}의 모델 파일이 없다: {path}")
        models[place_id] = load_model(path)
    return models
