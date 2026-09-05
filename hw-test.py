#!/usr/bin/env python3
"""Send a short, safe visual self-test to a paired MiniToo over Bluetooth.

Skips sleep opcodes, stopwatch, and countdown (those can freeze the device
or blast an alarm). USB audio is left alone.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from PIL import Image, ImageDraw

from minitoo_protocol import (
    WIDTH,
    HEIGHT,
    encode_images,
    frame,
    live_announce,
    live_chunk_frames,
    is_live_ready,
)
from minitoo_rfcomm import connect_mac, default_mac, list_windows_devices


def guess_mac() -> str:
    env = default_mac()
    if env:
        return env
    for mac, name in list_windows_devices():
        if any(token in name.lower() for token in ("minitoo", "tiivoo", "divoom")):
            return mac
    raise SystemExit("no MiniToo MAC found; pair it or set DIVOOM_MAC")


def send_live(transport, blob: bytes, pace_s: float = 0.005, announce_timeout: float = 0.5) -> None:
    chunks = live_chunk_frames(blob)
    transport.send(live_announce(blob))
    deadline = time.time() + announce_timeout
    while time.time() < deadline:
        for packet in transport.recv_frames(0.15):
            if is_live_ready(packet):
                deadline = 0
                break
    transport.send_all(chunks, pace_s)
    print(f"  sent {len(chunks)} live chunks ({len(blob)} bytes)")


def labeled(color: tuple[int, int, int], text: str) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle((4, 4, WIDTH - 5, HEIGHT - 5), outline=(255, 255, 255), width=2)
    draw.text((12, 52), text, fill=(255, 255, 255))
    return img


def sweep_frames(n: int = 8) -> list[Image.Image]:
    frames: list[Image.Image] = []
    for i in range(n):
        img = Image.new("RGB", (WIDTH, HEIGHT), (12, 12, 18))
        draw = ImageDraw.Draw(img)
        x = int(i * (WIDTH - 24) / max(1, n - 1))
        draw.rectangle((x, 20, x + 24, HEIGHT - 20), fill=(255, 80, 40))
        draw.text((8, 4), f"SWEEP {i + 1}/{n}", fill=(220, 220, 220))
        frames.append(img)
    return frames


def main() -> int:
    mac = guess_mac()
    print(f"connecting to {mac} ...")
    transport = connect_mac(mac)
    print(f"connected RFCOMM {transport.channel}")
    try:
        print("1. brightness 35")
        transport.send(frame(0x32, bytes((35,))))
        time.sleep(0.8)

        print("2. brightness 80")
        body = json.dumps(
            {"Command": "Channel/SetBrightness", "Brightness": 80},
            separators=(",", ":"),
        ).encode("utf-8")
        transport.send(frame(0x01, body))
        time.sleep(0.8)

        print("3. RGB flash (red / green / blue / yellow)")
        colors = [
            labeled((180, 24, 24), "RED"),
            labeled((24, 160, 48), "GREEN"),
            labeled((32, 64, 200), "BLUE"),
            labeled((200, 170, 24), "YELLOW"),
        ]
        blob = encode_images(colors, speed_ms=400, quality=70, fit="stretch", resample_name="nearest")
        send_live(transport, blob)
        time.sleep(1.8)

        print("4. orange sweep bar")
        blob = encode_images(sweep_frames(8), speed_ms=180, quality=65, fit="stretch", resample_name="nearest")
        send_live(transport, blob)
        time.sleep(1.6)

        print("done. you should have seen dim->bright, four color cards, then a moving bar.")
        print("USB audio was not touched. hardware button returns to the clock face.")
        return 0
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
