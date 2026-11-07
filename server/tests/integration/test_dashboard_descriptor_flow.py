"""FR-12A·FR-13 기기 선언 발표 → 실제 SQLite 저장 → 대시보드 장소 목록과 점검 (specs/dashboard-device-descriptor/spec.md).

IOT 가 v3 발표를 해석하고 저장소가 신뢰를 선언 해시에 묶은 뒤, 대시보드 조회 서비스가 같은 DB 를 읽는다.
근접 추정과 감사만 fake 다 (`_world.World`).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path

import pytest

from _world import LOCKER, NOW, World
from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import render_response
from nearpilot.core.dashboard import DashboardQueryService
from nearpilot.db.repositories.dashboard_reads import SqliteDashboardReads
from nearpilot.iot.messages import decode_message
from nearpilot.shared.descriptor import parse_descriptor
from nearpilot.shared.enums import TrustState

SESSION = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def world():
    w = World()
    yield w
    w.close()


def dashboard(w: World, tmp_path: Path) -> DashboardQueryService:
    @contextmanager
    def open_reads():
        yield SqliteDashboardReads(w.conn)

    return DashboardQueryService(
        open_reads=open_reads, settings=lambda: w.settings, weak_rssi_dbm=-80.0, models=lambda: {},
        mqtt_ready=lambda: True, holders=lambda: (), reports_dir=tmp_path / "reports", clock=lambda: NOW,
        started_at=lambda: NOW, tz=timezone.utc,
    )


def announce(w: World, node_id: str, descriptor: dict, seq: int = 1) -> None:
    body = {"schema_version": 3, "node_id": node_id, "session_id": SESSION, "seq": seq,
            "sent_at": NOW.isoformat().replace("+00:00", "Z"), "descriptor": descriptor}
    w.repo.upsert_announced_node(
        decode_message(f"nearpilot/node/{node_id}/announce", json.dumps(body).encode(), received_at=NOW))


def device(service: DashboardQueryService, node_id: str) -> dict:
    places = render_response("places", service.places(), PersonNames())["places"]
    (found,) = [d for p in places for d in p["devices"] if d["nodeId"] == node_id]
    return found


def trust(service: DashboardQueryService, node_id: str) -> str:
    (found,) = [d for d in service.health()["devices"] if d["nodeId"] == node_id]
    return found["trust"]


def test_FR13_places_show_new_hash_after_redeclare(world, tmp_path):
    """동작을 바꾼 선언은 해시가 바뀌어 `discovered` 가 된다. 소유자는 장소 목록에서 새 선언과 해시를 본다."""
    service = dashboard(world, tmp_path)
    assert (device(service, "locker-1")["descriptorHash"], trust(service, "locker-1")) == (LOCKER.hash, "trusted")

    changed = LOCKER.to_json()
    changed["vendor"] = {"name": "acme", "model": "l2", "firmware": "2.0"}
    changed["actions"] = [changed["actions"][0]]  # close 동작을 뺐다
    announce(world, "locker-1", changed)

    new = parse_descriptor(changed)
    shown = device(service, "locker-1")
    assert new.hash != LOCKER.hash
    assert shown["descriptorHash"] == new.hash
    assert shown["vendor"] == {"name": "acme", "model": "l2", "firmware": "2.0"}
    assert [a["name"] for a in shown["actions"]] == ["open"]
    assert trust(service, "locker-1") == TrustState.DISCOVERED
    assert trust(service, "locker-2") == TrustState.TRUSTED  # 다른 기기는 그대로다


def test_FR12A_places_show_vendor_change_with_same_hash(world, tmp_path):
    """제작사 정보만 바뀌면 해시와 신뢰는 그대로이고, 장소 목록은 새 제작사 정보를 보인다."""
    service = dashboard(world, tmp_path)
    changed = LOCKER.to_json()
    changed["vendor"] = {"name": "nearpilot", "model": "locker", "firmware": "v3"}
    announce(world, "locker-1", changed)

    shown = device(service, "locker-1")
    assert (shown["descriptorHash"], shown["vendor"]["firmware"]) == (LOCKER.hash, "v3")
    assert trust(service, "locker-1") == TrustState.TRUSTED
