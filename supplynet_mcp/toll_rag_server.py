"""Toll advisory + cost MCP server (port 8003).

What changed vs. the original:
  * Retrieval is real TF-IDF over tokenized title/tags/content (was raw substring matching).
  * Plazas are keyed by city explicitly, so "Gwalior" can never accidentally match the Mehgaon bypass doc.
  * Unknown cities are reported as unmatched. No invented Rs. 200 default; totals say if they are complete.
  * Unknown vehicle types are rejected instead of silently halving the price.
  * compare_toll_routes answers the reroute question: what does the detour cost vs the main route?
  * Data can be replaced without code changes via TOLL_DATA_PATH (JSON list of advisories).

NOTE: the built-in rates are demo data. Load real NHAI figures with TOLL_DATA_PATH before relying on totals.
"""

import json
import math
import os
import re
from collections import Counter
from typing import Dict, List, Literal, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from . import corridor as cor

mcp = FastMCP("TollRAGServer")

MAX_CITIES = 25
MAX_QUERY_LEN = 300
VEHICLE_MULTIPLIERS: Dict[str, float] = {"hcv": 1.0, "truck": 1.0, "heavy": 1.0, "lcv": 0.5, "light": 0.5}


# ------------------------- models & data -------------------------

class TollAdvisory(BaseModel):
    id: str
    title: str
    city: str                       # plaza city; normalized with corridor.normalize
    content: str
    cost_hcv: int = Field(ge=0)
    tags: List[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    advisory: TollAdvisory
    score: float


class TollLine(BaseModel):
    city: str
    advisory_id: str
    plaza: str
    cost_rs: int


class TollEstimate(BaseModel):
    vehicle_type: str
    lines: List[TollLine]
    known_total_rs: int
    unmatched_cities: List[str]     # no plaza on file: NOT free, just unknown
    complete: bool


class TollComparison(BaseModel):
    vehicle_type: str
    main: TollEstimate
    alternate: TollEstimate
    difference_rs: int              # alternate - main (negative = alternate is cheaper)
    cheaper: Literal["main", "alternate", "tie", "unknown"]
    comparable: bool
    note: Optional[str] = None


DEFAULT_ADVISORIES = [
    TollAdvisory(id="KB-TOLL-01", title="NH-44 Gwalior - Jhansi Toll Corridor Rates", city="gwalior", cost_hcv=385,
                 content="For 4-axle to 6-axle Heavy Commercial Vehicles (HCV/Trucks), the toll fee at Gwalior Toll Plaza "
                         "is Rs. 385. FASTag lane 3 and 4 are operational 24/7. Cash payments incur a 100% penalty rate.",
                 tags=["gwalior", "nh-44", "jhansi", "hcv", "truck", "rate", "fastag"]),
    TollAdvisory(id="KB-TOLL-02", title="Mehgaon Bypass Highway Circular (State Highway 19)", city="mehgaon", cost_hcv=140,
                 content="State Highway 19 via Mehgaon and Bhind serves as an approved commercial bypass during NH-44 "
                         "disruptions in Gwalior. The Mehgaon toll booth charges Rs. 140 for multi-axle trucks. Road weight "
                         "clearance is rated up to 40 metric tons.",
                 tags=["mehgaon", "bhind", "bypass", "detour", "sh-19", "gwalior", "toll"]),
    TollAdvisory(id="KB-TOLL-03", title="Agra - Gwalior Expressway Toll Charges", city="agra", cost_hcv=420,
                 content="Toll rate for heavy trucks at Agra Plaza on NH-44 is Rs. 420. Dynamic tolling applies during peak "
                         "rush hours (08:00 to 11:00 AM). Ensure minimum FASTag balance of Rs. 1,000.",
                 tags=["agra", "gwalior", "expressway", "hcv", "truck", "fastag"]),
    TollAdvisory(id="KB-TOLL-04", title="Nagpur Outer Ring Road Toll Advisory", city="nagpur", cost_hcv=260,
                 content="Commercial vehicles bypassing Nagpur city center via Outer Ring Road pay a flat toll fee of Rs. 260. "
                         "Overloaded trucks exceeding axle load limits will be turned back at weighbridge #2.",
                 tags=["nagpur", "ring road", "weighbridge", "toll", "hcv", "truck"]),
    TollAdvisory(id="KB-TOLL-05", title="Visakhapatnam Port Highway Toll & Entry Policy", city="visakhapatnam", cost_hcv=310,
                 content="Port entry highway toll at Visakhapatnam for freight trucks is Rs. 310. RFID gate scanning is "
                         "mandatory. Container trucks must present valid e-way bill documentation at checkpost.",
                 tags=["visakhapatnam", "port", "freight", "truck", "toll", "e-way"]),
]


def _load_advisories() -> List[TollAdvisory]:
    path = os.getenv("TOLL_DATA_PATH")
    if not path:
        return DEFAULT_ADVISORIES
    with open(path, encoding="utf-8") as f:
        return [TollAdvisory(**row) for row in json.load(f)]


ADVISORIES: List[TollAdvisory] = _load_advisories()
BY_CITY: Dict[str, List[TollAdvisory]] = {}
for _a in ADVISORIES:
    BY_CITY.setdefault(cor.normalize(_a.city), []).append(_a)


# ------------------------- retrieval (TF-IDF) -------------------------

_STOP = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "by", "via", "is", "are", "be", "with",
         "what", "how", "much", "does", "do", "rs", "from", "any", "which", "that", "this", "it"}
