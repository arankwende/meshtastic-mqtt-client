import base64
import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from meshtastic_mqtt_client import protocol
from meshtastic_mqtt_client.client import MeshClient
from meshtastic_mqtt_client.config import ConfigError, MeshConfig, node_id_from_num, parse_config
from meshtastic_mqtt_client.homeassistant import (
    HomeAssistantBridge,
    parse_text_sensors,
    split_message,
)

PRIVATE_KEY = base64.b64encode(bytes(range(1, 33))).decode()
SENSOR = 0x5E550001   # a node listed in homeassistant.nodes
PHONE = 0xA1B2C3D4    # the node allowed to talk to Assist
STRANGER = 0x0BADBEEF
ME = 0xC0FFEE01


def raw_config(**ha):
    return {
        "mqtt": {"host": "192.168.1.10", "root_topic": "msh/EU_868"},
        "meshtastic": {"node_id": "!c0ffee01", "long_name": "HA Bridge", "short_name": "HA",
                       "channel": "Home", "key": PRIVATE_KEY},
        "homeassistant": {
            "nodes": ["!5e550001"],
            "sensors": {"tank": {"unit_of_measurement": "%", "icon": "mdi:water"}},
            "assist": {"enabled": True, "allowed_nodes": ["!a1b2c3d4"]},
            "alerts": {"nodes": ["!a1b2c3d4"], "channel": True},
            "controls": {"!5e550001": [{"key": "pump", "type": "switch"},
                                       {"key": "Reset Counter", "type": "button", "name": "Reset"}]},
            "delivery": {"interval": 0, "ack_timeout": 1, "retries": 1},
            **ha,
        },
    }


PHONE_MESH = MeshConfig(node_id="!a1b2c3d4", long_name="Phone", short_name="PH", channel="Home",
                        key=PRIVATE_KEY)


@pytest.fixture
def bridge(storage):
    config = parse_config(raw_config())
    client = MeshClient(config, storage)
    client._mqtt = MagicMock()
    client._mqtt.is_connected.return_value = True
    b = HomeAssistantBridge(config, client, mqtt_client=MagicMock())
    client.add_listener(b.handle_event)
    b._mqtt.publish.reset_mock()  # forget the bridge's own entities announced at startup
    return b


def auto_ack(bridge, ack=True):
    """Every node the bridge messages acknowledges it, like a real radio would."""
    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    def publish(topic, payload, qos=0):
        pkt = protocol.decode_envelope(payload, bridge.mesh)
        if ack and pkt and pkt.want_ack:
            sender = replace(PHONE_MESH, node_id=node_id_from_num(pkt.to_num))
            data = mesh_pb2.Data(portnum=portnums_pb2.ROUTING_APP, request_id=pkt.packet_id,
                                 payload=mesh_pb2.Routing(error_reason=0).SerializeToString())
            bridge.client.handle_payload(protocol._envelope(sender, data, bridge.mesh.node_num, None)[1])
    bridge.client._mqtt.publish.side_effect = publish


def published(bridge) -> dict[str, str]:
    """Last payload per topic."""
    return {c.args[0]: c.args[1] for c in bridge._mqtt.publish.call_args_list}


def packet(from_num, to_num=protocol.BROADCAST, portnum=1, hop_start=3, hop_limit=2, packet_id=1):
    return protocol.Packet(packet_id=packet_id, from_num=from_num, to_num=to_num, portnum=portnum,
                           payload=b"", channel="Home", gateway_id="!deadbeef",
                           rx_time=1_760_000_000, hop_start=hop_start, hop_limit=hop_limit)


def text(from_num, body, to_num=protocol.BROADCAST, packet_id=1):
    return protocol.TextMessage(packet(from_num, to_num, packet_id=packet_id), body)


# Configuration rules


