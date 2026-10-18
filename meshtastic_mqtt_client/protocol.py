"""Encoding and decoding of Meshtastic 2.x MQTT ServiceEnvelopes."""

import json
import logging
import random
import time
from dataclasses import dataclass

from google.protobuf.message import DecodeError
from meshtastic.protobuf import (
    config_pb2,
    mesh_pb2,
    mqtt_pb2,
    portnums_pb2,
    telemetry_pb2,
)

from .config import MeshConfig, MqttConfig
from .crypto import AEAD_TAG_SIZE, aead_decrypt, aead_encrypt, channel_hash, crypt

log = logging.getLogger(__name__)

BROADCAST = 0xFFFFFFFF
DEFAULT_HOP_LIMIT = 3
# Data.bitfield bit 0: the sender allows this packet to be uplinked to MQTT.
OK_TO_MQTT = 1


def subscribe_topic(mqtt: MqttConfig, mesh: MeshConfig) -> str:
    return f"{mqtt.root_topic}/2/e/{mesh.channel}/#"


def publish_topic(mqtt: MqttConfig, mesh: MeshConfig) -> str:
    return f"{mqtt.root_topic}/2/e/{mesh.channel}/{mesh.node_id}"


@dataclass(frozen=True)
class Packet:
    """A decrypted mesh packet plus the envelope metadata it arrived with."""

    packet_id: int
    from_num: int
    to_num: int
    portnum: int
    payload: bytes
    channel: str
    gateway_id: str
    rx_time: int
    rx_snr: float = 0.0
    rx_rssi: int = 0
    hop_limit: int = 0
    hop_start: int = 0
    want_response: bool = False
    want_ack: bool = False
    request_id: int = 0

    @property
    def is_broadcast(self) -> bool:
        return self.to_num == BROADCAST

    @property
    def hops_away(self) -> int | None:
        if self.hop_start == 0:
            return None
        return self.hop_start - self.hop_limit


@dataclass(frozen=True)
class TextMessage:
    packet: Packet
    text: str


@dataclass(frozen=True)
class NodeInfo:
    packet: Packet
    node_id: str
    long_name: str
    short_name: str
    hw_model: str
    role: str


@dataclass(frozen=True)
class PositionReport:
    packet: Packet
    latitude: float
    longitude: float
    altitude: int | None
    time: int | None


@dataclass(frozen=True)
class TelemetryReport:
    packet: Packet
    battery_level: int | None = None
    voltage: float | None = None
    channel_utilization: float | None = None
    air_util_tx: float | None = None
    uptime_seconds: int | None = None
    temperature: float | None = None
    relative_humidity: float | None = None
    barometric_pressure: float | None = None


@dataclass(frozen=True)
class SensorData:
    """Custom readings a node sent as a JSON object on PRIVATE_APP (port 256)."""

    packet: Packet
    values: dict[str, float | int | str | bool]


@dataclass(frozen=True)
class RoutingReport:
    """An acknowledgement (error "NONE") or failure for a packet someone sent."""

    packet: Packet
    request_id: int
    error: str

    @property
    def delivered(self) -> bool:
        return self.error == "NONE"


MeshEvent = TextMessage | NodeInfo | PositionReport | TelemetryReport | SensorData | RoutingReport


def _parse_sensor_json(payload: bytes) -> dict | None:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    values = {str(k): v for k, v in obj.items()
              if isinstance(v, (int, float, str, bool))}
    return values or None


