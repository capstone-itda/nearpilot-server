"""Simulator run configuration (JSON). Relative paths resolve from the working directory.

No passwords are stored here: each device names its own password file, which
follows the `.env.*` ignore rule (NFR-08).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from nearpilot.shared.config import Settings
from tools.node_sim.device import BrokerConfig, Credentials, SimDevice
from tools.node_sim.node import ACTIONS, Beacon, Scenario, SimNode
from tools.node_sim.store import CommandStore

REPORTS = ("announce", "rssi", "status", "result")


class ConfigError(ValueError):
    """The simulator configuration is invalid."""


@dataclass(frozen=True)
class DeviceConfig:
    node_id: str
    place: str
    capability: str
    occupancy: str
    username: str
    password_file: Path = field(repr=False)
    store: Path
    scenario: Scenario


@dataclass(frozen=True)
class FleetConfig:
    broker: BrokerConfig
    settings: Settings
    devices: tuple[DeviceConfig, ...]


def _scenario(data: dict) -> Scenario:
    data = dict(data)
    data["beacons"] = tuple(Beacon(**beacon) for beacon in data.get("beacons", ()))
    return Scenario(**data)


def load_config(path: str | Path) -> FleetConfig:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        fleet = FleetConfig(BrokerConfig(**data["broker"]), Settings(**data.get("settings", {})),
            tuple(DeviceConfig(
                node_id=item["node_id"], place=item["place"], capability=item["capability"],
                occupancy=item["occupancy"], username=item["username"],
                password_file=Path(item["password_file"]), store=Path(item["store"]),
                scenario=_scenario(item["scenario"])) for item in data["devices"]))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigError(f"Invalid simulator configuration: {type(error).__name__}") from None
    for key in ("node_id", "username", "store"):
        values = [str(getattr(device, key)) for device in fleet.devices]
        if len(values) != len(set(values)):
            raise ConfigError(f"Duplicate device {key}")
    for device in fleet.devices:
        if (not device.node_id or any(c in device.node_id for c in "/+#\x00")
                or device.capability not in ACTIONS
                or device.occupancy not in ("rental", "shared") or not device.username):
            raise ConfigError("Invalid device entry")
    return fleet


def build_device(fleet: FleetConfig, device: DeviceConfig, **kwargs) -> SimDevice:
    """A fresh device over its persistent store; the same store models a restart."""
    node = SimNode(device.node_id, device.capability, device.occupancy,
                   CommandStore(device.store), device.scenario)
    return SimDevice(node, fleet.broker, Credentials(device.username, device.password_file),
                     fleet.settings, **kwargs)


def acl_sections(fleet: FleetConfig) -> str:
    """Mosquitto ACL sections for the fleet's device accounts (server section excluded)."""
    blocks = []
    for device in fleet.devices:
        base = f"nearpilot/node/{device.node_id}"
        lines = [f"user {device.username}"]
        lines += [f"topic write {base}/{kind}" for kind in REPORTS]
        lines += [f"topic read {base}/{kind}" for kind in ("cmd", "occupancy")]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