@pytest.mark.parametrize("change, error", [
    ({"meshtastic": {"key": "AQ=="}}, "private channel"),
    ({"meshtastic": {"key": ""}}, "private channel"),
    ({"meshtastic": {"key": base64.b64encode(b"short").decode()}}, "private channel"),
    ({"homeassistant": {"mqtt": {"host": "mqtt.meshtastic.org"}}}, "public broker"),
    ({"homeassistant": {"assist": {"enabled": True, "allowed_nodes": []}}}, "at least one"),
    ({"homeassistant": {"assist": {"enabled": True, "allowed_nodes": ["!c0ffee01"]}}}, "own node_id"),
    ({"homeassistant": {"nodes": ["kitchen"]}}, "homeassistant.nodes"),
    ({"homeassistant": {"sensors": {"tank": {"colour": "red"}}}}, "unknown settings"),
    ({"homeassistant": {"alerts": {"nodes": ["!c0ffee01"]}}}, "own node_id"),
    ({"homeassistant": {"controls": {"!0badbeef": [{"key": "pump"}]}}}, "also be listed"),
    ({"homeassistant": {"controls": {"!5e550001": [{"key": "pump", "type": "dimmer"}]}}}, "switch or button"),
    ({"homeassistant": {"controls": {"!5e550001": [{"type": "button"}]}}}, "needs a key"),
])
def test_config_rules(change, error):
    raw = raw_config()
    for section, values in change.items():
        raw[section] = raw[section] | values
    with pytest.raises(ConfigError, match=error):
        parse_config(raw)


def test_sensors_only_bridge_allows_default_channel():
    raw = raw_config(assist={"enabled": False}, alerts={}, controls={})
    raw["meshtastic"]["key"] = "AQ=="
    cfg = parse_config(raw)
    assert cfg.homeassistant.assist is None
    assert cfg.homeassistant.mqtt.host == "192.168.1.10"  # defaults to the mesh broker


def test_last_will_marks_bridge_offline(bridge):
    bridge._mqtt.will_set.assert_called_once_with("meshtastic-bridge/status", "offline", qos=1, retain=True)


def test_stale_offline_from_previous_instance_is_corrected(bridge):
    stale = MagicMock(topic="meshtastic-bridge/status", payload=b"offline")
    bridge._on_message(None, None, stale)
    assert published(bridge)["meshtastic-bridge/status"] == "online"
    bridge.stop()
    bridge._mqtt.publish.reset_mock()
    bridge._on_message(None, None, stale)
    assert bridge._mqtt.publish.call_count == 0


# Sensors


def test_telemetry_creates_device_and_sensors(bridge):
    bridge.handle_event(protocol.TelemetryReport(packet(SENSOR), battery_level=101, voltage=4.9,
                                                 temperature=21.5))
    pub = published(bridge)
    battery = json.loads(pub["homeassistant/sensor/meshtastic_5e550001/battery_level/config"])
    assert battery["device_class"] == "battery"
    assert battery["unique_id"] == "meshtastic_5e550001_battery_level"
    assert battery["state_topic"] == "meshtastic-bridge/5e550001/state"
    assert battery["availability_topic"] == "meshtastic-bridge/status"
    assert battery["device"]["identifiers"] == ["meshtastic_5e550001"]
    assert "homeassistant/sensor/meshtastic_5e550001/temperature/config" in pub
    state = json.loads(pub["meshtastic-bridge/5e550001/state"])
    assert state["battery_level"] == 100  # 101 = external power
    assert state["temperature"] == 21.5
    assert state["hops_away"] == 1
    assert state["last_heard"].startswith("2025-10-09T")
    for call in bridge._mqtt.publish.call_args_list:
        assert call.kwargs["retain"] is True


def test_nodes_not_listed_are_ignored(bridge):
    bridge.handle_event(protocol.TelemetryReport(packet(STRANGER), battery_level=50))
    bridge.handle_event(text(STRANGER, "#ha tank=40"))
    assert bridge._mqtt.publish.call_count == 0


