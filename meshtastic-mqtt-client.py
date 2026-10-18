#!/usr/bin/env python3
"""Run the client straight from a checkout: ./meshtastic-mqtt-client.py [--headless]"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from meshtastic_mqtt_client.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(default_config=HERE / "config" / "config.yaml"))
