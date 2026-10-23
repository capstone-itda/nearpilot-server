"""앱용 계정 REST를 실제 SQLite와 `DefaultAdminService`로 확인한다 (specs/api-rest-account/spec.md).

라우터는 저장소를 작업 스레드에서 부른다. SQLite 연결은 만든 스레드에서만 쓸 수 있어서
호출마다 같은 DB 파일에 새 연결을 연다. 서버 조립의 스레드별 연결과 같은 조건이다.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.rest.accounts import build_account_router
from nearpilot.core.admin import DefaultAdminService
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import Role
from nearpilot.shared.models import BeaconKey
from nearpilot.shared.tokens import hash_token

UUID = "e2c56db5-dffb-48d2-b060-d0f5a71096e0"


class PerCallDb:
    def __init__(self, path) -> None:
        self._path = str(path)

    @contextmanager
    def _conn(self):
        conn = connect(self._path)
        try:
            yield conn
        finally:
            conn.close()

    def register_account(self, *args):
        with self._conn() as conn:
            return DefaultAdminService(SqliteRepository(conn), SqliteAdminRepository(conn)).register_account(*args)

    def register_beacon(self, req):
        with self._conn() as conn:
            return DefaultAdminService(SqliteRepository(conn), SqliteAdminRepository(conn)).register_beacon(req)

    def account_by_token_hash(self, token_hash):
        with self._conn() as conn:
            return SqliteRepository(conn).account_by_token_hash(token_hash)

    def beacons_of(self, user_id):
        with self._conn() as conn:
            return list(SqliteRepository(conn).beacons_of(user_id))


@pytest.fixture
def db(tmp_path):
    return PerCallDb(tmp_path / "np.db")


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(build_account_router(db, Authenticator(db)))
    return TestClient(app)


def test_FR01_account_rest_with_real_admin_service(client, db):
    token = client.post("/accounts", json={"name": "방문자 A"}).json()["token"]
    account = db.account_by_token_hash(hash_token(token))
    assert account is not None and account.role is Role.VISITOR

    auth = {"Authorization": f"Bearer {token}"}
    assert client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 1}, headers=auth).status_code == 200
    # 같은 비콘을 다시 등록해도 거부하지 않는다.
    assert client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 1}, headers=auth).status_code == 200
    # 새 비콘을 등록하면 이전 비콘은 비활성화된다 (FR-01).
    assert client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 2}, headers=auth).status_code == 200
    assert db.beacons_of(account.user_id) == [BeaconKey(UUID, 1, 2)]

    other = client.post("/accounts", json={"name": "방문자 B"}).json()["token"]
    duplicate = client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 2},
                           headers={"Authorization": f"Bearer {other}"})
    assert duplicate.status_code == 409
    assert duplicate.json() == {"error": "BEACON_ALREADY_REGISTERED"}
