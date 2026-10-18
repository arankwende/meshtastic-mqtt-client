"""Keeps the Python package, Home Assistant app and Docker files consistent."""

import re
from pathlib import Path

import pytest
import yaml

from meshtastic_mqtt_client import __version__
from meshtastic_mqtt_client.config import ConfigError, load_config

ROOT = Path(__file__).parent.parent
APP = ROOT / "ha-app" / "meshtastic_bridge"


def test_app_version_matches_package():
    app = yaml.safe_load((APP / "config.yaml").read_text())
    assert app["version"] == __version__
    dockerfile = (APP / "Dockerfile").read_text()
    assert f"ARG APP_VERSION={__version__}" in dockerfile


def test_app_config_has_required_keys():
    app = yaml.safe_load((APP / "config.yaml").read_text())
    assert {"name", "version", "slug", "description", "arch"} <= set(app)
    assert set(app["arch"]) == {"aarch64", "amd64"}
    assert set(app["options"]) == set(app["schema"])
    assert (ROOT / "repository.yaml").exists()


def test_app_base_image_is_pinned():
    dockerfile = (APP / "Dockerfile").read_text()
    assert re.search(r"ARG BUILD_FROM=ghcr\.io/home-assistant/base-python:[\d.]+-alpine[\d.]+-\d{4}\.\d+\.\d+",
                     dockerfile)


def test_app_template_needs_editing_before_it_runs(tmp_path, monkeypatch):
    for var, value in {"MQTT_HOST": "core-mosquitto", "MQTT_PORT": "1883",
                       "MQTT_USERNAME": "addons", "MQTT_PASSWORD": "pw"}.items():
        monkeypatch.setenv(var, value)
    config = tmp_path / "config.yaml"
    config.write_text((APP / "config.template.yaml").read_text())
    with pytest.raises(ConfigError, match="meshtastic.key"):
        load_config(config)
    config.write_text(config.read_text().replace("PASTE-YOUR-CHANNEL-KEY-HERE", "AQ=="))
    cfg = load_config(config)
    assert (cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.password) == ("core-mosquitto", 1883, "pw")
    assert cfg.homeassistant.mqtt.host == "core-mosquitto"
