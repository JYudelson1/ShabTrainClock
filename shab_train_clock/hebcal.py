"""Hebcal/Shabbat data client.

Fetches candle lighting, havdalah, and parsha info from Hebcal.
Handles both regular Shabbat AND yontif (since Hebcal returns
candle/havdalah events for both).

We use the /hebcal endpoint (not /shabbat) because:
- It gives us a fixed date range we control
- It includes yontif candle lighting + havdalah events
- It correctly handles edge cases like yontif-into-Shabbat
  (Hebcal emits a single havdalah at the end of the combined period)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from astral import LocationInfo
from astral.sun import dusk

# Sun depression (degrees below horizon) for tzeit hakochavim — matches
# Hebcal's default for havdalah (Rambam's ~3 stars definition).
TZEIT_DEPRESSION_DEG = 8.5

logger = logging.getLogger(__name__)


@dataclass
class ShabbatEvent:
    """A candle lighting or havdalah event."""
    kind: Literal["candles", "havdalah"]
    when: datetime           # timezone-aware
    title: str               # "Candle lighting: 7:48pm"
    memo: str | None = None  # e.g. "Erev Pesach", "Shabbat", "Shavuot II"


@dataclass
class ParshaInfo:
    """The current week's Torah portion."""
    name: str    # e.g. "Parashat Bamidbar"
    hebrew: str  # e.g. "פרשת במדבר"


@dataclass
class HebrewDateInfo:
    """Hebrew date for a given civil date."""
    title: str        # e.g. "1st of Sivan, 5786"
    hebrew: str       # e.g. "א׳ סיון תשפ״ו"


@dataclass
class OmerInfo:
    """Omer count for a given date (only set during the Omer period)."""
    day: int          # 1..49
    title: str        # e.g. "33rd day of the Omer"


@dataclass
class ShabbatState:
    """Current Shabbat/yontif state for the dashboard."""
    # Where are we in the cycle?
    # "before": no upcoming candle lighting this week yet (rare; very early in week)
    # "approaching": candle lighting is the next event, not yet active
    # "active": we are currently in Shabbat/yontif (between candles and havdalah)
    # "after": post-havdalah, before next week's events
    phase: Literal["before", "approaching", "active", "after"]
    next_candles: ShabbatEvent | None
    next_havdalah: ShabbatEvent | None
    last_candles: ShabbatEvent | None  # most recent candle lighting in the past
    parsha: ParshaInfo | None
    # Human-readable label for what's happening: "Shabbat", "Pesach", "Shabbat + Pesach", etc.
    observance_label: str


