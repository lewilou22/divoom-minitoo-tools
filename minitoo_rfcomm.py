"""Classic RFCOMM transport for MiniToo.

macOS in this repo uses IOBluetooth (`core/dv`). Windows and Linux can speak
the same bytes over SPP:

  * `--mac`  WinRT RFCOMM on Windows, `AF_BLUETOOTH` sockets as fallback
  * `--com`  Windows Bluetooth serial port (`COM5`)

The device must already be paired, Bluetooth radio on, and the MiniToo powered.
One SPP client at a time.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from collections.abc import Iterable
from uuid import UUID

from minitoo_protocol import drain_frames

SPP_UUID = UUID("00001101-0000-1000-8000-00805F9B34FB")


class MiniTooLink:
    def recv_into(self, buf: bytearray, timeout: float) -> int:
        raise NotImplementedError

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SocketLink(MiniTooLink):
    def __init__(self, sock: socket.socket):
        self._sock = sock

    def recv_into(self, buf: bytearray, timeout: float) -> int:
        self._sock.settimeout(timeout)
        try:
            chunk = self._sock.recv(4096)
        except TimeoutError:
            return 0
        except socket.timeout:
            return 0
        if not chunk:
            return 0
        buf.extend(chunk)
        return len(chunk)

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


class SerialLink(MiniTooLink):
    def __init__(self, handle):
        self._handle = handle

    def recv_into(self, buf: bytearray, timeout: float) -> int:
        try:
            self._handle.timeout = timeout
        except Exception:
            pass
        chunk = self._handle.read(4096)
        if not chunk:
            return 0
        buf.extend(chunk)
        return len(chunk)

    def send(self, data: bytes) -> None:
        self._handle.write(data)
        try:
            self._handle.flush()
        except Exception:
            pass

    def close(self) -> None:
        self._handle.close()


class _WinRTLoop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float | None = None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


class WinRTLink(MiniTooLink):
    def __init__(self, loop: _WinRTLoop, sock, writer, reader):
        self._loop = loop
        self._sock = sock
        self._writer = writer
        self._reader = reader

    def recv_into(self, buf: bytearray, timeout: float) -> int:
        async def _read() -> bytes:
            loaded = await self._reader.load_async(4096)
            if not loaded:
                return b""
            chunk = bytearray(loaded)
            self._reader.read_bytes(chunk)
            return bytes(chunk)

        try:
            chunk = self._loop.run(_read(), timeout=max(0.05, timeout))
        except TimeoutError:
            return 0
        except Exception:
            return 0
        if not chunk:
            return 0
        buf.extend(chunk)
        return len(chunk)

    def send(self, data: bytes) -> None:
        payload = bytes(data)

        async def _write() -> None:
            try:
                self._writer.write_bytes(payload)
            except TypeError:
                self._writer.write_bytes(list(payload))
            await self._writer.store_async()

        self._loop.run(_write(), timeout=15)

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass
        try:
            self._reader.close()
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._loop.stop()


class MiniTooTransport:
    def __init__(self, link: MiniTooLink, channel: int | None = None):
        self._link = link
        self.channel = channel
        self._buf = bytearray()
        self._lock = threading.Lock()

    def send(self, packet: bytes) -> None:
        self._link.send(packet)

    def send_all(self, packets: Iterable[bytes], pace_s: float = 0.0) -> None:
        if pace_s > 0:
            for pkt in packets:
                self.send(pkt)
                time.sleep(pace_s)
            return
        # Burst the framed packets. Protocol chunks stay 256 bytes; the wire
        # write can be larger. Per-chunk WinRT store_async was the 60s-test stall.
        blob = b"".join(packets)
        step = 8192
        for i in range(0, len(blob), step):
            self.send(blob[i : i + step])

    def recv_frames(self, timeout: float) -> list[bytes]:
        deadline = time.time() + timeout
        with self._lock:
            while True:
                frames = drain_frames(self._buf)
                if frames:
                    return frames
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._link.recv_into(self._buf, remaining)

    def close(self) -> None:
        self._link.close()

    def __enter__(self) -> MiniTooTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def normalize_mac(mac: str) -> str:
    cleaned = mac.replace("-", ":").replace(".", ":").strip().upper()
    if ":" not in cleaned and len(cleaned) == 12:
        cleaned = ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
    parts = cleaned.split(":")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        raise ValueError(f"MAC must look like AA:BB:CC:DD:EE:FF, got {mac!r}")
    return ":".join(parts)


def connect_rfcomm(mac: str, channels: Iterable[int] = (1, 10)) -> MiniTooTransport:
    if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_RFCOMM"):
        raise RuntimeError(
            "this Python build has no Bluetooth RFCOMM sockets. "
            "On Windows, pair the MiniToo and use --com COMx, or install an official CPython."
        )
    addr = normalize_mac(mac)
    last_error: OSError | None = None
    for channel in channels:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(12)
        try:
            sock.connect((addr, channel))
            sock.settimeout(None)
            return MiniTooTransport(SocketLink(sock), channel)
        except OSError as exc:
            last_error = exc
            sock.close()
    raise RuntimeError(f"could not open RFCOMM to {addr} on channels {tuple(channels)}: {last_error}")


async def _connect_winrt_async(mac: str):
    from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothDevice
    from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.networking.sockets import StreamSocket
    from winrt.windows.storage.streams import DataReader, DataWriter, InputStreamOptions

    adapter = await BluetoothAdapter.get_default_async()
    if adapter is None:
        raise RuntimeError(
            "Windows has no ready Bluetooth adapter. Turn Bluetooth on in Settings, "
            "then power the MiniToo and Connect it."
        )

    addr_int = int(normalize_mac(mac).replace(":", ""), 16)
    device = await BluetoothDevice.from_bluetooth_address_async(addr_int)
    if device is None:
        selector = BluetoothDevice.get_device_selector_from_pairing_state(True)
        infos = await DeviceInformation.find_all_async_aqs_filter(selector)
        for info in infos:
            candidate = await BluetoothDevice.from_id_async(info.id)
            if candidate is not None and candidate.bluetooth_address == addr_int:
                device = candidate
                break
            if candidate is not None and "minitoo" in (candidate.name or "").lower():
                device = candidate
                break
    if device is None:
        raise RuntimeError(f"WinRT could not open Bluetooth device {mac}. Pair it in Settings first.")

    await device.request_access_async()
    spp = RfcommServiceId.from_uuid(SPP_UUID)
    result = await device.get_rfcomm_services_for_id_async(spp)
    services = list(result.services)
    if not services:
        result = await device.get_rfcomm_services_async()
        services = list(result.services)
    if not services:
        raise RuntimeError("MiniToo advertised no RFCOMM/SPP services. Reconnect it in Bluetooth settings.")

    service = services[0]
    await service.request_access_async()
    sock = StreamSocket()
    await sock.connect_async(service.connection_host_name, service.connection_service_name)
    writer = DataWriter(sock.output_stream)
    reader = DataReader(sock.input_stream)
    reader.input_stream_options = InputStreamOptions.PARTIAL
    return sock, writer, reader, service.connection_service_name


def connect_winrt(mac: str) -> MiniTooTransport:
    loop = _WinRTLoop()
    try:
        sock, writer, reader, service_name = loop.run(_connect_winrt_async(mac), timeout=20)
    except Exception:
        loop.stop()
        raise
    link = WinRTLink(loop, sock, writer, reader)
    channel = None
    try:
        channel = int(str(service_name))
    except (TypeError, ValueError):
        pass
    return MiniTooTransport(link, channel)


def connect_mac(mac: str, channels: Iterable[int] = (1, 10)) -> MiniTooTransport:
    """Windows prefers WinRT SPP; sockets are the fallback (Linux uses sockets)."""
    if sys.platform != "win32":
        return connect_rfcomm(mac, channels)
    errors: list[str] = []
    try:
        return connect_winrt(mac)
    except Exception as exc:
        errors.append(f"WinRT: {exc}")
    try:
        return connect_rfcomm(mac, channels)
    except Exception as exc:
        errors.append(f"sockets: {exc}")
    raise RuntimeError(
        "could not connect to "
        + normalize_mac(mac)
        + ". "
        + " ".join(errors)
        + " Turn Bluetooth on, power the MiniToo, then click Connect in Windows Settings."
    )


def connect_serial(port: str) -> MiniTooTransport:
    try:
        import serial  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pyserial is required for --com. Install it with: python -m pip install pyserial") from exc
    ser = serial.Serial(port=port, baudrate=115200, timeout=0.2, write_timeout=5)
    return MiniTooTransport(SerialLink(ser), None)


def list_windows_devices() -> list[tuple[str, str]]:
    """Return (mac, name) pairs from the Bluetooth port driver registry."""
    if sys.platform != "win32":
        return []
    import winreg

    path = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
    found: list[tuple[str, str]] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
    except OSError:
        return found
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        mac = ":".join(sub[j : j + 2] for j in range(0, len(sub), 2)).upper()
        name = sub
        try:
            key = winreg.OpenKey(root, sub)
            raw, _ = winreg.QueryValueEx(key, "Name")
            winreg.CloseKey(key)
            if isinstance(raw, bytes):
                name = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
            elif isinstance(raw, str):
                name = raw
        except OSError:
            pass
        found.append((mac, name))
    winreg.CloseKey(root)
    return found


def list_com_ports() -> list[tuple[str, str]]:
    if sys.platform != "win32":
        return []
    import winreg

    found: list[tuple[str, str]] = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return found
    i = 0
    while True:
        try:
            name, value, _ = winreg.EnumValue(key, i)
        except OSError:
            break
        i += 1
        found.append((str(value), str(name)))
    winreg.CloseKey(key)
    return found


def bluetooth_adapter_message() -> str | None:
    if sys.platform != "win32":
        return None

    async def _check() -> str | None:
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter
        except ImportError:
            return None
        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None:
            return "Bluetooth adapter is not ready (WinRT reports none). Turn Bluetooth on in Windows Settings."
        return None

    try:
        return asyncio.run(_check())
    except Exception as exc:
        return f"Bluetooth adapter check failed: {exc}"


def default_mac() -> str | None:
    return os.environ.get("DIVOOM_MAC")