def decode_envelope(raw: bytes, mesh: MeshConfig) -> Packet | None:
    """Parse and, if needed, decrypt an MQTT payload. None if it isn't for us."""
    envelope = mqtt_pb2.ServiceEnvelope()
    try:
        envelope.ParseFromString(raw)
    except DecodeError:
        log.debug("Ignoring payload that is not a ServiceEnvelope")
        return None
    pkt = envelope.packet
    from_num = getattr(pkt, "from")
    variant = pkt.WhichOneof("payload_variant")

    if variant == "decoded":
        data = pkt.decoded
    elif variant == "encrypted":
        if pkt.pki_encrypted or pkt.channel == 0:
            log.debug("Skipping PKI-encrypted direct message %08x from !%08x", pkt.id, from_num)
            return None
        key = mesh.aes_key
        if key and pkt.channel == channel_hash(mesh.channel, key):
            plain = crypt(key, pkt.id, from_num, pkt.encrypted)
        elif key and pkt.channel == channel_hash(mesh.channel, key, aead=True):
            plain = aead_decrypt(key, pkt.id, from_num, pkt.to, pkt.encrypted)
        else:
            log.debug("Skipping packet %08x encrypted for another channel/key", pkt.id)
            return None
        data = mesh_pb2.Data()
        try:
            data.ParseFromString(plain or b"")
        except DecodeError:
            data.Clear()
        if data.portnum == portnums_pb2.UNKNOWN_APP:
            log.debug("Could not decrypt packet %08x from !%08x", pkt.id, from_num)
            return None
    else:
        return None

    return Packet(
        packet_id=pkt.id,
        from_num=from_num,
        to_num=pkt.to,
        portnum=data.portnum,
        payload=data.payload,
        channel=envelope.channel_id,
        gateway_id=envelope.gateway_id,
        # The gateway's rx_time is only as good as its clock, which is often unset.
        rx_time=int(time.time()),
        rx_snr=pkt.rx_snr,
        rx_rssi=pkt.rx_rssi,
        hop_limit=pkt.hop_limit,
        hop_start=pkt.hop_start,
        want_response=data.want_response,
        want_ack=pkt.want_ack,
        request_id=data.request_id,
    )


def _opt(msg, name):
    return getattr(msg, name) if msg.HasField(name) else None


def parse_packet(packet: Packet) -> MeshEvent | None:
    """Turn a packet into a typed event for the ports this client understands."""
    try:
        if packet.portnum == portnums_pb2.TEXT_MESSAGE_APP:
            return TextMessage(packet, packet.payload.decode("utf-8", errors="replace"))

        if packet.portnum == portnums_pb2.NODEINFO_APP:
            user = mesh_pb2.User()
            user.ParseFromString(packet.payload)
            return NodeInfo(
                packet,
                node_id=user.id,
                long_name=user.long_name,
                short_name=user.short_name,
                hw_model=mesh_pb2.HardwareModel.Name(user.hw_model),
                role=config_pb2.Config.DeviceConfig.Role.Name(user.role),
            )

        if packet.portnum == portnums_pb2.POSITION_APP:
            pos = mesh_pb2.Position()
            pos.ParseFromString(packet.payload)
            if not (pos.latitude_i or pos.longitude_i):
                return None
            return PositionReport(
                packet,
                latitude=pos.latitude_i * 1e-7,
                longitude=pos.longitude_i * 1e-7,
                altitude=_opt(pos, "altitude"),
                time=pos.time or None,
            )

        if packet.portnum == portnums_pb2.ROUTING_APP:
            routing = mesh_pb2.Routing()
            routing.ParseFromString(packet.payload)
            if routing.WhichOneof("variant") != "error_reason" or not packet.request_id:
                return None
            return RoutingReport(packet, packet.request_id, mesh_pb2.Routing.Error.Name(routing.error_reason))

        if packet.portnum == portnums_pb2.PRIVATE_APP:
            values = _parse_sensor_json(packet.payload)
            return SensorData(packet, values) if values else None

        if packet.portnum == portnums_pb2.TELEMETRY_APP:
            tel = telemetry_pb2.Telemetry()
            tel.ParseFromString(packet.payload)
            variant = tel.WhichOneof("variant")
            if variant == "device_metrics":
                m = tel.device_metrics
                return TelemetryReport(
                    packet,
                    battery_level=_opt(m, "battery_level"),
                    voltage=_opt(m, "voltage"),
                    channel_utilization=_opt(m, "channel_utilization"),
                    air_util_tx=_opt(m, "air_util_tx"),
                    uptime_seconds=_opt(m, "uptime_seconds"),
                )
            if variant == "environment_metrics":
                m = tel.environment_metrics
                return TelemetryReport(
                    packet,
                    temperature=_opt(m, "temperature"),
                    relative_humidity=_opt(m, "relative_humidity"),
                    barometric_pressure=_opt(m, "barometric_pressure"),
                )
    except (DecodeError, ValueError) as exc:
        log.debug("Malformed %s payload from !%08x: %s",
                  portnums_pb2.PortNum.Name(packet.portnum), packet.from_num, exc)
    return None


