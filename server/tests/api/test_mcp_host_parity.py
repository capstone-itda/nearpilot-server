"""FR-10·NFR-09 호스트 3종이 도구 6개에서 같은 결과를 받는다. 서비스와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import pytest

from _mcp_client import Client, run

HOSTS = {
    "claude": "claude-ai/1.0",
    "chatgpt": "ChatGPT-User/1.0",
    "gemini": "Google-Gemini/1.0",
}
INIT = {"protocolVersion": "2025-06-18", "capabilities": {}}
CALLS = [
    ("request_use", {"target_spec": "nearest:locker", "action": "open", "request_id": "r1"}),
    ("request_access", {"node_id": "locker-1", "action": "open", "request_id": "a1"}),
    ("release_use", {"use_id": "use-1"}),
    ("get_use_status", {"use_id": "use-1"}),
    ("find_available_devices", {}),
    ("get_device_state", {"node_id": "locker-1"}),
]


def _host_client(name: str) -> Client:
    c = Client()
    headers = {"User-Agent": HOSTS[name]}
    r = run(c.post("initialize", {**INIT, "clientInfo": {"name": name, "version": "1"}}, headers=headers))
    assert r.status_code == 200
    c.headers = headers
    return c


def _call(c: Client, tool: str, args: dict):
    r = run(c.post("tools/call", {"name": tool, "arguments": args}, headers=c.headers))
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _calls_seen(c: Client):
    core = c.core
    return {
        "requests": [(r.user_id, r.request_id, r.target, r.action, r.host_kind, r.parent_internal_request_id)
                     for r in core.requests],
        "accesses": core.accesses, "releases": core.releases, "statuses": core.statuses,
        "devices": core.device_calls, "states": core.state_calls,
    }


def test_FR10_도구_목록은_호스트와_관계없이_같다():
    lists = []
    for name in HOSTS:
        c = _host_client(name)
        r = run(c.post("tools/list", headers=c.headers))
        assert r.status_code == 200
        lists.append(r.json()["result"]["tools"])
    assert {t["name"] for t in lists[0]} >= {tool for tool, _ in CALLS}
    assert lists[0] == lists[1] == lists[2]


@pytest.mark.parametrize("tool,args", CALLS, ids=[tool for tool, _ in CALLS])
def test_NFR09_도구_응답은_호스트와_관계없이_같다(tool, args):
    results = [_call(_host_client(name), tool, args) for name in HOSTS]
    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize("tool,args", CALLS, ids=[tool for tool, _ in CALLS])
def test_NFR09_서비스가_받는_호출은_호스트와_관계없이_같다(tool, args):
    seen = []
    for name in HOSTS:
        c = _host_client(name)
        _call(c, tool, args)
        seen.append(_calls_seen(c))
    assert seen[0] == seen[1] == seen[2]
    assert any(seen[0].values()), "서비스가 호출되지 않았다"