def test_position_becomes_device_tracker(bridge):
    bridge.handle_event(protocol.PositionReport(packet(SENSOR), -34.6037, -58.3816, 25, None))
    pub = published(bridge)
    tracker = json.loads(pub["homeassistant/device_tracker/meshtastic_5e550001/position/config"])
    assert tracker["json_attributes_topic"] == "meshtastic-bridge/5e550001/position"
    assert tracker["source_type"] == "gps"
    assert json.loads(pub["meshtastic-bridge/5e550001/position"]) == \
        {"latitude": -34.6037, "longitude": -58.3816, "altitude": 25}


def test_json_sensor_data_on_port_256(bridge):
    bridge.handle_event(protocol.SensorData(packet(SENSOR, portnum=256),
                                            {"Tank": 73, "door open": True, "mode": "eco"}))
    pub = published(bridge)
    tank = json.loads(pub["homeassistant/sensor/meshtastic_5e550001/custom_tank/config"])
    assert tank["value_template"] == "{{ value_json.custom['tank'] }}"
    assert tank["unit_of_measurement"] == "%" and tank["icon"] == "mdi:water"
    assert tank["state_class"] == "measurement"
    door = json.loads(pub["homeassistant/binary_sensor/meshtastic_5e550001/custom_door_open/config"])
    assert door["value_template"] == "{{ 'ON' if value_json.custom['door_open'] else 'OFF' }}"
    mode = json.loads(pub["homeassistant/sensor/meshtastic_5e550001/custom_mode/config"])
    assert "state_class" not in mode
    assert json.loads(pub["meshtastic-bridge/5e550001/state"])["custom"] == \
        {"tank": 73, "door_open": True, "mode": "eco"}


def test_text_sensor_data(bridge):
    bridge.handle_event(text(SENSOR, "#ha tank=55 pump=on note=ok"))
    state = json.loads(published(bridge)["meshtastic-bridge/5e550001/state"])
    assert state["custom"] == {"tank": 55, "pump": True, "note": "ok"}


def test_parse_text_sensors():
    assert parse_text_sensors("hello") is None
    assert parse_text_sensors("#HA t=21.5 n=3 flag=false junk =x") == {"t": 21.5, "n": 3, "flag": False}


def test_nodeinfo_renames_existing_entities(bridge, storage):
    bridge.handle_event(protocol.TelemetryReport(packet(SENSOR), battery_level=80))
    info = protocol.NodeInfo(packet(SENSOR, portnum=4), "!5e550001", "Water Tank", "TANK", "RAK4631", "SENSOR")
    storage.save_nodeinfo(info)
    bridge.handle_event(info)
    battery = json.loads(published(bridge)["homeassistant/sensor/meshtastic_5e550001/battery_level/config"])
    assert battery["device"]["name"] == "Water Tank"
    assert battery["device"]["model"] == "RAK4631"


def test_home_assistant_restart_republishes(bridge):
    bridge.handle_event(protocol.TelemetryReport(packet(SENSOR), battery_level=80))
    before = bridge._mqtt.publish.call_count
    msg = MagicMock(topic="homeassistant/status", payload=b"online")
    bridge._on_message(None, None, msg)
    topics = [c.args[0] for c in bridge._mqtt.publish.call_args_list[before:]]
    assert "homeassistant/sensor/meshtastic_5e550001/battery_level/config" in topics
    assert "meshtastic-bridge/5e550001/state" in topics


def test_restart_keeps_values_from_retained_state(bridge):
    retained = {"battery_level": 80, "temperature": 19.0, "custom": {"tank": 50, "valve": "open"}}
    bridge._on_message(None, None, MagicMock(topic="meshtastic-bridge/5e550001/state",
                                             payload=json.dumps(retained).encode()))
    bridge.handle_event(protocol.TelemetryReport(packet(SENSOR), battery_level=75))
    bridge.handle_event(protocol.SensorData(packet(SENSOR, portnum=256), {"tank": 60}))
    state = json.loads(published(bridge)["meshtastic-bridge/5e550001/state"])
    assert state["battery_level"] == 75 and state["temperature"] == 19.0
    assert state["custom"] == {"tank": 60, "valve": "open"}
    # A late echo of the old retained state must not overwrite newer values.
    bridge.restore_state("meshtastic-bridge/5e550001/state", json.dumps(retained).encode())
    assert bridge._state[SENSOR]["battery_level"] == 75
    bridge.restore_state("meshtastic-bridge/0badbeef/state", b'{"x": 1}')
    assert STRANGER not in bridge._state


