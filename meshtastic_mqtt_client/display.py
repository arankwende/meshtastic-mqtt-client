"""Human-readable formatting shared by the GUI and headless mode."""

import time
from datetime import datetime

from .config import node_id_from_num
from .protocol import BROADCAST, NodeInfo, PositionReport, SensorData, TelemetryReport
from .storage import Node, Storage

NODE_HEADINGS = ["Name", "Short", "ID", "Hardware", "Last heard", "Hops", "Battery", "Position"]


def node_name(storage: Storage, node_num: int) -> str:
    node = storage.node(node_num)
    return node.display_name if node else node_id_from_num(node_num)


def ago(ts: int | None, now: float | None = None) -> str:
    if not ts:
        return "never"
    seconds = max(0, int((now or time.time()) - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def battery_text(level: int | None) -> str:
    if level is None:
        return ""
    # Firmware reports 101 when running on external power.
    return "powered" if level > 100 else f"{level}%"


def chat_line(storage: Storage, own_num: int, from_num: int, to_num: int,
              text: str, ts: int) -> str:
    stamp = datetime.fromtimestamp(ts).strftime("%H:%M")
    sender = "You" if from_num == own_num else node_name(storage, from_num)
    if to_num == BROADCAST:
        return f"[{stamp}] {sender}: {text}"
    recipient = "you" if to_num == own_num else node_name(storage, to_num)
    return f"[{stamp}] {sender} → {recipient} (private): {text}"


def node_rows(nodes: list[Node], now: float | None = None) -> list[list[str]]:
    rows = []
    for n in nodes:
        position = f"{n.latitude:.5f}, {n.longitude:.5f}" if n.latitude is not None else ""
        rows.append([
            n.long_name or "?", n.short_name or "", n.node_id, n.hw_model or "",
            ago(n.last_heard, now), "" if n.hops_away is None else str(n.hops_away),
            battery_text(n.battery_level), position,
        ])
    return rows


def describe_event(storage: Storage, event) -> str | None:
    """One-line summary of non-chat events, for the log pane and headless output."""
    who = node_name(storage, event.packet.from_num)
    if isinstance(event, NodeInfo):
        return f"Node info: {event.long_name} ({event.short_name}, {event.node_id}, {event.hw_model})"
    if isinstance(event, PositionReport):
        return f"Position: {who} at {event.latitude:.5f}, {event.longitude:.5f}"
    if isinstance(event, SensorData):
        return f"Sensor data: {who}: " + ", ".join(f"{k}={v}" for k, v in event.values.items())
    if isinstance(event, TelemetryReport):
        parts = []
        if event.battery_level is not None:
            parts.append(f"battery {battery_text(event.battery_level)}")
        if event.voltage is not None:
            parts.append(f"{event.voltage:.2f} V")
        if event.channel_utilization is not None:
            parts.append(f"ch util {event.channel_utilization:.1f}%")
        if event.temperature is not None:
            parts.append(f"{event.temperature:.1f} °C")
        if event.relative_humidity is not None:
            parts.append(f"{event.relative_humidity:.0f}% RH")
        return f"Telemetry: {who}: {', '.join(parts)}" if parts else None
    return None
