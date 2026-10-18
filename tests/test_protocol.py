from dataclasses import replace

import pytest
from meshtastic.protobuf import mesh_pb2, mqtt_pb2, portnums_pb2, telemetry_pb2

from meshtastic_mqtt_client import protocol
from meshtastic_mqtt_client.crypto import channel_hash, crypt


def _wrap(mesh, data, packet_id=99, to=protocol.BROADCAST, encrypt=True):
    pkt = mesh_pb2.MeshPacket(to=to, id=packet_id, hop_limit=1, hop_start=3)
    setattr(pkt, "from", mesh.node_num)
    pkt.channel = channel_hash(mesh.channel, mesh.aes_key)
    if encrypt:
        pkt.encrypted = crypt(mesh.aes_key, packet_id, mesh.node_num, data.SerializeToString())
    else:
        pkt.decoded.CopyFrom(data)
    env = mqtt_pb2.ServiceEnvelope(channel_id=mesh.channel, gateway_id="!deadbeef")
    env.packet.CopyFrom(pkt)
    return env.SerializeToString()


def test_topics(config):
    assert protocol.subscribe_topic(config.mqtt, config.mesh) == "msh/EU_868/2/e/LongFast/#"
    assert protocol.publish_topic(config.mqtt, config.mesh) == "msh/EU_868/2/e/LongFast/!c0ffee01"


def test_text_round_trip(config, other_mesh):
    packet_id, raw = protocol.encode_text(other_mesh, "¿Hola, qué tal? 📡")
    packet = protocol.decode_envelope(raw, config.mesh)
    assert packet.packet_id == packet_id
    assert packet.from_num == 0x12345678
    assert packet.is_broadcast
    event = protocol.parse_packet(packet)
    assert isinstance(event, protocol.TextMessage)
    assert event.text == "¿Hola, qué tal? 📡"


def test_outgoing_packets_are_encrypted_with_channel_hash(config):
    _, raw = protocol.encode_text(config.mesh, "hi")
    pkt = mqtt_pb2.ServiceEnvelope.FromString(raw).packet
    assert pkt.WhichOneof("payload_variant") == "encrypted"
    assert pkt.channel == 8
    assert pkt.hop_start == pkt.hop_limit == 3


def test_unencrypted_channel(config):
    mesh = replace(config.mesh, key="")
    _, raw = protocol.encode_text(mesh, "plain")
    assert mqtt_pb2.ServiceEnvelope.FromString(raw).packet.WhichOneof("payload_variant") == "decoded"
    assert protocol.parse_packet(protocol.decode_envelope(raw, mesh)).text == "plain"


def test_wrong_key_is_ignored(config, other_mesh):
    _, raw = protocol.encode_text(replace(other_mesh, key="Ag=="), "secret")
    assert protocol.decode_envelope(raw, config.mesh) is None


def test_pki_direct_messages_are_skipped(config, other_mesh):
    raw = _wrap(other_mesh, mesh_pb2.Data(portnum=1, payload=b"x"), to=config.mesh.node_num)
    env = mqtt_pb2.ServiceEnvelope.FromString(raw)
    env.packet.channel = 0
    env.packet.pki_encrypted = True
    assert protocol.decode_envelope(env.SerializeToString(), config.mesh) is None


@pytest.mark.parametrize("raw", [b"", b"\xff\xff\xff", b"not a protobuf at all"])
def test_garbage_is_ignored(config, raw):
    assert protocol.decode_envelope(raw, config.mesh) is None


def test_nodeinfo(config, other_mesh):
    _, raw = protocol.encode_nodeinfo(other_mesh, want_response=True)
    packet = protocol.decode_envelope(raw, config.mesh)
    assert packet.want_response
    info = protocol.parse_packet(packet)
    assert isinstance(info, protocol.NodeInfo)
    assert (info.node_id, info.long_name, info.short_name) == ("!12345678", "Hilltop Repeater", "HILL")
    assert info.hw_model == "PRIVATE_HW"


