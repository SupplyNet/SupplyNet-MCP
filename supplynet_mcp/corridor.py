"""Geographic helpers for SupplyNet.

This module intentionally does NOT define the route corridor. OSRM decides the
actual road and Nominatim resolves locations that are not already cached.
"""
import math
import re
from typing import Optional, Tuple

ALIASES = {
    "new delhi": "delhi",
    "vizag": "visakhapatnam",
    "vishakhapatnam": "visakhapatnam",
    "gurgaon": "gurugram",
}

def normalize(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[,_]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return ALIASES.get(value, value)

def display_name(value: str) -> str:
    return normalize(value).title()

def lookup(value: str) -> Optional[Tuple[float, float]]:
    # No geographic route/city database is hardcoded here.
    # OSRM server uses Nominatim for geocoding.
    return None

def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = a; lat2, lon2 = b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2-lat1); dl = math.radians(lon2-lon1)
    x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.atan2(math.sqrt(x), math.sqrt(1-x))
