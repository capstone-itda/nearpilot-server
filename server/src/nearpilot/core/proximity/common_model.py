"""공통 근접 모델 파일 읽기와 쓰기 (specs/core-proximity-common-model/spec.md 6절 결정 1, 9).

모델 파일의 경로는 `{모델 루트}/{model_version}.json`이다. 장소마다 두지 않고 모든 장소가 파일 하나를 같이 쓴다.
파일에는 장소 ID, 기기 ID, 좌표, 앵커별 파라미터가 없다. 역할(앞, 옆, 없음)별 정규분포의 평균과 표준편차 여섯 값,
HMM 전이, 관측 구간, 보정, 자동 영점의 최소 칸 수만 담는다.
읽을 때 형식, 값, 경로를 모두 검사하고 잘못된 파일은 `ModelFileError`로 거부한다. 읽은 모델은 바꿀 수 없다.
이 모듈은 파일을 읽고 쓸 뿐 사후확률을 계산하지 않는다.
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
    """쓸 모델이 없다 (설정한 버전의 파일이 없거나, 모델 없이 시작했다)."""


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


def _plain(value: object) -> object:
    """읽기 전용 매핑은 dict로, list와 tuple은 list로 안쪽까지 되돌려 JSON으로 쓸 수 있는 값으로 만든다."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class RoleParams:
    """역할 하나의 RSSI 정규분포. 보정한 칸 평균 RSSI가 따르는 분포다."""

    mean: float  # 평균, dBm
    sigma: float  # 표준편차, dB. 0보다 크다


@dataclass(frozen=True, slots=True)
class CommonModel:
    """모든 장소에 공통인 근접 모델 (spec 결정 1, 9). 장소 ID, 기기 ID, 좌표가 없다.

    파일 내용을 그대로 담고, 읽은 뒤에는 바뀌지 않는다.
    기기 i 앞 상태에서 앵커 i는 `front`, 다른 앵커는 `other`를 따른다. "어느 기기 앞도 아님" 상태에서는 모든 앵커가 `none`을 따른다.
    """

    model_version: str
    window_sec: float  # 관측 구간 길이, 초
    stay: float  # 한 시간 칸에서 같은 상태에 머무를 확률
    calibration_method: str  # "none" | "temperature"
    calibration_tau: float  # 우도 온도 τ. 증거의 로그우도를 나누는 값이다. calibration_method가 "none"이면 1.0이다
    front: RoleParams  # 기기 앞의 앵커
    other: RoleParams  # 방문자가 선 기기가 아닌 앵커
    none: RoleParams  # "어느 기기 앞도 아님" 상태의 모든 앵커
    auto_zero_min_bins: int  # 자동 영점을 찾는 데 필요한 표본 수. 1 이상이다
    validation: Mapping[str, Any]  # 검증에 쓴 값과 결과. 계산에 쓰지 않는다

    def __post_init__(self) -> None:
        # 계산은 τ만 읽고 모델 파일은 방법을 쓴다. 둘이 어긋난 모델은 쓴 파일과 계산이 달라서 만들지 않는다.
        if self.calibration_method == "none":
            if self.calibration_tau != 1.0:
                raise ValueError(f'calibration_method가 "none"이면 calibration_tau는 1.0이어야 한다 ({self.calibration_tau!r})')
        elif self.calibration_method == "temperature":
            if not 0.0 < self.calibration_tau < inf:
                raise ValueError(f"calibration_tau는 0보다 큰 유한한 수여야 한다 ({self.calibration_tau!r})")
        else:
            raise ValueError(f'calibration_method는 "none", "temperature" 중 하나여야 한다 ({self.calibration_method!r})')
        object.__setattr__(self, "validation", _freeze(self.validation))


_TOP_KEYS = ("model_version", "window_sec", "transition", "calibration", "observation", "auto_zero")
_OPTIONAL_TOP_KEYS = ("validation",)
_ROLE_KEYS = ("mean", "sigma")


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelFileError(f"{where}: JSON 객체여야 한다")
    return value


