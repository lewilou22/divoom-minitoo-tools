"""Discover the MiniToo USB gadget (Jieli VID 4C4A / PID 4E55).

Plugged in, Windows sees a composite device:

  MI_00  Mass Storage   BR28 UDISK — two LUNs, usually 'No Media'
  MI_01  USB Audio      Speakers (Divoom Audio) + Microphone (Divoom Audio)
  MI_04  HID            Consumer Control (volume / play)

There is no CDC serial, WinUSB, or vendor bulk endpoint. The Divoom SPP
pixel protocol does not run over this cable. USB is the speaker; Bluetooth
RFCOMM is still the screen.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


USB_VID = "4C4A"
USB_PID = "4E55"
USB_ID_TOKEN = f"VID_{USB_VID}&PID_{USB_PID}"


@dataclass
class UsbFunction:
    class_name: str
    friendly_name: str
    instance_id: str
    role: str


def _ps_json(command: str) -> object:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = completed.stdout.strip()
    if not raw:
        return []
    return json.loads(raw)


def list_usb_functions() -> list[UsbFunction]:
    data = _ps_json(
        "Get-PnpDevice -PresentOnly | "
        f"Where-Object {{ $_.InstanceId -match '{USB_ID_TOKEN}' }} | "
        "Select-Object Status, Class, FriendlyName, InstanceId | ConvertTo-Json -Compress"
    )
    if isinstance(data, dict):
        data = [data]
    functions: list[UsbFunction] = []
    for row in data or []:
        instance = str(row.get("InstanceId") or "")
        class_name = str(row.get("Class") or "")
        name = str(row.get("FriendlyName") or "")
        role = "composite"
        if "&MI_00" in instance or "Mass Storage" in name:
            role = "mass-storage (no pixel protocol; LUNs are usually empty)"
        elif "&MI_01" in instance or class_name == "MEDIA":
            role = "usb-audio (this is the speaker you are hearing)"
        elif "HID" in class_name.upper() or "&MI_04" in instance:
            role = "hid consumer-control (volume / play keys only)"
        elif instance.endswith(USB_PID) or "Composite" in name:
            role = "composite root"
        functions.append(UsbFunction(class_name, name, instance, role))
    return functions


def list_udisk_luns() -> list[dict]:
    data = _ps_json(
        "Get-Disk | Where-Object { $_.FriendlyName -match 'BR28' } | "
        "Select-Object Number, FriendlyName, SerialNumber, Size, PartitionStyle, OperationalStatus | "
        "ConvertTo-Json -Compress"
    )
    if isinstance(data, dict):
        data = [data]
    return list(data or [])


def usb_audio_present() -> bool:
    return any(fn.role.startswith("usb-audio") for fn in list_usb_functions())


def format_usb_report() -> str:
    functions = list_usb_functions()
    luns = list_udisk_luns()
    lines = [
        f"USB gadget {USB_VID}:{USB_PID} (Jieli 'JL' / 'NU' defaults, advertised as Divoom Audio)",
    ]
    if not functions:
        lines.append("  not plugged in (or not enumerated)")
        return "\n".join(lines)
    for fn in functions:
        lines.append(f"  [{fn.class_name}] {fn.friendly_name}")
        lines.append(f"      {fn.role}")
    if luns:
        lines.append("  BR28 UDISK LUNs:")
        for lun in luns:
            size = lun.get("Size") or 0
            lines.append(
                f"      disk {lun.get('Number')}  status={lun.get('OperationalStatus')}  "
                f"size={size}  style={lun.get('PartitionStyle')}"
            )
        if all((lun.get("Size") or 0) == 0 for lun in luns):
            lines.append("      No media - TF/SD slot looks empty. MiniToo TF playback is music (MP3), not video.")
    lines.append("  Pixel/GIF commands still go over Bluetooth SPP (opcode 0x8B), not USB.")
    return "\n".join(lines)
