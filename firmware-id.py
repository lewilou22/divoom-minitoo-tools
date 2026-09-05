#!/usr/bin/env python3
"""Read-only firmware identity dump from a MiniToo over SPP.

Does not flash, dump SPI, or download OTA binaries. 0xBD 0x2B often makes
the device emit its boot JSON (Sys/DevUpdateConf, Device/GetFileVersion).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from minitoo_protocol import frame, response_body
from minitoo_rfcomm import connect_mac, default_mac, list_windows_devices

KEEPALIVE_PREFIX = bytes.fromhex("4e6f62")  # "Nob"


def guess_mac() -> str:
    env = default_mac()
    if env:
        return env
    for mac, name in list_windows_devices():
        if any(token in name.lower() for token in ("minitoo", "tiivoo", "divoom")):
            return mac
    raise SystemExit("no MiniToo MAC found; pair it or set DIVOOM_MAC")


def send_json(transport, obj: dict) -> None:
    transport.send(frame(0x01, json.dumps(obj, separators=(",", ":")).encode("utf-8")))


def collect(transport, seconds: float) -> list[bytes]:
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
            return f"json {body.decode('utf-8', 'replace')}"
        if body and body.startswith(KEEPALIVE_PREFIX):
            return f"keepalive {body.hex(' ')}"
        return f"ack op={echoed:02x} body={body.hex(' ') if body else ''}"
    return f"op={op:02x} {packet.hex(' ')}"


def dump_device(mac: str) -> int:
    print(f"connecting {mac}")
    transport = connect_mac(mac)
    print(f"RFCOMM {transport.channel}")
    try:
        print("-- drain --")
        for pkt in collect(transport, 1.2):
            print(" ", describe(pkt)[:400])

        probes = [
            ("json Device/GetStorageStatus", lambda: send_json(transport, {"Command": "Device/GetStorageStatus"})),
            ("json Device/GetUpdateInfo", lambda: send_json(transport, {"Command": "Device/GetUpdateInfo"})),
            ("json Sys/DevUpdateConf", lambda: send_json(transport, {"Command": "Sys/DevUpdateConf"})),
            ("json Device/GetFileVersion FileType=1", lambda: send_json(transport, {"Command": "Device/GetFileVersion", "FileType": 1})),
            ("raw 0x13 working mode", lambda: transport.send(frame(0x13))),
            ("raw 0x15 SD present", lambda: transport.send(frame(0x15))),
            ("raw 0x76 device name", lambda: transport.send(frame(0x76))),
            ("raw 0xBD 0x18 power-on channel", lambda: transport.send(frame(0xBD, bytes((0x18,))))),
            ("raw 0xBD 0x27 ANCS capability", lambda: transport.send(frame(0xBD, bytes((0x27,))))),
            ("raw 0xBD 0x2B device info", lambda: transport.send(frame(0xBD, bytes((0x2B, 0x00))))),
        ]
        for label, send in probes:
            print(f"-- {label} --")
            send()
            wait = 2.4 if "0x2B" in label else 1.0
            pkts = collect(transport, wait)
            if not pkts:
                print("  (no reply)")
            for pkt in pkts:
                text = describe(pkt)
                if "Tomato" in text:
                    continue
                print(" ", text[:500])
        return 0
    finally:
        transport.close()


def post_update_file(base: str, hardware: int) -> dict | None:
    url = f"{base.rstrip('/')}/GetUpdateFileV3"
    payload = json.dumps(
        {"Hardware": hardware, "IsTest": False, "Language": "EN", "UpdateFlag": 2},
        separators=(",", ":"),
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "minitoo-firmware-id/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None


def decode_explain(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.b64decode(padded).decode("utf-8", "replace")
    except Exception:
        return value


def interesting(hardware: int, data: dict) -> bool:
    blob = json.dumps(data).lower()
    explain = decode_explain(data.get("Explain")).lower()
    tokens = ("minitoo", "tiivoo", "tivoo 2", "jieli", "br28")
    if any(token in blob or token in explain for token in tokens):
        return True
    version = data.get("Version")
    if isinstance(version, int) and version in {240, 2400, 20400}:
        return True
    return False


def scan_one(hardware: int) -> tuple[int, dict | None, str | None]:
    for base in ("https://app.divoom-gz.com", "https://appin.divoom-gz.com"):
        data = post_update_file(base, hardware)
        if data and isinstance(data.get("Version"), int):
            return hardware, data, base
    return hardware, None, None


def scan_cloud(lo: int, hi: int) -> int:
    hits = 0
    rows: list[tuple[int, dict, str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(scan_one, hardware) for hardware in range(lo, hi + 1)]
        for fut in as_completed(futures):
            hardware, data, used = fut.result()
            if data is None or used is None:
                continue
            rows.append((hardware, data, used))
    for hardware, data, used in sorted(rows):
        version = data.get("Version")
        file_id = str(data.get("FileId") or "")
        explain = decode_explain(data.get("Explain"))
        mark = " ***" if interesting(hardware, data) else ""
        print(f"Hardware {hardware:3d} Version={version} FileId={file_id[:56]}{mark}")
        if explain:
            print("         " + explain[:180].encode("ascii", "replace").decode("ascii"))
        if mark:
            hits += 1
    print(f"-- cloud scan {lo}-{hi} done, listed={len(rows)} MiniToo-like={hits} --")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Identify MiniToo firmware without flashing")
    parser.add_argument("--mac", help="Bluetooth MAC")
    parser.add_argument("--cloud", action="store_true", help="scan Divoom GetUpdateFileV3 catalog (metadata only)")
    parser.add_argument("--from", dest="lo", type=int, default=61)
    parser.add_argument("--to", dest="hi", type=int, default=120)
    parser.add_argument("--skip-device", action="store_true")
    args = parser.parse_args()
    rc = 0
    if not args.skip_device:
        rc = dump_device(args.mac or guess_mac())
    if args.cloud:
        rc = scan_cloud(args.lo, args.hi) or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
