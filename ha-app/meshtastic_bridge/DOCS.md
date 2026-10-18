# Meshtastic Bridge

Brings a Meshtastic mesh into Home Assistant through the MQTT broker your
Meshtastic gateway already uses:

- Readings from mesh nodes appear as devices and sensors.
- Automations can send alerts to your phone over LoRa.
- Switches and buttons send commands to your own sensor nodes.
- You can chat with Assist from the Meshtastic app.

## Setup

1. Install the **Mosquitto broker** app. Point your Meshtastic gateway's MQTT settings at it, and enable *Uplink* and *Downlink* on your private channel.
2. Start this app once. It creates `config.yaml` in this app's configuration folder (`/addon_configs/<id>_meshtastic_bridge/` when viewed from Samba or the File editor) and stops.
3. Edit `config.yaml`: your channel name and key, the bridge's node ID, and the nodes and features you want. Start the app again.

The full configuration reference, the Assist automation to paste, and the
security rules are in the project README:
<https://github.com/arankwende/meshtastic-mqtt-client#home-assistant>

## Options

- **debug**: verbose logging, shown in the app's *Log* tab.
