"""⓪ 중복 검사에 쓰는 정규화 입력의 digest (spec 6절, #24)."""

from __future__ import annotations

import hashlib
import json

from nearpilot.shared.models import UseRequest


def input_digest(req: UseRequest) -> str:
    """대상 지정, 동작, 원 요청 연결 ID로 digest를 만든다.

    `request_id`(키 자체), `received_at`, `host_kind` 는 넣지 않는다.
    호스트가 바뀌어도 재전송은 같은 요청이다.
    """
    normalized = {
        "action": req.action.value,
        "parent_internal_request_id": req.parent_internal_request_id,
        "target": {
            "capability": req.target.capability.value,
            "kind": req.target.kind.value,
            "node_id": req.target.node_id,
        },
    }
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
