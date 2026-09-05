#!/usr/bin/env python3
"""Play a video or GIF on a Divoom MiniToo from Windows (or Linux).

The MiniToo is not a video player. This tool:

  1. Scales frames to 160x128 (8x10 cells)
  2. Encodes them into a live-animation blob (opcode 0x8B)
  3. Pushes 256-byte chunks over Classic RFCOMM / a Bluetooth COM port

Default encoding is magic 0x25 (RGB + zstd), matching the official Android app.
If a clip's zstd payload exceeds 64 KB, it falls back to the proven 0x23 JPEG blob.

A single upload holds at most 255 frames. Longer files are sent as successive
clips. USB (the cable you are listening on) is speakers + media keys only —
it cannot carry pixel frames. Pass --usb to play sound on that USB speaker
while the pictures still go over Bluetooth.

Examples:
  python core/video-play.py --list
  python core/video-play.py --usb movie.mp4
  python core/video-play.py --mac AA:BB:CC:DD:EE:FF clip.mp4
  python core/video-play.py --com COM5 movie.mp4 --fps 8 --clip-frames 24
  python core/video-play.py --encode-only animation.gif -o live.raw
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from PIL import Image, ImageSequence

from minitoo_protocol import (
    MAX_FRAMES,
    MIN_SPEED_MS,
    RESAMPLE_MODES,
    encode_live_images,
    is_live_ready,
    live_announce,
    live_chunk_frames,
    render_frame,
    requested_chunk_index,
    write_live_rawfile,
)
from minitoo_rfcomm import (
    MiniTooTransport,
    bluetooth_adapter_message,
    connect_mac,
    connect_serial,
    default_mac,
    list_com_ports,
    list_windows_devices,
)
from usb_minitoo import format_usb_report, usb_audio_present

IMAGE_SUFFIXES = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found on PATH. Install it from https://ffmpeg.org/download.html")
    return path


def ffplay_bin() -> str | None:
    return shutil.which("ffplay")


def scale_filter(fit: str) -> str:
    if fit == "stretch":
        return "scale=160:128"
    if fit == "contain":
        return "scale=160:128:force_original_aspect_ratio=decrease,pad=160:128:(ow-iw)/2:(oh-ih)/2:black"
    if fit == "cover":
        return "scale=160:128:force_original_aspect_ratio=increase,crop=160:128"
    raise ValueError(f"unknown fit mode: {fit}")


def iter_ffmpeg_frames(
    path: Path,
    fps: float,
    fit: str,
    start_s: float = 0.0,
    seconds: float | None = None,
) -> Iterator[Image.Image]:
    vf = f"{scale_filter(fit)},fps={fps}"
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error"]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", str(path)]
    if seconds is not None:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd += ["-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    frame_size = 160 * 128 * 3
    try:
        while True:
            data = proc.stdout.read(frame_size)
            if len(data) < frame_size:
                break
            yield Image.frombytes("RGB", (160, 128), data)
    finally:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if proc.returncode not in (0, None, -9) and err.strip():
            raise RuntimeError(f"ffmpeg failed: {err.strip()}")


def iter_image_frames(path: Path, fit: str, resample_name: str) -> Iterator[Image.Image]:
    resample = RESAMPLE_MODES[resample_name]
    img = Image.open(path)
    for frame in ImageSequence.Iterator(img):
        yield render_frame(frame, fit, resample)


def iter_source_frames(
    path: Path,
    fps: float,
    fit: str,
    resample_name: str,
    start_s: float = 0.0,
    seconds: float | None = None,
) -> Iterator[Image.Image]:
    if path.suffix.lower() in IMAGE_SUFFIXES:
        yield from iter_image_frames(path, fit, resample_name)
        return
    yield from iter_ffmpeg_frames(path, fps, fit, start_s=start_s, seconds=seconds)


def clip_images(images: Iterator[Image.Image], clip_frames: int) -> Iterator[list[Image.Image]]:
    batch: list[Image.Image] = []
    for img in images:
        batch.append(img.convert("RGB"))
        if len(batch) >= clip_frames:
            yield batch
            batch = []
    if batch:
        yield batch


def send_live_clip(transport: MiniTooTransport, blob: bytes, pace_s: float, announce_timeout: float) -> int:
    chunks = live_chunk_frames(blob)
    transport.recv_frames(0.05)
    ready = False
    wait_s = max(0.6, announce_timeout)
    for attempt in range(2):
        transport.send(live_announce(blob))
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for packet in transport.recv_frames(min(0.2, max(0.05, deadline - time.time()))):
                if is_live_ready(packet):
                    ready = True
                    break
            if ready:
                break
        if ready:
            break
    if not ready:
        print("  warning: no 0x8B ready ACK before chunks")
    transport.send_all(chunks, pace_s)
    extra = 0.5 if pace_s <= 0 else min(1.5, max(0.3, len(chunks) * pace_s))
    end = time.time() + extra
    while time.time() < end:
        for packet in transport.recv_frames(0.1):
            index = requested_chunk_index(packet)
            if index is not None and 0 <= index < len(chunks):
                transport.send(chunks[index])
    return len(chunks)


def start_audio(path: Path, start_s: float = 0.0, seconds: float | None = None) -> subprocess.Popen[bytes] | None:
    player = ffplay_bin()
    if player is None:
        print("warning: --audio requested but ffplay is not on PATH", file=sys.stderr)
        return None
    cmd = [player, "-nodisp", "-autoexit", "-loglevel", "error"]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    if seconds is not None:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd.append(str(path))
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def print_inventory() -> int:
    print(format_usb_report())
    print()
    adapter = bluetooth_adapter_message()
    if adapter:
        print(adapter)
    devices = list_windows_devices()
    ports = list_com_ports()
    if devices:
        print("Paired Bluetooth devices:")
        for mac, name in devices:
            mark = "  <-- MiniToo?" if "divoom" in name.lower() or "minitoo" in name.lower() or "tiivoo" in name.lower() else ""
            print(f"  {mac}  {name}{mark}")
    else:
        print("No Bluetooth devices found in the Windows registry (pair the MiniToo first).")
    if ports:
        print("Serial ports (Bluetooth SPP often shows up as COMx):")
        for port, name in ports:
            print(f"  {port}  {name}")
    print()
    print("USB carries audio only. Video frames still need Bluetooth:")
    print("  python core/video-play.py --usb movie.mp4")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a video/GIF and play it on a MiniToo over Windows Bluetooth.")
    parser.add_argument("input", nargs="?", type=Path, help="Video or GIF/PNG path")
    parser.add_argument("--list", action="store_true", help="List USB gadget, paired Bluetooth devices, and COM ports")
    parser.add_argument("--usb", action="store_true", help="Keep audio on the USB speaker and send video frames over Bluetooth")
    parser.add_argument("--mac", help="MiniToo Bluetooth MAC (or DIVOOM_MAC)")
    parser.add_argument("--com", help="Windows Bluetooth serial port, e.g. COM5")
    parser.add_argument("--rfcomm-channel", type=int, default=0, help="Force RFCOMM channel (default: try 1 then 10)")
    parser.add_argument("--fps", type=float, default=8.0, help="Decode frame rate for video (ignored for GIF timing)")
    parser.add_argument("--start", type=float, default=0.0, help="Start offset in seconds")
    parser.add_argument("--seconds", type=float, help="Only convert/send this many seconds (full file if omitted)")
    parser.add_argument("--speed-ms", type=positive_int, help="Override per-frame delay in ms (default: 1000/fps)")
    parser.add_argument("--fast", action="store_true", help="Fewer loads: 6 fps, q40, 96-frame clips, burst RFCOMM writes")
    parser.add_argument("--clip-frames", type=positive_int, default=24, help="Frames per 0x8B upload (max 255). Bigger = less loading.")
    parser.add_argument("--quality", type=int, default=70, help="JPEG quality 1-100 (magic 0x23 only)")
    parser.add_argument(
        "--blob",
        choices=("auto", "25", "23"),
        default="auto",
        help="0x8B payload: 25=app RGB+zstd, 23=JPEG, auto=25 then JPEG if zstd is too big",
    )
    parser.add_argument("--fit", choices=("stretch", "contain", "cover"), default="contain")
    parser.add_argument("--resample", choices=tuple(RESAMPLE_MODES), default="lanczos")
    parser.add_argument("--pace-ms", type=int, default=5, help="Delay between RFCOMM chunks. 0 bursts writes (faster).")
    parser.add_argument("--subsampling", type=int, default=0, choices=(0, 1, 2), help="JPEG chroma subsampling. 2 is smaller/faster for video.")
    parser.add_argument("--announce-timeout", type=float, default=0.5)
    parser.add_argument("--audio", action="store_true", help="Play file audio via ffplay on the default output device (use --usb if that device is the MiniToo cable)")
    parser.add_argument("--encode-only", action="store_true", help="Write a dv-compatible rawfile and exit")
    parser.add_argument("-o", "--output", type=Path, help="rawfile path for --encode-only")
    parser.add_argument("--dry-run", action="store_true", help="Encode clips and print sizes without connecting")
    return parser.parse_args()


def guess_mac() -> str | None:
    env = default_mac()
    if env:
        return env
    for mac, name in list_windows_devices():
        lowered = name.lower()
        if "minitoo" in lowered or "tiivoo" in lowered or "divoom" in lowered:
            return mac
    return None


def connect(args: argparse.Namespace) -> MiniTooTransport:
    if args.com:
        print(f"opening serial {args.com}")
        return connect_serial(args.com)
    mac = args.mac or guess_mac()
    if not mac:
        raise SystemExit("pass --mac, --com, or set DIVOOM_MAC (try --list)")
    channels = (args.rfcomm_channel,) if args.rfcomm_channel else (1, 10)
    transport = connect_mac(mac, channels)
    ch = transport.channel if transport.channel is not None else "SPP"
    print(f"connected to {mac} on RFCOMM {ch}")
    return transport


def main() -> int:
    args = parse_args()
    if args.list:
        return print_inventory()
    if args.input is None:
        print("error: input file required (or pass --list)", file=sys.stderr)
        return 2
    if not args.input.exists():
        print(f"error: missing file {args.input}", file=sys.stderr)
        return 2
    if not 1 <= args.quality <= 100:
        print("error: --quality must be 1..100", file=sys.stderr)
        return 2
    if args.clip_frames > MAX_FRAMES:
        print(f"error: --clip-frames cannot exceed {MAX_FRAMES}", file=sys.stderr)
        return 2
    if args.fast:
        args.fps = 6.0
        args.quality = 40
        args.clip_frames = 96
        args.pace_ms = 0
        args.announce_timeout = 1.2
        args.fit = "cover"
        args.subsampling = 2
        print("fast preset: 6fps q40 cover 96-frame clips, burst writes, jpeg 4:2:0")
    if args.usb:
        print(format_usb_report())
        print()
        if not usb_audio_present():
            print("warning: USB Divoom Audio not found. Plug the MiniToo in if you want USB sound.", file=sys.stderr)
        else:
            print("USB will keep carrying sound. Screen frames still go over Bluetooth SPP.")
        args.audio = True

    speed_ms = args.speed_ms or max(MIN_SPEED_MS, int(round(1000 / args.fps)))
    print(f"blob={args.blob}  speed_ms={speed_ms}  clip_frames={args.clip_frames}")
    frames = iter_source_frames(
        args.input,
        args.fps,
        args.fit,
        args.resample,
        start_s=args.start,
        seconds=args.seconds,
    )
    clips = clip_images(frames, args.clip_frames)

    if args.encode_only:
        if args.output is None:
            print("error: --encode-only needs -o <file.raw>", file=sys.stderr)
            return 2
        all_frames: list[Image.Image] = []
        for batch in clips:
            all_frames.extend(batch)
            if len(all_frames) > MAX_FRAMES:
                print(
                    f"error: file encodes to {len(all_frames)}+ frames; "
                    f"lower --fps or omit --encode-only and play in clips",
                    file=sys.stderr,
                )
                return 1
        blob, used = encode_live_images(
            all_frames,
            speed_ms=speed_ms,
            magic=args.blob,
            quality=args.quality,
            subsampling=args.subsampling,
        )
        chunks = write_live_rawfile(blob, str(args.output))
        print(
            f"wrote {args.output} frames={len(all_frames)} speed_ms={speed_ms} "
            f"magic=0x{used} bytes={len(blob)} chunks={chunks}"
        )
        return 0

    audio_proc = None
    transport = None
    try:
        if not args.dry_run:
            transport = connect(args)
            if args.audio:
                audio_proc = start_audio(args.input, start_s=args.start, seconds=args.seconds)
                if audio_proc is not None:
                    print("audio started via ffplay (uses the Windows default output device)")

        clip_index = 0
        total_frames = 0
        total_bytes = 0
        t0 = time.time()
        clip_iter = iter(clips)
        pending = next(clip_iter, None)
        while pending is not None:
            clip_index += 1
            batch = pending
            blob, used = encode_live_images(
                batch,
                speed_ms=speed_ms,
                magic=args.blob,
                quality=args.quality,
                subsampling=args.subsampling,
            )
            total_frames += len(batch)
            total_bytes += len(blob)
            play_s = len(batch) * speed_ms / 1000.0
            print(
                f"clip {clip_index}: frames={len(batch)} magic=0x{used} bytes={len(blob)} "
                f"chunks={(len(blob) + 255) // 256} play={play_s:.2f}s"
            )
            if args.dry_run:
                pending = next(clip_iter, None)
                continue
            assert transport is not None
            send_started = time.time()
            chunks = send_live_clip(transport, blob, args.pace_ms / 1000.0, args.announce_timeout)
            xfer_s = time.time() - send_started
            print(f"  sent {chunks} chunks in {xfer_s:.2f}s")
            # Encode the next clip during playback, but do not announce
            # another 0x8B until this clip has finished. Overlapping uploads
            # froze the panel.
            pending = next(clip_iter, None)
            wait_s = play_s - (time.time() - send_started)
            if wait_s > 0:
                time.sleep(wait_s)

        elapsed = time.time() - t0
        print(f"done clips={clip_index} frames={total_frames} payload_bytes={total_bytes} elapsed={elapsed:.1f}s")
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        if audio_proc is not None:
            audio_proc.terminate()
        if transport is not None:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
