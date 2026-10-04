"""SupplyNet Toll MCP - keyless OpenStreetMap toll lookup.

Uses live OpenStreetMap/Overpass data instead of TollGuru. The MCP searches
for toll booths close to the actual OSRM route geometry and uses a mapped
vehicle-specific/general `charge` tag when available.

Important:
- This is NOT a guaranteed tariff API. OSM coverage and charge tags can be
  incomplete or stale.
- Unknown tolls are never treated as Rs. 0 with `complete=True`.
- If a plaza is found without a usable charge, the result is marked partial
  and `lower_bound=True`.
"""

import math
import os
import re
import asyncio
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

mcp = FastMCP("SupplyNet Toll MCP")

OVERPASS_URLS = [
    os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_TIMEOUT = float(os.getenv("OVERPASS_TIMEOUT_SECONDS", "20"))
ROUTE_MATCH_KM = float(os.getenv("TOLL_ROUTE_MATCH_KM", "5.0"))
MAX_ROUTE_SAMPLES = int(os.getenv("TOLL_MAX_ROUTE_SAMPLES", "48"))
SAMPLES_PER_QUERY = int(os.getenv("TOLL_SAMPLES_PER_QUERY", "12"))
MAX_CONCURRENT_QUERIES = int(os.getenv("TOLL_MAX_CONCURRENT_QUERIES", "3"))


class TollLine(BaseModel):
    name: str
    amount_rs: float
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    payment_method: Optional[str] = None
    osm_id: Optional[str] = None
    charge_source: Optional[str] = None


class TollEstimate(BaseModel):
    status: Literal["ok", "unavailable", "error"]
    source: str
    vehicle_type: str
    lines: List[TollLine] = Field(default_factory=list)
    known_total_rs: Optional[float] = None
    complete: bool = False
    lower_bound: bool = False
    error: Optional[str] = None


class TollComparison(BaseModel):
    vehicle_type: str
    main: TollEstimate
    alternate: TollEstimate
    difference_rs: Optional[float] = None
    cheaper: Literal["main", "alternate", "tie", "unknown"]
    comparable: bool
    note: Optional[str] = None


def _vehicle_type(value: str) -> str:
    key = (value or "hcv").strip().lower()
    aliases = {"hcv": "hcv", "truck": "hcv", "heavy": "hcv", "lcv": "lcv", "light": "lcv"}
    if key not in aliases:
        raise ToolError("Unsupported vehicle_type. Use hcv or lcv.")
    return aliases[key]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _min_route_distance_km(lat: float, lon: float, geometry: List[List[float]]) -> float:
    # OSRM geometry is dense enough for a nearest-point route match. This is
    # deliberately conservative; we only use it to reject unrelated tolls.
    best = float("inf")
    for point in geometry:
        if len(point) < 2:
            continue
        d = _haversine_km(lat, lon, float(point[1]), float(point[0]))
        if d < best:
            best = d
    return best


def _overpass_query_text(samples: List[List[float]]) -> str:
    """Build a compact Overpass query around sampled route points."""
    clauses = []
    for point in samples:
        lon, lat = float(point[0]), float(point[1])
        # 5 km search radius around each actual route sample.
        clauses.append(
            f'node["barrier"="toll_booth"](around:5000,{lat},{lon});'
        )
        clauses.append(
            f'node["highway"="toll_booth"](around:5000,{lat},{lon});'
        )

    return "[out:json][timeout:20];\n(\n" + "\n".join(clauses) + "\n);\nout body;"


async def _overpass_query(samples: List[List[float]]):
    query = _overpass_query_text(samples)
    last_error = None

    async with httpx.AsyncClient(
        timeout=OVERPASS_TIMEOUT,
        follow_redirects=True,
    ) as client:
        for url in OVERPASS_URLS:
            try:
                response = await client.post(
                    url,
                    data={"data": query},
                )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc) or repr(exc)

    raise RuntimeError(last_error or "Overpass request failed")


