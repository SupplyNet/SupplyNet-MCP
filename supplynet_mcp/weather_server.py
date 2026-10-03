"""
SupplyNet Weather MCP Server.

Port:
    8002

Uses Open-Meteo for current weather information.

No API key is required for the public endpoint used here.

The server returns:
- temperature
- apparent temperature
- precipitation
- rain
- wind speed
- wind direction
- visibility
- weather condition
- source
- retrieval timestamp

If the weather provider is unavailable, the server reports
"unavailable" instead of inventing clear weather.
"""

import os
import time

from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel


from . import corridor as cor


mcp = FastMCP("SupplyNetWeatherServer")


# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------

GEOCODING_URL = (
    "https://geocoding-api.open-meteo.com/v1/search"
)

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
)

HTTP_TIMEOUT = float(
    os.getenv("WEATHER_HTTP_TIMEOUT", "8")
)

CACHE_TTL = int(
    os.getenv("WEATHER_CACHE_TTL", "120")
)


# -------------------------------------------------------------------
# Models
# -------------------------------------------------------------------

class WeatherData(BaseModel):

    location: str

    latitude: float

    longitude: float

    temperature_c: Optional[float] = None

    apparent_temperature_c: Optional[float] = None

    precipitation_mm: Optional[float] = None

    rain_mm: Optional[float] = None

    wind_speed_kmh: Optional[float] = None

    wind_direction_deg: Optional[float] = None

    visibility_m: Optional[float] = None

    weather_code: Optional[int] = None

    condition: str

    severity: str

    source: str

    retrieved_at: datetime

    status: str


class WeatherResult(BaseModel):

    location: str

    status: str

    weather: Optional[WeatherData] = None

    error: Optional[str] = None


# -------------------------------------------------------------------
# Cache
# -------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None

_cache: Dict[
    str,
    tuple[
        float,
        WeatherResult
    ]
] = {}


def _get_client():

    global _client

    if _client is None or _client.is_closed:

        _client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={
                "User-Agent":
                    "SupplyNet-MCP-Weather/1.0"
            },
        )

    return _client


# -------------------------------------------------------------------
# Geocoding
# -------------------------------------------------------------------

async def geocode_location(
    location: str,
):

    location = (
        location or ""
    ).strip()

    if not location:
        raise ToolError(
            "location must not be empty."
        )

    known = cor.lookup(location)

    if known:

        return (
            cor.display_name(location),
            known[0],
            known[1],
        )

    try:

        response = await _get_client().get(
            GEOCODING_URL,
            params={
                "name": location,
                "count": 1,
                "language": "en",
                "format": "json",
            },
        )

        response.raise_for_status()

        data = response.json()

        results = data.get(
            "results",
            [],
        )

        if not results:

            raise ToolError(
                f"Could not geocode '{location}'."
            )

        result = results[0]

        return (
            result["name"],
            float(result["latitude"]),
            float(result["longitude"]),
        )

    except ToolError:
        raise

    except Exception as exc:

        raise ToolError(
            f"Weather geocoding failed for "
            f"'{location}': {exc}"
        )


# -------------------------------------------------------------------
# Weather classification
# -------------------------------------------------------------------

def weather_description(
    code: Optional[int],
) -> str:

    if code is None:
        return "Unknown"

    mapping = {

        0: "Clear sky",

        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",

        45: "Fog",
        48: "Depositing rime fog",

        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",

        56: "Light freezing drizzle",
        57: "Dense freezing drizzle",

        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",

        66: "Light freezing rain",
        67: "Heavy freezing rain",

        71: "Slight snowfall",
        73: "Moderate snowfall",
        75: "Heavy snowfall",

        77: "Snow grains",

        80: "Slight rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",

        85: "Slight snow showers",
        86: "Heavy snow showers",

        95: "Thunderstorm",

        96: "Thunderstorm with slight hail",
        99: "Thunderstorm with heavy hail",
    }

    return mapping.get(
        code,
        "Unknown weather",
    )


def determine_severity(
    code: Optional[int],
    visibility_m: Optional[float],
    wind_speed: Optional[float],
    precipitation: Optional[float],
) -> str:

    score = 0

    if code in {95, 96, 99}:
        score += 3

    if code in {
        65,
        67,
        75,
        82,
        86,
    }:
        score += 2

    if visibility_m is not None:

        if visibility_m < 1000:
            score += 3

        elif visibility_m < 3000:
            score += 2

    if wind_speed is not None:

        if wind_speed >= 60:
            score += 3

        elif wind_speed >= 40:
            score += 2

    if precipitation is not None:

        if precipitation >= 20:
            score += 2

    if score >= 5:
        return "CRITICAL"

    if score >= 3:
        return "HIGH"

    if score >= 1:
        return "MEDIUM"

    # Normal conditions are not a transport hazard.
    return "NONE"