_SYN = {"trucks": "truck", "hcvs": "hcv", "heavy": "hcv", "vizag": "visakhapatnam", "vishakhapatnam": "visakhapatnam",
        "fees": "fee", "rates": "rate", "charges": "toll", "charge": "toll", "tolls": "toll",
        "detours": "detour", "bypasses": "bypass", "cost": "toll", "price": "toll"}


def _tokens(text: str) -> List[str]:
    out = []
    for tok in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower()):
        for part in ([tok] + tok.split("-")) if "-" in tok else [tok]:
            part = _SYN.get(part, part)
            if part not in _STOP:
                out.append(part)
    return out


def _build_index(docs: List[TollAdvisory]):
    tfs: List[Counter] = []
    for d in docs:
        tf: Counter = Counter()
        for t in _tokens(d.title) + _tokens(d.city):
            tf[t] += 3
        for tag in d.tags:
            for t in _tokens(tag):
                tf[t] += 2
        for t in _tokens(d.content):
            tf[t] += 1
        tfs.append(tf)
    n = len(docs)
    df: Counter = Counter()
    for tf in tfs:
        df.update(tf.keys())
    idf = {t: math.log((n + 1) / (c + 0.5)) + 1 for t, c in df.items()}
    return tfs, idf


_TFS, _IDF = _build_index(ADVISORIES)


def search(query: str, top_k: int = 3) -> List[SearchHit]:
    query = (query or "").strip()
    if not query:
        raise ToolError("Query must be non-empty.")
    if len(query) > MAX_QUERY_LEN:
        raise ToolError(f"Query too long (max {MAX_QUERY_LEN} chars).")
    q = set(_tokens(query))
    hits = []
    for doc, tf in zip(ADVISORIES, _TFS):
        score = sum(_IDF[t] * tf[t] / (tf[t] + 1.5) for t in q if t in tf)
        if score > 0:
            hits.append(SearchHit(advisory=doc, score=round(score, 3)))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:max(1, min(top_k, 10))]


# ------------------------- cost logic -------------------------

def _multiplier(vehicle_type: str) -> float:
    v = (vehicle_type or "").strip().lower()
    if v not in VEHICLE_MULTIPLIERS:
        raise ToolError(f"Unsupported vehicle_type '{vehicle_type}'. Use one of: {', '.join(sorted(VEHICLE_MULTIPLIERS))}.")
    return VEHICLE_MULTIPLIERS[v]


