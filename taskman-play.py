#!/usr/bin/env python3
"""Live Task Manager dashboard on MiniToo (160x128) plus a scaled PC preview.

Renders native pixels — does not capture the real Task Manager window, which
would be unreadable at 160x128. JPEG 4:4:4 + a 5x7 bitmap font keep labels
sharp on the panel.

  py -3 core/taskman-play.py
  py -3 core/taskman-play.py --preview
  py -3 core/taskman-play.py --scale 5 --hz 1

Close the preview window or Ctrl+C to stop. Phone Divoom app must be off.
"""

from __future__ import annotations

import argparse
import ctypes
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from PIL import Image, ImageDraw

from minitoo_protocol import (
    HEIGHT,
    MIN_SPEED_MS,
    WIDTH,
    encode_live_blob,
    is_live_ready,
    jpeg_bytes,
    live_announce,
    live_chunk_frames,
    requested_chunk_index,
)
from minitoo_rfcomm import (
    MiniTooTransport,
    connect_mac,
    connect_serial,
    default_mac,
    list_windows_devices,
)

# 5x7 glyphs, bit 4 = leftmost pixel. Public-domain style bitmap.
_GLYPH = {
    " ": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
    "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x01, 0x01, 0x01, 0x01, 0x11, 0x11, 0x0E),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0E, 0x11, 0x10, 0x0E, 0x01, 0x11, 0x0E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
    "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04),
    ":": (0x00, 0x04, 0x00, 0x00, 0x04, 0x00, 0x00),
    "%": (0x19, 0x1A, 0x02, 0x04, 0x08, 0x13, 0x03),
    "/": (0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10),
    "-": (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
    "_": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F),
    "+": (0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00),
    "*": (0x00, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x00),
    "(": (0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02),
    ")": (0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08),
    "?": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04),
    "!": (0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04),
    "'": (0x04, 0x04, 0x08, 0x00, 0x00, 0x00, 0x00),
    "<": (0x00, 0x02, 0x04, 0x08, 0x04, 0x02, 0x00),
    ">": (0x00, 0x08, 0x04, 0x02, 0x04, 0x08, 0x00),
    "^": (0x04, 0x0A, 0x11, 0x00, 0x00, 0x00, 0x00),
}

BG = (12, 14, 18)
HEADER = (16, 22, 32)
RULE = (36, 44, 56)
BAR_BG = (28, 34, 44)
TEXT = (232, 236, 240)
DIM = (140, 152, 168)
CPU_C = (0, 186, 199)
RAM_C = (184, 214, 80)
DSK_C = (242, 169, 0)
NET_C = (180, 142, 232)
HOT = (232, 72, 85)
SKIP_PROC = {
    "idle",
    "system idle process",
    "system",
    "registry",
    "memory compression",
    "interrupts",
    "dpcs",
    "secure system",
}

kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None


