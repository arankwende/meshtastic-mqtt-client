"""A standalone Leaflet map of node positions, opened in the browser."""

import json
import tempfile
import webbrowser
from pathlib import Path

from .display import ago, battery_text
from .storage import Node

LEAFLET = "https://unpkg.com/leaflet@1.9.4/dist"
LEAFLET_CSS_SRI = "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
LEAFLET_JS_SRI = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="

# Node names come from other people's radios, so they're passed as JSON data and
# rendered with textContent in the page; nothing from the mesh is ever parsed as HTML.
TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meshtastic node map</title>
<link rel="stylesheet" href="LEAFLET/leaflet.css" integrity="CSS_SRI" crossorigin="">
<script src="LEAFLET/leaflet.js" integrity="JS_SRI" crossorigin=""></script>
<style>
  html, body, #map { height: 100%; margin: 0; }
  body { font: 14px system-ui, sans-serif; }
  .popup b { display: block; margin-bottom: 4px; }
  #empty { position: absolute; inset: 0; display: grid; place-items: center; color: #666; }
</style>
</head>
<body>
<div id="map"></div>
<script id="nodes" type="application/json">NODES_JSON</script>
<script>
const nodes = JSON.parse(document.getElementById("nodes").textContent);
const map = L.map("map");
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
const bounds = [];
for (const n of nodes) {
  const popup = document.createElement("div");
  popup.className = "popup";
  const title = document.createElement("b");
  title.textContent = n.name;
  popup.appendChild(title);
  for (const line of n.details) {
    const row = document.createElement("div");
    row.textContent = line;
    popup.appendChild(row);
  }
  L.marker([n.lat, n.lon], {title: n.name}).addTo(map).bindPopup(popup);
  bounds.push([n.lat, n.lon]);
}
if (bounds.length) {
  map.fitBounds(bounds, {padding: [40, 40], maxZoom: 14});
} else {
  map.setView([0, 0], 2);
  const empty = document.createElement("div");
  empty.id = "empty";
  empty.textContent = "No node has shared a position yet.";
  document.body.appendChild(empty);
}
</script>
</body>
</html>
"""


def map_html(nodes: list[Node]) -> str:
    markers = []
    for n in nodes:
        if n.latitude is None or n.longitude is None:
            continue
        details = [n.node_id, f"Last heard {ago(n.last_heard)}"]
        if n.hw_model:
            details.append(n.hw_model)
        if n.battery_level is not None:
            details.append(f"Battery {battery_text(n.battery_level)}")
        if n.hops_away is not None:
            details.append(f"{n.hops_away} hop(s) away")
        markers.append({"name": n.display_name, "lat": n.latitude, "lon": n.longitude, "details": details})
    # "</" inside the JSON would end the <script> element early.
    data = json.dumps(markers, ensure_ascii=False).replace("</", "<\\/")
    return (TEMPLATE.replace("LEAFLET", LEAFLET).replace("CSS_SRI", LEAFLET_CSS_SRI)
            .replace("JS_SRI", LEAFLET_JS_SRI).replace("NODES_JSON", data))


def open_map(nodes: list[Node]) -> Path:
    # A fresh private file, not a fixed name another local user could pre-create.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="meshtastic-map-",
                                     suffix=".html", delete=False) as fh:
        fh.write(map_html(nodes))
    path = Path(fh.name)
    webbrowser.open(path.as_uri())
    return path
