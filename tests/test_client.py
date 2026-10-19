import csv
from unittest.mock import MagicMock

import pytest
from meshtastic.protobuf import mqtt_pb2

from meshtastic_mqtt_client import protocol
from meshtastic_mqtt_client.client import Deduplicator, MeshClient, NotConnected


@pytest.fixture
def client(config, storage):
    events = []
    c = MeshClient(config, storage, on_event=events.append)
    c._mqtt = MagicMock()
    c._mqtt.is_connected.return_value = True
    c.events = events
    return c


def test_deduplicator_is_bounded():
    d = Deduplicator(size=2)
    assert d.is_new(1, 1) and d.is_new(1, 2) and not d.is_new(1, 1)
    d.is_new(1, 3)
    assert d.is_new(1, 2)


def test_incoming_text_is_stored_once(client, storage, other_mesh):
    _, raw = protocol.encode_text(other_mesh, "it's working")  # apostrophe broke the old SQL
    assert client.handle_payload(raw) is not None
    assert client.handle_payload(raw) is None  # same packet via a second gateway
    assert [m.text for m in storage.recent_messages()] == ["it's working"]
    assert len(client.events) == 1


def test_own_echo_is_ignored(client, config):
    _, raw = protocol.encode_text(config.mesh, "echo")
    assert client.handle_payload(raw) is None


def test_direct_messages_for_others_are_ignored(client, other_mesh, storage):
    _, raw = protocol.encode_text(other_mesh, "not for you", to=0x0BADCAFE)
    assert client.handle_payload(raw) is None
    _, raw = protocol.encode_text(other_mesh, "for you", to=0xC0FFEE01)
    assert client.handle_payload(raw).text == "for you"
    assert [m.text for m in storage.recent_messages()] == ["for you"]


def test_nodeinfo_updates_node_and_answers_requests(client, storage, other_mesh):
    _, raw = protocol.encode_nodeinfo(other_mesh, want_response=True)
    client.handle_payload(raw)
    node = storage.node(0x12345678)
    assert node.long_name == "Hilltop Repeater"
    assert client._mqtt.publish.call_count == 1
    reply = protocol.decode_envelope(client._mqtt.publish.call_args.args[1], other_mesh)
    assert reply.to_num == 0x12345678 and not reply.want_response
    _, raw = protocol.encode_nodeinfo(other_mesh, want_response=True)
    client.handle_payload(raw)
    assert client._mqtt.publish.call_count == 1  # throttled


def test_own_node_is_listed(storage, client):
    assert storage.node(0xC0FFEE01).long_name == "Desk Client"


def test_send_text_publishes_and_stores(client, config, storage):
    msg = client.send_text("  hello  ")
    topic, payload = client._mqtt.publish.call_args.args
    assert topic == "msh/EU_868/2/e/LongFast/!c0ffee01"
    assert protocol.parse_packet(protocol.decode_envelope(payload, config.mesh)).text == "hello"
    stored = storage.recent_messages()[-1]
    assert stored.outgoing and stored.text == "hello" and msg.packet.packet_id == stored.packet_id


def test_send_requires_connection(client):
    client._mqtt.is_connected.return_value = False
    with pytest.raises(NotConnected):
        client.send_text("hi")


def test_exports(client, storage, other_mesh, tmp_path):
    _, raw = protocol.encode_text(other_mesh, "csv, with \"quotes\"")
    client.handle_payload(raw)
    assert storage.export_messages_csv(tmp_path / "m.csv") == 1
    rows = list(csv.DictReader(open(tmp_path / "m.csv")))
    assert rows[0]["text"] == "csv, with \"quotes\""
    assert rows[0]["from_id"] == "!12345678"
    assert storage.export_positions_csv(tmp_path / "p.csv") == 0


# Acknowledgements and delivery


def _ack(other_mesh, to, request_id, error=0):
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    routing = mesh_pb2.Routing(error_reason=error)
    data = mesh_pb2.Data(portnum=portnums_pb2.ROUTING_APP, payload=routing.SerializeToString(),
                         request_id=request_id)
    return protocol._envelope(other_mesh, data, to, None)[1]


def test_direct_messages_wanting_ack_are_acknowledged_even_when_repeated(client, other_mesh, config):
    _, raw = protocol.encode_text(other_mesh, "are you there?", to=config.mesh.node_num, want_ack=True)
    client.handle_payload(raw)
    client.handle_payload(raw)  # the sender retried because our ack was lost
    acks = [protocol.parse_packet(protocol.decode_envelope(c.args[1], other_mesh))
            for c in client._mqtt.publish.call_args_list]
    assert len(acks) == 2
    sent_id = protocol.decode_envelope(raw, config.mesh).packet_id
    assert all(a.delivered and a.request_id == sent_id and a.packet.to_num == 0x12345678 for a in acks)
    assert len(client.storage.recent_messages()) == 1