def _estimate(route_cities: List[str], vehicle_type: str) -> TollEstimate:
    if not route_cities:
        raise ToolError("route_cities must not be empty.")
    if len(route_cities) > MAX_CITIES:
        raise ToolError(f"Too many cities (max {MAX_CITIES}).")
    mult = _multiplier(vehicle_type)
    lines, unmatched, seen = [], [], set()
    for city in route_cities:
        key = cor.normalize(city)
        if not key or key in seen:
            continue
        seen.add(key)
        plazas = BY_CITY.get(key)
        if not plazas:
            unmatched.append(cor.display_name(city))
            continue
        for a in plazas:
            lines.append(TollLine(city=key.title(), advisory_id=a.id, plaza=a.title,
                                  cost_rs=int(round(a.cost_hcv * mult))))
    return TollEstimate(vehicle_type=vehicle_type.strip().lower(), lines=lines,
                        known_total_rs=sum(l.cost_rs for l in lines),
                        unmatched_cities=unmatched, complete=not unmatched)


def render_hits(query: str, hits: List[SearchHit]) -> str:
    if not hits:
        return f"No toll advisories match '{query}'. Standard NHAI rates apply."
    out = [f"=== Toll advisories for '{query}' ==="]
    for h in hits:
        a = h.advisory
        out += [f"[{a.id}] {a.title} (score {h.score})", f"  {a.content}", f"  HCV toll: Rs. {a.cost_hcv}"]
    return "\n".join(out)


def render_estimate(e: TollEstimate) -> str:
    out = [f"=== TOLL COST ESTIMATE ({e.vehicle_type.upper()}) ==="]
    out += [f"- {l.city} ({l.advisory_id}): Rs. {l.cost_rs}" for l in e.lines]
    out.append(f"Known total: Rs. {e.known_total_rs}" + ("" if e.complete else "  (INCOMPLETE)"))
    if e.unmatched_cities:
        out.append("No plaza data for: " + ", ".join(e.unmatched_cities))
    return "\n".join(out)


# ------------------------- tools -------------------------

@mcp.tool()
async def search_toll_advisories(query: str, top_k: int = 3) -> List[SearchHit]:
    """TF-IDF search over toll plaza advisories, FASTag rules and highway detour regulations."""
    return search(query, top_k)


@mcp.tool()
async def search_toll_advisories_summary(query: str, top_k: int = 3) -> str:
    """Same as search_toll_advisories but rendered as text, for display/logging only."""
    return render_hits(query, search(query, top_k))


@mcp.tool()
async def calculate_toll_cost(route_cities: List[str], vehicle_type: str = "hcv") -> TollEstimate:
    """Toll estimate for the plazas of the given cities. Cities without plaza data are listed as
    unmatched and the result is flagged complete=false (they are unknown, not free)."""
    return _estimate(route_cities, vehicle_type)


@mcp.tool()
async def calculate_toll_cost_summary(route_cities: List[str], vehicle_type: str = "hcv") -> str:
    """Same as calculate_toll_cost but rendered as text, for display/logging only."""
    return render_estimate(_estimate(route_cities, vehicle_type))


@mcp.tool()
async def compare_toll_routes(main_route_cities: List[str], alternate_route_cities: List[str],
                              vehicle_type: str = "hcv") -> TollComparison:
    """Compare toll cost of the main route vs a detour (e.g. NH-44 via Gwalior vs SH-19 via Mehgaon)."""
    main = _estimate(main_route_cities, vehicle_type)
    alt = _estimate(alternate_route_cities, vehicle_type)
    comparable = main.complete and alt.complete
    diff = alt.known_total_rs - main.known_total_rs
    cheaper = "unknown" if not comparable else "tie" if diff == 0 else "alternate" if diff < 0 else "main"
    note = None if comparable else "One or both routes have cities with no plaza data; totals are lower bounds."
    return TollComparison(vehicle_type=main.vehicle_type, main=main, alternate=alt, difference_rs=diff,
                          cheaper=cheaper, comparable=comparable, note=note)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8003")))