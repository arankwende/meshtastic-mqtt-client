"""MQTT connection handling and the glue between protocol and storage."""

import logging
import secrets
import ssl
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

import paho.mqtt.client as mqtt
from meshtastic.protobuf import portnums_pb2

from . import protocol
from .config import Config, node_id_from_num
from .protocol import (
    BROADCAST,
    MeshEvent,
    NodeInfo,
    Packet,
    PositionReport,
    RoutingReport,
    SensorData,
    TelemetryReport,
    TextMessage,
)
from .storage import Storage

log = logging.getLogger(__name__)

NODEINFO_REPLY_INTERVAL = 60


class NotConnected(Exception):
    pass


class Deduplicator:
    """Remembers recent (sender, packet id) pairs.

    The same packet usually reaches the broker through several gateways.
    """

    def __init__(self, size: int = 2048):
        self._seen: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._size = size

    def is_new(self, from_num: int, packet_id: int) -> bool:
        key = (from_num, packet_id)
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return True


@dataclass(frozen=True)
class Delivery:
    """Outcome of a send: "delivered" (acknowledged), "sent" (broadcast, which
    can't be acknowledged) or "failed" with the reason."""

    status: str
    attempts: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("delivered", "sent")


class MeshStats:
    """Rolling traffic numbers for health reporting. Thread-safe."""

    WINDOW = 3600

    def __init__(self):
        self._lock = threading.Lock()
        self._packets: deque[float] = deque()
        self._gateways: dict[str, float] = {}
        self._gateway_util: dict[str, tuple[float, float]] = {}
        self.last_packet: float | None = None

    def _prune(self, now: float) -> None:
        while self._packets and now - self._packets[0] > self.WINDOW:
            self._packets.popleft()
        for key in [k for k, seen in self._gateways.items() if now - seen > self.WINDOW]:
            del self._gateways[key]
        for key in [k for k, (_, seen) in self._gateway_util.items() if now - seen > self.WINDOW]:
            del self._gateway_util[key]

    def record(self, packet: Packet, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._packets.append(now)
            self._gateways[packet.gateway_id] = now
            self.last_packet = now
            self._prune(now)

    def record_telemetry(self, report: TelemetryReport, now: float | None = None) -> None:
        now = now or time.time()
        node_id = node_id_from_num(report.packet.from_num)
        with self._lock:
            if report.channel_utilization is not None and node_id in self._gateways:
                self._gateway_util[node_id] = (report.channel_utilization, now)

    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            self._prune(now)
            utils = [u for u, _ in self._gateway_util.values()]
            return {
                "packets_last_hour": len(self._packets),
                "gateways_last_hour": len(self._gateways),
                "gateway_channel_utilization": round(max(utils), 1) if utils else None,
                "last_packet": self.last_packet,
            }


class MeshClient:
    """Callbacks run on paho's network thread; GUIs must marshal them to their own thread."""

    def __init__(
        self,
        config: Config,
        storage: Storage,
        on_event: Callable[[MeshEvent], None] | None = None,
        on_status: Callable[[bool, str], None] | None = None,
    ):
        self.config = config
        self.storage = storage
        self.stats = MeshStats()
        self.status: tuple[bool, str] = (False, "Not connected")
        self._listeners: list[Callable[[MeshEvent], None]] = [on_event] if on_event else []
        self._status_listeners: list[Callable[[bool, str], None]] = [on_status] if on_status else []
        self._dedup = Deduplicator()
        self._announced = False
        self._last_nodeinfo_reply = 0.0
        self._pending_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list[str]]] = {}

        mesh = config.mesh
        self._mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"meshtastic-mqtt-client-{mesh.node_id[1:]}-{secrets.token_hex(3)}",
        )
        if config.mqtt.username:
            self._mqtt.username_pw_set(config.mqtt.username, config.mqtt.password)
        if config.mqtt.tls:
            self._mqtt.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self._mqtt.reconnect_delay_set(min_delay=1, max_delay=60)
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_disconnect = self._on_disconnect
        self._mqtt.on_message = self._on_message

        self._remember_self()

    def add_listener(self, listener: Callable[[MeshEvent], None]) -> None:
        """Also deliver received events to listener (called on the MQTT thread)."""
        self._listeners.append(listener)

    def add_status_listener(self, listener: Callable[[bool, str], None]) -> None:
        self._status_listeners.append(listener)

    def _set_status(self, connected: bool, detail: str) -> None:
        self.status = (connected, detail)
        for listener in self._status_listeners:
            try:
                listener(connected, detail)
            except Exception:
                log.exception("Status listener %r failed", listener)

    # Connection lifecycle

    def connect(self) -> None:
        m = self.config.mqtt
        log.info("Connecting to %s:%d", m.host, m.port)
        self._set_status(False, f"Connecting to {m.host}:{m.port}…")
        self._mqtt.connect_async(m.host, m.port, keepalive=60)
        self._mqtt.loop_start()

    def disconnect(self) -> None:
        self._mqtt.disconnect()
        self._mqtt.loop_stop()

    @property
    def connected(self) -> bool:
        return self._mqtt.is_connected()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("MQTT broker refused the connection: %s", reason_code)
            self._set_status(False, f"Connection refused: {reason_code}")
            return
        topic = protocol.subscribe_topic(self.config.mqtt, self.config.mesh)
        client.subscribe(topic, qos=0)
        log.info("Connected, subscribed to %s", topic)
        self._set_status(True, f"Connected to {self.config.mqtt.host}, listening on {topic}")
        if not self._announced:
            self._announced = True
            self.send_nodeinfo(want_response=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code == 0:
            self._set_status(False, "Disconnected")
        else:
            log.warning("Lost connection to MQTT broker (%s), retrying", reason_code)
            self._set_status(False, f"Connection lost ({reason_code}), reconnecting…")

    # Receiving

    def _on_message(self, client, userdata, msg) -> None:
        try:
            self.handle_payload(msg.payload)
        except Exception:
            log.exception("Failed to handle message on %s", msg.topic)

    def handle_payload(self, raw: bytes) -> MeshEvent | None:
        me = self.config.mesh.node_num
        packet = protocol.decode_envelope(raw, self.config.mesh)
        if packet is None or packet.from_num == me:
            return None
        if packet.want_ack and packet.to_num == me:
            # Acknowledge even repeats: the sender retries when our ack got lost.
            self._send_ack(packet)
        if not self._dedup.is_new(packet.from_num, packet.packet_id):
            return None
        self.stats.record(packet)

        event = protocol.parse_packet(packet)
        if isinstance(event, TextMessage):
            if not (packet.is_broadcast or packet.to_num == me):
                return None
            self.storage.save_text(event)
        elif isinstance(event, NodeInfo):
            self.storage.save_nodeinfo(event)
            if packet.want_response:
                self._answer_nodeinfo_request(packet.from_num)
        elif isinstance(event, PositionReport):
            self.storage.save_position(event)
        elif isinstance(event, TelemetryReport):
            self.storage.save_telemetry(event)
            self.stats.record_telemetry(event)
        elif isinstance(event, SensorData):
            self.storage.touch_node(packet)
        elif isinstance(event, RoutingReport):
            if packet.to_num != me:
                return None
            self._resolve(event)
        else:
            self.storage.touch_node(packet)
            log.debug("Heard port %d from !%08x", packet.portnum, packet.from_num)
            return None
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                log.exception("Event listener %r failed", listener)
        return event

    # Sending

    def _publish(self, payload: bytes) -> None:
        if not self.connected:
            raise NotConnected("Not connected to the MQTT broker")
        topic = protocol.publish_topic(self.config.mqtt, self.config.mesh)
        self._mqtt.publish(topic, payload, qos=1)
        log.debug("Published %d bytes to %s", len(payload), topic)

    def _send_ack(self, packet: Packet) -> None:
        _, payload = protocol.encode_ack(self.config.mesh, packet.from_num, packet.packet_id)
        try:
            self._publish(payload)
        except NotConnected:
            pass

    def send_text(self, text: str, to: int = BROADCAST) -> TextMessage:
        text = text.strip()
        if not text:
            raise ValueError("Message is empty")
        packet_id, payload = protocol.encode_text(self.config.mesh, text, to)
        self._publish(payload)
        msg = TextMessage(self._own_packet(packet_id, to, text.encode("utf-8")), text)
        self.storage.save_text(msg, outgoing=True)
        return msg

    def send_text_reliably(self, text: str, to: int, retries: int = 2,
                           timeout: float = 30.0) -> tuple[TextMessage | None, Delivery]:
        """Send and, for direct messages, wait for the acknowledgement, retrying.

        Blocks for up to (retries + 1) * timeout, so call it off the GUI thread.
        """
        text = text.strip()
        if not text:
            raise ValueError("Message is empty")
        sent: list[TextMessage] = []

        def on_sent(packet_id: int) -> None:
            sent.append(TextMessage(self._own_packet(packet_id, to, text.encode("utf-8")), text))
            self.storage.save_text(sent[0], outgoing=True)

        delivery = self._deliver(
            lambda pid: protocol.encode_text(self.config.mesh, text, to, pid, want_ack=to != BROADCAST),
            to, retries, timeout, on_sent,
        )
        return (sent[0] if sent else None), delivery

    def send_data_reliably(self, portnum: int, payload: bytes, to: int, retries: int = 2,
                           timeout: float = 30.0) -> Delivery:
        return self._deliver(
            lambda pid: protocol.encode_data(self.config.mesh, portnum, payload, to, pid,
                                             want_ack=to != BROADCAST),
            to, retries, timeout,
        )

    def _deliver(self, encode: Callable[[int | None], tuple[int, bytes]], to: int,
                 retries: int, timeout: float,
                 on_sent: Callable[[int], None] | None = None) -> Delivery:
        packet_id = None
        for attempt in range(1, retries + 2):
            packet_id, payload = encode(packet_id)  # retries reuse the id, like the firmware
            done = threading.Event()
            result: list[str] = []
            if to != BROADCAST:
                with self._pending_lock:
                    self._pending[packet_id] = (done, result)
            try:
                self._publish(payload)
            except NotConnected:
                self._forget(packet_id)
                return Delivery("failed", attempt, "NOT_CONNECTED")
            if attempt == 1 and on_sent:
                on_sent(packet_id)
            if to == BROADCAST:
                return Delivery("sent", 1)
            done.wait(timeout)
            self._forget(packet_id)
            if result:
                if result[0] == "NONE":
                    return Delivery("delivered", attempt)
                return Delivery("failed", attempt, result[0])
            log.info("No acknowledgement for %08x to !%08x (attempt %d)", packet_id, to, attempt)
        return Delivery("failed", retries + 1, "TIMEOUT")

    def _forget(self, packet_id: int) -> None:
        with self._pending_lock:
            self._pending.pop(packet_id, None)

    def _resolve(self, report: RoutingReport) -> None:
        with self._pending_lock:
            waiter = self._pending.get(report.request_id)
        if waiter:
            done, result = waiter
            result.append(report.error)
            done.set()

    def send_nodeinfo(self, want_response: bool = False, to: int = BROADCAST) -> None:
        _, payload = protocol.encode_nodeinfo(self.config.mesh, want_response, to)
        try:
            self._publish(payload)
            log.info("Announced ourselves as %s (%s)", self.config.mesh.long_name, self.config.mesh.node_id)
        except NotConnected:
            log.warning("Not connected, node info not sent")

    def _answer_nodeinfo_request(self, requester: int) -> None:
        # Like the firmware, answer the requester directly, and rate-limit
        # because busy public channels are full of requests.
        if time.monotonic() - self._last_nodeinfo_reply > NODEINFO_REPLY_INTERVAL:
            self._last_nodeinfo_reply = time.monotonic()
            self.send_nodeinfo(to=requester)

    def _own_packet(self, packet_id: int, to: int, payload: bytes,
                    portnum: int = portnums_pb2.TEXT_MESSAGE_APP) -> Packet:
        mesh = self.config.mesh
        return Packet(packet_id=packet_id, from_num=mesh.node_num, to_num=to, portnum=portnum,
                      payload=payload, channel=mesh.channel, gateway_id=mesh.node_id,
                      rx_time=int(time.time()))

    def _remember_self(self) -> None:
        mesh = self.config.mesh
        self.storage.save_nodeinfo(NodeInfo(
            self._own_packet(0, BROADCAST, b"", portnums_pb2.NODEINFO_APP),
            node_id=mesh.node_id, long_name=mesh.long_name, short_name=mesh.short_name,
            hw_model="PRIVATE_HW", role="CLIENT_MUTE",
        ))
