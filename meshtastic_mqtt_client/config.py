"""Loading and validating config.yaml."""

import base64
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .crypto import DEFAULT_KEY, expand_key

log = logging.getLogger(__name__)

NODE_ID_RE = re.compile(r"^![0-9a-f]{8}$")

# Brokers anyone can read and write; Home Assistant traffic must never go there.
PUBLIC_BROKERS = {"mqtt.meshtastic.org", "mqtt.meshtastic.com"}

SENSOR_OVERRIDE_KEYS = {"name", "unit_of_measurement", "device_class", "state_class", "icon"}


class ConfigError(Exception):
    pass


@dataclass
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    root_topic: str = "msh/US"


@dataclass
class MeshConfig:
    node_id: str
    long_name: str
    short_name: str
    channel: str = "LongFast"
    key: str = "AQ=="
    aead: bool = False

    @property
    def node_num(self) -> int:
        return int(self.node_id[1:], 16)

    @property
    def aes_key(self) -> bytes:
        return expand_key(self.key)


@dataclass
class AssistConfig:
    allowed_nodes: list[str]
    max_requests_per_minute: int = 6


@dataclass
class DeliveryConfig:
    retries: int = 2
    ack_timeout: float = 30.0
    interval: float = 5.0
    max_parts: int = 3


@dataclass
class ControlConfig:
    node: str
    key: str
    type: str  # "switch" or "button"
    name: str | None = None
    optimistic: bool = False


@dataclass
class HomeAssistantConfig:
    mqtt: MqttConfig
    discovery_prefix: str = "homeassistant"
    base_topic: str = "meshtastic-bridge"
    nodes: list[str] = field(default_factory=list)
    sensors: dict[str, dict] = field(default_factory=dict)
    expire_after: int | None = None
    assist: AssistConfig | None = None
    alert_nodes: list[str] = field(default_factory=list)
    alert_channel: bool = False
    controls: list[ControlConfig] = field(default_factory=list)
    events: bool = True
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)


@dataclass
class Config:
    mqtt: MqttConfig
    mesh: MeshConfig
    database: Path = field(default_factory=lambda: Path("meshtastic-mqtt-client.sqlite3"))
    path: Path | None = None
    homeassistant: HomeAssistantConfig | None = None


def node_id_from_num(num: int) -> str:
    return f"!{num:08x}"


def _from_legacy(raw: dict) -> dict:
    """Map the 1.x-era uppercase config layout onto the current one."""
    log.warning("config.yaml uses the old layout; see config/config.example.yaml for the new one")
    old_mqtt = raw.get("MQTT") or {}
    old_mesh = raw.get("MESHTASTIC") or {}
    node_id = old_mesh.get("CLIENT_FULL_ID")
    if not node_id and old_mesh.get("CLIENT_ID"):
        node_id = node_id_from_num(int(old_mesh["CLIENT_ID"]))
    return {
        "mqtt": {
            "host": old_mqtt.get("MQTT_ip") or old_mqtt.get("GATEWAY_ip"),
            "username": old_mqtt.get("MQTT_USER"),
            "password": old_mqtt.get("MQTT_PW"),
        },
        "meshtastic": {
            "channel": old_mesh.get("CHANNEL_ID", "LongFast"),
            "node_id": node_id,
            "long_name": old_mesh.get("LONG_NAME"),
            "short_name": old_mesh.get("SHORT_NAME"),
        },
    }


