"""Weather client.

Uses Open-Meteo (https://open-meteo.com) — free, no API key, no rate limit
for personal use. Pulls the current temperature + today's high/low + the
probability of precipitation for the rest of today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass
class WeatherState:
    current_temp: float | None        # in configured units
    current_code: int | None           # WMO weather code
    high: float | None
    low: float | None
    precip_probability_today: int | None  # 0-100, max for today
    units: str                         # "fahrenheit" or "celsius"
    last_updated: datetime | None


# WMO Weather interpretation codes -> (emoji, label)
# Reference: https://open-meteo.com/en/docs (search "WMO Weather interpretation codes")
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("☀️", "clear"),
    1: ("🌤️", "mostly clear"),
    2: ("⛅", "partly cloudy"),
    3: ("☁️", "overcast"),
    45: ("🌫️", "fog"),
    48: ("🌫️", "fog"),
    51: ("🌦️", "light drizzle"),
    53: ("🌦️", "drizzle"),
    55: ("🌧️", "heavy drizzle"),
    56: ("🌧️", "freezing drizzle"),
    57: ("🌧️", "freezing drizzle"),
    61: ("🌦️", "light rain"),
    63: ("🌧️", "rain"),
    65: ("🌧️", "heavy rain"),
    66: ("🌧️", "freezing rain"),
    67: ("🌧️", "freezing rain"),
    71: ("🌨️", "light snow"),
    73: ("🌨️", "snow"),
    75: ("❄️", "heavy snow"),
    77: ("🌨️", "snow grains"),
    80: ("🌦️", "rain showers"),
    81: ("🌧️", "rain showers"),
    82: ("⛈️", "violent rain"),
    85: ("🌨️", "snow showers"),
    86: ("❄️", "heavy snow showers"),
    95: ("⛈️", "thunderstorm"),
    96: ("⛈️", "thunderstorm w/ hail"),
    99: ("⛈️", "thunderstorm w/ hail"),
}


def code_to_emoji(code: int | None) -> str:
    if code is None:
        return "❓"
    return WMO_CODES.get(code, ("❓", "unknown"))[0]


def code_to_label(code: int | None) -> str:
    if code is None:
        return ""
    return WMO_CODES.get(code, ("", "unknown"))[1]


class WeatherClient:
    def __init__(
        self, latitude: float, longitude: float, units: str, timezone_name: str
    ) -> None:
        self._lat = latitude
        self._lon = longitude
        self._units = units
        self._tz = ZoneInfo(timezone_name)
        self._tz_name = timezone_name
        self._http = httpx.AsyncClient(timeout=15.0)
        self._state = WeatherState(
            current_temp=None,
            current_code=None,
            high=None,
            low=None,
            precip_probability_today=None,
            units=units,
            last_updated=None,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def refresh(self) -> None:
        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": self._units,
            "timezone": self._tz_name,
            "forecast_days": 1,
        }
        try:
            resp = await self._http.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("Failed to fetch weather from Open-Meteo")
            return

        try:
            current = data.get("current", {})
            daily = data.get("daily", {})
            self._state = WeatherState(
                current_temp=current.get("temperature_2m"),
                current_code=current.get("weather_code"),
                high=(daily.get("temperature_2m_max") or [None])[0],
                low=(daily.get("temperature_2m_min") or [None])[0],
                precip_probability_today=(
                    daily.get("precipitation_probability_max") or [None]
                )[0],
                units=self._units,
                last_updated=datetime.now(self._tz),
            )
            logger.info(
                "Weather: %s°%s, code=%s, hi/lo=%s/%s, precip%%=%s",
                self._state.current_temp, self._units[0].upper(),
                self._state.current_code, self._state.high, self._state.low,
                self._state.precip_probability_today,
            )
        except Exception:
            logger.exception("Failed to parse Open-Meteo response")

    def current_state(self) -> WeatherState:
        return self._state
