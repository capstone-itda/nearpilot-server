"""`nearpilot provision` (NFR-12, specs/core-ops-setup/spec.md 5절·8절).

실제 포트를 열지 않는다. `tools.node_lab.provision.run`을 가짜로 바꿔 끼우고, pyserial은 빈 모듈로 대신한다.
주입 절차 자체는 `tests/tools/test_node_lab_provision.py`가 확인한다.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK, main

REQUIRED = ["--node", "light-1", "--port", "COM9", "--time-trust-sec", "120", "--time-resync-sec", "60"]


class FakeRun:
    """`tools.node_lab.provision.run`을 대신한다. 받은 값을 쌓고 정해 둔 결과를 돌려준다."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.ok = True

    def __call__(self, folder, node, port_name, values, **options) -> bool:
        self.calls.append(dict(folder=folder, node=node, port=port_name, values=values, **options))
        return self.ok


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr("tools.node_lab.provision.run", fake)
    monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))
    return fake


@pytest.fixture
def lab(tmp_path: Path) -> Path:
    folder = tmp_path / "시험 폴더"
    folder.mkdir()
    (folder / "lab.json").write_text(json.dumps({"nodes": ["light-1"]}), encoding="utf-8")
    (folder / "server.toml").write_text(
        '[mqtt]\nhost = "10.0.0.5"\nport = 18883\n[delivery.clock_skew_sec]\n"light-1" = 1.0\n', encoding="utf-8")
    (folder / "fleet.json").write_text(json.dumps({"devices": [
        {"node_id": "light-1", "capability": "light", "occupancy": "shared"}]}), encoding="utf-8")
    (folder / ".env.light-1").write_text("mqtt-secret\n", encoding="utf-8")
    (folder / ".env.wifi.json").write_text(json.dumps({"ssid": "랩", "password": "wifi-secret"}), encoding="utf-8")
    return folder.resolve()


def test_NFR12_provision_defaults_time_server_to_broker_host(lab, fake_run):
    assert main(["--dir", str(lab), "provision", *REQUIRED]) == EXIT_OK
    [call] = fake_run.calls
    assert (call["folder"], call["node"], call["port"]) == (lab, "light-1", "COM9")
    assert call["values"]["time_server"] == "10.0.0.5"
    assert call["values"]["time_trust_ms"] == "120000"
    assert (call["timeout"], call["begin_interval"]) == (10.0, 1.0)


def test_NFR12_provision_uses_given_time_server(lab, fake_run):
    assert main(["provision", *REQUIRED, "--time-server", "10.0.0.9", "--dir", str(lab)]) == EXIT_OK
    assert fake_run.calls[0]["values"]["time_server"] == "10.0.0.9"


def test_NFR12_provision_device_failure_exit_code(lab, fake_run):
    fake_run.ok = False
    assert main(["--dir", str(lab), "provision", *REQUIRED]) == EXIT_FAILED


def test_NFR12_provision_requires_dir(fake_run, capsys):
    assert main(["provision", *REQUIRED]) == EXIT_INVALID
    assert "--dir" in capsys.readouterr().err
    assert fake_run.calls == []


def test_NFR12_provision_without_pyserial(lab, fake_run, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "serial", None)  # import serial이 ImportError가 된다
    assert main(["--dir", str(lab), "provision", *REQUIRED]) == EXIT_INVALID
    assert "pip install pyserial" in capsys.readouterr().err
    assert fake_run.calls == []


def test_NFR12_provision_rejects_unlisted_node(lab, fake_run, capsys):
    argv = ["--dir", str(lab), "provision", *REQUIRED]
    argv[argv.index("light-1")] = "light-9"
    assert main(argv) == EXIT_INVALID
    assert "시험 폴더의 값" in capsys.readouterr().err
    assert fake_run.calls == []
