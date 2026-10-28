"""FR-09·FR-11 실제 CORE 위임 객체를 MCP 앱에 연결한다. 기능 서비스는 fake 로 대신한다."""

from __future__ import annotations

from _mcp_client import NOW, Client
from nearpilot.core.decision.core_service import DelegatingCoreService
from nearpilot.shared.enums import ReasonCode, Verdict
from nearpilot.shared.models import Decision

_SECRET = "user-a"  # 토큰 계정. 응답 어디에도 나가면 안 된다


class LeakyServices:
    """허용 목록 밖의 필드를 일부러 담아 돌려준다."""

    def __init__(self):
        self.error: Exception | None = None

    def request_use(self, req):
        if self.error:
            raise self.error
        return Decision("internal-secret", req.request_id, Verdict.OK, ReasonCode.OK,
                        node_id="locker-1", use_id="use-1", p_max=0.97)

    def request_access(self, user_id, node_id, action, request_id):
        return Decision("internal-secret", request_id, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING,
                        node_id=node_id, use_id="leak")

    def find_available_devices(self, user_id):
        return [{"node_id": "locker-1", "capability": "locker", "occupancy": "rental",
                 "access": "operable", "holder": user_id, "place_id": "p1", "p": 0.9}]

    def get_device_state(self, user_id, node_id):
        return {"node_id": node_id, "capability": "locker", "occupancy": "rental", "state": "in_use",
                "online": True, "trusted": True, "access_mode": "open", "holder": user_id}

    def release_use(self, user_id, use_id):
        return Decision("internal-secret", "", Verdict.OK, ReasonCode.OK, node_id="locker-1", use_id=use_id)

    def get_use_status(self, user_id, use_id):
        return {"use_id": use_id, "state": "ACTIVE", "rental_due_at": NOW, "overtime_sec": 0.0,
                "closed_at": None, "user_id": user_id}


def client(services: LeakyServices) -> Client:
    c = Client()
    c.core = DelegatingCoreService(use=services, access=services, query=services, release=services)
    return c


def test_FR11_core_service_mcp_response_allowlist():
    c = client(LeakyServices())
    responses = [
        c.call({"target_spec": "locker-1", "action": "open", "request_id": "r1"}),
        c.call({"node_id": "locker-2", "action": "open", "request_id": "r2"}, tool="request_access"),
        c.call({}, tool="find_available_devices"),
        c.call({"node_id": "locker-1"}, tool="get_device_state"),
        c.call({"use_id": "use-1"}, tool="release_use"),
        c.call({"use_id": "use-1"}, tool="get_use_status"),
    ]
    for resp in responses:
        text = repr(resp)
        assert _SECRET not in text and "internal-secret" not in text and "p1" not in text
        assert "p_max" not in resp and "holder" not in resp
    assert responses[1]["use_id"] is None  # 승인 요청은 실행 허가가 아니다
    assert set(responses[2]["devices"][0]) == {"node_id", "capability", "occupancy", "access", "controls"}


def test_FR09_core_service_exception_reaches_mcp_guard():
    services = LeakyServices()
    services.error = RuntimeError("db down")
    resp = client(services).call({"target_spec": "locker-1", "action": "open", "request_id": "r1"})
    assert (resp["verdict"], resp["reason"], resp["use_id"]) == ("DENY", "INTERNAL_ERROR", None)
    assert "db down" not in repr(resp)