def parse_config(raw: dict, path: Path | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config file is empty or not a YAML mapping")
    if "MQTT" in raw or "MESHTASTIC" in raw:
        raw = _from_legacy(raw)

    mqtt_raw = raw.get("mqtt") or {}
    mesh_raw = raw.get("meshtastic") or {}
    storage_raw = raw.get("storage") or {}

    mqtt = _parse_mqtt(mqtt_raw, "mqtt")

    node_id = str(mesh_raw.get("node_id") or "").lower()
    if not NODE_ID_RE.match(node_id):
        raise ConfigError(f"meshtastic.node_id must look like '!1a2b3c4d', got {node_id!r}")
    long_name = str(mesh_raw.get("long_name") or "").strip()
    short_name = str(mesh_raw.get("short_name") or "").strip()
    if not long_name:
        raise ConfigError("meshtastic.long_name is required")
    if not 1 <= len(short_name) <= 4:
        raise ConfigError("meshtastic.short_name must be 1 to 4 characters")

    key = mesh_raw.get("key", "AQ==")
    key = "" if key is None else str(key)
    try:
        expand_key(key)
    except ValueError as exc:
        raise ConfigError(f"meshtastic.key is invalid: {exc}") from exc

    mesh = MeshConfig(
        node_id=node_id,
        long_name=long_name,
        short_name=short_name,
        channel=str(mesh_raw.get("channel", "LongFast")),
        key=key,
        aead=bool(mesh_raw.get("aead", False)),
    )
    if mesh.aead and not mesh.aes_key:
        raise ConfigError("meshtastic.aead needs a channel key")

    database = Path(storage_raw.get("database", "meshtastic-mqtt-client.sqlite3"))
    if path is not None and not database.is_absolute():
        database = path.parent / database
    homeassistant = None
    if raw.get("homeassistant"):
        homeassistant = _parse_homeassistant(raw["homeassistant"], mqtt_raw, mesh)
    return Config(mqtt=mqtt, mesh=mesh, database=database, path=path, homeassistant=homeassistant)


def _parse_mqtt(raw: dict, section: str) -> MqttConfig:
    if not raw.get("host"):
        raise ConfigError(f"{section}.host is required")
    return MqttConfig(
        host=str(raw["host"]),
        port=int(raw.get("port", 8883 if raw.get("tls") else 1883)),
        username=raw.get("username") or None,
        password=None if raw.get("password") is None else str(raw["password"]),
        tls=bool(raw.get("tls", False)),
        root_topic=str(raw.get("root_topic", "msh/US")).rstrip("/"),
    )


def _node_list(raw, section: str) -> list[str]:
    nodes = [str(n).lower() for n in (raw or [])]
    for n in nodes:
        if not NODE_ID_RE.match(n):
            raise ConfigError(f"{section} entries must look like '!1a2b3c4d', got {n!r}")
    return nodes


def is_private_key(key: str) -> bool:
    """True for a full 128/256-bit key that isn't the published default."""
    raw = base64.b64decode(key.replace("-", "+").replace("_", "/")) if key else b""
    return len(raw) in (16, 32) and raw != DEFAULT_KEY


def _parse_homeassistant(raw: dict, mesh_mqtt_raw: dict, mesh: MeshConfig) -> HomeAssistantConfig:
    if not isinstance(raw, dict):
        raise ConfigError("homeassistant must be a mapping")
    mqtt = _parse_mqtt(raw.get("mqtt") or mesh_mqtt_raw, "homeassistant.mqtt")
    if mqtt.host.lower() in PUBLIC_BROKERS:
        raise ConfigError(
            f"homeassistant.mqtt points at the public broker {mqtt.host}; "
            "use your own broker so strangers can't read or control your Home Assistant"
        )

    sensors = raw.get("sensors") or {}
    if not isinstance(sensors, dict):
        raise ConfigError("homeassistant.sensors must map sensor keys to settings")
    for key, overrides in sensors.items():
        unknown = set(overrides or {}) - SENSOR_OVERRIDE_KEYS
        if unknown:
            raise ConfigError(f"homeassistant.sensors.{key}: unknown settings {sorted(unknown)}")

    nodes = _node_list(raw.get("nodes"), "homeassistant.nodes")

    assist = None
    assist_raw = raw.get("assist") or {}
    if assist_raw.get("enabled"):
        allowed = _node_list(assist_raw.get("allowed_nodes"), "homeassistant.assist.allowed_nodes")
        if not allowed:
            raise ConfigError("homeassistant.assist.allowed_nodes must list at least one node")
        if mesh.node_id in allowed:
            raise ConfigError("homeassistant.assist.allowed_nodes must not include this client's own node_id")
        assist = AssistConfig(
            allowed_nodes=allowed,
            max_requests_per_minute=max(1, int(assist_raw.get("max_requests_per_minute", 6))),
        )

    alerts_raw = raw.get("alerts") or {}
    alert_nodes = _node_list(alerts_raw.get("nodes", assist.allowed_nodes if assist else []),
                             "homeassistant.alerts.nodes")
    if mesh.node_id in alert_nodes:
        raise ConfigError("homeassistant.alerts.nodes must not include this client's own node_id")

    controls = []
    for node_id, entries in (raw.get("controls") or {}).items():
        node_id = str(node_id).lower()
        if node_id not in nodes:
            raise ConfigError(f"homeassistant.controls: {node_id} must also be listed in homeassistant.nodes")
        for entry in entries or []:
            ctype = str(entry.get("type", "switch"))
            if ctype not in ("switch", "button"):
                raise ConfigError(f"homeassistant.controls.{node_id}: type must be switch or button")
            key = re.sub(r"[^a-z0-9_]+", "_", str(entry.get("key", "")).strip().lower()).strip("_")[:32]
            if not key:
                raise ConfigError(f"homeassistant.controls.{node_id}: every control needs a key")
            controls.append(ControlConfig(node=node_id, key=key, type=ctype, name=entry.get("name"),
                                          optimistic=bool(entry.get("optimistic", False))))

    if (assist or alert_nodes or alerts_raw.get("channel") or controls) and not is_private_key(mesh.key):
        raise ConfigError(
            "Assist, alerts and controls need a private channel: meshtastic.key must be a randomly "
            "generated 128 or 256-bit key (not empty, not AQ== or another default). Anyone with "
            "the channel key can read these messages and send commands to Home Assistant."
        )

    delivery_raw = raw.get("delivery") or {}
    delivery = DeliveryConfig(
        retries=max(0, int(delivery_raw.get("retries", 2))),
        ack_timeout=max(1.0, float(delivery_raw.get("ack_timeout", 30.0))),
        interval=max(0.0, float(delivery_raw.get("interval", 5.0))),
        max_parts=max(1, int(delivery_raw.get("max_parts", 3))),
    )

    expire_after = raw.get("expire_after")
    return HomeAssistantConfig(
        mqtt=mqtt,
        discovery_prefix=str(raw.get("discovery_prefix", "homeassistant")).rstrip("/"),
        base_topic=str(raw.get("base_topic", "meshtastic-bridge")).rstrip("/"),
        nodes=nodes,
        sensors={str(k): dict(v or {}) for k, v in sensors.items()},
        expire_after=int(expire_after) if expire_after else None,
        assist=assist,
        alert_nodes=alert_nodes,
        alert_channel=bool(alerts_raw.get("channel", False)),
        controls=controls,
        events=bool(raw.get("events", True)),
        delivery=delivery,
    )


def _secrets_loader(path: Path) -> type:
    """A SafeLoader that understands `!secret name` and `!env VARIABLE`.

    Secrets come from secrets.yaml next to the config file, the same
    convention Home Assistant uses.
    """
    secrets_path = path.parent / "secrets.yaml"
    cache: dict = {}

    def secret(loader, node):
        name = loader.construct_scalar(node)
        if "values" not in cache:
            try:
                with open(secrets_path, encoding="utf-8") as fh:
                    cache["values"] = yaml.safe_load(fh) or {}
            except FileNotFoundError:
                raise ConfigError(f"!secret {name} used but {secrets_path} does not exist") from None
        if name not in cache["values"]:
            raise ConfigError(f"secret {name!r} not found in {secrets_path}")
        return cache["values"][name]

    def env(loader, node):
        name = loader.construct_scalar(node)
        if name not in os.environ:
            raise ConfigError(f"environment variable {name} (used with !env) is not set")
        return os.environ[name]

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", secret)
    Loader.add_constructor("!env", env)
    return Loader


def load_config(path: Path) -> Config:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.load(fh, Loader=_secrets_loader(path))  # noqa: S506 - a SafeLoader subclass
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path} not found; copy config/config.example.yaml to {path} and edit it"
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    _warn_if_world_readable(path)
    return parse_config(raw, path)


def _warn_if_world_readable(path: Path) -> None:
    try:
        if os.name == "posix" and path.stat().st_mode & 0o004:
            log.warning("%s is readable by every user on this machine; consider chmod 600 "
                        "or moving passwords and keys to secrets.yaml", path)
    except OSError:
        pass
