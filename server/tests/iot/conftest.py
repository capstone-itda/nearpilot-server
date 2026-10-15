"""Shared IOT fixtures: a real localhost Mosquitto with temporary credentials.

No public broker or production credentials. Do not add `__init__.py` here:
`tools` must resolve to `server/tools/`, which this file puts on sys.path.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

SERVER = Path(__file__).resolve().parents[2]
CONFIG = SERVER / "config"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))


@dataclass
class Broker:
    executable: str
    config: Path
    port: int
    passwords: dict
    secret_file: Path
    process: object = None
    log_file: object = None

    def start(self):
        self.log_file = self.config.with_suffix(".log").open("ab")
        self.process = subprocess.Popen([self.executable, "-c", str(self.config)],
            stdout=self.log_file, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.stop()
                pytest.fail("Temporary Mosquitto failed to start")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return
            except OSError:
                threading.Event().wait(0.02)
        self.stop()
        pytest.fail("Temporary Mosquitto startup timed out")

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.process = None
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None


@pytest.fixture
def broker(tmp_path):
    executable = os.environ.get("MOSQUITTO_EXECUTABLE") or shutil.which("mosquitto")
    passwd = os.environ.get("MOSQUITTO_PASSWD_EXECUTABLE") or shutil.which("mosquitto_passwd")
    if not executable or not passwd:
        pytest.skip("Real Mosquitto and mosquitto_passwd are required")
    passwords = {user: secrets.token_hex(20) for user in ("nearpilot-server", "node-a", "node-b")}
    password_file = tmp_path / ".env.passwords"
    # Convert temporary plaintext entries in place; no passwords in argv/logs.
    password_file.write_text("".join(f"{user}:{secret}\n" for user, secret in passwords.items()),
                             encoding="utf-8")
    result = subprocess.run([passwd, "-U", str(password_file)], capture_output=True)
    assert result.returncode == 0, "Temporary password hashing failed"
    secret_file = tmp_path / ".env.server-password"
    secret_file.write_text(passwords["nearpilot-server"], encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    text = (CONFIG / "mosquitto.conf").read_text(encoding="utf-8")
    acl_file = tmp_path / "mosquitto.acl"
    shutil.copyfile(CONFIG / "mosquitto.acl.example", acl_file)
    text = text.replace("listener 1883 127.0.0.1", f"listener {port} 127.0.0.1")
    text = text.replace("config/.env.mosquitto-passwords", password_file.as_posix())
    text = text.replace("config/mosquitto.acl.example", acl_file.as_posix())
    config = tmp_path / "mosquitto.conf"
    config.write_text(text, encoding="utf-8")
    running = Broker(executable, config, port, passwords, secret_file)
    running.start()
    try:
        yield running
    finally:
        running.stop()
