"""OSRM routing MCP server (port 8004).

What changed vs. the original:
  * Unknown places are geocoded (known table -> Nominatim) or rejected. No made-up coordinates.
  * Transit cities / checkpoints are derived from the ACTUAL route geometry, not a hardcoded corridor.
  * If OSRM is unreachable the result is clearly labelled source="estimate" (haversine x road factor).
  * Detour routes report the extra km/hours against the direct route.
  * Structured (Pydantic) results; *_summary tools give text for display only.
"""

from __future__ import annotations

import asyncio
import os
import time
from enum import Enum
from dataclasses import is_dataclass, asdict
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from dotenv import load_dotenv
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from . import corridor as cor

load_dotenv()
mcp = FastMCP("OSRMServer")

OSRM_BASE = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
ENABLE_NOMINATIM = os.getenv("ENABLE_NOMINATIM", "1") == "1"
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))
TRANSIT_RADIUS_KM = float(os.getenv("TRANSIT_RADIUS_KM", "20"))
TRUCK_TIME_FACTOR = float(os.getenv("TRUCK_TIME_FACTOR", "1.25"))  # OSRM demo server uses car speeds
ESTIMATE_ROAD_FACTOR = 1.3
ESTIMATE_TRUCK_KMH = 45.0
MAX_WAYPOINTS = 10
MAX_NAME_LEN = 80
ROUTE_CACHE_TTL = 300


class Priority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ShipmentPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    pin: Optional[str] = None
    name: Optional[str] = None


class Constraints(BaseModel):
    gvw_kg: float = Field(gt=0, le=60000)
    axle_count: int = Field(ge=2, le=10)
    height_m: float = Field(default=0, ge=0, le=10)
    width_m: float = Field(default=0, ge=0, le=10)


class Cargo(BaseModel):
    type: str
    weight_kg: float = Field(ge=0)
    value: float = Field(ge=0)


class Shipment(BaseModel):
    shipment_id: str = Field(min_length=1, max_length=100)
    priority: Priority = Priority.MEDIUM
    constraints: Constraints
    origin: ShipmentPoint
    destination: ShipmentPoint
    cargo: Optional[Cargo] = None


# ------------------------- deterministic optimizer -------------------------

NEWS_MCP_URL = os.getenv("NEWS_MCP_URL", "http://127.0.0.1:8001/mcp")
WEATHER_MCP_URL = os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8002/mcp")
TOLL_MCP_URL = os.getenv("TOLL_MCP_URL", "http://127.0.0.1:8003/mcp")
FUEL_COST_PER_KM = float(os.getenv("FUEL_COST_PER_KM", "30"))
OBJECTIVE_TIME_SCALE_MIN = float(os.getenv("OBJECTIVE_TIME_SCALE_MIN", "3070"))
OBJECTIVE_FUEL_SCALE_RS = float(os.getenv("OBJECTIVE_FUEL_SCALE_RS", "60000"))
OBJECTIVE_TOLL_SCALE_RS = float(os.getenv("OBJECTIVE_TOLL_SCALE_RS", "8000"))
OBJECTIVE_W_TIME = float(os.getenv("OBJECTIVE_W_TIME", "0.35"))
OBJECTIVE_W_FUEL = float(os.getenv("OBJECTIVE_W_FUEL", "0.25"))
OBJECTIVE_W_TOLL = float(os.getenv("OBJECTIVE_W_TOLL", "0.15"))
OBJECTIVE_W_ROAD = float(os.getenv("OBJECTIVE_W_ROAD", "0.15"))
OBJECTIVE_W_WEATHER = float(os.getenv("OBJECTIVE_W_WEATHER", "0.10"))
SEVERITY_SCORE = {"NONE": 0.0, "LOW": 0.15, "MEDIUM": 0.35, "HIGH": 0.65, "CRITICAL": 1.0}


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return _to_plain(asdict(value))
    if hasattr(value, "dict") and callable(value.dict):
        try: return _to_plain(value.dict())
        except Exception: pass
    if hasattr(value, "__dict__") and not isinstance(value, type):
        try: return _to_plain(vars(value))
        except Exception: pass
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_plain(item) for key, item in value.items()}
    return value


