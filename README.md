# Wall Dashboard

A self-hosted info display for a wall-mounted tablet. Shows:
- Big clock (server-authoritative time)
- Shabbat / yom tov: candle-lighting / havdalah, parsha, Hebrew date, Omer count
- Weather (current conditions, today's high/low, precip likelihood)
- NYC subway arrivals at configured stations
- NYC bus arrivals at configured stops

During Shabbat and yom tov the transit columns hide themselves — see
[Behavior notes](#behavior-notes).

## Architecture

```
┌──────────────────┐         ┌────────────────────┐
│  Wall Tablet     │ poll    │  FastAPI server    │
│  (FreeKiosk      │ ──────► │  (this repo)       │
│   loads static/  │         │                    │
│   index.html)    │ ◄────── │  Polls MTA/Hebcal  │
└──────────────────┘  JSON   │  in background,    │
                             │  serves cached     │
                             │  JSON to clients   │
                             └────────────────────┘
```

The server polls upstream APIs on its own schedule and caches results. Tablets just hit `/api/data` every 15 seconds — they never talk to MTA/Hebcal directly. This means:
- No protobuf decoder in the browser
- API keys stay server-side
- Adding more tablets is free (no extra API load)
- All tablets show the exact same data at the exact same time

## Quickstart

```bash
# Install (you use uv, so):
uv sync

# Get a free MTA Bus API key:
# https://register.developer.obanyc.com/
# Then add it to ../../.env (LocalServerApps/.env) as
#   MTA_BUS_API_KEY=...
# This .env is shared across all sites under LocalServerApps/ and is
# gitignored. python-dotenv walks parent dirs to find it.

# Edit config.yaml with your stops/stations

# Run
uv run python -m shab_train_clock.main

# Visit http://localhost:8000 in a browser to preview
# Point FreeKiosk at http://<your-server-ip>:8000
```

## Config (`config.yaml`)

Lists subway stations and bus stops you care about. Names are fuzzy-matched
against MTA's static GTFS data at server startup — typos warn loudly, near
matches auto-resolve.

See `config.example.yaml` for the full schema.

## Behavior notes

Non-obvious things worth knowing before you edit:

### Transit hidden on Shabbat / yom tov
When it's Shabbat or a true yom tov (melacha forbidden), the bus and train
columns disappear and the clock + Shabbat block fill the screen. Driven by
`shabbat.phase === "active"` from the API.

- **Chol hamoed is *not* hidden** — transit shows on intermediate days, since
  travel is permitted. This falls out for free: Hebcal only brackets the
  true-chag days with candle-lighting/havdalah events and leaves chol hamoed
  unbracketed.
- **"active" = the most recent past candle/havdalah event is a candle-lighting.**
  We deliberately check the most recent event of *either* kind (not just the
  last candle) so motzei Shabbat and chol hamoed correctly read as
  not-active. Hebcal emits a candle event each evening of a multi-day yom
  tov, so continuous YT+Shabbat blocks stay active throughout.
- **Diaspora timing.** Keyed to the Brooklyn `geonameid`, so 2-day yom tov.
  Change `location.geonameid` for Israel/elsewhere and the chag days follow.

### Hebrew date & Omer roll over at nightfall, not midnight
A Hebrew day starts at sunset, so after dark the displayed Hebrew date and
Omer count advance to the next day. We roll over at **tzeit hakochavim**
(sun 8.5° below the horizon — the same definition Hebcal uses for its default
havdalah), computed locally from `location.latitude`/`longitude` via `astral`.
To use a different shita, change `TZEIT_DEPRESSION_DEG` in `hebcal.py`
(e.g. `7.0833` for the lighter Geonim definition). The Omer line only appears
during the Omer period (Pesach → Shavuot).

### Same-named subway stations expand into multiple rows
One `subway_stations` entry can resolve to several physical stops. E.g.
"Nostrand Ave" matches both the IRT (3) and IND (A/C) stations — two distinct
stops that share a name. At startup we cross-reference `trips.txt` +
`stop_times.txt` from the GTFS feed to learn which routes serve each stop, and
emit one display row per physical station, each carrying only the routes that
actually stop there. Requested routes served by no matching stop warn at
startup. (This makes startup take a few extra seconds — `stop_times.txt` is large.)

### Secrets live in a shared `.env`
The MTA bus key is read from `MTA_BUS_API_KEY` in `LocalServerApps/.env`
(one file shared by all sites under `LocalServerApps/`, gitignored). It is
*not* in `config.yaml`. `python-dotenv`'s `find_dotenv` walks parent dirs, so
it's found from anywhere in the tree.

### Weather has no API key
Uses Open-Meteo, which is free and keyless. Set `location.latitude`/
`longitude` for your exact spot; tune the rain-line threshold with
`weather.precip_probability_threshold`.

## Future

- [ ] Admin UI for editing config without YAML
- [ ] Service alerts surfaced on the display
- [ ] Multiple dashboard layouts (per-tablet config)
- [x] Yom tov handling: observance label, and transit auto-hides on chag/Shabbat (chol hamoed excluded)
- [x] Weather widget (Open-Meteo, keyless)
- [x] Hebrew date + Omer count, rolling over at tzeit hakochavim
- [x] App port configurable at runtime (`--port` / `$SHAB_TRAIN_CLOCK_PORT`)
- [x] Candle lighting / havdalah times prefix the weekday when not "today"
