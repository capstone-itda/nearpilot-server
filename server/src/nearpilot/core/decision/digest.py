"""⓪ 중복 검사에 쓰는 정규화 입력의 digest (spec 6절, #24)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from nearpilot.shared.models import TargetSpec, UseRequest


def _normalized_call(target: TargetSpec, action: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """대상 지정, 동작, 인자 하나를 정규화한다. 단일 요청과 묶음 요청의 항목이 같이 쓴다 (FR-22, #284)."""
    kind = target.node_id.rpartition("-")[0] if target.device_kind is None else target.device_kind
    normalized: dict[str, Any] = {
        "action": action,
        "target": {
            "capability": kind,
            "kind": target.kind.value,
            "node_id": target.node_id,
        },
    }
    if target.is_selector:
        normalized["target"]["start"] = target.start
        normalized["target"]["count"] = "all" if target.count is None else target.count
    if arguments is not None:
        normalized["arguments"] = dict(arguments)
    return normalized


def input_digest(req: UseRequest) -> str:
    """대상 지정, 동작, 원 요청 연결 ID로 digest를 만든다.

    `request_id`(키 자체), `received_at`, `host_kind` 는 넣지 않는다.
    호스트가 바뀌어도 재전송은 같은 요청이다.
    동작 인자는 받은 요청에만 넣는다. 인자 없는 요청의 digest 는 이전과 같다 (FR-09, FR-22).
    `target.capability` 키 이름과 명시 지정의 값(기기 ID 의 마지막 `-` 앞)은 이전 형식을 유지한다
    (spec core-device-actions 결정 3).
    거리 선택자(`near`·`far`)는 `target` 에 시작(`start`)과 개수(`count`, `all` 은 문자열)를 더 넣는다.
    다른 대상 지정의 digest 는 이전과 같다 (FR-22, #253).
    묶음 요청은 `batch` 에 항목마다 같은 정규화를 항목 순서대로 담는다. 항목 순서가 다르면 다른 입력이다.
    묶음이 아닌 요청의 digest 는 이전과 같다 (FR-22, #284).
    """
    if req.target.is_batch:
        normalized: dict[str, Any] = {
            "batch": [_normalized_call(item.target, item.action, item.arguments) for item in req.items],
            "parent_internal_request_id": req.parent_internal_request_id,
        }
    else:
        normalized = _normalized_call(req.target, req.action, req.arguments)
        normalized["parent_internal_request_id"] = req.parent_internal_request_id
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