# -------------------------------------------------------------------
# Live weather
# -------------------------------------------------------------------

async def _get_weather(
    location: str,
) -> WeatherResult:

    name, lat, lon = await geocode_location(
        location
    )

    key = cor.normalize(name)

    cached = _cache.get(key)

    now_mono = time.monotonic()

    if (
        cached
        and cached[0] > now_mono
    ):
        return cached[1]

    params = {

        "latitude": lat,

        "longitude": lon,

        "current": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "precipitation",
            "rain",
            "weather_code",
            "wind_speed_10m",
            "wind_direction_10m",
        ]),

        "hourly": "visibility",

        "forecast_days": 1,

        "timezone": "auto",
    }

    try:

        response = await _get_client().get(
            WEATHER_URL,
            params=params,
        )

        response.raise_for_status()

        data = response.json()

        current = data.get(
            "current",
            {},
        )

        hourly = data.get(
            "hourly",
            {},
        )

        visibility_values = hourly.get(
            "visibility",
            [],
        )

        visibility = (
            visibility_values[0]
            if visibility_values
            else None
        )

        code = current.get(
            "weather_code"
        )

        precipitation = current.get(
            "precipitation"
        )

        wind_speed = current.get(
            "wind_speed_10m"
        )

        weather = WeatherData(

            location=name,

            latitude=lat,

            longitude=lon,

            temperature_c=current.get(
                "temperature_2m"
            ),

            apparent_temperature_c=current.get(
                "apparent_temperature"
            ),

            precipitation_mm=precipitation,

            rain_mm=current.get(
                "rain"
            ),

            wind_speed_kmh=wind_speed,

            wind_direction_deg=current.get(
                "wind_direction_10m"
            ),

            visibility_m=visibility,

            weather_code=code,

            condition=weather_description(
                code
            ),

            severity=determine_severity(
                code,
                visibility,
                wind_speed,
                precipitation,
            ),

            source="Open-Meteo",

            retrieved_at=datetime.now(
                timezone.utc
            ),

            status="success",
        )

        result = WeatherResult(
            location=name,
            status="success",
            weather=weather,
        )

        _cache[key] = (
            time.monotonic() + CACHE_TTL,
            result,
        )

        return result

    except Exception as exc:

        return WeatherResult(
            location=name,
            status="unavailable",
            error=str(exc),
        )


# -------------------------------------------------------------------
# MCP tools
# -------------------------------------------------------------------

@mcp.tool()
async def get_city_weather(
    location: str,
) -> WeatherResult:

    """
    Get current weather conditions for a location.
    """

    return await _get_weather(
        location
    )


@mcp.tool()
async def get_weather_alerts(
    location: str,
) -> WeatherResult:

    """
    Get current weather and identify whether
    conditions may affect heavy vehicle transport.
    """

    return await _get_weather(
        location
    )


@mcp.tool()
async def check_route_weather_hazards(
    cities: List[str],
) -> dict:
    """
    Check current weather conditions for multiple route cities.

    Returns an aggregate dictionary because the OSRM optimizer consumes
    route-level weather observations and risk information.
    """
    if not cities:
        raise ToolError("cities must not be empty.")

    results = []

    for city in cities:
        results.append(await _get_weather(city))

    observations = []
    cities_unavailable = []

    for result in results:
        if result.status == "success" and result.weather is not None:
            weather = result.weather
            observations.append({
                "city": result.location,
                "severity": weather.severity,
                "condition": weather.condition,
                "visibility_m": weather.visibility_m,
                "wind_speed_kmh": weather.wind_speed_kmh,
                "precipitation_mm": weather.precipitation_mm,
                "source": weather.source,
                "retrieved_at": weather.retrieved_at.isoformat(),
            })
        else:
            cities_unavailable.append(result.location)

    if cities_unavailable and observations:
        status = "partial"
    elif cities_unavailable:
        status = "unavailable"
    else:
        status = "ok"

    return {
        "status": status,
        "source": "Open-Meteo",
        "observations": observations,
        "cities_checked": cities,
        "cities_unavailable": cities_unavailable,
    }


# -------------------------------------------------------------------
# Server
# -------------------------------------------------------------------

if __name__ == "__main__":

    mcp.run(
        transport="streamable-http",
        host=os.getenv(
            "MCP_HOST",
            "127.0.0.1",
        ),
        port=int(
            os.getenv(
                "MCP_PORT",
                "8002",
            )
        ),
    )