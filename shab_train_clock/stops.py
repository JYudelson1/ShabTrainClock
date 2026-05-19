"""Resolve human-typed station names to canonical stop_ids.

At startup, downloads the MTA static GTFS schedule data and builds:
  - a parent-station name index (for fuzzy name matching), and
  - a parent_stop_id -> set(route_id) index (for disambiguating same-named
    stations like the two "Nostrand Av"s — IRT 3 vs IND A/C).

When config says "Nostrand Av" with routes ["3","A","C","E"], we return
*both* matching parent stations, each tagged with the subset of requested
routes that physically serve it.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass

import httpx
from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

# Static GTFS data for NYC subway
GTFS_STATIC_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip"

# Confidence thresholds
AUTO_RESOLVE_THRESHOLD = 85  # exact-ish match: auto-resolve
SUGGEST_THRESHOLD = 60       # close enough to suggest


@dataclass
class StopInfo:
    stop_id: str           # e.g. "247" (parent station; child stops are "247N"/"247S")
    name: str              # e.g. "Nostrand Av"


@dataclass
class ResolvedStop:
    """One physical station that matched a config entry."""
    stop_id: str
    name: str
    routes: list[str]  # subset of the originally-requested routes that serve this stop


class SubwayStopResolver:
    """Loads NYC subway static GTFS and fuzzy-resolves stop names."""

    def __init__(self) -> None:
        # parent stop_id -> StopInfo
        self._parents: dict[str, StopInfo] = {}
        # Lower-cased name -> list of (stop_id, original_name)
        self._by_name: dict[str, list[tuple[str, str]]] = {}
        # child stop_id (e.g. "247N") -> parent stop_id ("247")
        self._child_to_parent: dict[str, str] = {}
        # parent stop_id -> set of route_ids that serve it
        self._stop_routes: dict[str, set[str]] = {}

    async def load(self) -> None:
        """Download static GTFS and parse stops.txt, trips.txt, stop_times.txt."""
        async with httpx.AsyncClient(timeout=60.0) as http:
            try:
                resp = await http.get(GTFS_STATIC_URL)
                resp.raise_for_status()
            except Exception:
                logger.exception(
                    "Failed to download static GTFS from %s. Stop resolution will be lossy.",
                    GTFS_STATIC_URL,
                )
                return

        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                self._parse_stops(zf)
                trip_to_route = self._parse_trips(zf)
                self._parse_stop_times(zf, trip_to_route)
        except Exception:
            logger.exception("Failed to parse static GTFS")
            return

        # Build the name search index
        for sid, info in self._parents.items():
            self._by_name.setdefault(info.name.lower(), []).append((sid, info.name))

        logger.info(
            "Loaded %d parent subway stations, %d stop->routes mappings",
            len(self._parents), len(self._stop_routes),
        )

    def _parse_stops(self, zf: zipfile.ZipFile) -> None:
        with zf.open("stops.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for row in reader:
                stop_id = row.get("stop_id", "")
                name = row.get("stop_name", "")
                parent = row.get("parent_station", "")
                if parent:
                    # child stop (e.g. "247N" -> "247")
                    self._child_to_parent[stop_id] = parent
                elif not stop_id.endswith(("N", "S")):
                    # parent station
                    self._parents[stop_id] = StopInfo(stop_id=stop_id, name=name)
                    # a parent stop_id maps to itself, useful if stop_times
                    # ever references the parent directly
                    self._child_to_parent[stop_id] = stop_id

    def _parse_trips(self, zf: zipfile.ZipFile) -> dict[str, str]:
        trip_to_route: dict[str, str] = {}
        with zf.open("trips.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for row in reader:
                tid = row.get("trip_id", "")
                rid = row.get("route_id", "")
                if tid and rid:
                    trip_to_route[tid] = rid
        return trip_to_route

    def _parse_stop_times(
        self, zf: zipfile.ZipFile, trip_to_route: dict[str, str]
    ) -> None:
        # stop_times.txt is large; stream it.
        with zf.open("stop_times.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for row in reader:
                tid = row.get("trip_id", "")
                sid = row.get("stop_id", "")
                if not tid or not sid:
                    continue
                route = trip_to_route.get(tid)
                if not route:
                    continue
                parent = self._child_to_parent.get(sid)
                if not parent:
                    continue
                self._stop_routes.setdefault(parent, set()).add(route)

    def resolve(
        self, query_name: str, routes: list[str]
    ) -> list[ResolvedStop]:
        """
        Resolve a user-typed name + routes to one or more physical stops.

        Returns one ResolvedStop per physical station whose name matches the
        query AND that actually serves at least one of the requested routes.
        Returns [] on no match.
        """
        q = query_name.lower().strip()

        # Collect candidate (stop_id, canonical_name) pairs by name.
        candidates: list[tuple[str, str]] = []

        if q in self._by_name:
            candidates = list(self._by_name[q])
        elif self._by_name:
            # Fuzzy match
            all_names = list(self._by_name.keys())
            result = process.extractOne(q, all_names, scorer=fuzz.WRatio)
            if result is None:
                logger.error("No reasonable match for subway station '%s'. Skipping.", query_name)
                return []
            best_name, score, _ = result
            if score >= AUTO_RESOLVE_THRESHOLD:
                candidates = list(self._by_name[best_name])
                logger.info(
                    "Fuzzy-resolved '%s' -> '%s' (score=%d)",
                    query_name, best_name, score,
                )
            elif score >= SUGGEST_THRESHOLD:
                suggestions = process.extract(
                    q, all_names, scorer=fuzz.WRatio, limit=3
                )
                suggest_names = [self._by_name[s[0]][0][1] for s in suggestions]
                logger.error(
                    "Could not confidently resolve subway station '%s'. "
                    "Did you mean one of: %s? Skipping.",
                    query_name, suggest_names,
                )
                return []
            else:
                logger.error("No reasonable match for subway station '%s'. Skipping.", query_name)
                return []
        else:
            # GTFS didn't load
            return []

        # Filter candidates by route service. If we have no stop->routes index
        # (e.g. GTFS partially failed), fall back to returning all candidates.
        wanted = set(routes)
        resolved: list[ResolvedStop] = []
        for stop_id, canonical in candidates:
            served = self._stop_routes.get(stop_id)
            if served is None:
                # No route info — keep all requested routes (lossy fallback)
                resolved.append(ResolvedStop(stop_id, canonical, list(routes)))
                continue
            matched = [r for r in routes if r in served]
            if matched:
                resolved.append(ResolvedStop(stop_id, canonical, matched))

        if not resolved:
            served_summary = {
                sid: sorted(self._stop_routes.get(sid, set())) for sid, _ in candidates
            }
            logger.error(
                "Station '%s' matched stops %s but none serve routes %s. "
                "Service per stop: %s",
                query_name, [c[0] for c in candidates], routes, served_summary,
            )
            return []

        # Warn if a requested route isn't served by ANY of the matched stops
        served_all = {r for rs in resolved for r in rs.routes}
        missing = wanted - served_all
        if missing:
            logger.warning(
                "Station '%s': routes %s not served by any matching stop.",
                query_name, sorted(missing),
            )

        if len(resolved) > 1:
            logger.info(
                "Station '%s' resolved to %d physical stops: %s",
                query_name,
                len(resolved),
                [(r.stop_id, r.name, r.routes) for r in resolved],
            )

        return resolved