def test_broadcasts_and_others_dms_are_not_acknowledged(client, other_mesh):
    client.handle_payload(protocol.encode_text(other_mesh, "hi all", want_ack=True)[1])
    client.handle_payload(protocol.encode_text(other_mesh, "hi bob", to=0x0B0B0B0B, want_ack=True)[1])
    assert client._mqtt.publish.call_count == 0


def _answer_with(client, other_mesh, error=0):
    """Make the fake broker answer every want_ack publish with an ack (or NAK)."""
    def publish(topic, payload, qos=0):
        pkt = protocol.decode_envelope(payload, other_mesh)
        if pkt and pkt.want_ack:
            client.handle_payload(_ack(other_mesh, pkt.from_num, pkt.packet_id, error))
    client._mqtt.publish.side_effect = publish


def test_reliable_send_delivered(client, other_mesh, storage):
    _answer_with(client, other_mesh)
    msg, delivery = client.send_text_reliably("ping", to=0x12345678, timeout=1)
    assert delivery.status == "delivered" and delivery.attempts == 1 and delivery.ok
    assert storage.recent_messages()[-1].text == "ping"


def test_reliable_send_retries_with_same_id_then_fails(client):
    msg, delivery = client.send_text_reliably("ping", to=0x12345678, retries=2, timeout=0.05)
    assert (delivery.status, delivery.attempts, delivery.error) == ("failed", 3, "TIMEOUT")
    ids = {protocol.decode_envelope(c.args[1], client.config.mesh).packet_id
           for c in client._mqtt.publish.call_args_list}
    assert len(ids) == 1 and client._mqtt.publish.call_count == 3
    assert len(client.storage.recent_messages()) == 1  # stored once


def test_reliable_send_reports_nak(client, other_mesh):
    _answer_with(client, other_mesh, error=8)  # NO_RESPONSE
    _, delivery = client.send_text_reliably("ping", to=0x12345678, timeout=1)
    assert (delivery.status, delivery.error) == ("failed", "NO_RESPONSE")


def test_broadcast_is_sent_without_waiting(client):
    _, delivery = client.send_text_reliably("all", to=protocol.BROADCAST, timeout=10)
    assert delivery.status == "sent" and delivery.ok


def test_send_data_when_disconnected(client):
    client._mqtt.is_connected.return_value = False
    delivery = client.send_data_reliably(256, b"{}", to=0x12345678)
    assert (delivery.status, delivery.error) == ("failed", "NOT_CONNECTED")


def test_stats(client, other_mesh):
    from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2

    tel = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(channel_utilization=12.5))
    data = mesh_pb2.Data(portnum=portnums_pb2.TELEMETRY_APP, payload=tel.SerializeToString())
    env = mqtt_pb2.ServiceEnvelope.FromString(protocol._envelope(other_mesh, data, protocol.BROADCAST, None)[1])
    env.gateway_id = "!12345678"  # the reporting node is itself the gateway
    client.handle_payload(env.SerializeToString())
    client.handle_payload(protocol.encode_text(other_mesh, "hello")[1])
    snap = client.stats.snapshot()
    assert snap["packets_last_hour"] == 2
    assert snap["gateways_last_hour"] == 1
    assert snap["gateway_channel_utilization"] == 12.5
    assert snap["last_packet"] is not None
    assert client.stats.snapshot(now=snap["last_packet"] + 7200)["packets_last_hour"] == 0


def test_search_messages_treats_wildcards_literally(client, other_mesh, storage):
    for text in ["tank at 50%", "tank_level ok", "nothing here", "C:\\path"]:
        client.handle_payload(protocol.encode_text(other_mesh, text)[1])
    assert [m.text for m in storage.search_messages("TANK")] == ["tank_level ok", "tank at 50%"]
    assert [m.text for m in storage.search_messages("50%")] == ["tank at 50%"]
    assert [m.text for m in storage.search_messages("_level")] == ["tank_level ok"]
    assert [m.text for m in storage.search_messages("%")] == ["tank at 50%"]
    assert [m.text for m in storage.search_messages("\\")] == ["C:\\path"]


def test_nodeinfo_answered_right_after_boot(client, other_mesh, monkeypatch):
    # time.monotonic() counts from boot, so it is small on a freshly started machine.
    monkeypatch.setattr("meshtastic_mqtt_client.client.time.monotonic", lambda: 5.0)
    client.handle_payload(protocol.encode_nodeinfo(other_mesh, want_response=True)[1])
    assert client._mqtt.publish.call_count == 1