class FILETIME(ctypes.Structure):
    _fields_ = (("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32))


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = (
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    )


def _filetime_u64(value: FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _read_system_times() -> tuple[int, int]:
    idle = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    if kernel32 is None or not kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        raise OSError("GetSystemTimes failed")
    idle_t = _filetime_u64(idle)
    busy_t = _filetime_u64(kernel) + _filetime_u64(user)
    # Kernel time includes idle time on Windows.
    return busy_t - idle_t, busy_t


def _read_memory() -> tuple[int, int, float]:
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if kernel32 is None or not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    used = int(status.ullTotalPhys - status.ullAvailPhys)
    total = int(status.ullTotalPhys)
    return used, total, float(status.dwMemoryLoad)


@dataclass
class ProcRow:
    name: str
    cpu: float
    rss: int


@dataclass
class Snapshot:
    cpu: float
    ram_used: int
    ram_total: int
    ram_pct: float
    disk_bps: float
    net_down: float
    net_up: float
    procs: list[ProcRow] = field(default_factory=list)
    cpu_hist: list[float] = field(default_factory=list)
    clock: str = ""


class Sampler:
    def __init__(self, top: int) -> None:
        try:
            import psutil  # type: ignore
        except ImportError as exc:
            raise SystemExit("psutil is required. Install it with: py -3 -m pip install psutil") from exc
        self._psutil = psutil
        self._top = top
        self._hist: list[float] = []
        self._stop = threading.Event()
        self._proc_lock = threading.Lock()
        self._proc_rows: list[ProcRow] = []
        self._busy_t, self._total_t = _read_system_times()
        self._net = None
        self._disk = None
        self._t = time.time()
        self._proc_thread = threading.Thread(target=self._proc_loop, name="taskman-procs", daemon=True)
        self._proc_thread.start()

    def close(self) -> None:
        self._stop.set()

    def _proc_loop(self) -> None:
        psutil = self._psutil
        ncpu = max(1, psutil.cpu_count() or 1)
        self._stop.wait(0.5)
        primed = False
        while not self._stop.is_set():
            merged: dict[str, ProcRow] = {}
            try:
                for proc in psutil.process_iter(["name", "cpu_percent", "memory_info"], ad_value=None):
                    info = proc.info
                    name = str(info.get("name") or "")
                    if not name or name.lower() in SKIP_PROC:
                        continue
                    cpu_p = float(info.get("cpu_percent") or 0.0) / ncpu
                    mem = info.get("memory_info")
                    rss = int(getattr(mem, "rss", 0) or 0)
                    key = name.lower()
                    if key in merged:
                        merged[key].cpu += cpu_p
                        merged[key].rss += rss
                    else:
                        merged[key] = ProcRow(name, cpu_p, rss)
            except (psutil.Error, OSError, TypeError):
                pass
            if not primed:
                primed = True
                self._stop.wait(0.4)
                continue
            rows = sorted(merged.values(), key=lambda row: (row.cpu, row.rss), reverse=True)
            with self._proc_lock:
                self._proc_rows = rows[: self._top]
            self._stop.wait(1.0)

    def sample(self) -> Snapshot:
        psutil = self._psutil
        now = time.time()
        dt = max(0.05, now - self._t)
        busy_t, total_t = _read_system_times()
        busy_d = busy_t - self._busy_t
        total_d = total_t - self._total_t
        if total_d < 80_000_000:
            time.sleep(0.12)
            now = time.time()
            dt = max(0.05, now - self._t)
            busy_t, total_t = _read_system_times()
            busy_d = busy_t - self._busy_t
            total_d = total_t - self._total_t
        cpu = 100.0 * busy_d / total_d if total_d > 0 else 0.0
        cpu = min(100.0, max(0.0, cpu))
        self._busy_t, self._total_t = busy_t, total_t
        ram_used, ram_total, ram_pct = _read_memory()
        net = psutil.net_io_counters()
        disk = psutil.disk_io_counters()
        down = (net.bytes_recv - self._net.bytes_recv) / dt if net and self._net else 0.0
        up = (net.bytes_sent - self._net.bytes_sent) / dt if net and self._net else 0.0
        disk_bps = 0.0
        if disk and self._disk:
            disk_bps = ((disk.read_bytes + disk.write_bytes) - (self._disk.read_bytes + self._disk.write_bytes)) / dt
        self._net = net
        self._disk = disk
        self._t = now
        self._hist.append(cpu)
        if len(self._hist) > 48:
            del self._hist[: len(self._hist) - 48]
        with self._proc_lock:
            procs = list(self._proc_rows)
        return Snapshot(
            cpu=cpu,
            ram_used=ram_used,
            ram_total=ram_total,
            ram_pct=ram_pct,
            disk_bps=disk_bps,
            net_down=max(0.0, down),
            net_up=max(0.0, up),
            procs=procs,
            cpu_hist=list(self._hist),
            clock=time.strftime("%H:%M"),
        )


def blit(img: Image.Image, x: int, y: int, text: str, color: tuple[int, int, int], scale: int = 1) -> int:
    px = img.load()
    assert px is not None
    cx = x
    for raw in text:
        ch = raw if raw in _GLYPH else raw.upper()
        glyph = _GLYPH.get(ch, _GLYPH["?"])
        for row, bits in enumerate(glyph):
            for col in range(5):
                if bits & (0x10 >> col):
                    for sy in range(scale):
                        for sx in range(scale):
                            xx = cx + col * scale + sx
                            yy = y + row * scale + sy
                            if 0 <= xx < WIDTH and 0 <= yy < HEIGHT:
                                px[xx, yy] = color
        cx += 6 * scale
    return cx


def bar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, frac: float, color: tuple[int, int, int]) -> None:
    draw.rectangle((x, y, x + w - 1, y + h - 1), fill=BAR_BG)
    inner = int(round((w - 2) * min(1.0, max(0.0, frac))))
    if inner > 0:
        draw.rectangle((x + 1, y + 1, x + inner, y + h - 2), fill=color)


def heat(frac: float, cool: tuple[int, int, int]) -> tuple[int, int, int]:
    if frac >= 0.90:
        return HOT
    if frac >= 0.75:
        return DSK_C
    return cool


def fmt_bytes(n: float) -> str:
    n = max(0.0, n)
    if n >= 1024**3:
        return f" {n / 1024**3:4.1f}G"
    if n >= 1024**2:
        v = n / 1024**2
        return f" {v:4.1f}M" if v < 100 else f" {v:4.0f}M"
    return f" {n / 1024:4.1f}K"


def fmt_rate(bps: float) -> str:
    return fmt_bytes(bps).strip()


def fmt_short(n: float) -> str:
    n = max(0.0, n)
    if n >= 1024**3:
        return f"{n / 1024**3:.1f}G"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return f"{n:.0f}B"


def short_name(name: str, width: int) -> str:
    stem = name[:-4] if name.lower().endswith(".exe") else name
    stem = "".join(ch if ch.isalnum() or ch in "._-" else " " for ch in stem)
    stem = stem.strip() or name
    if len(stem) <= width:
        return stem.upper()
    return stem[: width - 1].upper() + "+"


def spark(img: Image.Image, x: int, y: int, w: int, h: int, hist: list[float], color: tuple[int, int, int]) -> None:
    if w <= 1 or h <= 1 or len(hist) < 3:
        return
    px = img.load()
    assert px is not None
    series = hist[-w:]
    for i, val in enumerate(series):
        hh = max(1, int(round((h - 1) * min(1.0, max(0.0, val / 100.0)))))
        xx = x + i
        yy = y + h - hh
        if 0 <= xx < WIDTH and 0 <= yy < HEIGHT:
            px[xx, yy] = color


def render_dashboard(snap: Snapshot) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, WIDTH - 1, 11), fill=HEADER)
    blit(img, 2, 2, "TASKMAN", CPU_C)
    spark(img, 58, 2, 48, 8, snap.cpu_hist, CPU_C)
    clock = snap.clock
    blit(img, WIDTH - 2 - 6 * len(clock), 2, clock, TEXT)
    draw.line((0, 12, WIDTH - 1, 12), fill=RULE)

    metrics = (
        ("CPU", f"{snap.cpu:5.1f}%", snap.cpu / 100.0, CPU_C),
        ("RAM", f"{fmt_bytes(snap.ram_used).strip()}/{fmt_bytes(snap.ram_total).strip()}", snap.ram_pct / 100.0, RAM_C),
        ("DSK", fmt_rate(snap.disk_bps), min(1.0, snap.disk_bps / (200 * 1024 * 1024)), DSK_C),
        ("NET", f"{fmt_rate(snap.net_down)} {fmt_rate(snap.net_up)}", min(1.0, (snap.net_down + snap.net_up) / (50 * 1024 * 1024)), NET_C),
    )
    y = 16
    for label, value, frac, color in metrics:
        tint = heat(frac, color)
        blit(img, 2, y, label, tint)
        blit(img, 28, y, value, TEXT)
        bar(draw, 2, y + 8, WIDTH - 4, 5, frac, tint)
        y += 16

    draw.line((0, y, WIDTH - 1, y), fill=RULE)
    y += 3
    blit(img, 2, y, "PROCESS", DIM)
    blit(img, 98, y, "CPU", DIM)
    blit(img, 130, y, "RAM", DIM)
    y += 9
    for row in snap.procs:
        blit(img, 2, y, short_name(row.name, 15), TEXT)
        blit(img, 98, y, f"{row.cpu:4.1f}", heat(row.cpu / 100.0, CPU_C))
        blit(img, 128, y, f"{fmt_short(row.rss):>5}", RAM_C)
        y += 9
        if y > HEIGHT - 8:
            break
    return img


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
        raise SystemExit("pass --mac, --com, or set DIVOOM_MAC")
    channels = (args.rfcomm_channel,) if args.rfcomm_channel else (1, 10)
    transport = connect_mac(mac, channels)
    ch = transport.channel if transport.channel is not None else "SPP"
    print(f"connected to {mac} on RFCOMM {ch}")
    return transport


