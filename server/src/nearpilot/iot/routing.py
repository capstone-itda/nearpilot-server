"""`node_id`별 명령 게이트웨이 라우팅 (FR-06, `architecture.md` "동반 기기", specs/iot-smartthings-companion 결정 1).

바인딩한 동반 기기의 `node_id` 명령은 그 SmartThings 게이트웨이로, 그 밖의 명령은 기본(MQTT) 게이트웨이로 보낸다.
CORE는 이 게이트웨이 하나만 본다. 판정과 결과를 바꾸지 않는다 (NFR-07).
"""

from __future__ import annotations

from collections.abc import Mapping

from nearpilot.shared.interfaces import NodeCommandGateway
from nearpilot.shared.models import CommandResult, NodeCommand


class RoutingCommandGateway:
    """`shared.interfaces.NodeCommandGateway`를 구현한다."""

    def __init__(self, default: NodeCommandGateway, routes: Mapping[str, NodeCommandGateway]):
        self._default = default
        self._routes = dict(routes)

    def send(self, command: NodeCommand, timeout_sec: float) -> CommandResult:
        return self._routes.get(command.node_id, self._default).send(command, timeout_sec)
