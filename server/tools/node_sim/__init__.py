"""MQTT v2 device simulator (specs/iot-node-simulator/spec.md).

Virtual devices use real MQTT connections and per-device credentials. Their
control signal outputs are simulated; no physical state is reported.
"""

from tools.node_sim.config import (
    ConfigError, DeviceConfig, FleetConfig, acl_sections, build_device, load_config,
)
from tools.node_sim.device import BrokerConfig, Credentials, SimDevice
from tools.node_sim.node import Beacon, Scenario, SimNode
from tools.node_sim.store import CommandStore, StorageError

__all__ = [
    "Beacon", "BrokerConfig", "CommandStore", "ConfigError", "Credentials", "DeviceConfig",
    "FleetConfig", "Scenario", "SimDevice", "SimNode", "StorageError", "acl_sections",
    "build_device", "load_config",
]
