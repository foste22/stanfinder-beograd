from __future__ import annotations

import os
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from scrapers.four_zida import FourZidaScraper
from services.gtfs_router import GTFSRouter
from storage import load_state, save_public, save_state
from notify import notify_telegram


ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)


def load_config() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def next_workday(now: datetime) -> datetime:
    d = now
    if d.weekday() >= 5:
        d += timedelta(days=(7 - d.weekday()))
    return d


def sample_datetimes(timezone_name: str, times: list[str]) -> list[datetime]:
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    base = next_workday(now)
    result = []

    for t in times:
        hh, mm = map(int, t.split(":"))
        dt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now:
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
    route_cfg = cfg["routing"]
    source_cfg = cfg["sources"]["four_zida"]
    destinations = cfg["destinations"]

    state = load_state()
    state.setdefault("seen", {})
    state.setdefault("listings", [])
    state.setdefault("active_check_cursor", 0)

    scraper = FourZidaScraper(
        source_cfg["search_url"],
        delay_seconds=float(source_cfg.get("request_delay_seconds", 1.2)),
    )

    now = datetime.now(ZoneInfo(app_cfg["timezone"]))
    now_iso = now.isoformat()

    # ---------------------------------------------------------------
    # 1. OČISTI GALERIJE POSTOJEĆIH OGLASA
    #    Ako nakon deduplikacije nema nijedne validne fotografije,
    #    oglas više nije kandidat za StanFinder.
    # ---------------------------------------------------------------
    cleaned_existing = []
    removed_no_photos = 0

    for rec in state["listings"]:
        if rec.get("source") != "4zida":
            cleaned_existing.append(rec)
            continue

        old_images = rec.get("images")
        if not isinstance(old_images, list):
            old_images = [rec.get("image_url")] if rec.get("image_url") else []

        images = scraper.clean_image_urls(old_images)
        rec["images"] = images
        rec["image_url"] = images[0] if images else ""

        if not images:
            state["seen"][rec["id"]] = {
                "status": "no_valid_photos",
                "seen_at": now_iso,
                "url": rec.get("url"),
            }
            removed_no_photos += 1
            continue

        cleaned_existing.append(rec)

    state["listings"] = cleaned_existing

    # ---------------------------------------------------------------
    # 2. ROTIRAJUĆA PROVERA DA LI POSTOJEĆI OGLASI JOŠ POSTOJE
    # ---------------------------------------------------------------
    existing = state["listings"]
    if existing:
        batch_size = min(10, len(existing))
        start = int(state.get("active_check_cursor", 0)) % len(existing)
        indexes = [(start + i) % len(existing) for i in range(batch_size)]
        to_remove = set()

        for idx in indexes:
            rec = existing[idx]
            if rec.get("source") != "4zida":
                continue
            try:
                active = scraper.check_active(rec.get("url", ""), rec.get("source_id", ""))
                if active is False:
                    state["seen"][rec["id"]] = {
                        "status": "inactive",
                        "seen_at": now_iso,
                        "url": rec.get("url"),
                    }
                    to_remove.add(rec["id"])
            except Exception as exc:
                print(f"[WARN] Provera aktivnosti nije uspela: {exc}")

        if to_remove:
            state["listings"] = [
                x for x in state["listings"] if x.get("id") not in to_remove
            ]

        state["active_check_cursor"] = start + batch_size

    # ---------------------------------------------------------------
    # 3. NOVI OGLASI
    # ---------------------------------------------------------------
    candidates = scraper.get_latest_candidates()
    unseen = [
        c for c in candidates
        if listing_key("4zida", c.source_id) not in state["seen"]
    ][: int(source_cfg.get("max_new_details_per_run", 20))]

    parsed = []

    for c in unseen:
        key = listing_key("4zida", c.source_id)

        try:
            item = scraper.get_listing(c)
            if item is None:
                state["seen"][key] = {"status": "parse_failed", "seen_at": now_iso}
                continue

            # Najjeftiniji filter prvi.
            if item.price_eur > int(app_cfg["max_price_eur"]):
                state["seen"][key] = {"status": "over_price", "seen_at": now_iso}
                continue

            # NOVI OBAVEZNI FILTER: oglas mora imati bar jednu stvarnu
            # fotografiju pre nego što trošimo vreme na routing.
            if not item.images:
                state["seen"][key] = {
                    "status": "no_valid_photos",
                    "seen_at": now_iso,
                    "url": item.url,
                }
                continue

            if item.lat is None or item.lon is None:
                state["seen"][key] = {
                    "status": "missing_map_coordinates",
                    "seen_at": now_iso,
                    "address": item.address,
                    "url": item.url,
                }
                continue

            parsed.append(item)

        except Exception as exc:
            print(f"[WARN] Neuspešno čitanje {c.url}: {exc}")

    accepted_now = []

    # ---------------------------------------------------------------
    # 4. ROUTING SAMO ZA STANOVE KOJI SU PROŠLI CENU + FOTOGRAFIJE
    # ---------------------------------------------------------------
    if parsed:
        router = GTFSRouter(
            gtfs_path=route_cfg["gtfs_path"],
            gtfs_url=route_cfg["gtfs_url"],
            max_age_hours=float(route_cfg.get("gtfs_max_age_hours", 24)),
            walking_speed_mps=float(route_cfg.get("walking_speed_mps", 1.33)),
            walking_distance_factor=float(route_cfg.get("walking_distance_factor", 1.20)),
            max_access_walk_m=float(route_cfg.get("max_access_walk_m", 1200)),
            max_egress_walk_m=float(route_cfg.get("max_egress_walk_m", 1200)),
            max_transfer_walk_m=float(route_cfg.get("max_transfer_walk_m", 350)),
            max_transit_rides=int(route_cfg.get("max_transit_rides", 4)),
        )

        sample_times = sample_datetimes(app_cfg["timezone"], route_cfg["sample_times"])

        for item in parsed:
            key = listing_key(item.source, item.source_id)
            per_dest = {d: [] for d in range(len(destinations))}
            route_failed = False

            print(
                f"Računam: {item.address} / {item.price_eur} € / "
                f"{len(item.images)} originalnih fotografija"
            )

            for departure in sample_times:
                for di, dest in enumerate(destinations):
                    try:
                        minutes = router.travel_minutes(
                            item.lat, item.lon,
                            dest["lat"], dest["lon"],
                            departure,
                        )
                    except Exception as exc:
                        print(f"[WARN] Ruta nije izračunata: {exc}")
                        route_failed = True
                        minutes = None

                    if minutes is not None:
                        per_dest[di].append(minutes)

            min_samples = int(app_cfg["min_samples_per_destination"])
            if route_failed or any(len(per_dest[d]) < min_samples for d in per_dest):
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
                "images": item.images,
                "image_url": item.images[0],
                "price_eur": item.price_eur,
                "address": item.address,
                "formatted_address": item.address,
                "approximate_location": item.approximate_location,
                "lat": item.lat,
                "lon": item.lon,
                "area_m2": item.area_m2,
                "rooms": item.rooms,
                "furnished": item.furnished,
                "heating": item.heating,
                "average_minutes": overall_avg,
                "destination_averages": dest_avgs,
                "worst_sample_minutes": worst_trip,
                "sample_times": route_cfg["sample_times"],
                "route_samples": {str(d): per_dest[d] for d in per_dest},
                "routing_method": "Belgrade GTFS + approximate walking",
                "first_seen_at": now_iso,
            }

            state["listings"].append(record)
            accepted_now.append(record)

            state["seen"][key] = {
                "status": "accepted",
                "seen_at": now_iso,
                "average_minutes": overall_avg,
            }

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
            "removed_no_photos_this_run": removed_no_photos,
            "routing_method": "Official Belgrade GTFS; schedule-based, no live traffic",
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
        f"Gotovo. Novi kandidati={len(unseen)}, za routing={len(parsed)}, "
        f"prihvaćeni={len(accepted_now)}, bez fotografija uklonjeno={removed_no_photos}, "
        f"aktivnih na sajtu={len(state['listings'])}"
    )


if __name__ == "__main__":
    main()
