import asyncio
from fastmcp import Client


async def test(url, tool, args):
    print("\n" + "=" * 60)
    print("SERVER:", url)
    print("TOOL:", tool)

    async with Client(url) as client:
        result = await client.call_tool(tool, args)

        print("STRUCTURED:")
        print(result.structured_content)

        print("\nDATA:")
        print(result.data)

        print("\nCONTENT:")
        print(result.content)


async def main():
    cities = ["Chandigarh", "Agra", "Visakhapatnam"]

    await test(
        "http://127.0.0.1:8001/mcp",
        "check_route_disruptions",
        {"cities": cities, "min_severity": "LOW"},
    )

    await test(
        "http://127.0.0.1:8002/mcp",
        "check_route_weather_hazards",
        {"cities": cities},
    )

    await test(
        "http://127.0.0.1:8003/mcp",
        "calculate_toll_cost",
        {"route_cities": cities, "vehicle_type": "hcv"},
    )


if __name__ == "__main__":
    asyncio.run(main())