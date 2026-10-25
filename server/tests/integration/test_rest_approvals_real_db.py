"""앱용 승인 REST를 실제 SQLite와 `OwnerApprovalService`로 확인한다 (specs/api-rest-approval/spec.md).

라우터는 서비스를 작업 스레드에서 부른다. SQLite 연결은 만든 스레드에서만 쓸 수 있어서
호출마다 같은 DB 파일에 새 연결을 연다. 서버 조립의 스레드별 연결과 같은 조건이다.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.rest.approvals import build_approval_router
from nearpilot.core.approval import OwnerApprovalService
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import Action, Verdict
from nearpilot.shared.tokens import hash_token

from _world import NOW, OWNER, VISITOR_A, World



class PerCallDb:
    """서비스와 계정 조회를 호출마다 새 연결로 연다."""

    def __init__(self, path) -> None:
        self._path = str(path)

    @contextmanager
    def _service(self):
        conn = connect(self._path)
        try:
            yield OwnerApprovalService(SqliteRepository(conn), lambda: NOW)
        finally:
            conn.close()

    def pending_for(self, *args):
        with self._service() as service:
            return service.pending_for(*args)

    def get(self, *args):
        with self._service() as service:
            return service.get(*args)

    def decide(self, *args):
        with self._service() as service:
            return service.decide(*args)

    def account_by_token_hash(self, token_hash):
        conn = connect(self._path)
        try:
            return SqliteRepository(conn).account_by_token_hash(token_hash)
        finally:
            conn.close()


def _auth(user_id):
    return {"Authorization": f"Bearer token-{user_id}"}


def test_FR03_approval_rest_with_real_db(tmp_path):
    path = tmp_path / "nearpilot.db"
    world = World(db_path=str(path))
    try:
        decision = world.access.request_access(VISITOR_A, "locker-2", Action.OPEN, "a1")
        assert decision.verdict is Verdict.NEED_APPROVAL
        for user_id in (OWNER, VISITOR_A):
            world.conn.execute(
                "UPDATE user SET token_hash = ? WHERE user_id = ?", (hash_token(f"token-{user_id}"), user_id))
        world.conn.commit()
    finally:
        world.close()

    db = PerCallDb(path)
    app = FastAPI()
    app.include_router(build_approval_router(db, Authenticator(db)))
    client = TestClient(app)

    listed = client.get("/approvals", headers=_auth(OWNER))
    assert listed.status_code == 200
    [item] = listed.json()
    assert item["node_id"] == "locker-2"
    assert item["status"] == "pending"
    assert client.get("/approvals", headers=_auth(VISITOR_A)).json() == []

    url = f"/approvals/{item['approval_id']}"
    assert client.get(url, headers=_auth(VISITOR_A)).status_code == 403
    assert client.post(f"{url}/decision", headers=_auth(VISITOR_A), json={"decision": "approved"}).json() == {
        "error": "NOT_AUTHORIZED"}

    approved = client.post(f"{url}/decision", headers=_auth(OWNER), json={"decision": "approved"})
    again = client.post(f"{url}/decision", headers=_auth(OWNER), json={"decision": "approved"})
    opposite = client.post(f"{url}/decision", headers=_auth(OWNER), json={"decision": "denied"})

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["valid_until"] is not None
    assert again.status_code == 200
    assert again.json() == approved.json()
    assert opposite.status_code == 409
    assert opposite.json() == {"error": "ALREADY_DECIDED"}
    assert client.get("/approvals", headers=_auth(OWNER)).json() == []
    assert client.get("/approvals/missing", headers=_auth(OWNER)).status_code == 404
