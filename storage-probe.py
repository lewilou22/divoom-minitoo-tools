#!/usr/bin/env python3
"""Ask the MiniToo what it knows about storage. Read-only SPP probes."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from minitoo_protocol import frame, response_body
from minitoo_rfcomm import connect_mac, default_mac, list_windows_devices


def guess_mac() -> str:
    env = default_mac()
    if env:
        return env
    for mac, name in list_windows_devices():
        if any(token in name.lower() for token in ("minitoo", "tiivoo", "divoom")):
            return mac
    raise SystemExit("no MiniToo MAC found")


def send_json(transport, obj: dict) -> None:
    transport.send(frame(0x01, json.dumps(obj, separators=(",", ":")).encode("utf-8")))


def collect(transport, seconds: float = 1.2) -> list[bytes]:
    deadline = time.time() + seconds
    out: list[bytes] = []
    while time.time() < deadline:
        out.extend(transport.recv_frames(0.2))
    return out


def describe(packet: bytes) -> str:
    if len(packet) < 6:
        return packet.hex(" ")
    op = packet[3]
    if op == 0x04 and len(packet) > 6:
        echoed = packet[4]
        body = response_body(packet)
        if body and body[:1] == b"{":
            try:
                return f"json {body.decode('utf-8', 'replace')}"
            except Exception:
                pass
        return f"ack op={echoed:02x} body={body.hex(' ') if body else ''}"
    return f"op={op:02x} {packet.hex(' ')}"


def main() -> int:
    mac = guess_mac()
    print(f"connecting {mac}")
    transport = connect_mac(mac)
    print(f"RFCOMM {transport.channel}")
    try:
        print("-- drain background --")
        for pkt in collect(transport, 1.0):
            print("  bg", describe(pkt)[:200])

        probes = [
            ("json Device/GetStorageStatus", lambda: send_json(transport, {"Command": "Device/GetStorageStatus"})),
            ("json Device/GetFileVersion", lambda: send_json(transport, {"Command": "Device/GetFileVersion", "FileType": 1})),
            ("json Sys/DevUpdateConf", lambda: send_json(transport, {"Command": "Sys/DevUpdateConf"})),
            ("json Device/GetUpdateInfo", lambda: send_json(transport, {"Command": "Device/GetUpdateInfo"})),
            ("json WhiteNoise/Get", lambda: send_json(transport, {"Command": "WhiteNoise/Get"})),
            ("raw 0x15 SD status", lambda: transport.send(frame(0x15))),
            ("raw 0x06 SD play name", lambda: transport.send(frame(0x06))),
            ("raw 0x07 SD music list", lambda: transport.send(frame(0x07))),
            ("raw 0xb4 SD music info", lambda: transport.send(frame(0xB4))),
            ("raw 0x13 working mode", lambda: transport.send(frame(0x13))),
            ("raw 0x76 device name", lambda: transport.send(frame(0x76))),
            ("raw 0x7d SD list total", lambda: transport.send(frame(0x7D))),
            ("raw 0x8e user-define 0", lambda: transport.send(frame(0x8E, bytes((0,))))),
            ("raw 0x8e user-define 1", lambda: transport.send(frame(0x8E, bytes((1,))))),
            ("raw 0x8e user-define 2", lambda: transport.send(frame(0x8E, bytes((2,))))),
            ("raw 0xBD 0x18 power-on channel", lambda: transport.send(frame(0xBD, bytes((0x18,))))),
        ]
        for label, send in probes:
            print(f"-- {label} --")
            send()
            pkts = collect(transport, 1.0)
            if not pkts:
                print("  (no reply)")
            for pkt in pkts:
                text = describe(pkt)
                if "F7 55" in pkt.hex(" ").upper() or "Tomato" in text:
                    continue
                print(" ", text[:240])
        return 0
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
