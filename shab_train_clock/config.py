"""Config loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class LocationConfig(BaseModel):
    geonameid: int = 5110302  # Brooklyn
    timezone: str = "America/New_York"
    # Used by the weather client (Open-Meteo). Defaults to Crown Heights-ish.
    latitude: float = 40.6712
    longitude: float = -73.9442


class WeatherConfig(BaseModel):
    # "fahrenheit" or "celsius"
    units: Literal["fahrenheit", "celsius"] = "fahrenheit"
    # Probability threshold (%) above which we show a "rain expected" line.
    precip_probability_threshold: int = 40


class DisplayConfig(BaseModel):
    max_arrivals_per_route: int = 2
    max_minutes_out: int = 60
    min_minutes_out: int = 1


class SubwayStationConfig(BaseModel):
    name: str
    routes: list[str]
    direction: Literal["N", "S", "both"] = "both"


class BusStopConfig(BaseModel):
    stop_code: str
    label: str
    routes: list[str] = Field(default_factory=list)  # empty = all routes at stop


class Config(BaseModel):
    location: LocationConfig = Field(default_factory=LocationConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    subway_stations: list[SubwayStationConfig] = Field(default_factory=list)
    bus_stops: list[BusStopConfig] = Field(default_factory=list)


def load_config(path: Path | str = "config.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file {path} not found. Copy config.example.yaml to config.yaml and edit."
        )
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
