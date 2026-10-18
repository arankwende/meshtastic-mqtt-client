# meshtastic-mqtt-client

Chat with a [Meshtastic](https://meshtastic.org) network from your computer, without a radio, through the MQTT broker your mesh's gateway node already talks to.

- **Desktop GUI**:
  - Chat with broadcast and private messages, including a delivery confirmation for private messages.
  - A live node list showing battery, hops, last-heard time and position, plus a node map in your browser.
  - Message search, and notifications for messages that arrive while the window is in the background.
- **Headless mode**: prints mesh traffic to the terminal and records it. Useful on a server or Raspberry Pi.
- **Current Meshtastic protocol**: works with firmware 2.x up to 2.8, including encrypted channels (default `AQ==` key or your own PSK), the newer AES-CCM ("AEAD") channel encryption, and the `msh/<region>/2/e/...` topic layout.
- **SQLite history**: messages, nodes, positions and telemetry, exportable to CSV.
- **Duplicate filtering**: the same packet arriving through several gateways is shown once.
- **Auto-reconnect**: if the broker goes away, the client reconnects and resubscribes on its own.
- **Home Assistant bridge**:
  - Mesh sensor readings become Home Assistant entities.
  - Automations can send alerts to the mesh and switch things on your sensor nodes.
  - You can chat with Assist from the Meshtastic app.
  - Bridge health is shown in Home Assistant.

  It runs as a Docker container or a Home Assistant app. See [Home Assistant](#home-assistant).
- **Secrets**: passwords and keys can live in `secrets.yaml` or environment variables instead of `config.yaml`.

## How it works

```
 radios ⇄ gateway node (MQTT uplink + downlink) ⇄ MQTT broker ⇄ this client
```

The client acts like another node on one channel. It decrypts what the gateway uplinks to the broker. It publishes your messages back, encrypted with the channel key, for the gateway to transmit over LoRa.

## Setting up the gateway node

In the Meshtastic app, on the node connected to your broker:

1. **Settings → MQTT**: enable it, and set the address, username, password and **root topic** of your broker.
2. **Settings → Channels → *your channel***: turn on **Uplink enabled** and **Downlink enabled**.
3. **Settings → LoRa**: enable **OK to MQTT**.

Messages you send from this client reach the radios only through a gateway with *downlink* enabled. The public `mqtt.meshtastic.org` broker does not downlink to radios, so sending needs your own broker or your own gateway.

## Installing

Python 3.10 or newer, with tkinter for the GUI. On Debian/Ubuntu: `sudo apt install python3 python3-venv python3-tk`.

```sh
git clone https://github.com/arankwende/meshtastic-mqtt-client
cd meshtastic-mqtt-client
python3 -m venv .venv
. .venv/bin/activate
pip install .
```

## Configuring

```sh
cp config/config.example.yaml config/config.yaml
```

Then edit `config/config.yaml`:

```yaml
mqtt:
  host: 192.168.1.10
  port: 1883
  username: mesh
  password: secret
  tls: false
  root_topic: msh/US        # must match the gateway's root topic

meshtastic:
  channel: LongFast         # channel name, exactly as in the app
  key: AQ==                 # channel PSK (base64) from the app; "" if unencrypted
  aead: false               # true if the channel uses AES-CCM ("AEAD") encryption
  node_id: '!c0ffee01'      # this client's id on the mesh: '!' + 8 hex digits
  long_name: 'My Desktop'
  short_name: 'DESK'        # up to 4 characters
```

Config files from version 1 (with the uppercase `MQTT:` / `MESHTASTIC:` sections) still load, but you will probably need to add `root_topic` and `key`.

## Running

```sh
meshtastic-mqtt-client                    # GUI, reads config/config.yaml
meshtastic-mqtt-client --headless         # terminal only
meshtastic-mqtt-client -c /path/to/config.yaml --debug
```

From a checkout without installing, `./meshtastic-mqtt-client.py` works the same way, once the dependencies are installed.

Data is written next to the config file:

| File | Contents |
|---|---|
| `meshtastic-mqtt-client.sqlite3` | nodes, messages, positions, telemetry |
| `meshtastic-mqtt-client.log` | the log (rotated at 1 MB) |

### Using the GUI

- **Delivery confirmation.** Private messages ask for an acknowledgement, and a line under each one says "✓ delivered" or "✗ not delivered". Retries are automatic.
- **Node map.** *View → Map of nodes* opens a map of every node that has shared its position, in your browser.
- **Search.** The *Search* tab finds old messages by their text.
- **Notifications.** While the window is in the background, the title shows how many messages are unread. Direct messages also ring the bell and, on Linux, show a desktop notification.
- **Status bar.** Shows how many packets arrived in the last hour, and through how many gateways.
- **Export.** *File → Export messages* and *File → Export positions* save CSV files.

### Keeping secrets out of config.yaml

Any value in `config.yaml` can come from a `secrets.yaml` file next to it (the same convention as Home Assistant), or from an environment variable:

```yaml
# config.yaml
mqtt:
  password: !secret mqtt_password
meshtastic:
  key: !secret channel_key
homeassistant:
  mqtt:
    password: !env HA_MQTT_PASSWORD
```

```yaml
# secrets.yaml (chmod 600)
mqtt_password: s3cret
channel_key: 'base64 key from the app'
```

The client logs a warning if `config.yaml` is readable by every user on the machine.

### Docker

```sh
mkdir config && cp config/config.example.yaml config/config.yaml   # then edit it
docker build -t meshtastic-mqtt-client .
docker run -d --restart unless-stopped -v "$PWD/config:/config" meshtastic-mqtt-client
```

The container runs headless as an unprivileged user. It reads `/config/config.yaml`, and keeps its database and log in `/config`. `docker-compose.example.yaml` shows the same setup for Compose.

## Home Assistant

The client can bridge your mesh to Home Assistant over MQTT. Everything appears automatically through [MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery).

| Feature | What you get |
|---|---|
| [Sensors](#sensors) | Each listed mesh node becomes a device, with its telemetry, position and your own custom values. |
| [Alerts](#alerts) | `notify` entities that send a message from any automation to your phone, or to the whole channel. |
| [Controls](#controls) | Switches and buttons that send commands to your own sensor nodes. |
| [Message events](#message-events) | An `event` entity that fires for every message, so automations can react to "SOS" and similar. |
| [Assist chat](#assist-chat) | DM the bridge from the Meshtastic app and Home Assistant's Assist answers. |
| [Bridge health](#bridge-health) | Connection status, traffic and the outcome of the last message sent. |

Home Assistant needs the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) connected to the broker you configure below. Run the bridge in one of three ways:

- **As a Home Assistant app** (Home Assistant OS or Supervised). In *Settings → Apps → App store → ⋮ → Repositories*, add `https://github.com/arankwende/meshtastic-mqtt-client`, then install **Meshtastic Bridge**. It picks up the Mosquitto broker app automatically. Its first start creates `config.yaml` in the app's configuration folder and stops so you can edit it.
- **In Docker**, as described in [Docker](#docker).
- **Directly**, with `meshtastic-mqtt-client --headless`, for example as a systemd service.

### Configuration

Add a `homeassistant:` section to `config/config.yaml`:

```yaml
meshtastic:
  channel: Home                     # a private channel: see "Security" below
  key: 'paste the channel key from the app here'
  node_id: '!c0ffee01'
  long_name: 'Home Assistant'
  short_name: 'HA'

homeassistant:
  # Home Assistant's broker. Leave this out to use the broker in the mqtt: section.
  mqtt:
    host: 192.168.1.10
    username: meshbridge
    password: secret
  nodes: ['!5e550001', '!5e550002']  # nodes that become Home Assistant devices
  sensors:                           # optional presentation for custom values
    tank: {unit_of_measurement: '%', icon: 'mdi:water'}
  expire_after: 7200                 # optional: unavailable after 2 h without data
  assist:
    enabled: true
    allowed_nodes: ['!a1b2c3d4']     # your phone's node ID
    max_requests_per_minute: 6
  alerts:
    nodes: ['!a1b2c3d4']             # default: the Assist allowed_nodes
    channel: false                   # true adds a notify entity for the whole channel
  controls:
    '!5e550001':                     # must also be listed in nodes
      - {key: pump, type: switch}
      - {key: reset, type: button, name: Reset counter}
  events: true                       # the "Mesh message" event entity
  delivery:
    retries: 2                       # resends when a direct message isn't acknowledged
    ack_timeout: 30                  # seconds to wait for each acknowledgement
    interval: 5                      # seconds between consecutive messages
    max_parts: 3                     # long texts are split into at most this many parts
```

Assist, alerts and controls only work on a private channel. See [Security](#security).

### Sensors

Only nodes listed in `nodes` become devices, so busy channels don't flood Home Assistant. Each device is named after the node's long name, and an entity appears the first time its value is reported.

**Built-in telemetry.** Battery, voltage, temperature, humidity, pressure, channel utilization and airtime come from the node's telemetry. You also get *Last heard* and *Hops away*. If the node shares its position, it gets a `device_tracker` that shows it on the map. A battery level of 101, which means "externally powered", is reported as 100.

To get readings from a sensor wired to a node (BME280, SHT31 and similar), open the node's settings in the Meshtastic app and enable **Telemetry → Environment metrics**, plus **Position** if you want location. Since firmware 2.8, both are off until you turn them on.

**Custom values.** You can send your own readings in either of two formats.

*JSON on port 256 (PRIVATE_APP).* Use this for your own code running on or next to a node:

```python
import json
import meshtastic.serial_interface

iface = meshtastic.serial_interface.SerialInterface()
iface.sendData(json.dumps({"tank": 73, "pump_running": True}).encode(), channelIndex=1)
```

Set `channelIndex` to the index of the private channel on that node.

*Text messages starting with `#ha`.* Anything that can send a Meshtastic text can do this, including the phone app, which makes testing easy:

```
#ha tank=73 pump_running=on valve=open
```

Both formats follow the same rules:

- Numbers become sensors with long-term statistics.
- `true`/`false` and `on`/`off` become binary sensors.
- Anything else becomes a text sensor.
- Keys are lowercased, characters other than `a-z`, `0-9` and `_` become `_`, and keys are cut to 32 characters.
- Each node can have at most 32 custom values.

Use the `sensors:` setting to give a key a unit, device class, state class, icon or name. Custom entities have IDs like `sensor.water_tank_tank`.

### Assist chat

1. **Create the automation.** In Home Assistant, add this automation. In the UI, choose *Settings → Automations → Create → Edit in YAML*.

   ```yaml
   alias: Meshtastic → Assist
   mode: queued
   triggers:
     - trigger: mqtt
       topic: meshtastic-bridge/assist/request
   actions:
     - action: conversation.process
       data:
         text: "{{ trigger.payload_json.text }}"
         conversation_id: "mesh-{{ trigger.payload_json.from }}"
         agent_id: conversation.home_assistant      # or your LLM conversation agent
       response_variable: result
     - action: mqtt.publish
       data:
         topic: meshtastic-bridge/assist/reply
         payload: >-
           {{ {"to": trigger.payload_json.from,
               "text": result.response.speech.plain.speech} | to_json }}
   ```

2. **Expose entities.** Choose which entities Assist may control in *Settings → Voice assistants → Expose*. Assist ignores everything else, and replies with, for example, "Garage lights is not exposed".
3. **Chat from your phone.** In the Meshtastic app, open a direct message with the bridge node, for example "Home Assistant", and write "turn on the garage lights" or "what's the temperature in the tank?". The answer comes back as a direct message.

Replies are split into numbered parts of up to 200 bytes. Each part waits for your phone's acknowledgement and is resent if needed. With the default `delivery` settings that's at most 3 parts, and longer replies are cut with "…". If you use an LLM agent, ask it in its instructions to answer in one or two short sentences.

The bridge acknowledges your direct messages the way a radio does, so the Meshtastic app shows them as delivered.

Expect 5–30 seconds per round trip, depending on your preset and how many hops away you are.

### Alerts

Every node in `alerts.nodes` gets a `notify` entity on the bridge device, such as `notify.home_assistant_message_a1b2c3d4`. With `alerts.channel: true` you also get one for the whole channel. Use them from any automation:

```yaml
actions:
  - action: notify.send_message
    target:
      entity_id: notify.home_assistant_message_a1b2c3d4
    data:
      message: "Water tank below 20%"
```

Direct alerts wait for an acknowledgement and are retried. The [Last delivery](#bridge-health) sensor then shows `delivered` or `failed`. Channel alerts can't be acknowledged, so they show `sent`.

### Controls

Controls let Home Assistant send commands to sensor nodes that run your own code.

- **Switches.** Each `switch` control becomes a switch on the node's device. Turning it on sends `{"pump": true}` to the node as a direct message on port 256, and turning it off sends `{"pump": false}`.
- **Buttons.** Each `button` control sends `{"reset": "press"}`.
- **Delivery.** Commands wait for an acknowledgement and are retried, like alerts.

The node's code should act on the command and then report the new state in its normal sensor data, for example `{"pump": true}`. The switch shows what the node reports, not what was requested, so it stays truthful if a command is lost. A switch whose node never reports its state shows *unknown*, unless you set `optimistic: true` on that control.

For example, with the Meshtastic Python library on a computer attached to the node:

```python
import json
from pubsub import pub
import meshtastic.serial_interface

def on_command(packet, interface):
    command = json.loads(packet["decoded"]["payload"])
    if "pump" in command:
        set_pump(command["pump"])                          # your hardware code
        interface.sendData(json.dumps({"pump": command["pump"]}).encode(), channelIndex=1)

pub.subscribe(on_command, "meshtastic.receive.data.PRIVATE_APP")
iface = meshtastic.serial_interface.SerialInterface()
```

### Message events

The bridge device has an `event` entity called *Mesh message*. It fires `channel_message` for every broadcast on the channel, and `direct_message` for every direct message to the bridge. The event's attributes are:

- `from`, `name` and `text`.
- `hops_away`.
- The sender's last known `latitude` and `longitude`.
- For direct messages, `allowed`: whether the sender is in `assist.allowed_nodes`.

```yaml
triggers:
  - trigger: state
    entity_id: event.home_assistant_mesh_message
conditions:
  - "{{ 'sos' in trigger.to_state.attributes.text | lower }}"
actions:
  - action: notify.mobile_app_my_phone
    data:
      message: "SOS from {{ trigger.to_state.attributes.name }}"
```

`#ha` sensor messages don't fire events. Set `events: false` to remove the entity.

### Bridge health

The bridge device updates these every minute:

- **Mesh connection** (connectivity): whether the bridge is connected to the mesh broker.
- **Last packet**: when anything was last heard on the channel.
- **Packets last hour**, **Gateways last hour** and **Nodes heard (24 h)**.
- **Gateway channel utilization**: how busy the air is, as reported by your gateway node.
- **Last delivery**: the outcome of the most recent alert, Assist reply or control command (`delivered`, `sent` or `failed`). Its attributes hold the recipient, text, attempts and error.
- **Announce on mesh** (button): broadcasts the bridge's node info.

If the bridge stops, all of its entities become unavailable within about 90 seconds, through its MQTT last will.

### Security

Anyone who can get a message to Assist can control what Assist controls. The bridge enforces the following rules.

**Checked at startup.** The bridge refuses to start if any of these fail:

1. If Assist, alerts or controls are enabled, `meshtastic.key` is a full 128- or 256-bit key, not the published default (`AQ==` or another one-byte shorthand) and not empty. Generate it in the Meshtastic app: *Channels → your channel → Key → generate*. With only sensors enabled, any channel works.
2. `homeassistant.mqtt` is not the public Meshtastic broker.
3. `assist.allowed_nodes` lists at least one node. Neither it nor `alerts.nodes` contains the bridge's own `node_id`.
4. Every node in `controls` is also listed in `nodes`.

**Checked for every incoming message.** A message reaches Assist only if all of these hold:

1. It was encrypted with your channel key. Packets on other channels or with other keys are never decrypted.
2. It is a **direct message to the bridge's node ID**. Channel broadcasts never go to Assist.
3. The sender's node ID is in `assist.allowed_nodes`. Anything else is dropped without a reply, and logged.
4. **The same packet has not been received before**, checked against the message database so it holds across restarts. This stops someone from recording your encrypted "open the garage" and transmitting it again later.
5. The sender is within `max_requests_per_minute`.
6. It doesn't start with `#ha`. Those messages are sensor data and are never sent to Assist.

**Checked for every message to the mesh.** These rules apply to everything the bridge sends:

- An Assist reply goes only to a node in `assist.allowed_nodes`.
- An alert goes only to a node in `alerts.nodes`, or to the channel if `alerts.channel` is on.
- A control command goes only to a configured node and key. The switch payload must be `ON` or `OFF`, and buttons only take a press.
- Anything else is dropped and logged. Texts are capped at `delivery.max_parts` parts.

**What this does not protect against:**

- **Someone who has your channel key.** Meshtastic channel messages don't authenticate the sender, so anyone with the key can send a message that claims to come from your phone's node ID. Treat the channel key like a password: share it only with devices you control, and change it if a device is lost.
- **Someone who can write to your Home Assistant broker.** They could publish to `meshtastic-bridge/assist/request` directly, bypassing the mesh. They could also make the bridge message your allowed nodes or send your configured control commands. Require a username and password on the broker, and use per-user ACLs if your broker supports them.
- **Lost commands.** LoRa is unreliable, and a command can fail even after its retries. The *Last delivery* sensor and the switch's reported state show it. Don't rely on the mesh for anything safety-critical.
- **Whatever Assist is allowed to do.** The bridge can't make a command safe. Don't expose locks, alarms, garage doors or anything else you wouldn't want triggered by a stolen phone or a leaked key.

**Checklist:**

- [ ] The private channel uses a newly generated 256-bit key, and only on your own devices.
- [ ] The gateway has *Uplink* and *Downlink* enabled on that channel.
- [ ] Your broker requires a username and password, and is not reachable from the internet without TLS.
- [ ] `allowed_nodes` contains only your own devices.
- [ ] Only harmless entities are exposed to Assist.

### MQTT topics

The base topic is `meshtastic-bridge`; change it with `homeassistant.base_topic`.

| Topic | Direction | Contents |
|---|---|---|
| `meshtastic-bridge/status` | bridge → HA | `online` / `offline` (last will), retained |
| `meshtastic-bridge/<node>/state` | bridge → HA | JSON with every value for the node, retained |
| `meshtastic-bridge/<node>/position` | bridge → HA | `{"latitude": …, "longitude": …, "altitude": …}`, retained |
| `meshtastic-bridge/assist/request` | bridge → HA | `{"from": "!a1b2c3d4", "name": "…", "text": "…", "packet_id": …}` |
| `meshtastic-bridge/assist/reply` | HA → bridge | `{"to": "!a1b2c3d4", "text": "…"}` |
| `meshtastic-bridge/notify/<node>` or `…/notify/channel` | HA → bridge | alert text |
| `meshtastic-bridge/<node>/set/<key>` | HA → bridge | `ON` / `OFF` for a switch control |
| `meshtastic-bridge/<node>/press/<key>` | HA → bridge | button control press |
| `meshtastic-bridge/bridge/state` | bridge → HA | health JSON, retained |
| `meshtastic-bridge/bridge/delivery` | bridge → HA | last delivery outcome, retained |
| `meshtastic-bridge/bridge/message` | bridge → HA | message events |
| `meshtastic-bridge/bridge/announce` | HA → bridge | announce button press |
| `homeassistant/<component>/meshtastic_<node>/<key>/config` | bridge → HA | Discovery configuration, retained |

`<node>` is the node ID without the `!`, for example `5e550001`.

### Home Assistant troubleshooting

- **The automation never triggers.** If you created it before the MQTT integration was set up, its trigger never attached. Turn the automation off and on again, or restart Home Assistant.
- **Assist replies "… is not exposed".** Expose the entity in *Settings → Voice assistants → Expose*.
- **All entities are unavailable.** The bridge is stopped or can't reach the broker. Entities come back as soon as it reconnects.
- **No reply arrives on the phone.** Check that the gateway has *Downlink enabled* on the channel, and that your phone's node ID is in `allowed_nodes`. Run with `--debug` and look for "Ignoring Assist request".
- **Last delivery is always `failed` with `TIMEOUT`.** The recipient's acknowledgements aren't reaching the broker. Check that your phone's own node has **OK to MQTT** enabled (*Settings → LoRa*), and that the gateway uplinks the channel.
- **A switch stays *unknown*.** The node never reported the value. Have its code include the key in its sensor data, or set `optimistic: true`.

## Firmware compatibility

| Firmware | Status |
|---|---|
| 2.3 – 2.7 | Supported. |
| 2.8 (alpha) | Supported. 2.8 nodes sign their packets; this client reads signed packets but doesn't verify or add signatures. 2.8 nodes accept packets from nodes that have never signed, which includes this client. |
| AES-CCM channels | Supported. This is the `use_aead` channel option on the firmware development branch, not yet in a release. Incoming packets are recognised automatically. Set `aead: true` to send on such a channel. |
| 1.x | Not supported. Use version 1 of this project. |

Some 2.8 changes affect what you'll see:

- **New default preset.** New US nodes default to the **LongTurbo** preset instead of LongFast, and the two can't hear each other. Use the channel name your gateway shows.
- **Position and telemetry are opt-in.** Nodes on 2.8 only send position and telemetry when their owner turns it on, so expect fewer positions and battery readings.
- **Coarse positions on public channels.** On channels with known keys, such as the default, 2.8 nodes share only approximate positions. Precise positions need a private channel.

## Troubleshooting

- **Connected, but nothing arrives.** Check that `root_topic` and `channel` match the gateway exactly; they are case-sensitive. A US gateway set up on firmware 2.8 is probably on `LongTurbo`. Use `--debug` to see packets skipped for using another channel or key.
- **"Connection refused: Not authorized".** The MQTT username or password is wrong.
- **Others don't see my messages.** The gateway needs *Downlink enabled* on that channel, and the broker must be the one the gateway uses.
- **Direct messages from phones don't show up.** Since firmware 2.5, DMs between nodes that know each other's public keys use end-to-end (PKI) encryption, which the channel key cannot decrypt. Channel messages always work.

## Development

```sh
pip install -e ".[test]"
pytest
```

The code lives in `meshtastic_mqtt_client/`:

| Module | Role |
|---|---|
| `config.py` | YAML loading and validation |
| `crypto.py` | channel key expansion, AES-CTR, channel hash |
| `protocol.py` | ServiceEnvelope decoding and encoding (pure functions) |
| `storage.py` | SQLite |
| `client.py` | MQTT connection, deduplication, sending |
| `display.py` | text formatting |
| `gui.py` | FreeSimpleGUI window |
| `cli.py` | command line and headless mode |
| `homeassistant.py` | Home Assistant bridge: Discovery, sensors, alerts, controls, events, health, Assist |
| `mapview.py` | the browser node map |

Protobuf definitions come from the official [`meshtastic`](https://pypi.org/project/meshtastic/) package, so they stay current with the firmware.

The decoding was originally based on Josh Pirihi's [meshtastic-mqtt](https://github.com/joshpirihi/meshtastic-mqtt).