async def _call_supplynet_mcp(url: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
    async with Client(url) as client:
        result = await asyncio.wait_for(client.call_tool(tool_name, arguments), timeout=HTTP_TIMEOUT)
        if getattr(result, "is_error", False):
            raise RuntimeError(f"{tool_name} returned an MCP error")
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            data = structured.get("result") if isinstance(structured, dict) and set(structured) == {"result"} else structured
            return _to_plain(data)
        data = getattr(result, "data", None)
        if data is not None:
            return _to_plain(data)
        content = getattr(result, "content", []) or []
        texts = [getattr(item, "text") for item in content if getattr(item, "text", None)]
        if texts:
            return "\n".join(texts)
        raise RuntimeError(f"{tool_name} returned no data")


async def _safe_intelligence_call(url: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
    try:
        return await _call_supplynet_mcp(url, tool_name, arguments)
    except Exception as exc:
        message = str(exc) or repr(exc) or exc.__class__.__name__
        return {"status": "unavailable", "error": message}


async def _reverse_city(lat: float, lon: float) -> str:
    """Reverse-geocode a route sample to a usable city/town/locality name.

    This describes the already-selected OSRM route; it does not decide which
    route OSRM takes.
    """
    try:
        resp = await _get_client().get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat, "lon": lon, "format": "jsonv2",
                "zoom": 12, "addressdetails": 1,
            },
            headers={"User-Agent": "SupplyNet-MCP/1.0 (route checkpoint resolver)"},
        )
        resp.raise_for_status()
        payload = resp.json()
        addr = payload.get("address", {}) or {}
        for key in (
            "city", "town", "municipality", "city_district",
            "suburb", "village", "locality", "county",
        ):
            value = addr.get(key)
            if value:
                return str(value)
        display_name = payload.get("display_name")
        if display_name:
            return str(display_name).split(",")[0].strip()
    except Exception:
        pass
    return f"COORD:{lat:.3f},{lon:.3f}"

async def _checkpoints_from_geometry(geometry: List[List[float]], points: List[GeoPoint], total_km: float) -> List[Checkpoint]:
    """Build checkpoints from actual OSRM geometry and reverse-geocode samples; no corridor table."""
    n=len(geometry)
    if n==0: return []
    cum=[0.0]
    for i in range(1,n): cum.append(cum[-1]+cor.haversine_km((geometry[i-1][1],geometry[i-1][0]),(geometry[i][1],geometry[i][0])))
    scale=total_km/cum[-1] if cum[-1]>0 else 1.0
    indices={0,n-1}
    target_km=max(80.0,total_km/8.0)
    next_target=target_km
    for i,d in enumerate(cum):
        if d>=next_target and i<n-1:
            indices.add(i); next_target+=target_km
    idxs=sorted(indices)
    names=[]
    for i in idxs:
        lon,lat=geometry[i]; names.append(await _reverse_city(lat,lon))
    # Preserve exact shipment endpoints.
    if names: names[0]=points[0].name; names[-1]=points[-1].name
    return [Checkpoint(sequence=j+1,city=name,lat=geometry[i][1],lng=geometry[i][0],km_from_origin=round(cum[i]*scale,1)) for j,(i,name) in enumerate(zip(idxs,names))]

def _road_risk(news: Any) -> Tuple[float, str]:
    """Score only route-relevant news, not every article tagged with a city.

    A news provider can attach a broad city label even when the headline is about
    a different place (for example, a Kullu/Bhiwani story appearing under
    Chandigarh). Such an article must not automatically make the route CRITICAL.
    """
    if not isinstance(news, dict):
        return 1.0, "unavailable"
    status = str(news.get("status", "unavailable"))
    if status == "unavailable":
        return 1.0, "unavailable"

    disruptions = news.get("disruptions", [])
    if not isinstance(disruptions, list):
        return 1.0, "unavailable"

    relevant_scores = []
    checked = {str(c).strip().lower() for c in news.get("cities_checked", []) if c}
    for item in disruptions:
        city = str(_value(item, "city", "")).strip()
        title = str(_value(item, "title", "")).lower()
        details = str(_value(item, "details", "")).lower()
        event_type = str(_value(item, "event_type", "")).upper()
        city_lower = city.lower()

        # Road-closure/traffic events are inherently route relevant when the
        # provider has attached them to a checked route city. For accidents and
        # other events, require the city to appear in the actual headline/details.
        inherently_road_related = event_type in {"ROAD_CLOSURE", "ROADBLOCK", "PROTEST_ROADBLOCK", "FLOOD_ROAD", "LANDSLIDE"}
        headline_mentions_city = city_lower and (city_lower in title or city_lower in details)
        if city_lower not in checked or not (inherently_road_related or headline_mentions_city):
            continue

        relevant_scores.append(
            SEVERITY_SCORE.get(str(_value(item, "severity", "NONE")).upper(), 0.0)
        )

    highest = max(relevant_scores, default=0.0)
    quality = "partial" if status == "partial" else "live"
    return round(highest, 2), quality



