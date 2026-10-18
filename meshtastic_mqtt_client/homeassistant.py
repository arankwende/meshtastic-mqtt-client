"""Home Assistant bridge: mesh sensors via MQTT Discovery, and Assist chat over DMs."""

import json
import logging
import queue
import re
import secrets
import ssl
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from . import __version__
from .client import Delivery, MeshClient
from .config import Config, node_id_from_num
from .protocol import (
    BROADCAST,
    NodeInfo,
    PositionReport,
    SensorData,
    TelemetryReport,
    TextMessage,
)

log = logging.getLogger(__name__)

TEXT_SENSOR_PREFIX = "#ha"
PRIVATE_APP = 256
HEALTH_INTERVAL = 60
# Leaves room for the Data header and an AES-CCM tag inside one LoRa packet.
MAX_REPLY_BYTES = 200
MAX_CUSTOM_KEYS_PER_NODE = 32

ORIGIN = {
    "name": "meshtastic-mqtt-client",
    "sw_version": __version__,
    "support_url": "https://github.com/arankwende/meshtastic-mqtt-client",
}

# Built-in telemetry fields and how Home Assistant should present them.
STANDARD_SENSORS: dict[str, dict] = {
    "battery_level": {"name": "Battery", "device_class": "battery", "unit_of_measurement": "%",
                      "state_class": "measurement"},
    "voltage": {"name": "Voltage", "device_class": "voltage", "unit_of_measurement": "V",
                "state_class": "measurement", "suggested_display_precision": 2},
    "temperature": {"name": "Temperature", "device_class": "temperature", "unit_of_measurement": "°C",
                    "state_class": "measurement", "suggested_display_precision": 1},
    "relative_humidity": {"name": "Humidity", "device_class": "humidity", "unit_of_measurement": "%",
                          "state_class": "measurement", "suggested_display_precision": 0},
    "barometric_pressure": {"name": "Pressure", "device_class": "atmospheric_pressure",
                            "unit_of_measurement": "hPa", "state_class": "measurement",
                            "suggested_display_precision": 1},
    "channel_utilization": {"name": "Channel utilization", "unit_of_measurement": "%",
                            "state_class": "measurement", "entity_category": "diagnostic",
                            "icon": "mdi:radio-tower", "suggested_display_precision": 1},
    "air_util_tx": {"name": "Airtime", "unit_of_measurement": "%", "state_class": "measurement",
                    "entity_category": "diagnostic", "icon": "mdi:radio",
                    "suggested_display_precision": 1},
    "hops_away": {"name": "Hops away", "state_class": "measurement", "entity_category": "diagnostic",
                  "icon": "mdi:transit-connection-variant"},
    "last_heard": {"name": "Last heard", "device_class": "timestamp", "entity_category": "diagnostic"},
}

TELEMETRY_FIELDS = ("battery_level", "voltage", "channel_utilization", "air_util_tx",
                    "temperature", "relative_humidity", "barometric_pressure")


def sensor_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")[:32]


def parse_text_sensors(text: str) -> dict[str, float | int | str | bool] | None:
    """Parse '#ha tank=73 door=open temp=21.5' into {'tank': 73, ...}."""
    if not text.lower().startswith(TEXT_SENSOR_PREFIX):
        return None
    values: dict[str, float | int | str | bool] = {}
    for item in text[len(TEXT_SENSOR_PREFIX):].split():
        name, sep, value = item.partition("=")
        if not sep or not name:
            continue
        values[name] = _coerce(value)
    return values


