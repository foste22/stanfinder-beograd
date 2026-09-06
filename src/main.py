from __future__ import annotations

import os
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from scrapers.four_zida import FourZidaScraper
from services.google_maps import GoogleMapsClient
from storage import load_state, save_public, save_state
from notify import notify_telegram


ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)


def load_config() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def next_workday(now: datetime) -> datetime:
    d = now
    if d.weekday() >= 5:  # subota/nedelja
        d += timedelta(days=(7 - d.weekday()))
    return d


def sample_datetimes(timezone_name: str, times: list[str]) -> list[datetime]:
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    day = next_workday(now)

    result = []
    for t in times:
        hh, mm = map(int, t.split(":"))
        dt = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now:
            # Ako je današnji termin već prošao, koristi sledeći radni dan.
            dt += timedelta(days=1)
            while dt.weekday() >= 5:
                dt += timedelta(days=1)
        result.append(dt)
    return result


def listing_key(source: str, source_id: str) -> str:
    return f"{source}:{source_id}"


def main() -> None:
    cfg = load_config()
    app_cfg = cfg["app"]
    source_cfg = cfg["sources"]["four_zida"]
    destinations = cfg["destinations"]

    state = load_state()
    state.setdefault("seen", {})
    state.setdefault("listings", [])

    if not source_cfg.get("enabled", True):
        raise RuntimeError("Nijedan izvor nije uključen.")

    scraper = FourZidaScraper(
        source_cfg["search_url"],
        delay_seconds=float(source_cfg.get("request_delay_seconds", 1.2)),
    )
    candidates = scraper.get_latest_candidates()

    unseen = [
        c for c in candidates
        if listing_key("4zida", c.source_id) not in state["seen"]
    ]

    max_details = int(source_cfg.get("max_new_details_per_run", 20))
    unseen = unseen[:max_details]

    # Prvo parsiramo i primenjujemo najjeftiniji filter: cena.
    parsed = []
    now_iso = datetime.now(ZoneInfo(app_cfg["timezone"])).isoformat()

    for c in unseen:
        key = listing_key("4zida", c.source_id)
        try:
            item = scraper.get_listing(c)
            if item is None:
                state["seen"][key] = {"status": "parse_failed", "seen_at": now_iso}
                continue

            if item.price_eur > int(app_cfg["max_price_eur"]):
                state["seen"][key] = {"status": "over_price", "seen_at": now_iso}
                continue

            parsed.append(item)
        except Exception as exc:
            print(f"[WARN] Neuspešno čitanje {c.url}: {exc}")
            # Ne markiramo kao seen kako bi sledeći ciklus pokušao ponovo.

    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    maps = GoogleMapsClient(api_key)

    # Geokodiranje.
    geo_items = []
    for item in parsed:
        key = listing_key(item.source, item.source_id)
        try:
            geo = maps.geocode(item.address)
            if not geo:
                state["seen"][key] = {"status": "geocode_failed", "seen_at": now_iso}
                continue
            geo_items.append((item, geo))
        except Exception as exc:
            print(f"[WARN] Geocoding neuspešan za {item.address}: {exc}")

    # Google Routes transit matrica podržava max 100 origin×destination za TRANSIT.
    # Sa 2 destinacije obrađujemo najviše 50 stanova odjednom; naš MVP je podešen na 20.
    route_samples: dict[int, dict[int, list[float]]] = {
        i: {d: [] for d in range(len(destinations))}
        for i in range(len(geo_items))
    }

    times = sample_datetimes(app_cfg["timezone"], cfg["routing"]["sample_times"])
    origins = [{"lat": geo["lat"], "lon": geo["lon"]} for _, geo in geo_items]

    if origins:
        for departure in times:
            matrix = maps.route_matrix(origins, destinations, departure)
            for oi in range(len(origins)):
                for di in range(len(destinations)):
                    val = matrix.get(oi, {}).get(di)
                    if val is not None:
                        route_samples[oi][di].append(val)

    accepted_now = []

    for i, (item, geo) in enumerate(geo_items):
        key = listing_key(item.source, item.source_id)
        per_dest = route_samples[i]

        min_samples = int(app_cfg["min_samples_per_destination"])
        if any(len(per_dest[d]) < min_samples for d in per_dest):
            state["seen"][key] = {
                "status": "insufficient_route_data",
                "seen_at": now_iso,
                "address": item.address,
            }
            continue

        dest_avgs = [
            round(statistics.mean(per_dest[d]), 1)
            for d in range(len(destinations))
        ]
        all_samples = [x for vals in per_dest.values() for x in vals]
        overall_avg = round(statistics.mean(all_samples), 1)
        worst_trip = round(max(all_samples), 1)

        if overall_avg > float(app_cfg["max_average_minutes"]):
            state["seen"][key] = {
                "status": "over_average_time",
                "seen_at": now_iso,
                "average_minutes": overall_avg,
            }
            continue

        if worst_trip > float(app_cfg["max_single_trip_minutes"]):
            state["seen"][key] = {
                "status": "over_single_trip_time",
                "seen_at": now_iso,
                "worst_trip_minutes": worst_trip,
            }
            continue

        record = {
            "id": key,
            "source": item.source,
            "source_id": item.source_id,
            "title": item.title,
            "url": item.url,
            "price_eur": item.price_eur,
            "address": item.address,
            "formatted_address": geo["formatted_address"],
            "approximate_location": geo["approximate"],
            "lat": geo["lat"],
            "lon": geo["lon"],
            "area_m2": item.area_m2,
            "rooms": item.rooms,
            "furnished": item.furnished,
            "heating": item.heating,
            "average_minutes": overall_avg,
            "destination_averages": dest_avgs,
            "worst_sample_minutes": worst_trip,
            "sample_times": cfg["routing"]["sample_times"],
            "route_samples": {
                str(d): per_dest[d] for d in per_dest
            },
            "first_seen_at": now_iso,
        }

        # U slučaju ponovnog pojavljivanja istog ID-a ne pravimo duplikat.
        existing_index = next(
            (idx for idx, x in enumerate(state["listings"]) if x["id"] == key),
            None,
        )
        if existing_index is None:
            state["listings"].append(record)
            accepted_now.append(record)
        else:
            state["listings"][existing_index] = record

        state["seen"][key] = {
            "status": "accepted",
            "seen_at": now_iso,
            "average_minutes": overall_avg,
        }

    # Najbolje rute prve; ista ruta -> niža cena prva.
    state["listings"].sort(
        key=lambda x: (x.get("average_minutes", 999), x.get("price_eur", 9999))
    )

    save_state(state)
    save_public(
        state["listings"],
        meta={
            "updated_at": now_iso,
            "max_price_eur": app_cfg["max_price_eur"],
            "max_average_minutes": app_cfg["max_average_minutes"],
            "max_single_trip_minutes": app_cfg["max_single_trip_minutes"],
            "accepted_count": len(state["listings"]),
            "new_accepted_this_run": len(accepted_now),
        },
        destinations=destinations,
    )

    if cfg.get("notifications", {}).get("telegram", {}).get("enabled", False):
        for record in accepted_now:
            try:
                notify_telegram(record)
            except Exception as exc:
                print(f"[WARN] Telegram notifikacija nije poslata: {exc}")

    print(
        f"Gotovo. Kandidati={len(candidates)}, novi={len(unseen)}, "
        f"prihvaćeni sada={len(accepted_now)}, ukupno prihvaćeni={len(state['listings'])}"
    )


if __name__ == "__main__":
    main()
