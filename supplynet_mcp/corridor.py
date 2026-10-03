"""Shared geography for the supply-chain MCP servers: city coordinates, aliases, main corridor."""

import math
import re
from typing import Dict, List, Optional, Tuple

LatLng = Tuple[float, float]

CITIES: Dict[str, LatLng] = {
    "chandigarh": (30.7333, 76.7794),
    "delhi": (28.6139, 77.2090),
    "agra": (27.1767, 78.0081),
    "gwalior": (26.2183, 78.1828),
    "mehgaon": (26.4921, 78.3812),
    "bhind": (26.5622, 78.7834),
    "jhansi": (25.4484, 78.5685),
    "sagar": (23.8388, 78.7378),
    "nagpur": (21.1458, 79.0882),
    "raipur": (21.2514, 81.6296),
    "vizianagaram": (18.1066, 83.3955),
    "gondia": (21.4624, 80.1982),
    "visakhapatnam": (17.6868, 83.2185),
}

ALIASES: Dict[str, str] = {
    "vizag": "visakhapatnam",
    "vishakhapatnam": "visakhapatnam",
    "visakapatnam": "visakhapatnam",
}

# Main north-south freight corridor, in travel order.
CORRIDOR: List[str] = ["chandigarh", "delhi", "agra", "gwalior", "jhansi", "sagar", "nagpur", "raipur", "vizianagaram", "visakhapatnam"]


def normalize(name: str) -> str:
    key = re.sub(r"\s+", " ", (name or "").strip().lower())
    return ALIASES.get(key, key)


def lookup(name: str) -> Optional[LatLng]:
    return CITIES.get(normalize(name))


def display_name(name: str) -> str:
    return normalize(name).title()


def haversine_km(a: LatLng, b: LatLng) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))