def test_position(config, other_mesh):
    pos = mesh_pb2.Position(latitude_i=-345_000_000, longitude_i=-585_000_000, altitude=25, time=1_700_000_000)
    data = mesh_pb2.Data(portnum=portnums_pb2.POSITION_APP, payload=pos.SerializeToString())
    event = protocol.parse_packet(protocol.decode_envelope(_wrap(other_mesh, data), config.mesh))
    assert event.latitude == pytest.approx(-34.5)
    assert event.longitude == pytest.approx(-58.5)
    assert event.altitude == 25
    assert event.packet.hops_away == 2


def test_device_and_environment_telemetry(config, other_mesh):
    dev = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=87, voltage=4.01))
    env = telemetry_pb2.Telemetry(environment_metrics=telemetry_pb2.EnvironmentMetrics(temperature=21.5))
    for tel, check in [(dev, lambda e: e.battery_level == 87 and e.temperature is None),
                       (env, lambda e: e.temperature == 21.5 and e.battery_level is None)]:
        data = mesh_pb2.Data(portnum=portnums_pb2.TELEMETRY_APP, payload=tel.SerializeToString())
        event = protocol.parse_packet(protocol.decode_envelope(_wrap(other_mesh, data, encrypt=False), config.mesh))
        assert check(event)


def test_message_too_long(config):
    with pytest.raises(ValueError, match="too long"):
        protocol.encode_text(config.mesh, "x" * 300)


def test_aead_channel_round_trip(config, other_mesh):
    sender = replace(other_mesh, aead=True)
    _, raw = protocol.encode_text(sender, "ccm hello", to=config.mesh.node_num)
    pkt = mqtt_pb2.ServiceEnvelope.FromString(raw).packet
    assert pkt.channel == 8 ^ 0xAE
    # Receivers detect AES-CCM from the channel hash, whatever their own setting.
    for receiver in (config.mesh, replace(config.mesh, aead=True)):
        assert protocol.parse_packet(protocol.decode_envelope(raw, receiver)).text == "ccm hello"
    assert protocol.decode_envelope(raw, replace(config.mesh, key="Ag==")) is None


def test_aead_message_limit_leaves_room_for_tag(config):
    protocol.encode_text(config.mesh, "x" * 225)
    with pytest.raises(ValueError, match="too long"):
        protocol.encode_text(replace(config.mesh, aead=True), "x" * 225)


def test_fields_from_newer_firmware_are_tolerated(config, other_mesh):
    # 2.8 adds signature data our protobuf definitions don't know about yet.
    data = mesh_pb2.Data(portnum=portnums_pb2.TEXT_MESSAGE_APP, payload=b"signed")
    unknown_field = bytes([0xFA, 0x01, 64]) + bytes(64)  # field 31, 64 bytes
    raw_data = data.SerializeToString() + unknown_field
    pkt = mesh_pb2.MeshPacket(to=protocol.BROADCAST, id=7)
    setattr(pkt, "from", other_mesh.node_num)
    pkt.channel = channel_hash(other_mesh.channel, other_mesh.aes_key)
    pkt.encrypted = crypt(other_mesh.aes_key, 7, other_mesh.node_num, raw_data)
    env = mqtt_pb2.ServiceEnvelope(channel_id="LongFast", gateway_id="!deadbeef")
    env.packet.CopyFrom(pkt)
    raw = env.SerializeToString() + bytes([0xFA, 0x01, 2, 1, 2])
    assert protocol.parse_packet(protocol.decode_envelope(raw, config.mesh)).text == "signed"


def test_private_app_json_sensor_data(config, other_mesh):
    for payload, expected in [
        (b'{"tank": 73, "door": true, "nested": {"x": 1}}', {"tank": 73, "door": True}),
        (b'[1, 2]', None),
        (b'\xff\xfe', None),
    ]:
        data = mesh_pb2.Data(portnum=portnums_pb2.PRIVATE_APP, payload=payload)
        event = protocol.parse_packet(protocol.decode_envelope(_wrap(other_mesh, data), config.mesh))
        assert (event.values if event else None) == expected
