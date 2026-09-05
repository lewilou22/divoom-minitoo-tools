#!/usr/bin/env python3
"""Mirror the Windows desktop onto a MiniToo over live 0x8B.

This is a 160x128 preview, not a real monitor. Each update is a 1-frame (or
short) 0x8B clip. The next clip is announced only after the current transfer
finishes — overlapping uploads froze the panel.

Default encoding is magic 0x25 (RGB + zstd) until the blob exceeds 12 KB,
then JPEG 0x23 — a busy desktop does not zstd well and stalls the radio.
Bluetooth speaker audio (A2DP) shares this Classic link with SPP; use the
USB "Divoom Audio" output if you want sound without starving the screen.

Examples:
  python core/screen-play.py --fast
  python core/screen-play.py --fast --res auto
  python core/screen-play.py --res 800x600
  python core/screen-play.py --list-res
  python core/screen-play.py --bbox 0,0,1920,1080
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from PIL import Image, ImageDraw, ImageGrab

from minitoo_protocol import (
    HEIGHT,
    MIN_SPEED_MS,
    RESAMPLE_MODES,
    WIDTH,
    encode_live_images,
    is_live_ready,
    live_announce,
    live_chunk_frames,
    render_frame,
    requested_chunk_index,
)
from minitoo_rfcomm import (
    MiniTooTransport,
    connect_mac,
    connect_serial,
    default_mac,
    list_windows_devices,
)
from usb_minitoo import usb_audio_present

SRCCOPY = 0x00CC0020
COLORONCOLOR = 3
BI_RGB = 0
DIB_RGB_COLORS = 0
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
ENUM_CURRENT_SETTINGS = -1
CDS_FULLSCREEN = 0x00000004
DISP_CHANGE_SUCCESSFUL = 0
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_BITSPERPEL = 0x00040000
DM_DISPLAYFREQUENCY = 0x00400000
# Lowest common modes first so UI is chunky on 160x128. 5:4 (800x640) is ideal if the GPU offers it.
PREFERRED_RES = (
    (800, 640),
    (640, 512),
    (800, 600),
    (640, 480),
    (1024, 768),
    (960, 768),
    (1280, 1024),
)


class _DevmodeUnion(ctypes.Union):
    class _Printer(ctypes.Structure):
        _fields_ = (
            ("dmOrientation", ctypes.c_short),
            ("dmPaperSize", ctypes.c_short),
            ("dmPaperLength", ctypes.c_short),
            ("dmPaperWidth", ctypes.c_short),
            ("dmScale", ctypes.c_short),
            ("dmCopies", ctypes.c_short),
            ("dmDefaultSource", ctypes.c_short),
            ("dmPrintQuality", ctypes.c_short),
        )

    class _Display(ctypes.Structure):
        _fields_ = (
            ("dmPosition_x", ctypes.c_long),
            ("dmPosition_y", ctypes.c_long),
            ("dmDisplayOrientation", ctypes.c_ulong),
            ("dmDisplayFixedOutput", ctypes.c_ulong),
        )

    _fields_ = (("printer", _Printer), ("display", _Display))


class DEVMODEW(ctypes.Structure):
    _fields_ = (
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("u", _DevmodeUnion),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    )


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


class BITMAPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", BITMAPINFOHEADER),)


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


_ACK_WARNED = False


def send_live_clip(transport: MiniTooTransport, blob: bytes, pace_s: float, announce_timeout: float) -> int:
    global _ACK_WARNED
    chunks = live_chunk_frames(blob)
    transport.recv_frames(0.0)
    ready = False
    wait_s = max(0.0, announce_timeout)
    transport.send(live_announce(blob))
    if wait_s > 0:
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for packet in transport.recv_frames(min(0.015, max(0.004, deadline - time.time()))):
                if is_live_ready(packet):
                    ready = True
                    break
            if ready:
                break
    if not ready and wait_s > 0 and not _ACK_WARNED:
        print(
            "  warning: no 0x8B ready ACK (sending anyway; further misses are silent). "
            "Bluetooth speaker audio on this same radio makes ACKs late."
        )
        _ACK_WARNED = True
    transport.send_all(chunks, pace_s)
    extra = 0.008 + min(0.05, 0.00035 * len(chunks))
    if pace_s > 0:
        extra = min(0.2, max(extra, len(chunks) * pace_s))
    end = time.time() + extra
    while time.time() < end:
        for packet in transport.recv_frames(0.015):
            index = requested_chunk_index(packet)
            if index is not None and 0 <= index < len(chunks):
                transport.send(chunks[index])
    return len(chunks)


def parse_bbox(value: str) -> tuple[int, int, int, int]:
    parts = [int(p.strip()) for p in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be x,y,w,h")
    x, y, w, h = parts
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("bbox width and height must be positive")
    return x, y, w, h


def parse_res(value: str) -> str | tuple[int, int]:
    lowered = value.strip().lower()
    if lowered == "auto":
        return "auto"
    if "x" not in lowered:
        raise argparse.ArgumentTypeError("resolution must be WxH or auto, e.g. 800x600")
    width_s, height_s = lowered.split("x", 1)
    try:
        width, height = int(width_s), int(height_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("resolution must be WxH or auto") from exc
    if width < 160 or height < 128:
        raise argparse.ArgumentTypeError("resolution must be at least 160x128")
    return width, height


def _blank_devmode() -> DEVMODEW:
    mode = DEVMODEW()
    mode.dmSize = ctypes.sizeof(DEVMODEW)
    return mode


def current_display_mode() -> tuple[int, int, int, int]:
    mode = _blank_devmode()
    if not ctypes.windll.user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(mode)):
        raise OSError("EnumDisplaySettingsW failed")
    return int(mode.dmPelsWidth), int(mode.dmPelsHeight), int(mode.dmBitsPerPel), int(mode.dmDisplayFrequency)


def list_display_modes() -> list[tuple[int, int, int, int]]:
    modes: list[tuple[int, int, int, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    index = 0
    while True:
        mode = _blank_devmode()
        if not ctypes.windll.user32.EnumDisplaySettingsW(None, index, ctypes.byref(mode)):
            break
        item = (int(mode.dmPelsWidth), int(mode.dmPelsHeight), int(mode.dmBitsPerPel), int(mode.dmDisplayFrequency))
        if item not in seen:
            seen.add(item)
            modes.append(item)
        index += 1
    modes.sort(key=lambda item: (item[0] * item[1], -item[3]))
    return modes


def pick_stream_resolution(requested: str | tuple[int, int]) -> tuple[int, int]:
    available = {(w, h) for w, h, _bpp, _hz in list_display_modes()}
    if requested != "auto":
        width, height = requested
        if (width, height) not in available:
            raise SystemExit(
                f"display cannot do {width}x{height}. Run with --list-res to see modes."
            )
        return width, height
    for width, height in PREFERRED_RES:
        if (width, height) in available:
            return width, height
    raise SystemExit("no low-res mode available; run --list-res")


def set_display_mode(width: int, height: int) -> None:
    cur = _blank_devmode()
    if not ctypes.windll.user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(cur)):
        raise OSError("EnumDisplaySettingsW failed")
    cur.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL | DM_DISPLAYFREQUENCY
    cur.dmPelsWidth = width
    cur.dmPelsHeight = height
    rc = ctypes.windll.user32.ChangeDisplaySettingsW(ctypes.byref(cur), CDS_FULLSCREEN)
    if rc != DISP_CHANGE_SUCCESSFUL:
        raise OSError(f"ChangeDisplaySettingsW({width}x{height}) failed rc={rc}")


def restore_display_mode() -> None:
    ctypes.windll.user32.ChangeDisplaySettingsW(None, 0)


def print_display_modes() -> int:
    if sys.platform != "win32":
        print("display modes are Windows-only", file=sys.stderr)
        return 2
    current = current_display_mode()
    print(f"current {current[0]}x{current[1]} {current[2]}bpp {current[3]}Hz")
    print("unique sizes (* = good for MiniToo stream):")
    wanted = set(PREFERRED_RES)
    unique: dict[tuple[int, int], tuple[int, int]] = {}
    for width, height, bpp, hz in list_display_modes():
        key = (width, height)
        prev = unique.get(key)
        if prev is None or hz > prev[1]:
            unique[key] = (bpp, hz)
    for (width, height), (bpp, hz) in unique.items():
        mark = " *" if (width, height) in wanted else ""
        print(f"  {width}x{height}  {bpp}bpp up to {hz}Hz{mark}")
    return 0


def cursor_xy() -> tuple[int, int] | None:
    if sys.platform != "win32":
        return None

    class POINT(ctypes.Structure):
        _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))

    pt = POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return int(pt.x), int(pt.y)


def desktop_rect(bbox: tuple[int, int, int, int] | None, all_screens: bool) -> tuple[int, int, int, int]:
    if bbox is not None:
        return bbox
    if sys.platform != "win32":
        img = ImageGrab.grab(all_screens=all_screens)
        return 0, 0, img.width, img.height
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    if all_screens:
        return (
            int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
            int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
            int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
            int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
        )
    return 0, 0, int(user32.GetSystemMetrics(SM_CXSCREEN)), int(user32.GetSystemMetrics(SM_CYSCREEN))


def map_rect(src_w: int, src_h: int, fit: str) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    if fit == "stretch":
        return (0, 0, src_w, src_h), (0, 0, WIDTH, HEIGHT)
    if fit == "contain":
        scale = min(WIDTH / src_w, HEIGHT / src_h)
        tw = max(1, round(src_w * scale))
        th = max(1, round(src_h * scale))
        return (0, 0, src_w, src_h), ((WIDTH - tw) // 2, (HEIGHT - th) // 2, tw, th)
    if fit == "cover":
        if src_w / src_h > WIDTH / HEIGHT:
            crop_w = max(1, int(src_h * WIDTH / HEIGHT))
            crop_h = src_h
            return ((src_w - crop_w) // 2, 0, crop_w, crop_h), (0, 0, WIDTH, HEIGHT)
        crop_w = src_w
        crop_h = max(1, int(src_w * HEIGHT / WIDTH))
        return (0, (src_h - crop_h) // 2, crop_w, crop_h), (0, 0, WIDTH, HEIGHT)
    raise ValueError(f"unknown fit mode: {fit}")


def mark_cursor_mapped(
    img: Image.Image,
    origin: tuple[int, int, int, int],
    crop: tuple[int, int, int, int],
    dest: tuple[int, int, int, int],
) -> None:
    pos = cursor_xy()
    if pos is None:
        return
    sx, sy, sw, sh = crop
    dx, dy, dw, dh = dest
    x = dx + (pos[0] - origin[0] - sx) * dw / sw
    y = dy + (pos[1] - origin[1] - sy) * dh / sh
    if not (0 <= x < img.width and 0 <= y < img.height):
        return
    draw = ImageDraw.Draw(img)
    r = 3
    draw.ellipse((x - r, y - r, x + r, y + r), outline=(255, 40, 40), width=2)
    draw.point((round(x), round(y)), fill=(255, 255, 255))


class GdiGrabber:
    """Stretch the desktop into 160x128 with GDI so we never copy a 4K bitmap."""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32
        self._hdc_screen = None
        self._hdc_mem = None
        self._hbm = None
        self._old = None
        self._bits = ctypes.c_void_p()
        self._stride = (WIDTH * 3 + 3) & ~3
        try:
            self._user32.SetProcessDPIAware()
        except Exception:
            pass
        self._gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self._gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self._gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self._gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        self._gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self._gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self._gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
        self._gdi32.SetStretchBltMode.restype = ctypes.c_int
        self._gdi32.CreateSolidBrush.restype = wintypes.HANDLE
        self._gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        self._gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self._gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self._gdi32.StretchBlt.restype = wintypes.BOOL
        self._gdi32.StretchBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        self._user32.GetDC.restype = wintypes.HDC
        self._user32.GetDC.argtypes = [wintypes.HWND]
        self._user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self._user32.FillRect.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.HANDLE]
        self._hdc_screen = self._user32.GetDC(None)
        if not self._hdc_screen:
            raise OSError("GetDC failed")
        self._hdc_mem = self._gdi32.CreateCompatibleDC(self._hdc_screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = WIDTH
        bmi.bmiHeader.biHeight = -HEIGHT
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        bmi.bmiHeader.biCompression = BI_RGB
        self._hbm = self._gdi32.CreateDIBSection(
            self._hdc_mem,
            ctypes.byref(bmi),
            DIB_RGB_COLORS,
            ctypes.byref(self._bits),
            None,
            0,
        )
        if not self._hbm or not self._bits:
            self.close()
            raise OSError("CreateDIBSection failed")
        self._old = self._gdi32.SelectObject(self._hdc_mem, self._hbm)
        self._gdi32.SetStretchBltMode(self._hdc_mem, COLORONCOLOR)
        self._brush = self._gdi32.CreateSolidBrush(0)

    def grab(self, origin: tuple[int, int, int, int], fit: str) -> tuple[Image.Image, tuple[int, int, int, int], tuple[int, int, int, int]]:
        ox, oy, ow, oh = origin
        crop, dest = map_rect(ow, oh, fit)
        sx, sy, sw, sh = crop
        dx, dy, dw, dh = dest
        if fit == "contain":
            class RECT(ctypes.Structure):
                _fields_ = (("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long))

            rect = RECT(0, 0, WIDTH, HEIGHT)
            self._user32.FillRect(self._hdc_mem, ctypes.byref(rect), self._brush)
        ok = self._gdi32.StretchBlt(
            self._hdc_mem,
            dx,
            dy,
            dw,
            dh,
            self._hdc_screen,
            ox + sx,
            oy + sy,
            sw,
            sh,
            SRCCOPY,
        )
        if not ok:
            raise OSError("StretchBlt failed")
        buf = ctypes.string_at(self._bits, self._stride * HEIGHT)
        img = Image.frombytes("RGB", (WIDTH, HEIGHT), buf, "raw", "BGR", self._stride)
        return img, crop, dest

    def close(self) -> None:
        gdi32 = self._gdi32
        user32 = self._user32
        if self._old and self._hdc_mem:
            gdi32.SelectObject(self._hdc_mem, self._old)
            self._old = None
        if self._hbm:
            gdi32.DeleteObject(self._hbm)
            self._hbm = None
        if getattr(self, "_brush", None):
            gdi32.DeleteObject(self._brush)
            self._brush = None
        if self._hdc_mem:
            gdi32.DeleteDC(self._hdc_mem)
            self._hdc_mem = None
        if self._hdc_screen:
            user32.ReleaseDC(None, self._hdc_screen)
            self._hdc_screen = None


def capture_frame(
    grabber: GdiGrabber | None,
    bbox: tuple[int, int, int, int] | None,
    all_screens: bool,
    fit: str,
    resample_name: str,
    show_cursor: bool,
    origin: tuple[int, int, int, int],
) -> Image.Image:
    if grabber is not None:
        img, crop, dest = grabber.grab(origin, fit)
        if show_cursor:
            mark_cursor_mapped(img, origin, crop, dest)
        return img
    raw = ImageGrab.grab(bbox=(origin[0], origin[1], origin[0] + origin[2], origin[1] + origin[3]), all_screens=True)
    if show_cursor:
        crop, dest = map_rect(origin[2], origin[3], fit)
        img = render_frame(raw, fit, RESAMPLE_MODES[resample_name])
        mark_cursor_mapped(img, origin, crop, dest)
        return img
    return render_frame(raw, fit, RESAMPLE_MODES[resample_name])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send the Windows desktop to a MiniToo as live 0x8B frames.")
    parser.add_argument("--mac", help="MiniToo Bluetooth MAC (or DIVOOM_MAC)")
    parser.add_argument("--com", help="Windows Bluetooth serial port, e.g. COM16")
    parser.add_argument("--rfcomm-channel", type=int, default=0, help="Force RFCOMM channel (default: try 1 then 10)")
    parser.add_argument(
        "--fps",
        type=float,
        default=12.0,
        help="Capture/playback target. Actual rate is limited by one 0x8B round-trip per clip.",
    )
    parser.add_argument(
        "--clip-frames",
        type=int,
        default=1,
        help="Frames per 0x8B upload. 1 = lowest lag; 4-8 = higher fps, more delay.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Higher panel fps: 8 fps, 4-frame clips, no ACK wait, burst RFCOMM",
    )
    parser.add_argument("--quality", type=int, default=35, help="JPEG quality 1-100 (magic 0x23 only)")
    parser.add_argument(
        "--blob",
        choices=("auto", "25", "23"),
        default="auto",
        help="0x8B payload: 25=app RGB+zstd, 23=JPEG, auto=25 then JPEG if zstd is too big",
    )
    parser.add_argument("--fit", choices=("stretch", "contain", "cover"), default="stretch")
    parser.add_argument("--resample", choices=tuple(RESAMPLE_MODES), default="nearest")
    parser.add_argument("--pace-ms", type=int, default=0, help="Delay between RFCOMM chunks. 0 bursts writes.")
    parser.add_argument("--subsampling", type=int, default=2, choices=(0, 1, 2))
    parser.add_argument(
        "--announce-timeout",
        type=float,
        default=0.0,
        help="Seconds to wait for the 0x8B ready ACK before sending chunks. 0 = send immediately.",
    )
    parser.add_argument(
        "--max-blob",
        type=int,
        default=12000,
        help="With --blob auto, use JPEG if zstd is larger than this many bytes (0 = zstd until 64KB).",
    )
    parser.add_argument("--bbox", type=parse_bbox, help="Capture region x,y,w,h in pixels")
    parser.add_argument("--all-screens", action="store_true", help="Grab the whole virtual desktop")
    parser.add_argument(
        "--res",
        type=parse_res,
        help="Temporarily set the Windows desktop to WxH (or auto) so UI is chunkier on 160x128. Restores on exit.",
    )
    parser.add_argument("--list-res", action="store_true", help="List Windows display modes and exit")
    parser.add_argument("--no-cursor", action="store_true", help="Do not draw a mouse marker")
    parser.add_argument("--seconds", type=float, help="Stop after this many seconds (Ctrl+C otherwise)")
    parser.add_argument("--dry-run", action="store_true", help="Capture and encode without connecting")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_res:
        return print_display_modes()
    if args.fps <= 0:
        print("error: --fps must be positive", file=sys.stderr)
        return 2
    if not 1 <= args.quality <= 100:
        print("error: --quality must be 1..100", file=sys.stderr)
        return 2
    if args.clip_frames < 1:
        print("error: --clip-frames must be at least 1", file=sys.stderr)
        return 2
    if args.fast:
        args.fps = 8.0
        args.clip_frames = 4
        args.announce_timeout = 0.0
        args.pace_ms = 0
        print("fast preset: 8fps, 4-frame clips, send without ACK wait, burst writes")
    if args.res is not None and args.bbox is not None:
        print("error: --res cannot be combined with --bbox", file=sys.stderr)
        return 2
    if args.res is not None and sys.platform != "win32":
        print("error: --res is Windows-only", file=sys.stderr)
        return 2

    interval = 1.0 / args.fps
    restored = True
    original_mode = None
    grabber: GdiGrabber | None = None
    transport = None
    sent = 0
    frames_sent = 0
    t0 = time.time()
    try:
        if args.res is not None and not args.dry_run:
            original_mode = current_display_mode()
            target = pick_stream_resolution(args.res)
            print(
                f"display {original_mode[0]}x{original_mode[1]} -> {target[0]}x{target[1]} "
                f"(restores on exit)"
            )
            set_display_mode(target[0], target[1])
            restored = False
            time.sleep(0.3)
        origin = desktop_rect(args.bbox, args.all_screens)
        if sys.platform == "win32":
            try:
                grabber = GdiGrabber()
            except OSError as exc:
                print(f"warning: GDI capture unavailable ({exc}); using ImageGrab", file=sys.stderr)

        print(
            f"desktop -> MiniToo  cap={args.fps:g} fps  clip={args.clip_frames}  "
            f"ack={args.announce_timeout:g}s  blob={args.blob}  max_blob={args.max_blob}  "
            f"{args.fit}  {origin[2]}x{origin[3]}  Ctrl+C to stop"
        )
        if not args.dry_run:
            try:
                if usb_audio_present():
                    print(
                        "USB Divoom Audio is plugged in. Set Windows sound output to that "
                        "speaker so A2DP does not share this SPP radio."
                    )
                else:
                    print(
                        "If MiniToo is your Bluetooth speaker, pause that audio or plug USB — "
                        "A2DP shares this radio and will stall the screen."
                    )
            except Exception:
                print(
                    "If MiniToo is your Bluetooth speaker, pause that audio or plug USB — "
                    "A2DP shares this radio and will stall the screen."
                )

        next_capture = time.time()
        stat_t = next_capture
        stat_n = 0
        frames_sent = 0
        t0 = time.time()
        if not args.dry_run:
            transport = connect(args)

        def grab_clip(*, paced: bool = True) -> list:
            nonlocal next_capture
            batch: list = []
            while len(batch) < args.clip_frames:
                now = time.time()
                if args.seconds is not None and now - t0 >= args.seconds:
                    break
                if paced:
                    wait = next_capture - now
                    if wait > 0:
                        time.sleep(wait)
                batch.append(
                    capture_frame(
                        grabber,
                        args.bbox,
                        args.all_screens,
                        args.fit,
                        args.resample,
                        show_cursor=not args.no_cursor,
                        origin=origin,
                    )
                )
                next_capture = time.time() + interval
            return batch

        pending: list | None = None
        ema_xfer = 0.08
        max_bytes = args.max_blob if args.blob == "auto" else 0
        while True:
            if args.seconds is not None and time.time() - t0 >= args.seconds:
                break
            batch = pending
            pending = None
            if batch is None:
                batch = grab_clip()
            if not batch:
                break
            # Device must keep this clip on screen until the next upload finishes.
            # Next 0x8B is launched just before playback ends (not after), so the
            # panel does not fall into LOADING.
            capture_s = 0.0 if len(batch) == 1 else (len(batch) / args.fps)
            hold_s = capture_s + 2.0 * ema_xfer + 0.04
            speed_ms = max(MIN_SPEED_MS, min(400, int(round(1000 * hold_s / len(batch)))))
            blob, used = encode_live_images(
                batch,
                speed_ms=speed_ms,
                magic=args.blob,
                quality=args.quality,
                subsampling=args.subsampling,
                max_bytes=max_bytes,
            )
            play_s = len(batch) * speed_ms / 1000.0
            sent += 1
            frames_sent += len(batch)
            if args.dry_run:
                print(f"clip {sent}: frames={len(batch)} magic=0x{used} bytes={len(blob)} play={play_s:.2f}s")
                continue
            assert transport is not None
            started = time.time()
            chunks = send_live_clip(transport, blob, args.pace_ms / 1000.0, args.announce_timeout)
            xfer_s = time.time() - started
            ema_xfer = 0.65 * ema_xfer + 0.35 * xfer_s
            stat_n += len(batch)
            now = time.time()
            if now - stat_t >= 1.0:
                print(
                    f"{stat_n / (now - stat_t):.1f} fps  last 0x{used} {len(blob)}B / "
                    f"{len(batch)}f {chunks}ch xfer={xfer_s:.2f}s hold={speed_ms}ms"
                )
                stat_t = now
                stat_n = 0
            pending = grab_clip(paced=len(batch) > 1)
            launch = started + play_s - ema_xfer - 0.02
            delay = launch - time.time()
            if delay > 0:
                time.sleep(delay)
        print(f"done clips={sent} frames={frames_sent} elapsed={time.time() - t0:.1f}s")
        return 0
    except KeyboardInterrupt:
        elapsed = time.time() - t0
        hz = frames_sent / elapsed if elapsed else 0
        print(f"stopped clips={sent} frames={frames_sent} elapsed={elapsed:.1f}s avg={hz:.1f} fps", file=sys.stderr)
        return 130
    finally:
        if transport is not None:
            transport.close()
        if grabber is not None:
            grabber.close()
        if not restored:
            try:
                restore_display_mode()
                if original_mode:
                    print(f"display restored {original_mode[0]}x{original_mode[1]}")
            except OSError as exc:
                print(f"warning: could not restore display: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