def _weather_risk(weather: Any) -> Tuple[float, str]:
    if not isinstance(weather, dict):
        return 1.0, "unavailable"
    status = str(weather.get("status", "unavailable"))
    if status == "unavailable":
        return 1.0, "unavailable"
    observations = weather.get("observations", [])
    if not isinstance(observations, list):
        return 1.0, "unavailable"
    highest = max(
        (SEVERITY_SCORE.get(str(_value(x, "severity", "NONE")).upper(), 0.0) for x in observations),
        default=0.0,
    )
    quality = "partial" if status == "partial" else "live"
    return round(highest, 2), quality


def _toll_values(toll: Any) -> Tuple[int, bool, str]:
    if not isinstance(toll, dict) or toll.get("status") == "unavailable":
        return 0, False, "unavailable"
    return int(toll.get("known_total_rs", 0) or 0), bool(toll.get("complete", False)), str(toll.get("source", toll.get("data_source", "OpenStreetMap/Overpass")))


def _objective_j(duration_minutes: float, fuel_cost: float, toll_cost: float, road_risk: float, weather_risk: float) -> float:
    time_n = min(max(duration_minutes, 0) / OBJECTIVE_TIME_SCALE_MIN, 1.0)
    fuel_n = min(max(fuel_cost, 0) / OBJECTIVE_FUEL_SCALE_RS, 1.0)
    toll_n = min(max(toll_cost, 0) / OBJECTIVE_TOLL_SCALE_RS, 1.0)
    return round(10 * (OBJECTIVE_W_TIME*time_n + OBJECTIVE_W_FUEL*fuel_n + OBJECTIVE_W_TOLL*toll_n + OBJECTIVE_W_ROAD*road_risk + OBJECTIVE_W_WEATHER*weather_risk), 2)


