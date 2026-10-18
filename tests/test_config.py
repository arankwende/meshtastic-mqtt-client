from pathlib import Path

import pytest

from meshtastic_mqtt_client.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).parent.parent / "config" / "config.example.yaml"


def test_example_config_is_valid():
    cfg = load_config(EXAMPLE)
    assert cfg.mesh.node_num == int(cfg.mesh.node_id[1:], 16)
    assert cfg.database.parent == EXAMPLE.parent


def test_legacy_layout_is_translated():
    cfg = parse_config({
        "MQTT": {"MQTT_ip": "10.0.0.2", "MQTT_USER": "u", "MQTT_PW": "p"},
        "MESHTASTIC": {"CHANNEL_ID": "ShortFast", "CLIENT_ID": "305419896",
                       "LONG_NAME": "Old", "SHORT_NAME": "OLD"},
    })
    assert cfg.mqtt.host == "10.0.0.2"
    assert cfg.mqtt.username == "u"
    assert cfg.mesh.node_id == "!12345678"
    assert cfg.mesh.channel == "ShortFast"


def test_tls_defaults_to_port_8883(config):
    cfg = parse_config({"mqtt": {"host": "h", "tls": True},
                        "meshtastic": {"node_id": "!00000001", "long_name": "a", "short_name": "a"}})
    assert cfg.mqtt.port == 8883


@pytest.mark.parametrize("mesh, message", [
    ({"node_id": "12345678"}, "node_id"),
    ({"node_id": "!12345678", "long_name": "x", "short_name": "TOOLONG"}, "short_name"),
    ({"node_id": "!12345678", "long_name": "x", "short_name": "x", "key": "A" * 48}, "key"),
])
def test_validation_errors(mesh, message):
    with pytest.raises(ConfigError, match=message):
        parse_config({"mqtt": {"host": "h"}, "meshtastic": {"long_name": "x", "short_name": "x"} | mesh})


def test_missing_file_has_helpful_error(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_config(tmp_path / "config.yaml")


def test_aead_option():
    base = {"node_id": "!12345678", "long_name": "x", "short_name": "x"}
    assert parse_config({"mqtt": {"host": "h"}, "meshtastic": base | {"aead": True}}).mesh.aead
    with pytest.raises(ConfigError, match="aead"):
        parse_config({"mqtt": {"host": "h"}, "meshtastic": base | {"aead": True, "key": ""}})


def test_secrets_and_env(tmp_path, monkeypatch):
    (tmp_path / "secrets.yaml").write_text("mqtt_password: s3cret\nchannel_key: AQ==\n")
    (tmp_path / "config.yaml").write_text(
        "mqtt: {host: h, username: u, password: !secret mqtt_password}\n"
        "meshtastic: {node_id: '!12345678', long_name: x, short_name: x, key: !secret channel_key}\n"
        "homeassistant: {mqtt: {host: ha, password: !env HA_PW}}\n"
    )
    monkeypatch.setenv("HA_PW", "from-env")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.mqtt.password == "s3cret"
    assert cfg.homeassistant.mqtt.password == "from-env"
    monkeypatch.delenv("HA_PW")
    with pytest.raises(ConfigError, match="HA_PW"):
        load_config(tmp_path / "config.yaml")


def test_missing_secret(tmp_path):
    (tmp_path / "config.yaml").write_text("mqtt: {host: h, password: !secret nope}\n")
    with pytest.raises(ConfigError, match="secrets.yaml"):
        load_config(tmp_path / "config.yaml")
    (tmp_path / "secrets.yaml").write_text("other: 1\n")
    with pytest.raises(ConfigError, match="'nope' not found"):
        load_config(tmp_path / "config.yaml")


def test_unwritable_log_file_falls_back_to_console(tmp_path, caplog):
    import logging

    from meshtastic_mqtt_client.cli import setup_logging

    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        setup_logging(blocker / "sub" / "x.log", debug=False, console=False)
        assert "logging to the terminal only" in caplog.text
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
