from meshtastic_mqtt_client import protocol
from meshtastic_mqtt_client.display import ago, battery_text, chat_line, describe_event, node_rows


def test_ago():
    assert ago(None) == "never"
    assert ago(1000, now=1030) == "just now"
    assert ago(1000, now=1000 + 125) == "2m ago"
    assert ago(1000, now=1000 + 3 * 86400) == "3d ago"


def test_battery_text():
    assert battery_text(None) == ""
    assert battery_text(42) == "42%"
    assert battery_text(101) == "powered"


def _packet(from_num, to_num=protocol.BROADCAST):
    return protocol.Packet(packet_id=1, from_num=from_num, to_num=to_num, portnum=1, payload=b"",
                           channel="LongFast", gateway_id="!deadbeef", rx_time=1_700_000_000)


def test_chat_lines_use_names(storage):
    storage.save_nodeinfo(protocol.NodeInfo(_packet(5), "!00000005", "Base Camp", "BASE", "TBEAM", "CLIENT"))
    own = 0xC0FFEE01
    assert chat_line(storage, own, 5, protocol.BROADCAST, "hi", 1_700_000_000).endswith("] Base Camp: hi")
    assert chat_line(storage, own, 5, own, "psst", 1_700_000_000).endswith("Base Camp → you (private): psst")
    assert chat_line(storage, own, own, 7, "yo", 1_700_000_000).endswith("You → !00000007 (private): yo")


def test_node_rows_and_events(storage):
    storage.save_telemetry(protocol.TelemetryReport(_packet(9), battery_level=101, voltage=4.2))
    assert node_rows(storage.nodes(), now=1_700_000_060)[0][2:] == \
        ["!00000009", "", "1m ago", "", "powered", ""]
    summary = describe_event(storage, protocol.TelemetryReport(_packet(9), battery_level=50))
    assert summary == "Telemetry: !00000009: battery 50%"
    assert describe_event(storage, protocol.TelemetryReport(_packet(9))) is None