async def _score_candidate(route: dict, shipment: Shipment, index: int, include_geometry: bool) -> dict:
    origin = GeoPoint(name=shipment.origin.name or "Origin", lat=shipment.origin.lat, lng=shipment.origin.lon, source="known")
    destination = GeoPoint(name=shipment.destination.name or "Destination", lat=shipment.destination.lat, lng=shipment.destination.lon, source="known")
    checkpoints = await _checkpoints_from_geometry(route["geometry"], [origin, destination], float(route["distance_km"]))
    # Checkpoints come from the actual OSRM geometry + reverse geocoding.
    # Do not replace them with a hardcoded nearest-city/corridor lookup.
    cities = [
        c.city for c in checkpoints
        if c.city and not c.city.startswith("COORD:")
    ]
    vehicle_type = "lcv" if shipment.constraints.gvw_kg < 12000 else "hcv"
    if not cities:
        cities = [shipment.origin.name or "Origin", shipment.destination.name or "Destination"]
    news, weather, toll = await asyncio.gather(
        _safe_intelligence_call(NEWS_MCP_URL, "check_route_disruptions", {"cities": cities, "min_severity": "LOW"}),
        _safe_intelligence_call(WEATHER_MCP_URL, "check_route_weather_hazards", {"cities": cities}),
        _safe_intelligence_call(TOLL_MCP_URL, "calculate_toll_cost", {"route_geometry": route["geometry"], "vehicle_type": vehicle_type}),
    )
    road_risk, news_quality = _road_risk(news)
    weather_risk, weather_quality = _weather_risk(weather)
    toll_cost, toll_complete, toll_quality = _toll_values(toll)
    # An incomplete toll result is a lower bound, not evidence that the route is free.
    # Keep the verified amount in the output, but apply a conservative objective penalty
    # when the provider has no usable toll data at all.
    toll_score = OBJECTIVE_TOLL_SCALE_RS if toll_quality == "unavailable" else toll_cost
    distance = float(route["distance_km"])
    raw_minutes = float(route["duration_h"]) * 60
    truck_minutes = raw_minutes * TRUCK_TIME_FACTOR
    fuel = round(distance * FUEL_COST_PER_KM)
    j = _objective_j(truck_minutes, fuel, toll_score, road_risk, weather_risk)
    partial = news_quality in ("unavailable", "partial") or weather_quality in ("unavailable", "partial") or toll_quality == "unavailable" or not toll_complete
    return {
        "route_id": f"route_{index+1}", "distance_km": round(distance), "duration_minutes": round(raw_minutes),
        "truck_duration_minutes": round(truck_minutes), "fuel_cost": fuel, "toll_cost": toll_cost,
        "road_risk_score": road_risk, "weather_risk_score": weather_risk, "objective_j_score": j,
        "partial": partial,
        "checkpoints": [{"order": i+1, "city_name": c.city, "lat": c.lat, "lon": c.lng, "km_from_origin": c.km_from_origin} for i,c in enumerate(checkpoints)],
        "geometry": {"type": "LineString", "coordinates": route["geometry"]} if include_geometry else None,
        "evidence": {"news": news, "weather": weather, "toll": toll},
        "data_quality": {"route_source": "OSRM", "news": news_quality, "weather": weather_quality,
                         "toll": "complete" if toll_complete else ("unavailable" if toll_quality == "unavailable" else "partial"),
                         "fuel": "configured_flat_rate", "toll_is_lower_bound": not toll_complete and toll_quality != "unavailable"}
    }


@mcp.tool()
async def optimize_route(shipment: Shipment, include_geometry: bool = False) -> Dict[str, Any]:
    '''Deterministically score up to three OSRM alternatives and select the minimum J route. Gemini interprets; the server calculates.'''
    separation = cor.haversine_km((shipment.origin.lat, shipment.origin.lon), (shipment.destination.lat, shipment.destination.lon))
    if separation < 1.0:
        raise ToolError("Origin and destination are identical or less than 1 km apart.")
    origin = GeoPoint(name=shipment.origin.name or "Origin", lat=shipment.origin.lat, lng=shipment.origin.lon, source="known")
    destination = GeoPoint(name=shipment.destination.name or "Destination", lat=shipment.destination.lat, lng=shipment.destination.lon, source="known")
    routes = await _osrm_routes([origin, destination])
    if not routes:
        raise ToolError("OSRM could not calculate a driving route.")
    scored = await asyncio.gather(*(_score_candidate(route, shipment, i, include_geometry) for i, route in enumerate(routes)))
    selected = min(scored, key=lambda x: x["objective_j_score"])
    alternatives = [{k: r[k] for k in ("route_id","distance_km","duration_minutes","truck_duration_minutes","fuel_cost","toll_cost","road_risk_score","weather_risk_score","objective_j_score","partial")} for r in scored]
    return {
        "shipment_id": shipment.shipment_id,
        "status": "PARTIAL" if selected["partial"] else "SUCCESS",
        "selected_route": {k: selected[k] for k in ("route_id","distance_km","duration_minutes","truck_duration_minutes","fuel_cost","toll_cost","road_risk_score","weather_risk_score","objective_j_score","geometry")},
        "checkpoints": selected["checkpoints"], "alternatives": alternatives, "evidence": selected["evidence"],
        "data_quality": {**selected["data_quality"], "candidate_count": len(routes), "selection": "minimum objective_j_score"},
        "missing_inputs": sorted(set(
            selected["evidence"].get("toll", {}).get("unmatched_cities", [])
            if isinstance(selected["evidence"].get("toll"), dict) else []
        ))
    }


# ------------------------- models -------------------------

class GeoPoint(BaseModel):
    name: str
    lat: float
    lng: float
    source: Literal["known", "nominatim"]