# Assist


def requests(bridge):
    return [json.loads(c.args[1]) for c in bridge._mqtt.publish.call_args_list
            if c.args[0] == "meshtastic-bridge/assist/request"]


def test_dm_from_allowed_node_goes_to_assist(bridge):
    bridge.handle_event(text(PHONE, "Turn off the garage lights", to_num=ME, packet_id=42))
    assert requests(bridge) == [{"from": "!a1b2c3d4", "name": "!a1b2c3d4",
                                 "text": "Turn off the garage lights", "packet_id": 42}]
    assert bridge._mqtt.publish.call_args.kwargs["retain"] is False


@pytest.mark.parametrize("message", [
    text(PHONE, "broadcasts never reach Assist"),
    text(STRANGER, "unknown sender", to_num=ME),
    text(PHONE, "DM for someone else", to_num=STRANGER),
    text(PHONE, "#ha tank=1", to_num=ME),
    text(PHONE, "   ", to_num=ME),
])
def test_requests_that_must_not_reach_assist(bridge, message):
    bridge.handle_event(message)
    assert requests(bridge) == []


def test_replayed_request_is_dropped_even_after_restart(bridge, storage):
    _, raw = protocol.encode_text(PHONE_MESH, "open the garage", to=ME, packet_id=777)
    bridge.client.handle_payload(raw)
    assert len(requests(bridge)) == 1
    # A restarted client has an empty in-memory deduplicator but the same database.
    restarted = MeshClient(replace(bridge.client.config), storage)
    restarted.add_listener(bridge.handle_event)
    restarted.handle_payload(raw)
    assert len(requests(bridge)) == 1


def test_rate_limit(bridge):
    for i in range(10):
        bridge.handle_event(text(PHONE, f"request {i}", to_num=ME, packet_id=i))
    assert len(requests(bridge)) == 6


def _drain(bridge, ack=True):
    """Run the outbox to completion; return the messages that went to the mesh (not acks)."""
    auto_ack(bridge, ack)
    bridge._outbox.put(None)
    bridge._send_loop()
    sent = [protocol.decode_envelope(c.args[1], bridge.mesh) for c in bridge.client._mqtt.publish.call_args_list]
    return [(p, protocol.parse_packet(p)) for p in sent if p.from_num == bridge.mesh.node_num]


def test_reply_is_sent_as_dm(bridge):
    bridge.handle_reply(json.dumps({"to": "!A1B2C3D4", "text": "Garage lights are off."}).encode())
    [(pkt, msg)] = _drain(bridge)
    assert msg.text == "Garage lights are off."
    assert msg.packet.to_num == PHONE and pkt.want_ack
    delivery = json.loads(published(bridge)["meshtastic-bridge/bridge/delivery"])
    assert delivery["status"] == "delivered" and delivery["kind"] == "assist" and delivery["attempts"] == 1


@pytest.mark.parametrize("payload", [
    {"to": "!0badbeef", "text": "not allowed"},
    {"to": "everyone", "text": "bad id"},
    {"text": "no recipient"},
    {"to": "!a1b2c3d4", "text": ""},
])
def test_replies_that_must_not_be_sent(bridge, payload):
    bridge.handle_reply(json.dumps(payload).encode())
    bridge.handle_reply(b"not json")
    assert _drain(bridge) == []


def test_long_reply_is_split_and_capped(bridge):
    long_text = " ".join(f"word{i}" for i in range(400))
    bridge.handle_reply(json.dumps({"to": "!a1b2c3d4", "text": long_text}).encode())
    texts = [msg.text for _, msg in _drain(bridge)]
    assert len(texts) == 3
    assert [t.split(" ")[0] for t in texts] == ["1/3", "2/3", "3/3"]
    assert texts[-1].endswith("…")


