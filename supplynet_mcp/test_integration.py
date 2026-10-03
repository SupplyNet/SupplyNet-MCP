"""Real HTTP integration test for SupplyNet MCP.

Tests:
8004 OSRM optimizer
   -> 8001 News MCP
   -> 8002 Weather MCP
   -> 8003 Toll MCP

All four servers must be running.
"""

import asyncio
from fastmcp import Client


OSRM = "http://127.0.0.1:8004/mcp"


SHIPMENT = {
    "shipment_id": "integration-test-001",
    "priority": "MEDIUM",
    "constraints": {
        "gvw_kg": 25000,
        "axle_count": 2,
        "height_m": 0,
        "width_m": 0,
    },
    "origin": {
        "lat": 30.7046,
        "lon": 76.801,
        "pin": "160002",
        "name": "Chandigarh",
    },
    "destination": {
        "lat": 17.6868,
        "lon": 83.2185,
        "pin": "530001",
        "name": "Visakhapatnam",
    },
    "cargo": {
        "type": "Industrial Machinery",
        "weight_kg": 12500,
        "value": 1850000,
    },
}


async def main():

    print("=" * 60)
    print("SUPPLYNET REAL MCP INTEGRATION TEST")
    print("=" * 60)

    async with Client(OSRM) as client:

        print("\n[1] Checking OSRM MCP...")

        tools = await client.list_tools()
        tool_names = {tool.name for tool in tools}

        if "optimize_route" not in tool_names:
            raise AssertionError(
                "optimize_route is not exposed by OSRM MCP"
            )

        print("PASS: optimize_route available")

        print("\n[2] Calling optimize_route through HTTP...")

        result = await client.call_tool(
            "optimize_route",
            {
                "shipment": SHIPMENT,
                "include_geometry": False,
            },
        )

        if getattr(result, "is_error", False):
            raise RuntimeError(result)

        structured = getattr(result, "structured_content", None)

        if structured is not None:
            if (
                isinstance(structured, dict)
                and set(structured) == {"result"}
            ):
                data = structured["result"]
            else:
                data = structured
        else:
            data = getattr(result, "data", None)

        if not isinstance(data, dict):
            raise AssertionError(
                f"Unexpected optimize_route response: {data!r}"
            )

        print("PASS: optimize_route returned structured data")

    print("\n[3] Inspecting selected route...")

    selected = data["selected_route"]

    print(
        f"Distance       : {selected['distance_km']} km"
    )
    print(
        f"Fuel cost      : Rs {selected['fuel_cost']}"
    )
    print(
        f"Toll cost      : Rs {selected['toll_cost']}"
    )
    print(
        f"Road risk      : {selected['road_risk_score']}"
    )
    print(
        f"Weather risk   : {selected['weather_risk_score']}"
    )
    print(
        f"Objective J    : {selected['objective_j_score']}"
    )

    print("\n[4] Inspecting provider evidence...")

    evidence = data["evidence"]

    print("\nNEWS:")
    print(evidence["news"])

    print("\nWEATHER:")
    print(evidence["weather"])

    print("\nTOLL:")
    print(evidence["toll"])

    print("\n[5] Validating provider separation...")

    news = evidence["news"]
    weather = evidence["weather"]
    toll = evidence["toll"]

    # News must not accidentally contain Toll fields.
    assert "known_total_rs" not in news, (
        "BUG: News evidence contains Toll response"
    )

    # Weather must not accidentally contain Toll fields.
    assert "known_total_rs" not in weather, (
        "BUG: Weather evidence contains Toll response"
    )

    # Toll should contain toll fields.
    assert "known_total_rs" in toll, (
        "Toll evidence does not contain known_total_rs"
    )

    assert "complete" in toll, (
        "Toll evidence does not contain completeness information"
    )

    print("PASS: News / Weather / Toll responses are separated")

    print("\n[6] Validating toll calculation...")

    print(
        f"MCP reported toll: Rs {toll['known_total_rs']}"
    )

    assert selected["toll_cost"] == toll["known_total_rs"], (
        "Optimizer toll_cost does not match Toll MCP"
    )

    print("PASS: optimizer toll matches Toll MCP")

    print("\n[7] Validating status...")

    print(
        f"Overall status: {data['status']}"
    )

    if not toll["complete"]:
        assert data["status"] == "PARTIAL", (
            "Incomplete Toll data should produce PARTIAL status"
        )

    print("PASS: data quality status is consistent")

    print("\n" + "=" * 60)
    print("REAL MCP INTEGRATION TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())