class HebcalClient:
    """Fetches Shabbat/yontif data from Hebcal REST API."""

    def __init__(
        self,
        geonameid: int,
        timezone_name: str,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> None:
        self._geonameid = geonameid
        self._tz = ZoneInfo(timezone_name)
        # lat/lon used to compute sunset, so we can roll Hebrew date forward
        # after sundown (since Hebrew days start at sunset).
        self._location = (
            LocationInfo(
                "user", "user", timezone_name, latitude, longitude
            )
            if latitude is not None and longitude is not None
            else None
        )
        self._http = httpx.AsyncClient(timeout=10.0)
        # Cache: list of events covering the next ~10 days
        self._events: list[ShabbatEvent] = []
        self._parshiyot: dict[str, ParshaInfo] = {}  # date_str -> parsha for that Shabbat
        self._hebrew_dates: dict[str, HebrewDateInfo] = {}  # YYYY-MM-DD -> Hebrew date
        self._omer: dict[str, OmerInfo] = {}                # YYYY-MM-DD -> Omer info
        self._last_fetch: datetime | None = None

    async def close(self) -> None:
        await self._http.aclose()

    async def refresh(self) -> None:
        """Fetch a 10-day window of events. Call once a day at most."""
        now_local = datetime.now(self._tz)
        start = (now_local - timedelta(days=1)).date()
        end = (now_local + timedelta(days=10)).date()

        params = {
            "cfg": "json",
            "v": "1",
            "geonameid": str(self._geonameid),
            "c": "on",       # include candle lighting
            "M": "on",       # include havdalah at nightfall
            "maj": "on",     # major holidays
            "min": "off",    # skip minor holidays/fasts
            "mod": "off",
            "ss": "off",
            "mf": "off",
            "s": "on",       # include parshiyot
            "d": "on",       # include Hebrew date for each day
            "o": "on",       # include Omer count
            "leyning": "off",
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

        try:
            resp = await self._http.get(
                "https://www.hebcal.com/hebcal", params=params
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("Failed to fetch Hebcal data")
            return

        events: list[ShabbatEvent] = []
        parshiyot: dict[str, ParshaInfo] = {}
        hebrew_dates: dict[str, HebrewDateInfo] = {}
        omer: dict[str, OmerInfo] = {}
        for item in data.get("items", []):
            try:
                cat = item.get("category")
                if cat == "hebdate":
                    hebrew_dates[item["date"]] = HebrewDateInfo(
                        title=item.get("title", ""),
                        hebrew=item.get("hebrew", ""),
                    )
                elif cat == "omer":
                    # Hebcal's `omer` field is a rich object ({count, sefira, ...});
                    # parse the day number from `title_orig` ("Omer 45") or the
                    # title ("45th day of the Omer").
                    day_num: int | None = None
                    m = re.match(r"Omer\s+(\d+)", item.get("title_orig", ""))
                    if m:
                        day_num = int(m.group(1))
                    else:
                        m = re.match(r"(\d+)", item.get("title", ""))
                        if m:
                            day_num = int(m.group(1))
                    if day_num is not None:
                        omer[item["date"]] = OmerInfo(
                            day=day_num,
                            title=item.get("title", ""),
                        )
                elif cat == "candles":
                    events.append(
                        ShabbatEvent(
                            kind="candles",
                            when=datetime.fromisoformat(item["date"]),
                            title=item.get("title", ""),
                            memo=item.get("memo"),
                        )
                    )
                elif cat == "havdalah":
                    events.append(
                        ShabbatEvent(
                            kind="havdalah",
                            when=datetime.fromisoformat(item["date"]),
                            title=item.get("title", ""),
                            memo=item.get("memo"),
                        )
                    )
                elif cat == "parashat":
                    # date is YYYY-MM-DD (a Saturday)
                    parshiyot[item["date"]] = ParshaInfo(
                        name=item.get("title", ""),
                        hebrew=item.get("hebrew", ""),
                    )
            except Exception:
                logger.exception("Failed to parse Hebcal item: %r", item)
                continue

        events.sort(key=lambda e: e.when)
        self._events = events
        self._parshiyot = parshiyot
        self._hebrew_dates = hebrew_dates
        self._omer = omer
        self._last_fetch = now_local
        logger.info(
            "Hebcal: %d candle/havdalah events, %d parshiyot, %d hebrew dates, %d omer days",
            len(events), len(parshiyot), len(hebrew_dates), len(omer),
        )

    def _hebrew_date_key(self) -> str:
        """Civil date for today's Hebrew day. After local tzeit hakochavim
        (sun 8.5° below horizon — same definition as Hebcal's default
        havdalah), roll forward by one civil day, since Hebcal files each
        Hebrew day under the Gregorian date during which its daytime falls."""
        now = datetime.now(self._tz)
        d = now.date()
        if self._location is not None:
            try:
                tzeit = dusk(
                    self._location.observer,
                    date=d,
                    depression=TZEIT_DEPRESSION_DEG,
                    tzinfo=self._tz,
                )
                if now >= tzeit:
                    d = d + timedelta(days=1)
            except Exception:
                logger.exception("Failed to compute tzeit; using civil date")
        return d.isoformat()

    def hebrew_date_today(self) -> HebrewDateInfo | None:
        return self._hebrew_dates.get(self._hebrew_date_key())

    def omer_today(self) -> OmerInfo | None:
        return self._omer.get(self._hebrew_date_key())

    def current_state(self) -> ShabbatState:
        """Compute the current Shabbat state. Cheap; call as often as needed."""
        now = datetime.now(self._tz)

        next_candles = None
        next_havdalah = None
        last_candles = None

        for ev in self._events:
            if ev.when > now:
                if ev.kind == "candles" and next_candles is None:
                    next_candles = ev
                elif ev.kind == "havdalah" and next_havdalah is None:
                    next_havdalah = ev
            else:
                if ev.kind == "candles":
                    last_candles = ev  # keep updating to get the most recent

        # Determine phase: are we currently between candles and havdalah?
        active = False
        if last_candles and next_havdalah:
            # We had a recent candle lighting and havdalah is still upcoming
            if last_candles.when < now < next_havdalah.when:
                active = True

        if active:
            phase = "active"
        elif next_candles and next_havdalah and next_candles.when < next_havdalah.when:
            phase = "approaching"
        elif next_candles is None and next_havdalah is None:
            phase = "after"
        else:
            phase = "approaching"

        # Find this week's parsha. The relevant parsha is the one for the
        # upcoming Saturday (or today, if it IS Saturday).
        parsha = None
        if phase in ("active", "approaching") and next_candles:
            # The parsha for the Saturday on/after next_candles
            target_date = next_candles.when.date()
            # Walk forward to the next Saturday
            for _ in range(10):
                key = target_date.isoformat()
                if key in self._parshiyot:
                    parsha = self._parshiyot[key]
                    break
                target_date = target_date + timedelta(days=1)
        elif phase == "active" and last_candles:
            target_date = last_candles.when.date()
            for _ in range(10):
                key = target_date.isoformat()
                if key in self._parshiyot:
                    parsha = self._parshiyot[key]
                    break
                target_date = target_date + timedelta(days=1)

        # Observance label
        observance_label = self._observance_label(
            phase, last_candles, next_candles, next_havdalah
        )

        return ShabbatState(
            phase=phase,
            next_candles=next_candles,
            next_havdalah=next_havdalah,
            last_candles=last_candles,
            parsha=parsha,
            observance_label=observance_label,
        )

    @staticmethod
    def _observance_label(
        phase: str,
        last_candles: ShabbatEvent | None,
        next_candles: ShabbatEvent | None,
        next_havdalah: ShabbatEvent | None,
    ) -> str:
        """Build a human-readable label like 'Shabbat', 'Pesach', 'Shabbat + Pesach'."""
        relevant_memo = None
        if phase == "active" and last_candles:
            relevant_memo = last_candles.memo
        elif next_candles:
            relevant_memo = next_candles.memo

        if not relevant_memo:
            # Default to Shabbat if it's the right day of week
            return "Shabbat"

        memo = relevant_memo
        # Memo is often like "Pesach I" or "Shavuot II" or "Shabbat"
        # We just return it as-is; Hebcal already handles the combined case
        # by emitting two candle events with appropriate memos.
        return memo
