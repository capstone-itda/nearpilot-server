"""python -m tools.node_lab init|broker|run|demo."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import time

from tools.node_lab.setup import broker_process, initialize
from tools.node_lab.session import DiagnosticHandler, Lab, demo
from tools.node_sim.config import build_device, load_config as load_fleet


def parser():
    root = argparse.ArgumentParser(description="ESP32 관측·명령 연결 시험. server/에서 실행한다.")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="새 시험 폴더를 만든다. 기존 폴더는 덮어쓰지 않는다")
    init.add_argument("--dir", type=Path, default=Path("var/node-lab"))
    init.add_argument("--mode", choices=("sim", "hardware"), required=True)
    init.add_argument("--host", default="127.0.0.1")
    init.add_argument("--port", type=int, default=18883)
    init.add_argument("--node", action="append", dest="nodes")
    init.add_argument("--uuid", default="11111111-2222-3333-4444-555555555555")
    init.add_argument("--major", type=int, default=1)
    init.add_argument("--minor", type=int, default=2)
    for name in ("broker", "run", "demo"):
        command = commands.add_parser(name)
        command.add_argument("--dir", type=Path, default=Path("var/node-lab"))
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    folder = args.dir.resolve()
    try:
        if args.command == "init":
            initialize(folder, mode=args.mode, host=args.host, port=args.port,
                nodes=args.nodes or ["light-1", "light-2"], beacon_uuid=args.uuid,
                major=args.major, minor=args.minor)
            print(f"시험 설정: {folder}\n비밀번호 원문은 .env.* 파일에만 있다.")
            return 0
        if args.command == "broker":
            with broker_process(folder) as process:
                print("시험 브로커 실행 중. Ctrl+C로 종료한다.", flush=True)
                return process.wait()
        options = json.loads((folder / "lab.json").read_text(encoding="utf-8"))
        if args.command == "demo" and options["mode"] != "sim":
            raise ValueError("demo는 같은 PC 시뮬레이터 설정에서만 실행한다")
        from contextlib import ExitStack
        with ExitStack() as stack:
            if args.command == "demo":
                process = stack.enter_context(broker_process(folder))
                # MqttConnection has bounded startup/reconnect waits; ensure process did not immediately exit.
                time.sleep(0.2)
                if process.poll() is not None:
                    raise ValueError("시험 브로커 시작 실패. broker.log를 확인한다")
            lab = Lab(folder)
            stack.callback(lab.close)
            logger = logging.getLogger("nearpilot")
            handler = DiagnosticHandler(lab.evidence)
            logger.addHandler(handler)
            stack.callback(logger.removeHandler, handler)
            lab.start()
            if args.command == "demo":
                fleet = load_fleet(folder / "fleet.json")
                for config in fleet.devices:
                    device = build_device(fleet, config)
                    stack.callback(device.node.store.close)
                    stack.callback(device.stop)
                    device.start()
                demo(lab)
            else:
                lab.console()
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"시험을 끝내지 못했다: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