def _envelope(mesh: MeshConfig, data: mesh_pb2.Data, to: int, packet_id: int | None,
              want_ack: bool = False, priority: int = 0) -> tuple[int, bytes]:
    data.bitfield |= OK_TO_MQTT
    plain = data.SerializeToString()
    limit = mesh_pb2.Constants.DATA_PAYLOAD_LEN - (AEAD_TAG_SIZE if mesh.aead else 0)
    if len(plain) > limit:
        raise ValueError(f"Message too long ({len(plain)} bytes, max {limit})")

    pkt = mesh_pb2.MeshPacket()
    packet_id = packet_id or random.randint(1, 0xFFFFFFFF)
    setattr(pkt, "from", mesh.node_num)
    pkt.to = to
    pkt.id = packet_id
    pkt.rx_time = int(time.time())
    pkt.hop_limit = DEFAULT_HOP_LIMIT
    pkt.hop_start = DEFAULT_HOP_LIMIT
    pkt.want_ack = want_ack
    if priority:
        pkt.priority = priority
    key = mesh.aes_key
    pkt.channel = channel_hash(mesh.channel, key, aead=mesh.aead)
    if key and mesh.aead:
        pkt.encrypted = aead_encrypt(key, packet_id, mesh.node_num, to, plain)
    elif key:
        pkt.encrypted = crypt(key, packet_id, mesh.node_num, plain)
    else:
        pkt.decoded.CopyFrom(data)

    envelope = mqtt_pb2.ServiceEnvelope(channel_id=mesh.channel, gateway_id=mesh.node_id)
    envelope.packet.CopyFrom(pkt)
    return packet_id, envelope.SerializeToString()


def encode_data(mesh: MeshConfig, portnum: int, payload: bytes, to: int = BROADCAST,
                packet_id: int | None = None, want_ack: bool = False) -> tuple[int, bytes]:
    data = mesh_pb2.Data(portnum=portnum, payload=payload)
    return _envelope(mesh, data, to, packet_id, want_ack=want_ack)


def encode_text(mesh: MeshConfig, text: str, to: int = BROADCAST,
                packet_id: int | None = None, want_ack: bool = False) -> tuple[int, bytes]:
    return encode_data(mesh, portnums_pb2.TEXT_MESSAGE_APP, text.encode("utf-8"), to, packet_id, want_ack)


def encode_ack(mesh: MeshConfig, to: int, request_id: int) -> tuple[int, bytes]:
    """The routing reply a node sends when it receives a want_ack packet."""
    routing = mesh_pb2.Routing(error_reason=mesh_pb2.Routing.Error.NONE)
    data = mesh_pb2.Data(portnum=portnums_pb2.ROUTING_APP, payload=routing.SerializeToString(),
                         request_id=request_id)
    return _envelope(mesh, data, to, None, priority=mesh_pb2.MeshPacket.Priority.ACK)


def encode_nodeinfo(mesh: MeshConfig, want_response: bool = False, to: int = BROADCAST,
                    packet_id: int | None = None) -> tuple[int, bytes]:
    user = mesh_pb2.User(
        id=mesh.node_id,
        long_name=mesh.long_name,
        short_name=mesh.short_name,
        hw_model=mesh_pb2.HardwareModel.PRIVATE_HW,
        role=config_pb2.Config.DeviceConfig.Role.CLIENT_MUTE,
    )
    data = mesh_pb2.Data(
        portnum=portnums_pb2.NODEINFO_APP,
        payload=user.SerializeToString(),
        want_response=want_response,
    )
    return _envelope(mesh, data, to, packet_id)