def _coerce(value: str) -> float | int | str | bool:
    lowered = value.lower()
    if lowered in ("true", "on"):
        return True
    if lowered in ("false", "off"):
        return False
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def _truncate(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _wrap(text: str, max_bytes: int) -> list[str]:
    chunks, current = [], ""
    for word in text.split():
        while len(word.encode("utf-8")) > max_bytes:
            head = _truncate(word, max_bytes)
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
            word = word[len(head):]
        candidate = f"{current} {word}" if current else word
        if len(candidate.encode("utf-8")) > max_bytes:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def split_message(text: str, max_parts: int, max_bytes: int = MAX_REPLY_BYTES) -> list[str]:
    """Split a reply into numbered packet-sized parts, truncating past max_parts."""
    chunks = _wrap(text, max_bytes)
    if len(chunks) <= 1:
        return chunks
    numbering = len(f"{max_parts}/{max_parts} ")
    chunks = _wrap(text, max_bytes - numbering)
    if len(chunks) > max_parts:
        chunks = chunks[:max_parts]
        chunks[-1] = _truncate(chunks[-1], max_bytes - numbering - len("…".encode())) + "…"
    total = len(chunks)
    return [f"{i}/{total} {chunk}" for i, chunk in enumerate(chunks, 1)]


@dataclass(frozen=True)
class Outgoing:
    """Something queued for the mesh: a text (Assist reply or alert) or a control command."""

    kind: str  # "assist", "alert" or "control"
    to: int
    text: str | None = None
    payload: dict | None = None


def _node_hex(node_id: str) -> str:
    return node_id[1:]


class HomeAssistantBridge:
    """Listens to MeshClient events and talks to Home Assistant over its MQTT broker."""

    def __init__(self, config: Config, client: MeshClient, mqtt_client: mqtt.Client | None = None):
        assert config.homeassistant is not None
        self.ha = config.homeassistant
        self.mesh = config.mesh
        self.client = client
        self.storage = client.storage
        base = self.ha.base_topic
        self.status_topic = f"{base}/status"
        self.request_topic = f"{base}/assist/request"
        self.reply_topic = f"{base}/assist/reply"
        self.bridge_state_topic = f"{base}/bridge/state"
        self.delivery_topic = f"{base}/bridge/delivery"
        self.message_topic = f"{base}/bridge/message"
        self.announce_topic = f"{base}/bridge/announce"
        self.ha_status_topic = f"{self.ha.discovery_prefix}/status"
        self.bridge_id = f"meshtastic_bridge_{_node_hex(self.mesh.node_id)}"

        self._sensor_nodes = {int(n[1:], 16) for n in self.ha.nodes}
        assist = self.ha.assist
        self._assist_nodes = {int(n[1:], 16) for n in assist.allowed_nodes} if assist else set()
        self._alert_nodes = {int(n[1:], 16) for n in self.ha.alert_nodes}
        self._controls = {(int(c.node[1:], 16), c.key): c for c in self.ha.controls}
        self._requests: dict[int, deque[float]] = defaultdict(deque)

        self._lock = threading.Lock()
        self._stopping = False
        self._stopped = threading.Event()
        self._state: dict[int, dict] = {}
        self._discovery: dict[str, str] = {}
        self._entities: dict[int, set[str]] = defaultdict(set)
        self._last_delivery: dict | None = None
        self._outbox: queue.Queue[Outgoing | None] = queue.Queue()
        self._sender = threading.Thread(target=self._send_loop, name="ha-outbox", daemon=True)
        self._health = threading.Thread(target=self._health_loop, name="ha-health", daemon=True)

        if mqtt_client is None:
            m = self.ha.mqtt
            mqtt_client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"meshtastic-ha-bridge-{self.mesh.node_id[1:]}-{secrets.token_hex(3)}",
            )
            if m.username:
                mqtt_client.username_pw_set(m.username, m.password)
            if m.tls:
                mqtt_client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._mqtt = mqtt_client
        self._mqtt.will_set(self.status_topic, "offline", qos=1, retain=True)
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message
        self._register_bridge_entities()

    # Lifecycle

    def start(self) -> None:
        self.client.add_listener(self.handle_event)
        self.client.add_status_listener(lambda connected, detail: self.publish_health())
        self._sender.start()
        self._health.start()
        m = self.ha.mqtt
        log.info("Connecting to Home Assistant broker %s:%d", m.host, m.port)
        self._mqtt.connect_async(m.host, m.port, keepalive=60)
        self._mqtt.loop_start()

    def stop(self) -> None:
        self._stopping = True
        self._stopped.set()
        self._outbox.put(None)
        self._mqtt.publish(self.status_topic, "offline", qos=1, retain=True)
        self._mqtt.disconnect()
        self._mqtt.loop_stop()

    def _publish(self, topic: str, payload, retain: bool = False) -> None:
        if not isinstance(payload, str):
            payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        self._mqtt.publish(topic, payload, qos=1, retain=retain)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("Home Assistant broker refused the connection: %s", reason_code)
            return
        base = self.ha.base_topic
        topics = [
            self.ha_status_topic,
            # A previous instance's last will can arrive after we announced ourselves.
            self.status_topic,
            # Our own retained states: restores what each node reported before a restart.
            f"{base}/+/state",
            self.announce_topic,
        ]
        if self.ha.assist:
            topics.append(self.reply_topic)
        if self._alert_nodes or self.ha.alert_channel:
            topics.append(f"{base}/notify/+")
        if self._controls:
            topics += [f"{base}/+/set/+", f"{base}/+/press/+"]
        for topic in topics:
            client.subscribe(topic, qos=1)
        self._publish(self.status_topic, "online", retain=True)
        self._republish()
        self.publish_health()
        log.info("Connected to Home Assistant broker")

    def _on_message(self, client, userdata, msg) -> None:
        try:
            self._route(msg.topic, msg.payload)
        except Exception:
            log.exception("Failed to handle %s", msg.topic)

    def _route(self, topic: str, payload: bytes) -> None:
        base = re.escape(self.ha.base_topic)
        if topic == self.ha_status_topic:
            if payload == b"online":
                log.info("Home Assistant restarted, republishing discovery")
                self._republish()
                self.publish_health()
        elif topic == self.status_topic:
            if payload == b"offline" and not self._stopping:
                log.info("Replacing a stale 'offline' status left by a previous instance")
                self._publish(self.status_topic, "online", retain=True)
        elif topic == self.reply_topic:
            self.handle_reply(payload)
        elif topic == self.announce_topic:
            self.client.send_nodeinfo()
        elif m := re.fullmatch(base + r"/notify/([0-9a-f]{8}|channel)", topic):
            self.handle_alert(m.group(1), payload)
        elif m := re.fullmatch(base + r"/([0-9a-f]{8})/(set|press)/([a-z0-9_]+)", topic):
            self.handle_control(int(m.group(1), 16), m.group(3), m.group(2), payload)
        elif topic.endswith("/state"):
            self.restore_state(topic, payload)

    def _republish(self) -> None:
        with self._lock:
            discovery = list(self._discovery.items())
            states = list(self._state.items())
            delivery = self._last_delivery
        for topic, payload in discovery:
            self._publish(topic, payload, retain=True)
        for node_num, state in states:
            self._publish_state(node_num, state)
        if delivery:
            self._publish(self.delivery_topic, delivery, retain=True)

    def restore_state(self, topic: str, payload: bytes) -> None:
        match = re.fullmatch(re.escape(self.ha.base_topic) + r"/([0-9a-f]{8})/state", topic)
        if not match:
            return
        node = int(match.group(1), 16)
        if node not in self._sensor_nodes:
            return
        try:
            retained = json.loads(payload)
        except ValueError:
            return
        if not isinstance(retained, dict):
            return
        with self._lock:
            current = self._state.setdefault(node, {"custom": {}})
            # Values received since startup are newer than anything retained.
            merged = {**retained, **current}
            merged["custom"] = {**(retained.get("custom") or {}), **current["custom"]}
            current.clear()
            current.update(merged)

    # Discovery helpers

    def _bridge_device(self) -> dict:
        return {
            "identifiers": [self.bridge_id],
            "name": self.mesh.long_name,
            "manufacturer": "Meshtastic",
            "model": "MQTT bridge",
            "sw_version": __version__,
        }

    def _device(self, node: int) -> dict:
        info = self.storage.node(node)
        device = {
            "identifiers": [f"meshtastic_{node:08x}"],
            "name": (info.long_name if info and info.long_name else node_id_from_num(node)),
            "manufacturer": "Meshtastic",
            "via_device": self.bridge_id,
        }
        if info and info.hw_model:
            device["model"] = info.hw_model
        return device

    def _register(self, topic: str, payload: dict) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self._discovery[topic] = encoded
        self._publish(topic, encoded, retain=True)

    def _entity(self, node: int, component: str, key: str, config: dict) -> None:
        topic = f"{self.ha.discovery_prefix}/{component}/meshtastic_{node:08x}/{key}/config"
        payload = {
            "unique_id": f"meshtastic_{node:08x}_{key}",
            "availability_topic": self.status_topic,
            "device": self._device(node),
            "origin": ORIGIN,
            **config,
        }
        if self.ha.expire_after and component in ("sensor", "binary_sensor"):
            payload["expire_after"] = self.ha.expire_after
        with self._lock:
            self._entities[node].add(key)
        self._register(topic, payload)

    def _bridge_entity(self, component: str, key: str, config: dict) -> None:
        topic = f"{self.ha.discovery_prefix}/{component}/{self.bridge_id}/{key}/config"
        self._register(topic, {
            "unique_id": f"{self.bridge_id}_{key}",
            "availability_topic": self.status_topic,
            "device": self._bridge_device(),
            "origin": ORIGIN,
            **config,
        })

    def _register_bridge_entities(self) -> None:
        state = self.bridge_state_topic
        self._bridge_entity("binary_sensor", "mesh_connected", {
            "name": "Mesh connection", "device_class": "connectivity", "state_topic": state,
            "value_template": "{{ 'ON' if value_json.mesh_connected else 'OFF' }}",
            "entity_category": "diagnostic",
        })
        for key, extra in {
            "last_packet": {"name": "Last packet", "device_class": "timestamp"},
            "packets_last_hour": {"name": "Packets last hour", "state_class": "measurement",
                                  "unit_of_measurement": "packets", "icon": "mdi:counter"},
            "nodes_last_24h": {"name": "Nodes heard (24 h)", "state_class": "measurement",
                               "icon": "mdi:account-group"},
            "gateways_last_hour": {"name": "Gateways last hour", "state_class": "measurement",
                                   "icon": "mdi:router-wireless"},
            "gateway_channel_utilization": {"name": "Gateway channel utilization",
                                            "unit_of_measurement": "%", "state_class": "measurement",
                                            "icon": "mdi:radio-tower"},
        }.items():
            self._bridge_entity("sensor", key, {
                "state_topic": state, "value_template": f"{{{{ value_json.{key} }}}}",
                "entity_category": "diagnostic", **extra,
            })
        self._bridge_entity("sensor", "last_delivery", {
            "name": "Last delivery", "icon": "mdi:message-check", "state_topic": self.delivery_topic,
            "value_template": "{{ value_json.status }}", "json_attributes_topic": self.delivery_topic,
        })
        self._bridge_entity("button", "announce", {
            "name": "Announce on mesh", "command_topic": self.announce_topic, "icon": "mdi:bullhorn",
            "entity_category": "config",
        })
        if self.ha.events:
            self._bridge_entity("event", "message", {
                "name": "Mesh message", "state_topic": self.message_topic,
                "event_types": ["channel_message", "direct_message"], "icon": "mdi:message-text",
            })
        for node_id in self.ha.alert_nodes:
            self._bridge_entity("notify", f"alert_{_node_hex(node_id)}", {
                "name": f"Message {node_id}", "command_topic": f"{self.ha.base_topic}/notify/{_node_hex(node_id)}",
            })
        if self.ha.alert_channel:
            self._bridge_entity("notify", "alert_channel", {
                "name": f"Message channel {self.mesh.channel}",
                "command_topic": f"{self.ha.base_topic}/notify/channel",
            })
        for control in self.ha.controls:
            node = int(control.node[1:], 16)
            self._register_control(node, control)

    def _register_control(self, node: int, control) -> None:
        name = control.name or control.key.replace("_", " ").capitalize()
        node_hex = f"{node:08x}"
        if control.type == "switch":
            k = control.key
            self._entity(node, "switch", f"control_{k}", {
                "name": name,
                "command_topic": f"{self.ha.base_topic}/{node_hex}/set/{k}",
                "state_topic": self._state_topic(node),
                "value_template": (
                    f"{{% set v = value_json.custom.get('{k}') if value_json.custom is defined else none %}}"
                    "{{ 'None' if v is none else ('ON' if v in [true, 1, 'on', 'ON', 'true'] else 'OFF') }}"
                ),
                "optimistic": control.optimistic,
            })
        else:
            self._entity(node, "button", f"control_{control.key}", {
                "name": name,
                "command_topic": f"{self.ha.base_topic}/{node_hex}/press/{control.key}",
            })

    # Bridge health

    def health(self) -> dict:
        snap = self.client.stats.snapshot()
        last = snap["last_packet"]
        return {
            "mesh_connected": self.client.connected,
            "last_packet": datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
            "packets_last_hour": snap["packets_last_hour"],
            "gateways_last_hour": snap["gateways_last_hour"],
            "gateway_channel_utilization": snap["gateway_channel_utilization"],
            "nodes_last_24h": self.storage.count_nodes_heard_since(
                int(time.time()) - 86400, exclude=self.mesh.node_num),
        }

    def publish_health(self) -> None:
        self._publish(self.bridge_state_topic, self.health(), retain=True)

    def _health_loop(self) -> None:
        while not self._stopped.wait(HEALTH_INTERVAL):
            try:
                self.publish_health()
            except Exception:
                log.exception("Failed to publish bridge health")

    # Mesh → Home Assistant

    def handle_event(self, event) -> None:
        packet = event.packet
        node = packet.from_num

        if isinstance(event, TextMessage):
            values = parse_text_sensors(event.text)
            if values is None:
                self._publish_message_event(event)
                if self.ha.assist:
                    self.handle_assist_request(event)
            if node in self._sensor_nodes:
                self._update(node, packet, custom=values)
            elif values is not None:
                log.debug("Ignoring #ha message from !%08x, not in homeassistant.nodes", node)
            return

        if node not in self._sensor_nodes:
            return
        if isinstance(event, NodeInfo):
            self._update(node, packet, refresh_device=True)
        elif isinstance(event, TelemetryReport):
            fields = {f: getattr(event, f) for f in TELEMETRY_FIELDS if getattr(event, f) is not None}
            if fields.get("battery_level", 0) > 100:
                fields["battery_level"] = 100  # 101 means "on external power"
            self._update(node, packet, fields=fields)
        elif isinstance(event, PositionReport):
            self._update(node, packet, position=event)
        elif isinstance(event, SensorData):
            self._update(node, packet, custom=event.values)

    def _publish_message_event(self, msg: TextMessage) -> None:
        if not self.ha.events:
            return
        p = msg.packet
        if p.to_num not in (BROADCAST, self.mesh.node_num):
            return
        info = self.storage.node(p.from_num)
        event = {
            "event_type": "channel_message" if p.is_broadcast else "direct_message",
            "from": node_id_from_num(p.from_num),
            "name": info.long_name if info and info.long_name else node_id_from_num(p.from_num),
            "text": msg.text,
            "channel": self.mesh.channel,
            "hops_away": p.hops_away,
        }
        if not p.is_broadcast:
            event["allowed"] = p.from_num in self._assist_nodes
        if info and info.latitude is not None:
            event["latitude"], event["longitude"] = info.latitude, info.longitude
        self._publish(self.message_topic, event)

    def _state_topic(self, node: int) -> str:
        return f"{self.ha.base_topic}/{node:08x}/state"

    def _position_topic(self, node: int) -> str:
        return f"{self.ha.base_topic}/{node:08x}/position"

    def _update(self, node: int, packet, fields: dict | None = None, custom: dict | None = None,
                position: PositionReport | None = None, refresh_device: bool = False) -> None:
        fields = dict(fields or {})
        fields["last_heard"] = datetime.fromtimestamp(packet.rx_time, timezone.utc).isoformat()
        if packet.hops_away is not None:
            fields["hops_away"] = packet.hops_away

        with self._lock:
            state = self._state.setdefault(node, {"custom": {}})
            state.update(fields)
            known = set(self._entities[node])

        state_topic = self._state_topic(node)
        for key in fields:
            if key not in known:
                self._entity(node, "sensor", key, {
                    "state_topic": state_topic,
                    "value_template": f"{{{{ value_json.{key} }}}}",
                    **STANDARD_SENSORS[key],
                })

        for raw_key, value in (custom or {}).items():
            key = sensor_key(raw_key)
            entity_key = f"custom_{key}"
            if not key:
                continue
            with self._lock:
                if key not in state["custom"] and len(state["custom"]) >= MAX_CUSTOM_KEYS_PER_NODE:
                    log.warning("!%08x sent more than %d custom sensors; ignoring %s",
                                node, MAX_CUSTOM_KEYS_PER_NODE, key)
                    continue
                state["custom"][key] = value
            # A controlled value is shown by its switch, not by a separate sensor.
            if entity_key not in known and (node, key) not in self._controls:
                self._entity(node, *self._custom_entity(key, value, state_topic))

        if position is not None:
            if "position" not in known:
                self._entity(node, "device_tracker", "position", {
                    "name": "Position",
                    "json_attributes_topic": self._position_topic(node),
                    "source_type": "gps",
                })
            self._publish(self._position_topic(node), {
                "latitude": round(position.latitude, 7),
                "longitude": round(position.longitude, 7),
                **({"altitude": position.altitude} if position.altitude is not None else {}),
            }, retain=True)

        if refresh_device:
            # Names arrive with NodeInfo; re-announce every entity under the new name.
            with self._lock:
                topics = [t for t in self._discovery if f"/meshtastic_{node:08x}/" in t]
            device = self._device(node)
            for topic in topics:
                with self._lock:
                    payload = json.loads(self._discovery[topic])
                    payload["device"] = device
                    self._discovery[topic] = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                self._publish(topic, self._discovery[topic], retain=True)

        self._publish_state(node, state)

    def _custom_entity(self, key: str, value, state_topic: str) -> tuple[str, str, dict]:
        overrides = self.ha.sensors.get(key, {})
        base = {"name": overrides.get("name", key.replace("_", " ").capitalize()),
                "state_topic": state_topic}
        if isinstance(value, bool):
            return "binary_sensor", f"custom_{key}", {
                **base,
                "value_template": f"{{{{ 'ON' if value_json.custom['{key}'] else 'OFF' }}}}",
                **{k: v for k, v in overrides.items() if k in ("device_class", "icon")},
            }
        config = {**base, "value_template": f"{{{{ value_json.custom['{key}'] }}}}"}
        if isinstance(value, (int, float)):
            config["state_class"] = "measurement"
        config.update({k: v for k, v in overrides.items() if k != "name"})
        return "sensor", f"custom_{key}", config

    def _publish_state(self, node: int, state: dict) -> None:
        with self._lock:
            payload = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
        self._publish(self._state_topic(node), payload, retain=True)

    # Assist chat

    def handle_assist_request(self, msg: TextMessage) -> None:
        p = msg.packet
        if p.to_num != self.mesh.node_num:
            return  # Only direct messages to this client, never channel broadcasts.
        if p.from_num not in self._assist_nodes:
            log.warning("Ignoring Assist request from !%08x: not in assist.allowed_nodes", p.from_num)
            return
        text = msg.text.strip()
        if not text:
            return
        # The client stores each message before listeners run, so more than one
        # copy means this exact packet was received before: a replay.
        if self.storage.message_seen_count(p.from_num, p.packet_id) > 1:
            log.warning("Dropping replayed Assist request %08x from !%08x", p.packet_id, p.from_num)
            return
        now = time.monotonic()
        recent = self._requests[p.from_num]
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= self.ha.assist.max_requests_per_minute:
            log.warning("Dropping Assist request from !%08x: rate limit reached", p.from_num)
            return
        recent.append(now)
        info = self.storage.node(p.from_num)
        log.info("Assist request from !%08x: %s", p.from_num, text)
        self._publish(self.request_topic, {
            "from": node_id_from_num(p.from_num),
            "name": info.long_name if info and info.long_name else node_id_from_num(p.from_num),
            "text": text,
            "packet_id": p.packet_id,
        })

    def handle_reply(self, raw: bytes) -> None:
        try:
            reply = json.loads(raw)
            to = str(reply["to"]).lower()
            text = str(reply.get("text") or "").strip()
        except (ValueError, KeyError, TypeError):
            log.warning("Ignoring malformed Assist reply: %r", raw[:200])
            return
        if not re.fullmatch(r"![0-9a-f]{8}", to) or int(to[1:], 16) not in self._assist_nodes:
            log.warning("Ignoring Assist reply to %s: not in assist.allowed_nodes", to)
            return
        self._queue_text("assist", int(to[1:], 16), text)

    # Alerts and controls (Home Assistant → mesh)

    def handle_alert(self, target: str, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if target == "channel":
            if not self.ha.alert_channel:
                log.warning("Ignoring channel alert: alerts.channel is disabled")
                return
            self._queue_text("alert", BROADCAST, text)
            return
        node = int(target, 16)
        if node not in self._alert_nodes:
            log.warning("Ignoring alert to !%08x: not in alerts.nodes", node)
            return
        self._queue_text("alert", node, text)

    def handle_control(self, node: int, key: str, action: str, raw: bytes) -> None:
        control = self._controls.get((node, key))
        if control is None or (action == "set") != (control.type == "switch"):
            log.warning("Ignoring unknown control %s/%s for !%08x", action, key, node)
            return
        if action == "set":
            command = raw.decode().strip().upper()
            if command not in ("ON", "OFF"):
                log.warning("Ignoring switch command %r for %s", command, key)
                return
            payload = {key: command == "ON"}
        else:
            payload = {key: "press"}
        log.info("Control %s → !%08x: %s", key, node, payload)
        self._outbox.put(Outgoing("control", node, payload=payload))

    def _queue_text(self, kind: str, to: int, text: str) -> None:
        if not text:
            return
        for part in split_message(text, self.ha.delivery.max_parts):
            self._outbox.put(Outgoing(kind, to, text=part))

    def _send_loop(self) -> None:
        while (item := self._outbox.get()) is not None:
            try:
                self._deliver(item)
            except Exception:
                log.exception("Failed to send %s to !%08x", item.kind, item.to)
            if not self._outbox.empty():
                time.sleep(self.ha.delivery.interval)

    def _deliver(self, item: Outgoing) -> Delivery:
        d = self.ha.delivery
        if item.text is not None:
            _, delivery = self.client.send_text_reliably(item.text, item.to, d.retries, d.ack_timeout)
        else:
            body = json.dumps(item.payload, separators=(",", ":")).encode()
            delivery = self.client.send_data_reliably(PRIVATE_APP, body, item.to, d.retries, d.ack_timeout)
        if not delivery.ok:
            log.error("%s to %s not delivered: %s after %d attempt(s)", item.kind.capitalize(),
                      "channel" if item.to == BROADCAST else node_id_from_num(item.to),
                      delivery.error, delivery.attempts)
        status = {
            "status": delivery.status,
            "kind": item.kind,
            "to": "channel" if item.to == BROADCAST else node_id_from_num(item.to),
            "text": item.text if item.text is not None else json.dumps(item.payload),
            "attempts": delivery.attempts,
            "error": delivery.error,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._last_delivery = status
        self._publish(self.delivery_topic, status, retain=True)
        return delivery
