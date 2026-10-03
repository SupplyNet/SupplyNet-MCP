"""Traffic news / disruption MCP server (port 8001).

Builds on the earlier structured design and adds:
  * shared HTTP client + per-city cache (5 min), concurrent route checks
  * stale headlines dropped (older than NEWS_MAX_AGE_HOURS), duplicates removed, stable live ids
  * live disruptions get coordinates from the shared city table when the city is known
  * user-reported disruptions expire (ttl_hours) instead of living forever; ids can't collide
  * min_severity filter; input validation; seed/mock data can be switched off (NEWS_INCLUDE_MOCK=0)
"""

import asyncio
import hashlib
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Dict, List, Literal, Optional, Tuple

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from . import corridor as cor

load_dotenv()
mcp = FastMCP("NewsServer")

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
HTTP_TIMEOUT = 5.0
CACHE_TTL_OK = 300
CACHE_TTL_FAIL = 60
MAX_AGE_HOURS = float(os.getenv("NEWS_MAX_AGE_HOURS", "72"))
INCLUDE_MOCK = os.getenv("NEWS_INCLUDE_MOCK", "0") == "1"
MAX_CITIES = 25
MAX_CITY_LEN = 80


# ------------------------- models -------------------------

class Severity(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EventType(str, Enum):
    PROTEST_ROADBLOCK = "PROTEST_ROADBLOCK"
    HIGHWAY_REPAIR = "HIGHWAY_REPAIR"
    ACCIDENT = "ACCIDENT"
    FLOOD = "FLOOD"
    OTHER = "OTHER"


SEVERITY_RANK = {Severity.NONE: 0, Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3, Severity.CRITICAL: 4}


class Coordinates(BaseModel):
    lat: float = 0.0
    lng: float = 0.0


class Disruption(BaseModel):
    id: str
    city: str
    location: str
    event_type: EventType
    severity: Severity
    title: str
    details: str
    coordinates: Coordinates = Field(default_factory=Coordinates)
    source: Literal["live", "mock", "user_reported"]
    reported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None


# ------------------------- store -------------------------

_SEED: List[Disruption] = [
    Disruption(
        id="DIS-101", city="Gwalior", location="NH-44 Gwalior Bypass",
        event_type=EventType.PROTEST_ROADBLOCK, severity=Severity.HIGH,
        title="Farmer Protest causing 4-hour traffic standstill on NH-44 near Gwalior",
        details="Major roadblock reported on NH-44 near Gwalior. Heavy vehicles advised to divert via Mehgaon / Bhind corridor.",
        coordinates=Coordinates(lat=26.2183, lng=78.1828), source="mock"),
    Disruption(
        id="DIS-102", city="Nagpur", location="Outer Ring Road Junction",
        event_type=EventType.HIGHWAY_REPAIR, severity=Severity.MEDIUM,
        title="Bridge repair work slowing traffic near Nagpur",
        details="Single lane movement due to flyover joint replacement. Expect 20-30 min delays.",
        coordinates=Coordinates(lat=21.1458, lng=79.0882), source="mock"),
]
STORE: List[Disruption] = list(_SEED) if INCLUDE_MOCK else []


def _active_store() -> List[Disruption]:
    now = datetime.now(timezone.utc)
    STORE[:] = [d for d in STORE if d.expires_at is None or d.expires_at > now]
    return list(STORE)


def _clean_city(city: str) -> str:
    c = (city or "").strip()
    if not c:
        raise ToolError("city must be a non-empty string.")
    if len(c) > MAX_CITY_LEN:
        raise ToolError(f"city too long (max {MAX_CITY_LEN} chars).")
    return c


def city_matches(query_city: str, candidate_city: str, candidate_location: str) -> bool:
    """ONE shared matching rule used by every tool."""
    q = cor.normalize(query_city)
    return q == cor.normalize(candidate_city) or q in candidate_location.lower()


# ------------------------- fetch layer -------------------------

_client: Optional[httpx.AsyncClient] = None
_cache: Dict[str, Tuple[float, List[Tuple[str, Optional[datetime]]]]] = {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": "supply-chain-news-mcp/1.0"})
    return _client


def _parse_pubdate(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def fetch_live_news(city: str, max_results: int = 8) -> List[Tuple[str, Optional[datetime]]]:
    """Raw (headline, published_at) pairs from Google News RSS. Never raises; [] on failure. Cached."""
    key = cor.normalize(city)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    query = f'{city} (highway OR protest OR accident OR flood OR roadblock OR bandh)'
    params = {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    items: List[Tuple[str, Optional[datetime]]] = []
    try:
        resp = await _get_client().get(GOOGLE_NEWS_RSS, params=params)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        for it in root.findall(".//item"):
            title = (it.findtext("title") or "").strip()
            if title:
                items.append((title, _parse_pubdate(it.findtext("pubDate"))))
    except Exception as exc:
        raise RuntimeError(f"Google News unavailable for {city}: {exc}") from exc
    items = items[:max_results]
    _cache[key] = (time.monotonic() + CACHE_TTL_OK, items)
    return items


# ------------------------- classification layer -------------------------

EVENT_KEYWORDS = {
    EventType.FLOOD: ["flood", "waterlog", "cyclone", "heavy rain", "landslide"],
    EventType.PROTEST_ROADBLOCK: ["protest", "bandh", "roadblock", "blockade", "strike", "agitation"],
    EventType.ACCIDENT: ["accident", "collision", "crash", "overturn"],
    EventType.HIGHWAY_REPAIR: ["repair", "construction", "maintenance", "resurfacing", "flyover work"],
}

SEVERITY_KEYWORDS = {
    Severity.CRITICAL: ["shut down", "closed", "collapse", "killed", "died", "washed away"],
    Severity.HIGH: ["standstill", "hours-long", "major", "blocked", "halted"],
    Severity.MEDIUM: ["delay", "delays", "slow", "single lane", "diversion"],
}


def _match_any(text: str, keywords: List[str]) -> bool:
    return any(re.search(rf"\b{re.escape(k)}\b", text) for k in keywords)


def classify_headline(headline: str, city: str, published: Optional[datetime] = None) -> Optional[Disruption]:
    text = headline.lower()
    event_type = next((et for et, kws in EVENT_KEYWORDS.items() if _match_any(text, kws)), None)
    if event_type is None:
        return None
    severity = next((s for s, kws in SEVERITY_KEYWORDS.items() if _match_any(text, kws)), Severity.LOW)
    ll = cor.lookup(city)
    return Disruption(
        id="LIVE-" + hashlib.sha1(f"{cor.normalize(city)}|{headline}".encode()).hexdigest()[:8],
        city=city, location=f"{city} (from live news)", event_type=event_type, severity=severity,
        title=headline, details="Classified automatically from a live news headline; verify before acting.",
        coordinates=Coordinates(lat=ll[0], lng=ll[1]) if ll else Coordinates(),
        source="live", reported_at=published or datetime.now(timezone.utc))


async def get_disruptions_for_city(city: str, min_severity: Severity = Severity.LOW) -> List[Disruption]:
    city = _clean_city(city)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    headlines = await fetch_live_news(city)
    live = [d for h, ts in headlines if (ts is None or ts >= cutoff) and (d := classify_headline(h, city, ts))]
    known = [d for d in _active_store() if city_matches(city, d.city, d.location)]

    merged: Dict[str, Disruption] = {}
    for d in live + known:
        merged.setdefault(d.id, d)
    out = [d for d in merged.values() if SEVERITY_RANK[d.severity] >= SEVERITY_RANK[min_severity]]
    out.sort(key=lambda d: (SEVERITY_RANK[d.severity], d.reported_at), reverse=True)
    return out


# ------------------------- formatting layer -------------------------

def render_text(disruptions: List[Disruption]) -> str:
    if not disruptions:
        return "No active disruptions found."
    lines = [f"=== {len(disruptions)} disruption(s) found ==="]
    for d in disruptions:
        lines.append(f"[{d.severity.value}] {d.title}")
        lines.append(f"  Location: {d.location} | Type: {d.event_type.value} | Source: {d.source}")
    return "\n".join(lines)


# ------------------------- tools -------------------------

@mcp.tool()
async def get_traffic_news(city: str, min_severity: Severity = Severity.LOW) -> List[Disruption]:
    """Active traffic/news disruptions for a city: live headlines plus known and reported alerts, worst first."""
    return await get_disruptions_for_city(city, min_severity)


@mcp.tool()
async def get_traffic_news_summary(city: str) -> str:
    """Same as get_traffic_news but rendered as human-readable text, for display/logging only."""
    return render_text(await get_disruptions_for_city(city))


@mcp.tool()
async def report_disruption(city: str, event_type: EventType, severity: Severity, details: str,
                            lat: float = 0.0, lng: float = 0.0, ttl_hours: float = 12.0) -> Disruption:
    """Report a new disruption. It auto-expires after ttl_hours (0 < ttl <= 168)."""
    city = _clean_city(city)
    if not 0 < ttl_hours <= 168:
        raise ToolError("ttl_hours must be in (0, 168].")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ToolError("lat/lng out of range.")
    ll = cor.lookup(city)
    if lat == 0.0 and lng == 0.0 and ll:
        lat, lng = ll
    event = Disruption(
        id=f"RPT-{uuid.uuid4().hex[:8]}", city=city, location=f"{city} Highway Corridor",
        event_type=event_type, severity=severity,
        title=f"{event_type.value.replace('_', ' ').title()} reported in {city}",
        details=details.strip()[:500], coordinates=Coordinates(lat=lat, lng=lng), source="user_reported",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours))
    STORE.append(event)
    return event


@mcp.tool()
async def check_route_disruptions(cities: List[str], min_severity: Severity = Severity.LOW) -> Dict[str, object]:
    """Check route cities and explicitly report live-feed failures."""
    seen, unique = set(), []
    for c in cities:
        c = _clean_city(c)
        if cor.normalize(c) not in seen:
            seen.add(cor.normalize(c))
            unique.append(c)
    if len(unique) > MAX_CITIES:
        raise ToolError(f"Too many cities (max {MAX_CITIES}).")
    results = await asyncio.gather(*(get_disruptions_for_city(c, min_severity) for c in unique), return_exceptions=True)
    merged: Dict[str, Disruption] = {}
    unavailable: List[str] = []
    for city, result in zip(unique, results):
        if isinstance(result, Exception):
            unavailable.append(city)
            continue
        for d in result:
            merged.setdefault(d.id, d)
    out = list(merged.values())
    out.sort(key=lambda d: (SEVERITY_RANK[d.severity], d.reported_at), reverse=True)
    return {
        "status": "ok" if not unavailable else "partial",
        "source": "live+mock" if INCLUDE_MOCK else "live",
        "disruptions": [d.model_dump(mode="json") for d in out],
        "cities_checked": unique,
        "cities_unavailable": unavailable,
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8001")))