"""Desktop GUI built on FreeSimpleGUI (tkinter)."""

import logging
import shutil
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import datetime

import FreeSimpleGUI as sg

from . import __version__
from .client import MeshClient, NotConnected
from .config import Config
from .display import NODE_HEADINGS, chat_line, describe_event, node_name, node_rows
from .mapview import open_map
from .protocol import BROADCAST, TextMessage
from .storage import Storage

log = logging.getLogger(__name__)

EVENT_MESH = "-MESH-"
EVENT_STATUS = "-STATUS-"
EVENT_DELIVERY = "-DELIVERY-"
SEARCH_HEADINGS = ["Time", "From", "To", "Message"]
EVERYONE = "Everyone"
NODE_REFRESH_SECONDS = 2
NODE_MAX_AGE_SECONDS = 30  # keeps "Last heard" current

COLOR_OUTGOING = "#1565c0"
COLOR_PRIVATE = "#6a1b9a"
COLOR_SYSTEM = "#757575"

HELP_TEXT = """\
This client connects to the MQTT broker set in config.yaml and talks to your \
Meshtastic network through any node that has MQTT uplink and downlink enabled \
on the configured channel.

Chat: pick "Everyone" or a node in the "To" box and press Enter to send. \
Private messages are sent on the channel key, so other members of the channel \
can technically decrypt them.

Private messages ask for an acknowledgement: you'll see "✓ delivered" or \
"✗ not delivered" under them.

Nodes: every node heard on the channel, with battery, hops and last position. \
Double-click a node to message it. View → Map of nodes opens their positions \
in your browser.

Search: find old messages by text.

Requirements on the gateway node (Meshtastic app → Settings):
  • MQTT module enabled, pointing at the same broker and root topic
  • "Uplink enabled" and "Downlink enabled" on the channel
  • "OK to MQTT" enabled so its own messages are uplinked
"""


def _menu() -> list:
    return [
        ["&File", ["&Settings", "Export &messages…", "Export &positions…", "---", "E&xit"]],
        ["&View", ["&Map of nodes", "&Search messages"]],
        ["&Mesh", ["Send &node info", "---", "&Connect", "&Disconnect"]],
        ["&Help", ["&Help", "&About"]],
    ]


def _layout(config: Config) -> list:
    chat_tab = [
        [sg.Multiline(key="-CHAT-", size=(100, 26), disabled=True, autoscroll=True,
                      expand_x=True, expand_y=True, font=("Helvetica", 11))],
        [sg.Text("To:"),
         sg.Combo([EVERYONE], default_value=EVERYONE, key="-DEST-", readonly=True, size=(26, 1)),
         sg.Input(key="-MSG-", expand_x=True, focus=True),
         sg.Button("Send", key="-SEND-", bind_return_key=True)],
    ]
    nodes_tab = [
        [sg.Table(values=[], headings=NODE_HEADINGS, key="-NODES-", expand_x=True, expand_y=True,
                  auto_size_columns=False, col_widths=[22, 6, 10, 14, 10, 5, 8, 20],
                  justification="left", num_rows=26, enable_click_events=False,
                  bind_return_key=True, select_mode=sg.TABLE_SELECT_MODE_BROWSE)],
        [sg.Text("Double-click a node to send it a private message.", text_color=COLOR_SYSTEM)],
    ]
    search_tab = [
        [sg.Input(key="-QUERY-", expand_x=True),
         sg.Button("Search", key="-SEARCH-")],
        [sg.Table(values=[], headings=SEARCH_HEADINGS, key="-RESULTS-", expand_x=True, expand_y=True,
                  auto_size_columns=False, col_widths=[16, 18, 18, 60], justification="left",
                  num_rows=26)],
    ]
    return [
        [sg.Menu(_menu(), key="-MENU-")],
        [sg.TabGroup([[sg.Tab("Chat", chat_tab, key="-TAB_CHAT-"),
                       sg.Tab("Nodes", nodes_tab, key="-TAB_NODES-"),
                       sg.Tab("Search", search_tab, key="-TAB_SEARCH-")]],
                     expand_x=True, expand_y=True)],
        [sg.Text("●", key="-LED-", text_color="red"),
         sg.Text("Starting…", key="-STATUS-", expand_x=True),
         sg.Text("", key="-HEALTH-", text_color=COLOR_SYSTEM),
         sg.Text(f"{config.mesh.long_name} ({config.mesh.node_id}) on {config.mesh.channel}",
                 text_color=COLOR_SYSTEM)],
    ]


