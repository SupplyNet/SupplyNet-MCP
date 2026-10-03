# SupplyNet MCP

Production-oriented multi-server Model Context Protocol (MCP) intelligence layer for SupplyNet disaster-aware supply-chain routing.

## Services

| Server | Port | Purpose |
|---|---:|---|
| News MCP | 8001 | Live transport/disruption intelligence |
| Weather MCP | 8002 | Live weather and route hazards |
| Toll MCP | 8003 | Toll retrieval and route cost estimation |
| OSRM MCP | 8004 | Route calculation, geocoding and checkpoints |

## Run

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
python mcp/run_all_servers.py
```

The MCP endpoints are:

- http://localhost:8001/mcp
- http://localhost:8002/mcp
- http://localhost:8003/mcp
- http://localhost:8004/mcp

## Test

With the servers running:

```bash
python mcp/test_all.py
```

## Configuration

Copy `.env.example` to `.env` when local environment configuration is needed. Never commit `.env` or API credentials.

## Architecture

```text
SupplyNet Agent
      |
      | MCP Client
      v
+-----------------------+
| SupplyNet MCP Layer   |
+-----------------------+
 |       |       |     |
News  Weather   Toll  OSRM
 |       |       |     |
Google  wttr.in  RAG   OSRM
News            data  routing
```

The MCP layer provides structured external intelligence and a deterministic initial
route-optimization function. The `optimize_route` MCP tool is exposed by the OSRM /
SupplyNet MCP endpoint on port 8004. It performs OSRM routing, checkpoint extraction,
News/Weather/Toll retrieval, fuel calculation and objective scoring. Gemini/agent code
can call this single tool and interpret the returned structured result; it does not
perform the mathematical calculations.

## Deterministic Route Optimization

The main SupplyNet MCP entry point is `optimize_route(shipment, include_geometry=False)` on port 8004.

It performs the deterministic workflow inside the MCP layer:

1. Validate the shipment with Pydantic.
2. Request up to three OSRM alternative routes.
3. Extract checkpoints from each real route geometry.
4. Query News, Weather and Toll MCP services concurrently.
5. Calculate truck-adjusted duration, fuel cost, road risk, weather risk and objective `J`.
6. Select the route with the minimum `J`.
7. Return structured JSON with alternatives and per-provider data-quality status.

Gemini/agent code should interpret the returned result rather than recomputing the mathematics.

### Data quality behavior

- News mock events are **off by default** (`NEWS_INCLUDE_MOCK=0`).
- Weather uses live `wttr.in` data for route checks. Demo alerts are opt-in with `INCLUDE_DEMO_WEATHER=1`.
- Toll data is a knowledge-base/demo dataset unless a verified `TOLL_DATA_PATH` is supplied. Incomplete toll coverage is reported as a lower bound instead of zero.
- Provider outages produce `PARTIAL` results and explicit `data_quality` / `missing_inputs` information; they are not silently treated as clear conditions.
- Full route geometry is omitted by default to keep MCP responses compact. Pass `include_geometry=true` when the frontend needs it.

### Example

Use `sample_shipment.json` with the HTTP client:

```bash
python -m supplynet_mcp.run_all_servers
python -m supplynet_mcp.client
```

For hermetic tests:

```bash
python -m supplynet_mcp.test_all
```

The test suite stubs OSRM and provider responses, so it does not require external network access.

### Gemini / MCP responsibility split

```text
Gemini / Agent
      |
      | MCP tool call
      v
optimize_route()
      |
      +-- OSRM alternatives
      +-- News
      +-- Weather
      +-- Toll
      +-- deterministic calculations
      v
structured result
      |
      v
Gemini explains the result
```

FastMCP is the MCP server framework. `fastmcp.Client` is used for MCP-to-MCP calls. A Gemini SDK/client (or an existing LangChain Gemini integration) can consume the typed `Shipment` tool schema; LangChain is not required by the MCP server itself.