def send_live_clip(transport: MiniTooTransport, blob: bytes, pace_s: float, announce_timeout: float) -> int:
    chunks = live_chunk_frames(blob)
    transport.recv_frames(0.0)
    ready = False
    wait_s = max(0.35, announce_timeout)
    for _attempt in range(2):
        transport.send(live_announce(blob))
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for packet in transport.recv_frames(min(0.05, max(0.01, deadline - time.time()))):
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
    extra = 0.08 if pace_s <= 0 else min(0.4, max(0.08, len(chunks) * pace_s))
    end = time.time() + extra
    while time.time() < end:
        for packet in transport.recv_frames(0.04):
            index = requested_chunk_index(packet)
            if index is not None and 0 <= index < len(chunks):
                transport.send(chunks[index])
    return len(chunks)


def scaled_preview(img: Image.Image, scale: int) -> Image.Image:
    return img.resize((WIDTH * scale, HEIGHT * scale), Image.Resampling.NEAREST)


def run_preview(
    frames: queue.SimpleQueue[Image.Image | None],
    scale: int,
    stop: threading.Event,
    topmost: bool,
) -> None:
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError:
        print("no tkinter; preview window skipped", file=sys.stderr)
        while not stop.is_set():
            try:
                item = frames.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
        return

    root = tk.Tk()
    root.title("MiniToo Task Manager")
    root.configure(bg="#000000")
    root.resizable(False, False)
    if topmost:
        root.attributes("-topmost", True)
    holder = {"photo": None}
    label = tk.Label(root, bg="#000000", bd=0, highlightthickness=0)
    label.pack()
    placeholder = Image.new("RGB", (WIDTH * scale, HEIGHT * scale), BG)
    holder["photo"] = ImageTk.PhotoImage(placeholder)
    label.configure(image=holder["photo"])

    def on_close() -> None:
        stop.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    def poll() -> None:
        latest: Image.Image | None = None
        while True:
            try:
                item = frames.get_nowait()
            except queue.Empty:
                break
            if item is None:
                on_close()
                return
            latest = item
        if latest is not None:
            holder["photo"] = ImageTk.PhotoImage(scaled_preview(latest, scale))
            label.configure(image=holder["photo"])
        if not stop.is_set():
            root.after(50, poll)

    root.after(50, poll)
    root.mainloop()
    stop.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a readable Task Manager dashboard to MiniToo.")
    parser.add_argument("--mac", help="MiniToo Bluetooth MAC (or DIVOOM_MAC)")
    parser.add_argument("--com", help="Windows Bluetooth serial port, e.g. COM16")
    parser.add_argument("--rfcomm-channel", type=int, default=0)
    parser.add_argument("--hz", type=float, default=1.0, help="Dashboard refresh rate (Task Manager is ~1 Hz)")
    parser.add_argument("--quality", type=int, default=88, help="JPEG quality; keep high so text stays sharp")
    parser.add_argument("--pace-ms", type=int, default=0)
    parser.add_argument("--announce-timeout", type=float, default=0.8)
    parser.add_argument("--top", type=int, default=4, help="Process rows")
    parser.add_argument("--scale", type=int, default=4, help="PC preview scale (4 = 640x512)")
    parser.add_argument("--preview", action="store_true", help="Window only; do not connect to MiniToo")
    parser.add_argument("--no-window", action="store_true", help="Send to MiniToo without the PC preview")
    parser.add_argument("--topmost", action="store_true", help="Keep the preview above other windows")
    parser.add_argument("--seconds", type=float, help="Stop after this many seconds")
    parser.add_argument("--save", help="Write the first rendered frame to this PNG")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.hz <= 0:
        print("error: --hz must be positive", file=sys.stderr)
        return 2
    if not 1 <= args.quality <= 100:
        print("error: --quality must be 1..100", file=sys.stderr)
        return 2
    if args.scale < 1:
        print("error: --scale must be at least 1", file=sys.stderr)
        return 2
    if args.preview and args.no_window and args.save is None and args.seconds is None:
        print("error: --preview --no-window needs --save or --seconds", file=sys.stderr)
        return 2

    sampler = Sampler(max(1, args.top))
    stop = threading.Event()
    frames: queue.SimpleQueue[Image.Image | None] = queue.SimpleQueue()
    transport: MiniTooTransport | None = None
    error: list[BaseException] = []
    saved = False

    def worker() -> None:
        nonlocal saved, transport
        sent = 0
        t0 = time.time()
        try:
            if not args.preview:
                transport = connect(args)
            print(
                f"taskman -> MiniToo  {args.hz:g} Hz  q{args.quality}  "
                f"{'preview only' if args.preview else 'device+window' if not args.no_window else 'device'}  "
                f"Ctrl+C or close window to stop"
            )
            next_tick = time.time()
            while not stop.is_set():
                if args.seconds is not None and time.time() - t0 >= args.seconds:
                    break
                wait = next_tick - time.time()
                if wait > 0 and stop.wait(wait):
                    break
                snap = sampler.sample()
                img = render_dashboard(snap)
                if args.save:
                    img.save(args.save)
                    if not saved:
                        print(f"wrote {args.save}")
                        saved = True
                elif args.preview:
                    print(
                        f"cpu {snap.cpu:5.1f}%  ram {snap.ram_pct:4.1f}%  "
                        f"net {fmt_rate(snap.net_down)}/{fmt_rate(snap.net_up)}"
                    )
                if not args.no_window:
                    frames.put(img)
                if transport is not None:
                    blob = encode_live_blob([jpeg_bytes(img, args.quality, subsampling=0)], MIN_SPEED_MS)
                    started = time.time()
                    chunks = send_live_clip(transport, blob, args.pace_ms / 1000.0, args.announce_timeout)
                    sent += 1
                    xfer = time.time() - started
                    print(
                        f"cpu {snap.cpu:5.1f}%  ram {snap.ram_pct:4.1f}%  "
                        f"net {fmt_rate(snap.net_down)}/{fmt_rate(snap.net_up)}  "
                        f"{len(blob)}B/{chunks}ch {xfer:.2f}s"
                    )
                next_tick = time.time() + (1.0 / args.hz)
        except Exception as exc:
            error.append(exc)
        finally:
            stop.set()
            if not args.no_window:
                frames.put(None)
            if transport is not None:
                transport.close()
                transport = None

    thread = threading.Thread(target=worker, name="minitoo-taskman", daemon=True)
    thread.start()
    try:
        if args.no_window:
            while thread.is_alive() and not stop.is_set():
                thread.join(0.2)
        else:
            run_preview(frames, args.scale, stop, args.topmost)
            stop.set()
            thread.join(2.0)
    except KeyboardInterrupt:
        stop.set()
        thread.join(2.0)
        print("stopped", file=sys.stderr)
        return 130
    finally:
        sampler.close()
    if error:
        raise error[0]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
