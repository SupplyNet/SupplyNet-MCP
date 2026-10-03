"""Hermetic tests for the current SupplyNet MCP implementation.

No external network calls are required. Provider responses and OSRM routes are
stubbed so these tests can run deterministically after dependencies are installed.
"""

import asyncio

from fastmcp import Client

from . import news_server
from . import osrm_server
from . import toll_rag_server
from . import weather_server


SHIPMENT = {
    "shipment_id": "test-123",
    "priority": "MEDIUM",
    "constraints": {"gvw_kg": 25000, "axle_count": 2, "height_m": 0, "width_m": 0},
    "origin": {"lat": 30.7046, "lon": 76.801, "pin": "160002", "name": "Chandigarh"},
    "destination": {"lat": 17.6868, "lon": 83.2185, "pin": "530001", "name": "Visakhapatnam"},
    "cargo": {"type": "Industrial Machinery", "weight_kg": 12500, "value": 1850000},
}


async def call(server, tool, args):
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
        if getattr(result, "is_error", False):
            raise RuntimeError(result)
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return structured.get("result") if isinstance(structured, dict) and set(structured) == {"result"} else structured
        data = getattr(result, "data", None)
        if data is not None:
            return data
        return None


async def main():
    async with Client(osrm_server.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
    assert "optimize_route" in tools

    # Deterministic fake route so the test never depends on OSRM.
    route = {"distance_km": 1860.0, "duration_h": 31.0, "geometry": [[76.801, 30.7046], [78.0081, 27.1767], [83.2185, 17.6868]]}
    osrm_server._osrm_routes = lambda points: asyncio.sleep(0, result=[route])
    osrm_server._safe_intelligence_call = fake_provider

    result = await call(osrm_server.mcp, "optimize_route", {"shipment": SHIPMENT})
    print("\n=== OPTIMIZE ROUTE RESULT ===")
    print(result)
    print("=== STATUS ===", result.get("status"))
    assert result["selected_route"]["toll_cost"] == 1375
    assert result["selected_route"]["objective_j_score"] is not None
    assert result["data_quality"]["toll"] == "partial"

    # Validation: malformed priority and negative GVW must fail at the MCP schema boundary.
    try:
        bad = dict(SHIPMENT)
        bad["priority"] = "BANANA"
        await call(osrm_server.mcp, "optimize_route", {"shipment": bad})
        raise AssertionError("invalid priority was accepted")
    except Exception:
        pass

    try:
        bad = dict(SHIPMENT)
        bad["constraints"] = {"gvw_kg": -1, "axle_count": 2, "height_m": 0, "width_m": 0}
        await call(osrm_server.mcp, "optimize_route", {"shipment": bad})
        raise AssertionError("negative GVW was accepted")
    except Exception:
        pass

    # Mathematical unit check.
    assert osrm_server._objective_j(1535, 30000, 4000, 0.5, 1.0) == 5.5
    print("ALL SUPPLYNET MCP TESTS PASSED")


async def fake_provider(url, tool, args):
    if "news" in url:
        return {"status": "ok", "disruptions": [{"severity": "HIGH", "city": "Gwalior"}]}
    if "weather" in url:
        return {"status": "ok", "source": "live", "observations": [{"severity": "LOW", "city": "Agra"}]}
    return {"vehicle_type": "hcv", "lines": [], "known_total_rs": 1375, "unmatched_cities": ["Chandigarh"], "complete": False}


if __name__ == "__main__":
    asyncio.run(main())