def test_split_message_respects_byte_limit():
    assert split_message("short", 3) == ["short"]
    parts = split_message("ñ" * 500, 5)
    assert all(len(p.encode()) <= 200 for p in parts)
    assert len(parts) == 5
    parts = split_message("a " * 150, 3)
    assert len(parts) == 2 and all(len(p.encode()) <= 200 for p in parts)


def test_alerts_alone_require_private_channel():
    raw = raw_config(assist={"enabled": False}, controls={})
    raw["meshtastic"]["key"] = "AQ=="
    with pytest.raises(ConfigError, match="private channel"):
        parse_config(raw)


# Bridge device, health and events


def discovery(bridge, component, key):
    topic = f"homeassistant/{component}/meshtastic_bridge_c0ffee01/{key}/config"
    return json.loads(bridge._discovery[topic])


def test_bridge_device_entities(bridge):
    assert discovery(bridge, "binary_sensor", "mesh_connected")["device_class"] == "connectivity"
    assert discovery(bridge, "sensor", "packets_last_hour")["state_topic"] == "meshtastic-bridge/bridge/state"
    event = discovery(bridge, "event", "message")
    assert event["event_types"] == ["channel_message", "direct_message"]
    notify = discovery(bridge, "notify", "alert_a1b2c3d4")
    assert notify["command_topic"] == "meshtastic-bridge/notify/a1b2c3d4"
    assert discovery(bridge, "notify", "alert_channel")["command_topic"] == "meshtastic-bridge/notify/channel"
    assert discovery(bridge, "button", "announce")["command_topic"] == "meshtastic-bridge/bridge/announce"
    assert discovery(bridge, "sensor", "last_delivery")["json_attributes_topic"] == "meshtastic-bridge/bridge/delivery"
    assert discovery(bridge, "sensor", "packets_last_hour")["device"]["identifiers"] == ["meshtastic_bridge_c0ffee01"]


def test_health(bridge, storage):
    bridge.client.handle_payload(protocol.encode_text(PHONE_MESH, "hello all")[1])
    bridge.publish_health()
    health = json.loads(published(bridge)["meshtastic-bridge/bridge/state"])
    assert health["mesh_connected"] is True
    assert health["packets_last_hour"] == 1
    assert health["gateways_last_hour"] == 1
    assert health["nodes_last_24h"] == 1
    assert health["last_packet"].endswith("+00:00")


def test_message_events(bridge):
    bridge.handle_event(text(STRANGER, "SOS at the river"))
    bridge.handle_event(text(PHONE, "hi bridge", to_num=ME))
    bridge.handle_event(text(STRANGER, "psst", to_num=ME))
    bridge.handle_event(text(PHONE, "for bob", to_num=0x0B0B0B0B))
    bridge.handle_event(text(SENSOR, "#ha tank=1"))
    events = [json.loads(c.args[1]) for c in bridge._mqtt.publish.call_args_list
              if c.args[0] == "meshtastic-bridge/bridge/message"]
    assert [(e["event_type"], e["text"], e.get("allowed")) for e in events] == [
        ("channel_message", "SOS at the river", None),
        ("direct_message", "hi bridge", True),
        ("direct_message", "psst", False),
    ]
    assert events[0]["from"] == "!0badbeef"


def test_events_can_be_disabled(storage):
    config = parse_config(raw_config(events=False))
    b = HomeAssistantBridge(config, MeshClient(config, storage), mqtt_client=MagicMock())
    assert not any("/event/" in t for t in b._discovery)
    b.handle_event(text(STRANGER, "hello"))
    assert not any(c.args[0].endswith("/message") for c in b._mqtt.publish.call_args_list)


def test_announce_button(bridge):
    bridge._route("meshtastic-bridge/bridge/announce", b"PRESS")
    pkt = protocol.decode_envelope(bridge.client._mqtt.publish.call_args.args[1], bridge.mesh)
    assert pkt.portnum == 4