class Checkpoint(BaseModel):
    sequence: int
    city: str
    lat: float
    lng: float
    km_from_origin: Optional[float] = None


class RouteResult(BaseModel):
    origin: str
    destination: str
    waypoints: List[str] = Field(default_factory=list)
    distance_km: float
    duration_hours: float            # raw engine duration
    truck_duration_hours: float      # duration_hours x TRUCK_TIME_FACTOR (OSRM) or estimate speed
    checkpoints: List[Checkpoint]
    source: Literal["osrm", "estimate"]
    note: Optional[str] = None
    extra_km: Optional[float] = None       # detour only: vs direct route
    extra_hours: Optional[float] = None


# ------------------------- geocoding -------------------------

_client: Optional[httpx.AsyncClient] = None
_geo_cache: Dict[str, Optional[GeoPoint]] = {}
_route_cache: Dict[tuple, Tuple[float, RouteResult]] = {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": "supply-chain-osrm-mcp/1.0"})
    return _client


def _clean(name: str) -> str:
    n = (name or "").strip()
    if not n:
        raise ToolError("Location name must be a non-empty string.")
    if len(n) > MAX_NAME_LEN:
        raise ToolError(f"Location name too long (max {MAX_NAME_LEN} chars).")
    return n


async def geocode(name: str) -> GeoPoint:
    name = _clean(name)
    known = cor.lookup(name)
    if known:
        return GeoPoint(name=cor.display_name(name), lat=known[0], lng=known[1], source="known")

    key = cor.normalize(name)
    if key in _geo_cache:
        cached = _geo_cache[key]
        if cached:
            return cached
        raise ToolError(f"Could not geocode '{name}'.")

    if ENABLE_NOMINATIM:
        try:
            resp = await _get_client().get(
                NOMINATIM_URL, params={"q": f"{name}, India", "format": "json", "limit": 1})
            resp.raise_for_status()
            hits = resp.json()
            if hits:
                pt = GeoPoint(name=name.title(), lat=float(hits[0]["lat"]), lng=float(hits[0]["lon"]),
                              source="nominatim")
                _geo_cache[key] = pt
                return pt
        except Exception:
            pass
    _geo_cache[key] = None
    raise ToolError(f"Could not geocode '{name}': not in the known city table and lookup failed. "
                    f"Known cities: {', '.join(sorted(cor.CITIES))}.")


# ------------------------- routing -------------------------

async def _osrm_routes(points: List[GeoPoint]) -> List[dict]:
    coords = ";".join(f"{p.lng},{p.lat}" for p in points)
    try:
        resp = await _get_client().get(f"{OSRM_BASE}/route/v1/driving/{coords}",
            params={"overview":"full", "geometries":"geojson", "steps":"false", "alternatives":"true"})
        if resp.status_code != 200:
            return []
        data = resp.json()
        if data.get("code") != "Ok":
            return []
        return [{"distance_km": r["distance"]/1000.0, "duration_h": r["duration"]/3600.0,
                 "geometry": r["geometry"]["coordinates"]} for r in data.get("routes", [])[:3]]
    except Exception:
        return []


async def _osrm_route(points: List[GeoPoint]) -> Optional[dict]:
    routes = await _osrm_routes(points)
    return routes[0] if routes else None

def _estimate(points: List[GeoPoint]) -> RouteResult:
    o, d = points[0], points[-1]
    path: List[Tuple[str, float, float]] = [(p.name, p.lat, p.lng) for p in points]
    cps, total = [], 0.0
    for s, (name, lat, lng) in enumerate(path):
        if s:
            total += cor.haversine_km((path[s - 1][1], path[s - 1][2]), (lat, lng)) * ESTIMATE_ROAD_FACTOR
        cps.append(Checkpoint(sequence=s + 1, city=name, lat=lat, lng=lng, km_from_origin=round(total, 1)))
    hours = total / ESTIMATE_TRUCK_KMH
    return RouteResult(origin=o.name, destination=d.name, distance_km=round(total, 1),
                       duration_hours=round(hours, 1), truck_duration_hours=round(hours, 1),
                       checkpoints=cps, source="estimate",
                       note="OSRM unreachable: straight-line x1.3 road factor at 45 km/h. Treat as a rough estimate.")


async def _plan(origin: str, waypoints: List[str], destination: str) -> RouteResult:
    if len(waypoints) > MAX_WAYPOINTS:
        raise ToolError(f"Too many waypoints (max {MAX_WAYPOINTS}).")
    names = [origin, *waypoints, destination]
    points = list(await asyncio.gather(*(geocode(n) for n in names)))
    cache_key = tuple((round(p.lat, 4), round(p.lng, 4)) for p in points)
    hit = _route_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1].model_copy(deep=True)

    live = await _osrm_route(points)
    if live:
        result = RouteResult(
            origin=points[0].name, destination=points[-1].name, waypoints=[p.name for p in points[1:-1]],
            distance_km=round(live["distance_km"], 1), duration_hours=round(live["duration_h"], 1),
            truck_duration_hours=round(live["duration_h"] * TRUCK_TIME_FACTOR, 1),
            checkpoints=await _checkpoints_from_geometry(live["geometry"], points, live["distance_km"]),
            source="osrm")
    else:
        result = _estimate(points)
        result.waypoints = [p.name for p in points[1:-1]]
    _route_cache[cache_key] = (time.monotonic() + ROUTE_CACHE_TTL, result)
    return result.model_copy(deep=True)


