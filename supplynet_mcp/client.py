"""Small HTTP MCP client for the SupplyNet route-optimization demo."""

import asyncio
import json
from typing import Any, Dict

from fastmcp import Client

SERVERS = {
    "news": "http://localhost:8001/mcp",
    "weather": "http://localhost:8002/mcp",
    "toll": "http://localhost:8003/mcp",
    "supplynet": "http://localhost:8004/mcp",
}


async def call_mcp_tool(server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
    url = SERVERS.get(server_name)
    if not url:
        raise ValueError(f"Unknown server '{server_name}'")
    async with Client(url) as client:
        result = await client.call_tool(tool_name, arguments)
        if getattr(result, "is_error", False):
            raise RuntimeError(f"MCP tool '{tool_name}' returned an error")
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            if isinstance(structured, dict) and set(structured) == {"result"}:
                return structured["result"]
            return structured
        data = getattr(result, "data", None)
        if data is not None:
            return data
        content = getattr(result, "content", []) or []
        return "\n".join(item.text for item in content if getattr(item, "text", None))


async def run_initial_route_optimization() -> None:
    shipment = {
        "shipment_id": "uuid-1234-5678",
        "priority": "MEDIUM",
        "constraints": {"gvw_kg": 25000, "axle_count": 2, "height_m": 0, "width_m": 0},
        "origin": {"lat": 30.7046, "lon": 76.801, "pin": "160002", "name": "Chandigarh"},
        "destination": {"lat": 17.6868, "lon": 83.2185, "pin": "530001", "name": "Visakhapatnam"},
        "cargo": {"type": "Industrial Machinery", "weight_kg": 12500, "value": 1850000},
    }
    result = await call_mcp_tool("supplynet", "optimize_route", {"shipment": shipment})
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(run_initial_route_optimization())
