import pytest

from meshtastic_mqtt_client.config import parse_config
from meshtastic_mqtt_client.storage import Storage


@pytest.fixture
def config():
    return parse_config({
        "mqtt": {"host": "broker.local", "root_topic": "msh/EU_868"},
        "meshtastic": {
            "node_id": "!c0ffee01",
            "long_name": "Desk Client",
            "short_name": "DESK",
            "channel": "LongFast",
            "key": "AQ==",
        },
    })


@pytest.fixture
def other_mesh(config):
    """The same channel, seen from another node."""
    return parse_config({
        "mqtt": {"host": "broker.local", "root_topic": "msh/EU_868"},
        "meshtastic": {
            "node_id": "!12345678",
            "long_name": "Hilltop Repeater",
            "short_name": "HILL",
            "channel": config.mesh.channel,
            "key": config.mesh.key,
        },
    }).mesh


@pytest.fixture
def storage():
    s = Storage(":memory:")
    yield s
    s.close()