def render_text(r: RouteResult) -> str:
    lines = [f"=== Route: {r.origin} -> {r.destination}" + (f" via {', '.join(r.waypoints)}" if r.waypoints else "")
             + f" [{r.source}] ===",
             f"Distance: {r.distance_km} km | Truck time: {r.truck_duration_hours} h (raw {r.duration_hours} h)",
             "Path: " + " -> ".join(c.city for c in r.checkpoints)]
    if r.extra_km is not None:
        lines.append(f"Detour cost vs direct: {r.extra_km:+} km, {r.extra_hours:+} h")
    if r.note:
        lines.append(f"Note: {r.note}")
    return "\n".join(lines)


# ------------------------- tools -------------------------

@mcp.tool()
async def geocode_location(location_name: str) -> GeoPoint:
    """Convert a city name to latitude/longitude. Known supply-chain nodes first, then OpenStreetMap Nominatim."""
    return await geocode(location_name)


@mcp.tool()
async def get_route(origin: str, destination: str) -> RouteResult:
    """Primary driving route: distance, duration, truck-adjusted duration and ordered transit-city checkpoints."""
    return await _plan(origin, [], destination)


@mcp.tool()
async def get_route_summary(origin: str, destination: str) -> str:
    """Same as get_route but rendered as human-readable text, for display/logging only."""
    return render_text(await _plan(origin, [], destination))


@mcp.tool()
async def get_route_with_waypoints(origin: str, waypoints: List[str], destination: str) -> RouteResult:
    """Detour route forced through intermediate waypoints; reports extra km/hours vs the direct route."""
    if not waypoints:
        raise ToolError("Provide at least one waypoint, or use get_route.")
    direct, detour = await asyncio.gather(_plan(origin, [], destination), _plan(origin, waypoints, destination))
    if direct.source == detour.source:
        detour.extra_km = round(detour.distance_km - direct.distance_km, 1)
        detour.extra_hours = round(detour.truck_duration_hours - direct.truck_duration_hours, 1)
    else:
        detour.note = ((detour.note + " ") if detour.note else "") + \
            "Extra km/hours omitted: direct and detour came from different engines."
    return detour


@mcp.tool()
async def extract_route_checkpoints(origin: str, destination: str) -> List[Checkpoint]:
    """Ordered transit-city checkpoints along the real route, for truck spatial tracking."""
    return (await _plan(origin, [], destination)).checkpoints


@mcp.tool()
async def get_route_options(origin: str, destination: str) -> Dict[str, Any]:
    """Return all OSRM alternatives before intelligence scoring. Useful for debugging and viva demos."""
    points=[await geocode(origin), await geocode(destination)]
    routes=await _osrm_routes(points)
    return {"source":"OSRM","candidate_count":len(routes),"routes":[{"route_id":f"route_{i+1}","distance_km":round(r["distance_km"],1),"duration_minutes":round(r["duration_h"]*60),"geometry_points":len(r["geometry"])} for i,r in enumerate(routes)]}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8004")))