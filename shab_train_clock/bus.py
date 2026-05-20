"""NYC Bus realtime data client.

Uses the MTA BusTime SIRI StopMonitoring JSON API. Requires a free API key
from https://register.developer.obanyc.com/.

Rate limit: MTA asks for max 1 req/30s per stop. We batch and cache.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# MTA migrated the bus SIRI API off bustime.mta.info (now edge-blocks /api/*
# with a 403) to bustime-classic.mta.info. Same path, same key/params.
SIRI_URL = "https://bustime-classic.mta.info/api/siri/stop-monitoring.json"


@dataclass
class BusArrival:
    route: str           # e.g. "B44"
    stop_code: str       # e.g. "308213"
    arrival_time: datetime  # UTC
    headsign: str        # e.g. "SBS WLMSBRG BRDG PLZ via NSTRND via RGRS"
    distance_text: str   # "2 stops away", "1.3 miles away", etc. (SIRI provides this)

    @property
    def minutes_away(self) -> float:
        delta = self.arrival_time - datetime.now(timezone.utc)
        return delta.total_seconds() / 60


class BusClient:
    """Fetches realtime bus arrivals via SIRI StopMonitoring."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=10.0)
        # Cache: stop_code -> list of arrivals
        self._cache: dict[str, list[BusArrival]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def refresh(self, stop_codes: list[str]) -> None:
        """Refresh arrival data for the given stop codes."""
        if not self._api_key or self._api_key == "YOUR_KEY_HERE":
            logger.warning("No MTA bus API key set; skipping bus refresh")
            return
        for code in stop_codes:
            try:
                self._cache[code] = await self._fetch_stop(code)
            except Exception:
                logger.exception("Failed to fetch bus stop %s", code)

    async def _fetch_stop(self, stop_code: str) -> list[BusArrival]:
        params = {
            "key": self._api_key,
            "version": "2",
            "OperatorRef": "MTA",
            "MonitoringRef": stop_code,
            "StopMonitoringDetailLevel": "minimum",
            "MaximumStopVisits": "10",
        }
        resp = await self._http.get(SIRI_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        arrivals: list[BusArrival] = []
        try:
            delivery = (
                data["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"][0]
            )
            visits = delivery.get("MonitoredStopVisit", [])
        except (KeyError, IndexError):
            logger.warning("Unexpected SIRI response for stop %s", stop_code)
            return arrivals

        for visit in visits:
            try:
                mvj = visit["MonitoredVehicleJourney"]
                # Route: PublishedLineName is like "B44" (preferred for display)
                route = (
                    mvj.get("PublishedLineName", [""])[0]
                    if isinstance(mvj.get("PublishedLineName"), list)
                    else mvj.get("PublishedLineName", "")
                )
                # Fallback: LineRef like "MTA NYCT_B44"
                if not route and "LineRef" in mvj:
                    route = mvj["LineRef"].split("_")[-1]

                headsign = ""
                if isinstance(mvj.get("DestinationName"), list):
                    headsign = mvj["DestinationName"][0] if mvj["DestinationName"] else ""
                else:
                    headsign = mvj.get("DestinationName", "")

                call = mvj.get("MonitoredCall", {})
                expected = call.get("ExpectedArrivalTime") or call.get(
                    "AimedArrivalTime"
                )
                if not expected:
                    continue
                # ISO 8601 with timezone
                arr_time = datetime.fromisoformat(expected).astimezone(timezone.utc)

                distance_text = (
                    call.get("Extensions", {})
                    .get("Distances", {})
                    .get("PresentableDistance", "")
                )

                arrivals.append(
                    BusArrival(
                        route=route,
                        stop_code=stop_code,
                        arrival_time=arr_time,
                        headsign=headsign,
                        distance_text=distance_text,
                    )
                )
            except Exception:
                logger.exception("Failed to parse a SIRI visit; skipping")
                continue

        arrivals.sort(key=lambda a: a.arrival_time)
        return arrivals

    def arrivals_at_stop(
        self,
        stop_code: str,
        routes: list[str] | None = None,
    ) -> list[BusArrival]:
        """Filter cached arrivals."""
        cached = self._cache.get(stop_code, [])
        if not routes:
            return [a for a in cached if a.minutes_away >= 0]
        return [
            a for a in cached if a.route in routes and a.minutes_away >= 0
        ]
