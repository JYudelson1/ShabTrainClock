"""NYC Subway realtime data client.

Polls the public GTFS-Realtime protobuf feeds, decodes them, and exposes
arrival times by stop_id.

MTA splits the subway into multiple feeds by line group. We map each
route to its feed and only fetch the feeds we actually need.

API keys are NOT required (MTA dropped that requirement in 2024).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from google.transit import gtfs_realtime_pb2

logger = logging.getLogger(__name__)

# Map from route -> feed URL.
# https://api.mta.info/#/subwayRealTimeFeeds
_BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds"
ROUTE_TO_FEED: dict[str, str] = {
    # 1/2/3/4/5/6/7/GS (Grand Central Shuttle)
    "1": f"{_BASE}/nyct%2Fgtfs",
    "2": f"{_BASE}/nyct%2Fgtfs",
    "3": f"{_BASE}/nyct%2Fgtfs",
    "4": f"{_BASE}/nyct%2Fgtfs",
    "5": f"{_BASE}/nyct%2Fgtfs",
    "6": f"{_BASE}/nyct%2Fgtfs",
    "7": f"{_BASE}/nyct%2Fgtfs-7",
    "GS": f"{_BASE}/nyct%2Fgtfs",
    # A/C/E/H (Rockaway Park Shuttle), FS (Franklin Av Shuttle)
    "A": f"{_BASE}/nyct%2Fgtfs-ace",
    "C": f"{_BASE}/nyct%2Fgtfs-ace",
    "E": f"{_BASE}/nyct%2Fgtfs-ace",
    "H": f"{_BASE}/nyct%2Fgtfs-ace",
    "FS": f"{_BASE}/nyct%2Fgtfs-ace",
    # B/D/F/M
    "B": f"{_BASE}/nyct%2Fgtfs-bdfm",
    "D": f"{_BASE}/nyct%2Fgtfs-bdfm",
    "F": f"{_BASE}/nyct%2Fgtfs-bdfm",
    "M": f"{_BASE}/nyct%2Fgtfs-bdfm",
    # G
    "G": f"{_BASE}/nyct%2Fgtfs-g",
    # J/Z
    "J": f"{_BASE}/nyct%2Fgtfs-jz",
    "Z": f"{_BASE}/nyct%2Fgtfs-jz",
    # N/Q/R/W
    "N": f"{_BASE}/nyct%2Fgtfs-nqrw",
    "Q": f"{_BASE}/nyct%2Fgtfs-nqrw",
    "R": f"{_BASE}/nyct%2Fgtfs-nqrw",
    "W": f"{_BASE}/nyct%2Fgtfs-nqrw",
    # L
    "L": f"{_BASE}/nyct%2Fgtfs-l",
    # SI (Staten Island Railway)
    "SI": f"{_BASE}/nyct%2Fgtfs-si",
}


@dataclass
class Arrival:
    """A single upcoming train arrival at a stop."""
    route: str
    stop_id: str       # e.g. "247N" or "247S" — direction baked in
    direction: str     # "N" or "S"
    arrival_time: datetime  # UTC
    headsign: str | None = None

    @property
    def minutes_away(self) -> float:
        delta = self.arrival_time - datetime.now(timezone.utc)
        return delta.total_seconds() / 60


class SubwayClient:
    """Fetches and caches realtime subway arrivals."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10.0)
        # Cache: feed_url -> list of arrivals from that feed
        self._cache: dict[str, list[Arrival]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def refresh(self, routes: set[str]) -> None:
        """Refresh feeds covering the given routes."""
        # Dedupe: many routes share feeds
        feeds_to_fetch = {ROUTE_TO_FEED[r] for r in routes if r in ROUTE_TO_FEED}
        for url in feeds_to_fetch:
            try:
                arrivals = await self._fetch_feed(url)
                self._cache[url] = arrivals
                logger.debug("Fetched %d arrivals from %s", len(arrivals), url)
            except Exception:
                logger.exception("Failed to fetch feed %s", url)

    async def _fetch_feed(self, url: str) -> list[Arrival]:
        resp = await self._http.get(url)
        resp.raise_for_status()

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)

        arrivals: list[Arrival] = []
        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue
            tu = entity.trip_update
            route = tu.trip.route_id
            for stu in tu.stop_time_update:
                stop_id = stu.stop_id  # already has N/S suffix
                if not stop_id:
                    continue
                ts = stu.arrival.time or stu.departure.time
                if not ts:
                    continue
                direction = stop_id[-1] if stop_id[-1] in ("N", "S") else ""
                arrivals.append(
                    Arrival(
                        route=route,
                        stop_id=stop_id,
                        direction=direction,
                        arrival_time=datetime.fromtimestamp(ts, tz=timezone.utc),
                    )
                )
        return arrivals

    def arrivals_at_stop(
        self,
        parent_stop_id: str,
        routes: list[str],
        direction: str = "both",
    ) -> list[Arrival]:
        """
        Get upcoming arrivals at a stop.

        parent_stop_id: 3-char stop code (e.g. "247" for Nostrand Av on the 3 line).
                        Per-direction stop_ids have N/S appended (e.g. "247N").
        """
        wanted_stop_ids: set[str] = set()
        if direction in ("N", "both"):
            wanted_stop_ids.add(f"{parent_stop_id}N")
        if direction in ("S", "both"):
            wanted_stop_ids.add(f"{parent_stop_id}S")

        results: list[Arrival] = []
        for arrivals_list in self._cache.values():
            for a in arrivals_list:
                if a.stop_id in wanted_stop_ids and a.route in routes:
                    if a.minutes_away >= 0:  # filter past arrivals
                        results.append(a)
        results.sort(key=lambda a: a.arrival_time)
        return results
