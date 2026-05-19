# Wall Dashboard

A self-hosted info display for a wall-mounted tablet. Shows:
- Big clock (server-authoritative time)
- Shabbat / candle-lighting / havdalah (auto-active Thu evening through Sat night)
- NYC subway arrivals at configured stations
- NYC bus arrivals at configured stops

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
# Then put it in config.yaml

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

## Future

- [ ] Yontif handling (currently only Shabbat is highlighted) — backend mostly does this already via `observance_label`; revisit if anything looks off on an actual yontif
- [ ] Admin UI for editing config without YAML
- [ ] Service alerts surfaced on the display
- [ ] Multiple dashboard layouts (per-tablet config)
- [x] App port configurable at runtime (`--port` / `$SHAB_TRAIN_CLOCK_PORT`)
- [x] Candle lighting / havdalah times prefix the weekday when not "today"