def _fixed_object(
    value: Any, where: str, required: tuple[str, ...], optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    """키가 정해진 JSON 객체다. 빠진 키와 모르는 키를 모두 거부한다 (오타와 배치 항목을 막는다)."""
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


def _min_bins(value: Any, where: str) -> int:
    """bool이 아닌 1 이상의 정수다. 1.5나 20.0 같은 float는 거부한다."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelFileError(f"{where}: 정수여야 한다 ({value!r})")
    if value < 1:
        raise ModelFileError(f"{where}: 1 이상이어야 한다 ({value!r})")
    return value


def _role(raw: Any, where: str) -> RoleParams:
    entry = _fixed_object(raw, where, _ROLE_KEYS)
    return RoleParams(
        mean=_number(entry["mean"], f"{where}.mean"),
        sigma=_number(entry["sigma"], f"{where}.sigma", gt=0.0),
    )


def load_common_model(path: Path) -> CommonModel:
    """모델 파일 하나를 읽어 검사한다. 형식, 값, 경로가 맞지 않으면 `ModelFileError`.

    파일이 없는 경우처럼 읽기 자체가 실패하면 `OSError`가 그대로 올라간다.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:  # JSONDecodeError, UnicodeDecodeError
        raise ModelFileError(f"{path}: UTF-8 JSON으로 읽을 수 없다 ({error})") from error

    top = _fixed_object(raw, str(path), _TOP_KEYS, _OPTIONAL_TOP_KEYS)
    model_version = _string(top["model_version"], f"{path}: model_version")
    if path.stem != model_version:
        raise ModelFileError(
            f"{path}: model_version은 파일 이름과 같아야 한다 (model_version={model_version!r}, 파일={path.stem!r})"
        )

    transition = _fixed_object(top["transition"], f"{path}: transition", ("stay",))
    # 보정 방법마다 키가 정해져 있다. method를 먼저 읽고, 그 방법의 키로 검사한다.
    calibration = _object(top["calibration"], f"{path}: calibration")
    method = _string(calibration.get("method"), f"{path}: calibration.method")
    if method == "none":
        _fixed_object(calibration, f"{path}: calibration", ("method",))
        tau = 1.0
    elif method == "temperature":
        _fixed_object(calibration, f"{path}: calibration", ("method", "tau"))
        tau = _number(calibration["tau"], f"{path}: calibration.tau", gt=0.0)
    else:
        raise ModelFileError(f'{path}: calibration.method는 "none", "temperature"만 지원한다 ({method!r})')

    observation = _fixed_object(top["observation"], f"{path}: observation", ("front", "other", "none"))
    auto_zero = _fixed_object(top["auto_zero"], f"{path}: auto_zero", ("min_bins",))

    return CommonModel(
        model_version=model_version,
        window_sec=_number(top["window_sec"], f"{path}: window_sec", gt=0.0),
        stay=_number(transition["stay"], f"{path}: transition.stay", gt=0.0, lt=1.0),
        calibration_method=method,
        calibration_tau=tau,
        front=_role(observation["front"], f"{path}: observation.front"),
        other=_role(observation["other"], f"{path}: observation.other"),
        none=_role(observation["none"], f"{path}: observation.none"),
        auto_zero_min_bins=_min_bins(auto_zero["min_bins"], f"{path}: auto_zero.min_bins"),
        validation=_object(top.get("validation", {}), f"{path}: validation"),
    )


def load_model_version(root: Path, model_version: str) -> CommonModel:
    """`{root}/{model_version}.json`을 읽는다. 파일이 아니면 `ModelMismatchError`."""
    path = root / f"{model_version}.json"
    if not path.is_file():
        raise ModelMismatchError(f"모델 {model_version!r}의 파일이 없다: {path}")
    return load_common_model(path)


def _role_to_dict(role: RoleParams) -> dict[str, float]:
    return {"mean": role.mean, "sigma": role.sigma}


def common_model_to_dict(model: CommonModel) -> dict[str, Any]:
    """모델을 모델 파일의 JSON 내용으로 바꾼다. `load_common_model`이 읽는 형식이고, 키 순서는 파일에 쓰는 순서다.

    `validation`은 비어 있으면 키를 넣지 않고, 있으면 읽기 전용 매핑을 dict로, tuple을 list로 되돌린다.
    보정 방법과 τ가 맞는지는 `CommonModel`이 만들 때 검사한다.
    """
    if model.calibration_method == "none":
        calibration: dict[str, Any] = {"method": "none"}
    else:
        calibration = {"method": "temperature", "tau": model.calibration_tau}
    content: dict[str, Any] = {
        "model_version": model.model_version,
        "window_sec": model.window_sec,
        "transition": {"stay": model.stay},
        "calibration": calibration,
        "observation": {
            "front": _role_to_dict(model.front),
            "other": _role_to_dict(model.other),
            "none": _role_to_dict(model.none),
        },
        "auto_zero": {"min_bins": model.auto_zero_min_bins},
    }
    if model.validation:
        content["validation"] = _plain(model.validation)
    return content


def _is_path_component(value: object) -> bool:
    """경로 조각 하나인가. 모델 파일 경로 `{root}/{model_version}.json`이 루트 밖으로 나가지 않게 한다."""
    return isinstance(value, str) and value not in ("", ".", "..") and "/" not in value and "\\" not in value


def write_common_model_file(root: Path, model: CommonModel) -> Path:
    """모델 파일 `{root}/{model_version}.json`을 새로 쓰고 그 경로를 돌려준다. `root`가 없으면 만든다.

    `model_version`이 경로 조각 하나가 아니면(빈 값, `.`, `..`, `/`나 `\\`가 있다) `ValueError`다.
    JSON 직렬화를 먼저 한다. 무한대나 NaN이 남아 있어서 `ValueError`가 나면 디렉터리도 파일도 만들지 않는다.
    같은 버전의 파일이 이미 있으면 `FileExistsError`가 그대로 올라가고, 있는 파일은 바뀌지 않는다 (§1.2).
    """
    if not _is_path_component(model.model_version):
        raise ValueError(f"model_version: 경로 구분자가 없는 비어 있지 않은 이름이어야 한다 ({model.model_version!r})")
    path = root / f"{model.model_version}.json"
    text = json.dumps(common_model_to_dict(model), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    root.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as file:  # 새로 만들기 모드라서 있는 파일을 덮어쓰지 않는다
        file.write(text)
    return path