async def _calculate_osm_tolls(route_geometry: List[List[float]], vehicle_type: str) -> TollEstimate:
    vehicle = _vehicle_type(vehicle_type)

    if not route_geometry or len(route_geometry) < 2:
        return TollEstimate(
            status="error",
            source="OpenStreetMap/Overpass",
            vehicle_type=vehicle,
            error="route_geometry must contain at least two points.",
        )

    samples = _sample_route_geometry(route_geometry)
    if not samples:
        return TollEstimate(
            status="error",
            source="OpenStreetMap/Overpass",
            vehicle_type=vehicle,
            error="No valid route geometry points.",
        )

    # Split samples into a few compact Overpass requests.
    groups = [
        samples[i:i + SAMPLES_PER_QUERY]
        for i in range(0, len(samples), SAMPLES_PER_QUERY)
    ]

    found: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)

    async def fetch_group(group):
        async with semaphore:
            try:
                return await _overpass_query(group), None
            except Exception as exc:
                return None, str(exc) or repr(exc) or exc.__class__.__name__

    results = await asyncio.gather(*(fetch_group(group) for group in groups))

    for payload, error in results:
        if error:
            errors.append(error)
            continue

        for element in payload.get("elements", []):
            lat = element.get("lat")
            lon = element.get("lon")
            if lat is None or lon is None:
                continue

            if _min_route_distance_km(
                float(lat),
                float(lon),
                route_geometry,
            ) > ROUTE_MATCH_KM:
                continue

            tags = element.get("tags", {}) or {}
            osm_key = f"node/{element.get('id')}"
            name = (
                tags.get("name")
                or tags.get("ref")
                or f"OSM toll booth {element.get('id')}"
            )

            amount, charge_source = _extract_charge(tags, vehicle)

            found[osm_key] = {
                "name": name,
                "lat": float(lat),
                "lon": float(lon),
                "amount": amount,
                "charge_source": charge_source,
                "payment": (
                    tags.get("payment:electronic")
                    or tags.get("payment:fastag")
                ),
            }

    if not found:
        if errors:
            return TollEstimate(
                status="unavailable",
                source="OpenStreetMap/Overpass",
                vehicle_type=vehicle,
                error="Overpass lookup failed: " + errors[0],
            )

        return TollEstimate(
            status="ok",
            source="OpenStreetMap/Overpass",
            vehicle_type=vehicle,
            lines=[],
            known_total_rs=0.0,
            complete=False,
            lower_bound=True,
            error=(
                "No mapped toll booth was found near this route; "
                "OSM coverage may be incomplete."
            ),
        )

    lines = []
    unknown_count = 0

    for osm_id, item in sorted(
        found.items(),
        key=lambda x: x[1]["name"],
    ):
        amount = item["amount"]

        if amount is None:
            unknown_count += 1
            continue

        lines.append(
            TollLine(
                name=item["name"],
                amount_rs=round(amount, 2),
                latitude=item["lat"],
                longitude=item["lon"],
                payment_method=item["payment"],
                osm_id=osm_id,
                charge_source=item["charge_source"],
            )
        )

    known_total = round(
        sum(x.amount_rs for x in lines),
        2,
    )

    complete = unknown_count == 0 and not errors

    return TollEstimate(
        status="ok",
        source="OpenStreetMap/Overpass",
        vehicle_type=vehicle,
        lines=lines,
        known_total_rs=known_total,
        complete=complete,
        lower_bound=not complete,
        error=(
            f"{unknown_count} mapped toll booth(s) had no usable charge tag."
            if unknown_count
            else (errors[0] if errors else None)
        ),
    )


@mcp.tool()
async def calculate_toll_cost(route_geometry: List[List[float]], vehicle_type: str = "hcv") -> TollEstimate:
    """Estimate tolls for the exact OSRM route using live OpenStreetMap toll data."""
    return await _calculate_osm_tolls(route_geometry, vehicle_type)


@mcp.tool()
async def calculate_toll_cost_summary(route_geometry: List[List[float]], vehicle_type: str = "hcv") -> str:
    result = await _calculate_osm_tolls(route_geometry, vehicle_type)
    if result.status != "ok":
        return f"TOLL DATA {result.status.upper()}\nSource: {result.source}\nError: {result.error}"
    out = [f"=== OSM TOLL ESTIMATE ({result.vehicle_type.upper()}) ===", f"Source: {result.source}"]
    for line in result.lines:
        out.append(f"- {line.name}: Rs. {line.amount_rs:.2f}")
    out.append(f"Known toll total: Rs. {result.known_total_rs:.2f}")
    out.append(f"Complete: {result.complete}")
    if result.error:
        out.append(f"Note: {result.error}")
    return "\n".join(out)


@mcp.tool()
async def compare_toll_routes(main_route_geometry: List[List[float]], alternate_route_geometry: List[List[float]], vehicle_type: str = "hcv") -> TollComparison:
    main, alternate = await _calculate_osm_tolls(main_route_geometry, vehicle_type), await _calculate_osm_tolls(alternate_route_geometry, vehicle_type)
    comparable = main.status == "ok" and alternate.status == "ok" and main.complete and alternate.complete and main.known_total_rs is not None and alternate.known_total_rs is not None
    if not comparable:
        return TollComparison(vehicle_type=_vehicle_type(vehicle_type), main=main, alternate=alternate, difference_rs=None, cheaper="unknown", comparable=False, note="OSM toll data is incomplete for one or both routes; comparison is not authoritative.")
    difference = round(alternate.known_total_rs - main.known_total_rs, 2)
    cheaper = "tie" if difference == 0 else "alternate" if difference < 0 else "main"
    return TollComparison(vehicle_type=_vehicle_type(vehicle_type), main=main, alternate=alternate, difference_rs=difference, cheaper=cheaper, comparable=True)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=os.getenv("MCP_HOST", "127.0.0.1"), port=int(os.getenv("MCP_PORT", "8003")))
