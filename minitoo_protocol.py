"""MiniToo SPP framing and the live-animation (0x8B) payload used by video-play.

The device is Classic RFCOMM, not BLE. MiniToo firmware 2.4.0 accepts two
live blobs on opcode 0x8B:

  * magic 0x25 (W2.c.f) — what the official Android app sends: RGB888
    pixels, one 16x16 cell per header unit, zstd-compressed. 8x10 cells is
    160x128. Confirmed on a RedMagic HCI snoop 2026-09-05.
  * magic 0x23 (W2.c.r) — JPEG frames, the path this repo proved first.

Chunks are 256 bytes. The frame-count field is one byte, so a single upload
is at most 255 frames — longer video is sent as successive 0x8B clips.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Iterable, Sequence

from PIL import Image

WIDTH = 160
HEIGHT = 128
CELL_ROWS = 8
CELL_COLS = 10
CELL_PX = 16
CHUNK_SIZE = 256
MAX_FRAMES = 255
MIN_SPEED_MS = 40
LIVE_OPCODE = 0x8B
ZSTD_LEN_MAX = 65535
RESAMPLE_MODES = {
    "nearest": Image.Resampling.NEAREST,
    "lanczos": Image.Resampling.LANCZOS,
}


def checksum(payload: bytes) -> bytes:
    total = sum(payload) & 0xFFFF
    return bytes((total & 0xFF, (total >> 8) & 0xFF))


def frame(opcode: int, args: bytes = b"") -> bytes:
    length = len(args) + 3
    payload = bytes((length & 0xFF, (length >> 8) & 0xFF, opcode)) + args
    return b"\x01" + payload + checksum(payload) + b"\x02"


def drain_frames(buf: bytearray) -> list[bytes]:
    """Pull complete 0x01…0x02 frames off the front of `buf`."""
    out: list[bytes] = []
    i = 0
    while i + 4 <= len(buf):
        if buf[i] != 0x01:
            i += 1
            continue
        length = buf[i + 1] | (buf[i + 2] << 8)
        total = length + 4
        if length < 3 or i + total > len(buf):
            break
        if buf[i + total - 1] != 0x02:
            i += 1
            continue
        out.append(bytes(buf[i : i + total]))
        i += total
    if i:
        del buf[:i]
    return out


def frame_opcode(packet: bytes) -> int | None:
    if len(packet) < 6 or packet[0] != 0x01:
        return None
    return packet[3]


def response_body(packet: bytes) -> bytes | None:
    """Inner bytes after the 0x04 0x?? 0x55 response wrapper, before checksum."""
    if len(packet) < 10 or packet[0] != 0x01 or packet[3] != 0x04:
        return None
    # 01 len_lo len_hi 04 echoed_op 55 [body…] csum_lo csum_hi 02
    return packet[6:-3]


def is_live_ready(packet: bytes) -> bool:
    body = response_body(packet)
    if body is None or len(body) < 2:
        return False
    return packet[4] == LIVE_OPCODE and body[0] == 0x00 and body[1] == 0x01


def requested_chunk_index(packet: bytes) -> int | None:
    body = response_body(packet)
    if body is None or len(body) < 3:
        return None
    if packet[4] != LIVE_OPCODE or body[0] != 0x01:
        return None
    return body[1] | (body[2] << 8)


def render_frame(frame_img: Image.Image, fit: str, resample: Image.Resampling) -> Image.Image:
    rgba = frame_img.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    bg.alpha_composite(rgba)
    rgb = bg.convert("RGB")

    if fit == "stretch":
        return rgb.resize((WIDTH, HEIGHT), resample)

    if fit == "contain":
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        copy = rgb.copy()
        copy.thumbnail((WIDTH, HEIGHT), resample)
        canvas.paste(copy, ((WIDTH - copy.width) // 2, (HEIGHT - copy.height) // 2))
        return canvas

    if fit == "cover":
        scale = max(WIDTH / rgb.width, HEIGHT / rgb.height)
        resized = rgb.resize(
            (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
            resample,
        )
        x = (resized.width - WIDTH) // 2
        y = (resized.height - HEIGHT) // 2
        return resized.crop((x, y, x + WIDTH, y + HEIGHT))

    raise ValueError(f"unknown fit mode: {fit}")


def jpeg_bytes(image: Image.Image, quality: int, subsampling: int = 0) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, subsampling=subsampling)
    return buf.getvalue()


def _zstd_compress(payload: bytes) -> bytes:
    try:
        import zstandard  # type: ignore
    except ImportError as exc:
        raise RuntimeError("zstandard is required for magic 0x25. Install it with: py -3 -m pip install zstandard") from exc
    compressed = zstandard.ZstdCompressor(level=3).compress(payload)
    if len(compressed) <= ZSTD_LEN_MAX:
        return compressed
    compressed = zstandard.ZstdCompressor(level=19).compress(payload)
    if len(compressed) > ZSTD_LEN_MAX:
        raise OverflowError(f"zstd payload is {len(compressed)} bytes; 0x25 length field is uint16")
    return compressed


def encode_live_blob(jpegs: Sequence[bytes], speed_ms: int) -> bytes:
    """W2.c.r JPEG live blob (magic 0x23, 8x10 cells). Proven on this MiniToo."""
    if not jpegs:
        raise ValueError("need at least one JPEG frame")
    if len(jpegs) > MAX_FRAMES:
        raise ValueError(f"one 0x8B upload can hold at most {MAX_FRAMES} frames, got {len(jpegs)}")
    if speed_ms <= 0 or speed_ms > 65535:
        raise ValueError(f"speed must fit uint16 milliseconds, got {speed_ms}")

    payload = bytes(
        (
            0x23,
            len(jpegs) & 0xFF,
            (speed_ms >> 8) & 0xFF,
            speed_ms & 0xFF,
            CELL_ROWS,
            CELL_COLS,
        )
    )
    for jpeg in jpegs:
        payload += bytes((0x01,)) + struct.pack(">I", len(jpeg)) + jpeg
    return payload


def encode_live_zstd(images: Sequence[Image.Image], speed_ms: int) -> bytes:
    """W2.c.f RGB+zstd live blob (magic 0x25). What the Android app sends on 0x8B."""
    if not images:
        raise ValueError("need at least one frame")
    if len(images) > MAX_FRAMES:
        raise ValueError(f"one 0x8B upload can hold at most {MAX_FRAMES} frames, got {len(images)}")
    if speed_ms <= 0 or speed_ms > 65535:
        raise ValueError(f"speed must fit uint16 milliseconds, got {speed_ms}")

    width, height = CELL_COLS * CELL_PX, CELL_ROWS * CELL_PX
    raw = bytearray()
    for img in images:
        rgb = img.convert("RGB")
        if rgb.size != (width, height):
            rgb = rgb.resize((width, height), Image.Resampling.NEAREST)
        raw.extend(rgb.tobytes())
    compressed = _zstd_compress(bytes(raw))
    return (
        bytes(
            (
                0x25,
                len(images) & 0xFF,
                (speed_ms >> 8) & 0xFF,
                speed_ms & 0xFF,
                CELL_ROWS,
                CELL_COLS,
                0x00,
                0x00,
            )
        )
        + struct.pack(">H", len(compressed))
        + compressed
    )


def encode_live_images(
    images: Sequence[Image.Image],
    *,
    speed_ms: int,
    magic: str = "auto",
    quality: int = 70,
    subsampling: int = 0,
    max_bytes: int = 0,
) -> tuple[bytes, str]:
    """Encode a 0x8B blob. Returns (blob, magic_used) where magic_used is '23' or '25'.

    `max_bytes` (auto only) falls back to JPEG when the zstd blob is larger than
    that — desktop photos compress poorly as RGB+zstd and stall RFCOMM.
    """
    frames = [img.convert("RGB") for img in images]
    want = magic.strip().lower()
    if want not in {"23", "25", "auto"}:
        raise ValueError("magic must be 23, 25, or auto")
    if want in {"25", "auto"}:
        try:
            blob = encode_live_zstd(frames, speed_ms)
            if max_bytes and len(blob) > max_bytes:
                raise OverflowError(f"zstd blob is {len(blob)} bytes; budget is {max_bytes}")
            return blob, "25"
        except OverflowError:
            if want == "25":
                raise
    jpegs = [jpeg_bytes(frame, quality, subsampling=subsampling) for frame in frames]
    return encode_live_blob(jpegs, speed_ms), "23"


def live_announce_args(blob: bytes) -> bytes:
    return bytes((0x00,)) + struct.pack("<I", len(blob))


def live_announce(blob: bytes) -> bytes:
    return frame(LIVE_OPCODE, live_announce_args(blob))


def live_chunk_args(blob: bytes) -> list[bytes]:
    args: list[bytes] = []
    total = struct.pack("<I", len(blob))
    for index in range((len(blob) + CHUNK_SIZE - 1) // CHUNK_SIZE):
        piece = blob[index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE]
        args.append(bytes((0x01,)) + total + struct.pack("<H", index) + piece)
    return args


def live_chunk_frames(blob: bytes) -> list[bytes]:
    return [frame(LIVE_OPCODE, args) for args in live_chunk_args(blob)]


def write_live_rawfile(blob: bytes, path: str) -> int:
    """Write opcode+args hex lines compatible with `dv rawfile`."""
    lines = [(bytes((LIVE_OPCODE,)) + live_announce_args(blob)).hex(" ")]
    chunks = live_chunk_args(blob)
    lines.extend((bytes((LIVE_OPCODE,)) + args).hex(" ") for args in chunks)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(chunks)


def encode_images(
    images: Iterable[Image.Image],
    *,
    speed_ms: int,
    quality: int,
    fit: str,
    resample_name: str,
    magic: str = "auto",
    subsampling: int = 0,
) -> bytes:
    resample = RESAMPLE_MODES[resample_name]
    frames = [render_frame(img, fit, resample) for img in images]
    blob, _used = encode_live_images(
        frames, speed_ms=speed_ms, magic=magic, quality=quality, subsampling=subsampling
    )
    return blob
