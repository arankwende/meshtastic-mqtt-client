"""Command line entry point."""

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .client import MeshClient
from .config import ConfigError, load_config
from .display import chat_line, describe_event
from .protocol import TextMessage
from .storage import Storage

log = logging.getLogger("meshtastic_mqtt_client")


def setup_logging(log_file: Path | None, debug: bool, console: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_error = None
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3,
                                                      encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:
            log_error = exc
            console = True
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG if debug else logging.WARNING)
        root.addHandler(sh)
    # paho's DEBUG output is very chatty and rarely useful.
    logging.getLogger("paho").setLevel(logging.INFO)
    if log_error:
        log.warning("Can't write the log file %s (%s); logging to the terminal only", log_file, log_error)


def run_headless(client: MeshClient) -> None:
    """Print traffic to stdout and keep logging to the database until interrupted."""
    storage, own = client.storage, client.config.mesh.node_num

    def on_event(event) -> None:
        if isinstance(event, TextMessage):
            p = event.packet
            print(chat_line(storage, own, p.from_num, p.to_num, event.text, p.rx_time), flush=True)
        elif summary := describe_event(storage, event):
            print(summary, flush=True)

    client.add_listener(on_event)
    client.add_status_listener(lambda ok, detail: print(f"* {detail}", flush=True))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    client.connect()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    client.disconnect()


def main(argv: list[str] | None = None, default_config: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="meshtastic-mqtt-client",
        description="Chat with a Meshtastic network through an MQTT broker.",
    )
    parser.add_argument("-c", "--config", type=Path,
                        default=default_config or Path("config/config.yaml"),
                        help="path to config.yaml (default: %(default)s)")
    parser.add_argument("--headless", action="store_true",
                        help="no GUI: print traffic to the terminal and record it to the database")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--log-file", type=Path,
                        help="log file (default: meshtastic-mqtt-client.log next to the config)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    log_file = args.log_file or args.config.parent / "meshtastic-mqtt-client.log"
    setup_logging(log_file, args.debug, console=True)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        if not args.headless:
            _gui_error(str(exc))
        return 2

    storage = Storage(config.database)
    bridges = []

    def start_bridge(client: MeshClient) -> None:
        if config.homeassistant:
            from .homeassistant import HomeAssistantBridge

            bridge = HomeAssistantBridge(config, client)
            bridge.start()
            bridges.append(bridge)

    try:
        if args.headless:
            client = MeshClient(config, storage)
            start_bridge(client)
            run_headless(client)
        else:
            from .gui import run_gui  # tkinter is only needed for the GUI

            run_gui(config, storage, on_client=start_bridge)
    finally:
        for bridge in bridges:
            bridge.stop()
        storage.close()
    return 0


def _gui_error(message: str) -> None:
    """People who start the app by double-clicking never see stderr."""
    try:
        import FreeSimpleGUI as sg

        sg.popup_error(message, title="Meshtastic MQTT Client")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
