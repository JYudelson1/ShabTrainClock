"""FastAPI server: polls upstream APIs in the background, exposes /api/data."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .bus import BusClient
from .config import Config, load_config
from .hebcal import HebcalClient
from .stops import SubwayStopResolver
from .subway import SubwayClient
from .weather import WeatherClient, code_to_emoji, code_to_label

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Refresh cadences (seconds)
SUBWAY_REFRESH_S = 30
BUS_REFRESH_S = 30
HEBCAL_REFRESH_S = 6 * 60 * 60  # 6 hours; data only changes daily
WEATHER_REFRESH_S = 10 * 60  # 10 minutes


class AppState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.tz = ZoneInfo(config.location.timezone)
        self.subway = SubwayClient()
        self.bus = BusClient(config.mta_bus_api_key)
        self.hebcal = HebcalClient(
            geonameid=config.location.geonameid,
            timezone_name=config.location.timezone,
            latitude=config.location.latitude,
            longitude=config.location.longitude,
        )
        self.weather = WeatherClient(
            latitude=config.location.latitude,
            longitude=config.location.longitude,
            units=config.weather.units,
            timezone_name=config.location.timezone,
        )
        self.stop_resolver = SubwayStopResolver()

        # Resolved station configs: name -> (stop_id, canonical_name, routes, direction)
        self.resolved_subway: list[dict] = []

    async def startup(self) -> None:
        logger.info("Loading static GTFS for stop resolution...")
        await self.stop_resolver.load()

        # Resolve every configured subway station name. One config entry may
        # expand to multiple physical stops (e.g. "Nostrand Av" -> IRT 3 +
        # IND A/C, two different stations sharing a name).
        for s in self.config.subway_stations:
            resolved_stops = self.stop_resolver.resolve(s.name, s.routes)
            if not resolved_stops:
                logger.error("Could not resolve subway station: %s", s.name)
                continue
            for rs in resolved_stops:
                self.resolved_subway.append({
                    "stop_id": rs.stop_id,
                    "name": rs.name,
                    "configured_name": s.name,
                    "routes": rs.routes,
                    "direction": s.direction,
                })

        # Initial fetches
        all_routes = {r for s in self.config.subway_stations for r in s.routes}
        await asyncio.gather(
            self.subway.refresh(all_routes),
            self.bus.refresh([s.stop_code for s in self.config.bus_stops]),
            self.hebcal.refresh(),
            self.weather.refresh(),
            return_exceptions=True,
        )

    async def shutdown(self) -> None:
        await self.subway.close()
        await self.bus.close()
        await self.hebcal.close()
        await self.weather.close()


async def _subway_loop(state: AppState) -> None:
    routes = {r for s in state.config.subway_stations for r in s.routes}
    if not routes:
        return
    while True:
        await asyncio.sleep(SUBWAY_REFRESH_S)
        await state.subway.refresh(routes)


async def _bus_loop(state: AppState) -> None:
    codes = [s.stop_code for s in state.config.bus_stops]
    if not codes:
        return
    while True:
        await asyncio.sleep(BUS_REFRESH_S)
        await state.bus.refresh(codes)


async def _hebcal_loop(state: AppState) -> None:
    while True:
        await asyncio.sleep(HEBCAL_REFRESH_S)
        await state.hebcal.refresh()


async def _weather_loop(state: AppState) -> None:
    while True:
        await asyncio.sleep(WEATHER_REFRESH_S)
        await state.weather.refresh()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config(Path("config.yaml"))
    state = AppState(config)
    app.state.dashboard = state
    await state.startup()

    # Start background refresh loops
    tasks = [
        asyncio.create_task(_subway_loop(state)),
        asyncio.create_task(_bus_loop(state)),
        asyncio.create_task(_hebcal_loop(state)),
        asyncio.create_task(_weather_loop(state)),
    ]

    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await state.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/api/data")
async def get_dashboard_data() -> JSONResponse:
    """The endpoint the tablet polls every 15 seconds."""
    state: AppState = app.state.dashboard
    cfg = state.config
    now = datetime.now(state.tz)

    # Subway arrivals
    subway_stations = []
    for resolved in state.resolved_subway:
        arrivals = state.subway.arrivals_at_stop(
            parent_stop_id=resolved["stop_id"],
            routes=resolved["routes"],
            direction=resolved["direction"],
        )
        # Group by (route, direction), keep top N
        by_group: dict[tuple[str, str], list] = {}
        for a in arrivals:
            mins = a.minutes_away
            if mins < cfg.display.min_minutes_out or mins > cfg.display.max_minutes_out:
                continue
            key = (a.route, a.direction)
            by_group.setdefault(key, [])
            if len(by_group[key]) < cfg.display.max_arrivals_per_route:
                by_group[key].append({
                    "minutes": round(mins),
                    "direction": a.direction,
                    "arrival_time": a.arrival_time.astimezone(state.tz).isoformat(),
                })
        routes_out = [
            {
                "route": route,
                "direction": direction,
                "arrivals": arrivals_list,
            }
            for (route, direction), arrivals_list in sorted(by_group.items())
        ]
        subway_stations.append({
            "name": resolved["name"],
            "routes": routes_out,
        })

    # Bus arrivals
    bus_stops = []
    for s in cfg.bus_stops:
        arrivals = state.bus.arrivals_at_stop(s.stop_code, s.routes or None)
        # Group by route
        by_route: dict[str, list] = {}
        for a in arrivals:
            mins = a.minutes_away
            if mins < cfg.display.min_minutes_out or mins > cfg.display.max_minutes_out:
                continue
            by_route.setdefault(a.route, [])
            if len(by_route[a.route]) < cfg.display.max_arrivals_per_route:
                by_route[a.route].append({
                    "minutes": round(mins),
                    "headsign": a.headsign,
                    "distance_text": a.distance_text,
                    "arrival_time": a.arrival_time.astimezone(state.tz).isoformat(),
                })
        routes_out = [
            {"route": route, "arrivals": arrs}
            for route, arrs in sorted(by_route.items())
        ]
        bus_stops.append({
            "label": s.label,
            "stop_code": s.stop_code,
            "routes": routes_out,
        })

    # Hebrew date + Omer
    hd = state.hebcal.hebrew_date_today()
    om = state.hebcal.omer_today()
    hebrew_today = {
        "title": hd.title if hd else None,
        "hebrew": hd.hebrew if hd else None,
        "omer_day": om.day if om else None,
        "omer_title": om.title if om else None,
    }

    # Shabbat / yontif
    shabbat_state = state.hebcal.current_state()
    shabbat_data = {
        "phase": shabbat_state.phase,
        "observance_label": shabbat_state.observance_label,
        "parsha": (
            {
                "name": shabbat_state.parsha.name,
                "hebrew": shabbat_state.parsha.hebrew,
            }
            if shabbat_state.parsha else None
        ),
        "next_candles": (
            {
                "title": shabbat_state.next_candles.title,
                "when": shabbat_state.next_candles.when.isoformat(),
                "memo": shabbat_state.next_candles.memo,
            }
            if shabbat_state.next_candles else None
        ),
        "next_havdalah": (
            {
                "title": shabbat_state.next_havdalah.title,
                "when": shabbat_state.next_havdalah.when.isoformat(),
                "memo": shabbat_state.next_havdalah.memo,
            }
            if shabbat_state.next_havdalah else None
        ),
    }

    # Weather
    w = state.weather.current_state()
    precip_threshold = cfg.weather.precip_probability_threshold
    precip_likely = (
        w.precip_probability_today is not None
        and w.precip_probability_today >= precip_threshold
    )
    weather_data = {
        "current_temp": w.current_temp,
        "current_emoji": code_to_emoji(w.current_code),
        "current_label": code_to_label(w.current_code),
        "high": w.high,
        "low": w.low,
        "units": w.units,
        "precip_probability_today": w.precip_probability_today,
        "precip_likely": precip_likely,
    }

    return JSONResponse({
        "server_time": now.isoformat(),
        "hebrew_today": hebrew_today,
        "shabbat": shabbat_data,
        "subway_stations": subway_stations,
        "bus_stops": bus_stops,
        "weather": weather_data,
    })


# Static files: index.html at root, plus /static/*
_static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(_static_dir / "index.html")


def main() -> None:
    import argparse
    import os
    import uvicorn

    parser = argparse.ArgumentParser(description="Wall dashboard server")
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("SHAB_TRAIN_CLOCK_PORT", "8000")),
        help="Port to bind (default: 8000, or $SHAB_TRAIN_CLOCK_PORT)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("SHAB_TRAIN_CLOCK_HOST", "0.0.0.0"),
        help="Host to bind (default: 0.0.0.0)",
    )
    args = parser.parse_args()
    uvicorn.run("shab_train_clock.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
