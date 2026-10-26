"""Run from `server/`: `python -m tools.node_sim run --config tools/node_sim/fleet.example.json`."""

from __future__ import annotations

import argparse
import logging
import sys

from tools.node_sim.config import ConfigError, acl_sections, build_device, load_config

HELP = "commands: quit"


def main(argv: list[str] | None = None, *, read_line=sys.stdin.readline, write=print) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.node_sim")
    parser.add_argument("command", choices=("run", "acl"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--node", action="append", help="run only these node IDs")
    args = parser.parse_args(argv)
    try:
        fleet = load_config(args.config)
    except ConfigError as error:
        write(str(error))
        return 2
    if args.command == "acl":
        write(acl_sections(fleet), end="")
        return 0
    selected = [d for d in fleet.devices if not args.node or d.node_id in args.node]
    if not selected:
        write("No matching devices")
        return 2
    logging.basicConfig(level=logging.INFO)
    devices = {d.node_id: build_device(fleet, d) for d in selected}
    for device in devices.values():
        device.start()
    write(f"running {len(devices)} devices; {HELP}")
    try:
        while True:
            line = read_line()
            if not line:
                break
            words = line.split()
            if words == ["quit"]:
                break
            if words:
                write(HELP)
    except KeyboardInterrupt:
        pass
    finally:
        for device in devices.values():
            device.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