def notify_desktop(title: str, body: str) -> None:
    """Best-effort desktop notification (Linux notify-send); silently skipped elsewhere."""
    exe = shutil.which("notify-send")
    if exe:
        try:
            subprocess.Popen([exe, "--app-name=Meshtastic", title, body[:200]],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


class App:
    def __init__(self, config: Config, storage: Storage):
        self.config = config
        self.storage = storage
        sg.theme("Reddit")
        self.window = sg.Window(f"Meshtastic MQTT Client {__version__}", _layout(config),
                                resizable=True, finalize=True)
        self.client = MeshClient(
            config, storage,
            # Called from the MQTT thread; write_event_value hands off to the GUI thread.
            on_event=lambda e: self.window.write_event_value(EVENT_MESH, e),
            on_status=lambda ok, msg: self.window.write_event_value(EVENT_STATUS, (ok, msg)),
        )
        self._destinations: dict[str, int] = {EVERYONE: BROADCAST}
        self._node_table: list[int] = []
        self._nodes_dirty = True
        self._last_refresh = 0.0
        self._title = f"Meshtastic MQTT Client {__version__}"
        self._unread = 0

    # Output helpers

    def _print(self, line: str, color: str | None = None) -> None:
        self.window["-CHAT-"].print(line, text_color=color)

    def _system(self, line: str) -> None:
        self._print(f"[{datetime.now():%H:%M}] {line}", COLOR_SYSTEM)

    def _print_text(self, from_num: int, to_num: int, text: str, ts: int) -> None:
        own = self.config.mesh.node_num
        color = COLOR_OUTGOING if from_num == own else COLOR_PRIVATE if to_num != BROADCAST else None
        self._print(chat_line(self.storage, own, from_num, to_num, text, ts), color)

    def _refresh_nodes(self) -> None:
        nodes = [n for n in self.storage.nodes() if n.node_num != self.config.mesh.node_num]
        self._node_table = [n.node_num for n in nodes]
        self.window["-NODES-"].update(values=node_rows(nodes))
        self._destinations = {EVERYONE: BROADCAST}
        for n in nodes:
            self._destinations[f"{n.display_name} ({n.node_id})"] = n.node_num
        current = self.window["-DEST-"].get()
        self.window["-DEST-"].update(
            value=current if current in self._destinations else EVERYONE,
            values=list(self._destinations),
        )
        self._nodes_dirty = False
        self._last_refresh = time.monotonic()

    # Actions

    def _send(self, values: dict) -> None:
        text = values["-MSG-"]
        if not text.strip():
            return
        to = self._destinations.get(values["-DEST-"], BROADCAST)
        if to != BROADCAST:
            self._send_direct(text.strip(), to)
            return
        try:
            msg = self.client.send_text(text, to)
        except NotConnected:
            self._system("Not connected — message not sent.")
            return
        except ValueError as exc:
            sg.popup_error(str(exc), title="Can't send")
            return
        self.window["-MSG-"].update("")
        p = msg.packet
        self._print_text(p.from_num, p.to_num, msg.text, p.rx_time)

    def _send_direct(self, text: str, to: int) -> None:
        """Direct messages wait for an acknowledgement on a worker thread."""
        if not self.client.connected:
            self._system("Not connected — message not sent.")
            return
        self.window["-MSG-"].update("")
        self._print_text(self.config.mesh.node_num, to, text, int(time.time()))

        def worker() -> None:
            try:
                _, delivery = self.client.send_text_reliably(text, to)
            except ValueError as exc:
                self.window.write_event_value(EVENT_DELIVERY, (to, None, str(exc)))
                return
            self.window.write_event_value(EVENT_DELIVERY, (to, delivery, None))

        threading.Thread(target=worker, name="gui-send", daemon=True).start()

    def _show_delivery(self, to: int, delivery, error: str | None) -> None:
        who = node_name(self.storage, to)
        if delivery is not None and delivery.ok:
            self._system(f"✓ delivered to {who}")
        else:
            reason = error or (delivery.error if delivery else "unknown")
            self._system(f"✗ not delivered to {who} ({reason})")

    def _search(self, values: dict) -> None:
        query = values["-QUERY-"].strip()
        if not query:
            return
        own = self.config.mesh.node_num
        rows = []
        for m in self.storage.search_messages(query):
            sender = "You" if m.from_num == own else node_name(self.storage, m.from_num)
            recipient = "Everyone" if m.to_num == BROADCAST else (
                "You" if m.to_num == own else node_name(self.storage, m.to_num))
            rows.append([datetime.fromtimestamp(m.received_at).strftime("%Y-%m-%d %H:%M"),
                         sender, recipient, m.text])
        self.window["-RESULTS-"].update(values=rows)
        if not rows:
            self._system(f"No messages contain “{query}”.")

    def _focused(self) -> bool:
        try:
            return self.window.TKroot.focus_displayof() is not None
        except Exception:
            return True

    def _note_incoming(self, msg: TextMessage) -> None:
        if self._focused():
            return
        self._unread += 1
        self.window.set_title(f"({self._unread}) {self._title}")
        if msg.packet.to_num == self.config.mesh.node_num:
            self.window.TKroot.bell()
            notify_desktop(f"Message from {node_name(self.storage, msg.packet.from_num)}", msg.text)

    def _update_health(self) -> None:
        stats = self.client.stats.snapshot()
        gateways = stats["gateways_last_hour"]
        self.window["-HEALTH-"].update(
            f"{stats['packets_last_hour']} packets/h · {gateways} gateway{'s' if gateways != 1 else ''}"
        )

    def _message_selected_node(self, values: dict) -> None:
        rows = values["-NODES-"]
        if not rows or rows[0] >= len(self._node_table):
            return
        node_num = self._node_table[rows[0]]
        for label, num in self._destinations.items():
            if num == node_num:
                self.window["-DEST-"].update(value=label)
        self.window["-TAB_CHAT-"].select()
        self.window["-MSG-"].set_focus()

    def _export(self, kind: str) -> None:
        path = sg.popup_get_file(f"Save {kind} as CSV", save_as=True, no_window=True,
                                 default_extension=".csv", file_types=(("CSV", "*.csv"),))
        if not path:
            return
        export = self.storage.export_messages_csv if kind == "messages" else self.storage.export_positions_csv
        count = export(path)
        self._system(f"Exported {count} {kind} to {path}")

    def _show_settings(self) -> None:
        m, mesh = self.config.mqtt, self.config.mesh
        rows = [
            ("Config file", str(self.config.path or "")),
            ("Broker", f"{'mqtts' if m.tls else 'mqtt'}://{m.host}:{m.port}"),
            ("User", m.username or "(anonymous)"),
            ("Root topic", m.root_topic),
            ("Channel", mesh.channel),
            ("Channel key", "default (AQ==)" if mesh.key == "AQ==" else "none" if not mesh.aes_key else "custom"),
            ("Encryption", "none" if not mesh.aes_key else "AES-CCM (AEAD)" if mesh.aead else "AES-CTR"),
            ("Node ID", mesh.node_id),
            ("Long name", mesh.long_name),
            ("Short name", mesh.short_name),
            ("Database", str(self.config.database)),
        ]
        layout = [[sg.Text(k, size=(12, 1), font=("Helvetica", 10, "bold")), sg.Text(v)] for k, v in rows]
        layout.append([sg.Text("Edit config.yaml and restart to change these.", text_color=COLOR_SYSTEM)])
        layout.append([sg.Push(), sg.Button("Close")])
        sg.Window("Settings", layout, modal=True).read(close=True)

    # Main loop

    def run(self) -> None:
        for m in self.storage.recent_messages(200):
            self._print_text(m.from_num, m.to_num, m.text, m.received_at)
        self._system("— history above —")
        self.client.connect()

        while True:
            event, values = self.window.read(timeout=1000)
            if event in (sg.WIN_CLOSED, "Exit"):
                break
            if event == EVENT_MESH:
                mesh_event = values[EVENT_MESH]
                if isinstance(mesh_event, TextMessage):
                    p = mesh_event.packet
                    self._print_text(p.from_num, p.to_num, mesh_event.text, p.rx_time)
                    self._note_incoming(mesh_event)
                else:
                    summary = describe_event(self.storage, mesh_event)
                    if summary:
                        log.info(summary)
                self._nodes_dirty = True
            elif event == EVENT_STATUS:
                ok, detail = values[EVENT_STATUS]
                self.window["-LED-"].update(text_color="green" if ok else "red")
                self.window["-STATUS-"].update(detail)
            elif event == "-SEND-":
                self._send(values)
            elif event == EVENT_DELIVERY:
                self._show_delivery(*values[EVENT_DELIVERY])
            elif event == "-SEARCH-":
                self._search(values)
            elif event == "Search messages":
                self.window["-TAB_SEARCH-"].select()
                self.window["-QUERY-"].set_focus()
            elif event == "Map of nodes":
                nodes = [n for n in self.storage.nodes() if n.latitude is not None]
                open_map(nodes)
                self._system(f"Opened a map of {len(nodes)} node(s) in your browser.")
            elif event == "-NODES-":
                self._message_selected_node(values)
            elif event == "Send node info":
                self.client.send_nodeinfo()
                self._system("Node info sent.")
            elif event == "Connect":
                if not self.client.connected:
                    self.client.connect()
            elif event == "Disconnect":
                self.client.disconnect()
            elif event == "Export messages…":
                self._export("messages")
            elif event == "Export positions…":
                self._export("positions")
            elif event == "Settings":
                self._show_settings()
            elif event == "Help":
                sg.popup_scrolled(HELP_TEXT, title="Help", size=(80, 20))
            elif event == "About":
                if sg.popup_yes_no(
                    f"Meshtastic MQTT Client {__version__}\n\n"
                    "Open the project page on GitHub?", title="About") == "Yes":
                    webbrowser.open("https://github.com/arankwende/meshtastic-mqtt-client")

            since = time.monotonic() - self._last_refresh
            if (self._nodes_dirty and since > NODE_REFRESH_SECONDS) or since > NODE_MAX_AGE_SECONDS:
                self._refresh_nodes()
                self._update_health()
            if self._unread and self._focused():
                self._unread = 0
                self.window.set_title(self._title)

        self.client.disconnect()
        self.window.close()


def run_gui(config: Config, storage: Storage,
            on_client: Callable[[MeshClient], None] | None = None) -> None:
    app = App(config, storage)
    if on_client:
        on_client(app.client)
    app.run()
