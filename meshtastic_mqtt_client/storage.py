"""SQLite persistence for nodes, messages, positions and telemetry."""

import csv
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import node_id_from_num
from .protocol import NodeInfo, Packet, PositionReport, TelemetryReport, TextMessage

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_num      INTEGER PRIMARY KEY,
    node_id       TEXT NOT NULL,
    long_name     TEXT,
    short_name    TEXT,
    hw_model      TEXT,
    role          TEXT,
    last_heard    INTEGER,
    hops_away     INTEGER,
    battery_level INTEGER,
    voltage       REAL,
    latitude      REAL,
    longitude     REAL,
    altitude      INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id   INTEGER NOT NULL,
    from_num    INTEGER NOT NULL,
    to_num      INTEGER NOT NULL,
    channel     TEXT,
    text        TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    outgoing    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_num    INTEGER NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    altitude    INTEGER,
    time        INTEGER,
    received_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    node_num            INTEGER NOT NULL,
    received_at         INTEGER NOT NULL,
    battery_level       INTEGER,
    voltage             REAL,
    channel_utilization REAL,
    air_util_tx         REAL,
    uptime_seconds      INTEGER,
    temperature         REAL,
    relative_humidity   REAL,
    barometric_pressure REAL
);
CREATE INDEX IF NOT EXISTS messages_received ON messages(received_at);
CREATE INDEX IF NOT EXISTS messages_sender_packet ON messages(from_num, packet_id);
CREATE INDEX IF NOT EXISTS positions_node ON positions(node_num, received_at);
"""


@dataclass(frozen=True)
class Node:
    node_num: int
    node_id: str
    long_name: str | None
    short_name: str | None
    hw_model: str | None
    role: str | None
    last_heard: int | None
    hops_away: int | None
    battery_level: int | None
    voltage: float | None
    latitude: float | None
    longitude: float | None
    altitude: int | None

    @property
    def display_name(self) -> str:
        return self.long_name or self.node_id


@dataclass(frozen=True)
class StoredMessage:
    packet_id: int
    from_num: int
    to_num: int
    channel: str | None
    text: str
    received_at: int
    outgoing: bool


class Storage:
    """Thread-safe: the MQTT network thread writes while the GUI thread reads."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, sql: str, params: tuple) -> None:
        with self._lock, self._db:
            self._db.execute(sql, params)

    def _read(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def touch_node(self, packet: Packet) -> None:
        """Record that we heard from a node, creating it if it's new."""
        self._write(
            """INSERT INTO nodes (node_num, node_id, last_heard, hops_away) VALUES (?, ?, ?, ?)
               ON CONFLICT(node_num) DO UPDATE SET
                   last_heard = MAX(COALESCE(last_heard, 0), excluded.last_heard),
                   hops_away = COALESCE(excluded.hops_away, hops_away)""",
            (packet.from_num, node_id_from_num(packet.from_num), packet.rx_time, packet.hops_away),
        )

    def save_nodeinfo(self, info: NodeInfo) -> None:
        self.touch_node(info.packet)
        self._write(
            """UPDATE nodes SET long_name = ?, short_name = ?, hw_model = ?, role = ?
               WHERE node_num = ?""",
            (info.long_name, info.short_name, info.hw_model, info.role, info.packet.from_num),
        )

    def save_text(self, msg: TextMessage, outgoing: bool = False) -> None:
        p = msg.packet
        if not outgoing:
            self.touch_node(p)
        self._write(
            """INSERT INTO messages (packet_id, from_num, to_num, channel, text, received_at, outgoing)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (p.packet_id, p.from_num, p.to_num, p.channel, msg.text, p.rx_time, int(outgoing)),
        )

    def save_position(self, pos: PositionReport) -> None:
        p = pos.packet
        self.touch_node(p)
        self._write(
            """INSERT INTO positions (node_num, latitude, longitude, altitude, time, received_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (p.from_num, pos.latitude, pos.longitude, pos.altitude, pos.time, p.rx_time),
        )
        self._write(
            "UPDATE nodes SET latitude = ?, longitude = ?, altitude = ? WHERE node_num = ?",
            (pos.latitude, pos.longitude, pos.altitude, p.from_num),
        )

    def save_telemetry(self, tel: TelemetryReport) -> None:
        p = tel.packet
        self.touch_node(p)
        self._write(
            """INSERT INTO telemetry (node_num, received_at, battery_level, voltage,
                   channel_utilization, air_util_tx, uptime_seconds, temperature,
                   relative_humidity, barometric_pressure)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p.from_num, p.rx_time, tel.battery_level, tel.voltage, tel.channel_utilization,
             tel.air_util_tx, tel.uptime_seconds, tel.temperature, tel.relative_humidity,
             tel.barometric_pressure),
        )
        if tel.battery_level is not None or tel.voltage is not None:
            self._write(
                """UPDATE nodes SET battery_level = COALESCE(?, battery_level),
                       voltage = COALESCE(?, voltage) WHERE node_num = ?""",
                (tel.battery_level, tel.voltage, p.from_num),
            )

    def nodes(self) -> list[Node]:
        rows = self._read(
            """SELECT node_num, node_id, long_name, short_name, hw_model, role, last_heard,
                      hops_away, battery_level, voltage, latitude, longitude, altitude
               FROM nodes ORDER BY last_heard DESC"""
        )
        return [Node(*row) for row in rows]

    def node(self, node_num: int) -> Node | None:
        rows = self._read(
            """SELECT node_num, node_id, long_name, short_name, hw_model, role, last_heard,
                      hops_away, battery_level, voltage, latitude, longitude, altitude
               FROM nodes WHERE node_num = ?""",
            (node_num,),
        )
        return Node(*rows[0]) if rows else None

    def count_nodes_heard_since(self, since: int, exclude: int | None = None) -> int:
        return self._read(
            "SELECT COUNT(*) FROM nodes WHERE last_heard >= ? AND node_num IS NOT ?",
            (since, exclude),
        )[0][0]

    def search_messages(self, query: str, limit: int = 200) -> list[StoredMessage]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._read(
            """SELECT packet_id, from_num, to_num, channel, text, received_at, outgoing
               FROM messages WHERE text LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?""",
            (f"%{escaped}%", limit),
        )
        return [StoredMessage(*row[:6], outgoing=bool(row[6])) for row in rows]

    def recent_messages(self, limit: int = 200) -> list[StoredMessage]:
        rows = self._read(
            """SELECT packet_id, from_num, to_num, channel, text, received_at, outgoing
               FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id""",
            (limit,),
        )
        return [StoredMessage(*row[:6], outgoing=bool(row[6])) for row in rows]

    def message_seen_count(self, from_num: int, packet_id: int) -> int:
        """How many times this (sender, packet id) has been stored, across restarts."""
        return self._read(
            "SELECT COUNT(*) FROM messages WHERE from_num = ? AND packet_id = ?",
            (from_num, packet_id),
        )[0][0]

    def _export(self, path: Path | str, sql: str) -> int:
        with self._lock:
            cur = self._db.execute(sql)
            header = [col[0] for col in cur.description]
            rows = cur.fetchall()
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
        return len(rows)

    def export_messages_csv(self, path: Path | str) -> int:
        return self._export(
            path,
            """SELECT datetime(m.received_at, 'unixepoch') AS time, n.node_id AS from_id,
                      n.long_name AS from_name, printf('!%08x', m.to_num) AS to_id,
                      m.channel, m.outgoing, m.text
               FROM messages m LEFT JOIN nodes n ON n.node_num = m.from_num
               ORDER BY m.id""",
        )

    def export_positions_csv(self, path: Path | str) -> int:
        return self._export(
            path,
            """SELECT datetime(p.received_at, 'unixepoch') AS time, n.node_id, n.long_name,
                      p.latitude, p.longitude, p.altitude
               FROM positions p LEFT JOIN nodes n ON n.node_num = p.node_num
               ORDER BY p.id""",
        )