# Alerts


def test_alert_to_node_is_delivered(bridge):
    bridge._route("meshtastic-bridge/notify/a1b2c3d4", "Tank below 20%".encode())
    [(pkt, msg)] = _drain(bridge)
    assert msg.text == "Tank below 20%" and pkt.to_num == PHONE and pkt.want_ack
    delivery = json.loads(published(bridge)["meshtastic-bridge/bridge/delivery"])
    assert (delivery["status"], delivery["kind"], delivery["to"]) == ("delivered", "alert", "!a1b2c3d4")


def test_alert_failure_is_reported(bridge):
    bridge._route("meshtastic-bridge/notify/a1b2c3d4", b"anyone?")
    sent = _drain(bridge, ack=False)
    assert len(sent) == 2  # retries=1
    delivery = json.loads(published(bridge)["meshtastic-bridge/bridge/delivery"])
    assert (delivery["status"], delivery["error"], delivery["attempts"]) == ("failed", "TIMEOUT", 2)


def test_channel_alert_is_broadcast(bridge):
    bridge._route("meshtastic-bridge/notify/channel", b"Storm warning")
    [(pkt, msg)] = _drain(bridge)
    assert pkt.is_broadcast and not pkt.want_ack and msg.text == "Storm warning"
    assert json.loads(published(bridge)["meshtastic-bridge/bridge/delivery"])["status"] == "sent"


def test_alerts_to_other_nodes_are_refused(bridge):
    bridge._route("meshtastic-bridge/notify/0badbeef", b"hi")
    assert _drain(bridge) == []


# Controls


def test_switch_and_button_entities(bridge):
    switch = json.loads(bridge._discovery["homeassistant/switch/meshtastic_5e550001/control_pump/config"])
    assert switch["command_topic"] == "meshtastic-bridge/5e550001/set/pump"
    assert switch["state_topic"] == "meshtastic-bridge/5e550001/state"
    button = json.loads(bridge._discovery["homeassistant/button/meshtastic_5e550001/control_reset_counter/config"])
    assert button["command_topic"] == "meshtastic-bridge/5e550001/press/reset_counter"
    assert button["name"] == "Reset"


def test_switch_command_sends_json_to_node(bridge):
    bridge._route("meshtastic-bridge/5e550001/set/pump", b"ON")
    bridge._route("meshtastic-bridge/5e550001/press/reset_counter", b"PRESS")
    sent = _drain(bridge)
    assert [(p.to_num, p.portnum, e.values) for p, e in sent] == [
        (SENSOR, 256, {"pump": True}),
        (SENSOR, 256, {"reset_counter": "press"}),
    ]
    assert all(p.want_ack for p, _ in sent)
    delivery = json.loads(published(bridge)["meshtastic-bridge/bridge/delivery"])
    assert delivery["kind"] == "control" and delivery["status"] == "delivered"


@pytest.mark.parametrize("topic, payload", [
    ("meshtastic-bridge/5e550001/set/valve", b"ON"),           # not a configured control
    ("meshtastic-bridge/0badbeef/set/pump", b"ON"),            # wrong node
    ("meshtastic-bridge/5e550001/press/pump", b"PRESS"),       # pump is a switch, not a button
    ("meshtastic-bridge/5e550001/set/pump", b"TOGGLE"),        # invalid payload
])
def test_invalid_control_commands_are_refused(bridge, topic, payload):
    bridge._route(topic, payload)
    assert _drain(bridge) == []


def test_controlled_value_has_no_duplicate_sensor(bridge):
    bridge.handle_event(protocol.SensorData(packet(SENSOR, portnum=256), {"pump": True, "tank": 5}))
    topics = published(bridge)
    assert "homeassistant/binary_sensor/meshtastic_5e550001/custom_pump/config" not in topics
    assert "homeassistant/sensor/meshtastic_5e550001/custom_tank/config" in topics
    assert json.loads(topics["meshtastic-bridge/5e550001/state"])["custom"]["pump"] is True
