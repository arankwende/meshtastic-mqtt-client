import json
import re

from meshtastic_mqtt_client.mapview import map_html
from meshtastic_mqtt_client.storage import Node


def node(**kw):
    base = dict(node_num=1, node_id="!00000001", long_name="Base", short_name="B", hw_model="TBEAM",
                role=None, last_heard=None, hops_away=2, battery_level=101, voltage=None,
                latitude=-34.6, longitude=-58.4, altitude=None)
    return Node(**base | kw)


def embedded(html):
    return json.loads(re.search(r'<script id="nodes" type="application/json">(.*?)</script>', html, re.S)
                      .group(1).replace("<\\/", "</"))


def test_nodes_with_positions_become_markers():
    data = embedded(map_html([node(), node(node_num=2, node_id="!00000002", latitude=None, longitude=None)]))
    assert len(data) == 1
    assert data[0]["name"] == "Base" and data[0]["lat"] == -34.6
    assert "Battery powered" in data[0]["details"] and "2 hop(s) away" in data[0]["details"]


def test_hostile_node_names_cannot_inject_markup():
    evil = '</script><script>alert(1)</script><img src=x onerror=alert(2)>'
    html = map_html([node(long_name=evil)])
    assert "</script><script>alert(1)" not in html
    assert embedded(html)[0]["name"] == evil  # still shown, as text
    assert "innerHTML" not in html


def test_leaflet_is_pinned_with_integrity():
    html = map_html([])
    assert 'leaflet@1.9.4/dist/leaflet.js" integrity="sha256-' in html
    assert 'leaflet.css" integrity="sha256-' in html
