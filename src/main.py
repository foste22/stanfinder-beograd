from __future__ import annotations

import os
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from scrapers.four_zida import FourZidaScraper
from scrapers.other_portals import (
    NekretnineRSScraper,
    HaloOglasiScraper,
    OglasiRSScraper,
)
from services.gtfs_router import GTFSRouter
from services.geocoder import CachedNominatimGeocoder
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


def build_scrapers(cfg: dict):
    sources = cfg["sources"]
    result = {}

    if sources.get("four_zida", {}).get("enabled"):
        c = sources["four_zida"]
        result["4zida"] = FourZidaScraper(
            c["search_url"], float(c.get("request_delay_seconds", 1.2))
        )

    if sources.get("nekretnine_rs", {}).get("enabled"):
        c = sources["nekretnine_rs"]
        result["nekretnine_rs"] = NekretnineRSScraper(
            c["search_url"], float(c.get("request_delay_seconds", 1.3))
        )

    if sources.get("halo_oglasi", {}).get("enabled"):
        c = sources["halo_oglasi"]
        result["halo_oglasi"] = HaloOglasiScraper(
            c["search_url"], float(c.get("request_delay_seconds", 1.5))
        )

    if sources.get("oglasi_rs", {}).get("enabled"):
        c = sources["oglasi_rs"]
        result["oglasi_rs"] = OglasiRSScraper(
            c["search_url"], float(c.get("request_delay_seconds", 1.3))
        )

    return result


def source_cfg(cfg: dict, source: str) -> dict:
    mapping = {
        "4zida": "four_zida",
        "nekretnine_rs": "nekretnine_rs",
        "halo_oglasi": "halo_oglasi",
        "oglasi_rs": "oglasi_rs",
    }
    return cfg["sources"][mapping[source]]


def main():
    cfg = load_config()
    app_cfg = cfg["app"]
    route_cfg = cfg["routing"]
    destinations = cfg["destinations"]
    scrapers = build_scrapers(cfg)

    state = load_state()
    state.setdefault("seen", {})
    state.setdefault("listings", [])
    state.setdefault("active_check_cursor", 0)
    state.setdefault("gallery_refresh_cursor", 0)
    state.setdefault("geocode_cache", {})

    now = datetime.now(ZoneInfo(app_cfg["timezone"]))
    now_iso = now.isoformat()

    four = scrapers.get("4zida")

    # ------------------------------------------------------------
    # A) 4zida special maintenance: clean + refresh its galleries.
    # ------------------------------------------------------------
    if four:
        for rec in state["listings"]:
            if rec.get("source") != "4zida":
                continue
            old_images = rec.get("images")
            if not isinstance(old_images, list):
                old_images = [rec.get("image_url")] if rec.get("image_url") else []
            images = four.clean_image_urls(
                old_images,
                expected_listing_id=rec.get("source_id"),
            )
            rec["images"] = images
            rec["image_url"] = images[0] if images else ""

        four_records = [
            rec for rec in state["listings"]
            if rec.get("source") == "4zida" and rec.get("url") and rec.get("source_id")
        ]
        priority = [
            rec for rec in four_records
            if len(rec.get("images") or []) <= 1 or not rec.get("neighborhood")
        ]
        remaining = [rec for rec in four_records if rec not in priority]
        batch = priority[:6]

        if len(batch) < 6 and remaining:
            cursor = int(state.get("gallery_refresh_cursor", 0)) % len(remaining)
            need = 6 - len(batch)
            for i in range(min(need, len(remaining))):
                batch.append(remaining[(cursor + i) % len(remaining)])
            state["gallery_refresh_cursor"] = cursor + need

        for rec in batch:
            try:
                details = four.refresh_details(rec["url"], rec["source_id"])
                images = details.get("images") or []
                if images:
                    rec["images"] = images
                    rec["image_url"] = images[0]
                if details.get("neighborhood"):
                    rec["neighborhood"] = details["neighborhood"]
            except Exception as exc:
                print(f"[WARN] 4zida refresh {rec.get('address')}: {exc}")

    # ------------------------------------------------------------
    # B) Rotating activity check across ALL portals.
    # ------------------------------------------------------------
    existing = state["listings"]
    if existing:
        batch_size = min(12, len(existing))
        start = int(state["active_check_cursor"]) % len(existing)
        indexes = [(start + i) % len(existing) for i in range(batch_size)]
        remove_ids = set()

        for idx in indexes:
            rec = existing[idx]
            scraper = scrapers.get(rec.get("source"))
            if not scraper:
                continue
            try:
                active = scraper.check_active(rec.get("url", ""), rec.get("source_id", ""))
                if active is False:
                    remove_ids.add(rec["id"])
                    state["seen"][rec["id"]] = {
                        "status": "inactive",
                        "seen_at": now_iso,
                        "url": rec.get("url"),
                    }
            except Exception as exc:
                print(f"[WARN] active-check {rec.get('source')}: {exc}")

        if remove_ids:
            state["listings"] = [
                x for x in state["listings"] if x.get("id") not in remove_ids
            ]
        state["active_check_cursor"] = start + batch_size

    # ------------------------------------------------------------
    # C) Discover and parse new ads from every enabled portal.
    # ------------------------------------------------------------
    parsed = []
    source_counts = {}
    max_price = int(app_cfg["max_price_eur"])

    for source, scraper in scrapers.items():
        scfg = source_cfg(cfg, source)
        max_new = int(scfg.get("max_new_details_per_run", 10))

        try:
            candidates = scraper.get_latest_candidates()
        except Exception as exc:
            print(f"[WARN] {source}: search page nije pročitana: {exc}")
            source_counts[source] = {"candidates": 0, "new": 0}
            continue

        unseen = []
        for c in candidates:
            key = listing_key(source, c.source_id)
            previous = state["seen"].get(key, {})
            previous_status = previous.get("status") if isinstance(previous, dict) else None

            # Retriable states: parser/geocoding can improve in later versions/runs.
            if key not in state["seen"] or previous_status in {
                "no_valid_photos",
                "missing_map_coordinates",
                "parse_failed",
            }:
                unseen.append(c)

        unseen = unseen[:max_new]
        source_counts[source] = {"candidates": len(candidates), "new": len(unseen)}

        for c in unseen:
            key = listing_key(source, c.source_id)
            try:
                item = scraper.get_listing(c)
                if item is None:
                    state["seen"][key] = {"status": "parse_failed", "seen_at": now_iso}
                    continue

                if item.price_eur > max_price:
                    state["seen"][key] = {"status": "over_price", "seen_at": now_iso}
                    continue

                if not item.images:
                    state["seen"][key] = {
                        "status": "no_valid_photos",
                        "seen_at": now_iso,
                        "url": item.url,
                    }
                    continue

                parsed.append(item)

            except Exception as exc:
                print(f"[WARN] {source} detalj {c.url}: {exc}")
                state["seen"][key] = {
                    "status": "parse_failed",
                    "seen_at": now_iso,
                    "url": c.url,
                }

    # ------------------------------------------------------------
    # D) Coordinate fallback using heavily rate-limited cached OSM.
    # ------------------------------------------------------------
    geocoder = CachedNominatimGeocoder(
        state["geocode_cache"],
        max_new_requests=int(cfg.get("geocoding", {}).get("max_new_requests_per_run", 4)),
        min_interval_seconds=float(cfg.get("geocoding", {}).get("min_interval_seconds", 16)),
    )

    routable = []
    for item in parsed:
        key = listing_key(item.source, item.source_id)

        if item.lat is None or item.lon is None:
            geo = geocoder.geocode(item.address, item.neighborhood)
            if geo:
                item.lat = geo["lat"]
                item.lon = geo["lon"]
                item.approximate_location = True

        if item.lat is None or item.lon is None:
            # Keep retryable: another run can use cached/geocoding quota.
            state["seen"][key] = {
                "status": "missing_map_coordinates",
                "seen_at": now_iso,
                "address": item.address,
                "neighborhood": item.neighborhood,
                "url": item.url,
            }
            continue

        routable.append(item)

    # ------------------------------------------------------------
    # E) GTFS routing once for all portals.
    # ------------------------------------------------------------
    accepted_now = []

    if routable:
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
        samples = sample_datetimes(app_cfg["timezone"], route_cfg["sample_times"])

        for item in routable:
            key = listing_key(item.source, item.source_id)
            per_dest = {d: [] for d in range(len(destinations))}
            failed = False

            print(
                f"Računam [{item.source}]: {item.neighborhood or ''} "
                f"{item.address} / {item.price_eur} € / {len(item.images)} slika"
            )

            for departure in samples:
                for di, dest in enumerate(destinations):
                    try:
                        minutes = router.travel_minutes(
                            item.lat, item.lon,
                            dest["lat"], dest["lon"],
                            departure,
                        )
                    except Exception as exc:
                        print(f"[WARN] ruta {item.source}: {exc}")
                        minutes = None
                        failed = True

                    if minutes is not None:
                        per_dest[di].append(minutes)

            min_samples = int(app_cfg["min_samples_per_destination"])
            if failed or any(len(per_dest[d]) < min_samples for d in per_dest):
                state["seen"][key] = {
                    "status": "insufficient_route_data",
                    "seen_at": now_iso,
                }
                continue

            dest_avgs = [
                round(statistics.mean(per_dest[d]), 1)
                for d in range(len(destinations))
            ]
            all_samples = [v for values in per_dest.values() for v in values]
            avg = round(statistics.mean(all_samples), 1)
            worst = round(max(all_samples), 1)

            if avg > float(app_cfg["max_average_minutes"]):
                state["seen"][key] = {
                    "status": "over_average_time",
                    "seen_at": now_iso,
                    "average_minutes": avg,
                }
                continue

            if worst > float(app_cfg["max_single_trip_minutes"]):
                state["seen"][key] = {
                    "status": "over_single_trip_time",
                    "seen_at": now_iso,
                    "worst_trip_minutes": worst,
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
                "neighborhood": item.neighborhood,
                "formatted_address": item.address,
                "approximate_location": item.approximate_location,
                "lat": item.lat,
                "lon": item.lon,
                "area_m2": item.area_m2,
                "rooms": item.rooms,
                "furnished": item.furnished,
                "heating": item.heating,
                "average_minutes": avg,
                "destination_averages": dest_avgs,
                "worst_sample_minutes": worst,
                "sample_times": route_cfg["sample_times"],
                "route_samples": {str(d): per_dest[d] for d in per_dest},
                "routing_method": "Belgrade GTFS + approximate walking",
                "first_seen_at": now_iso,
            }

            # No duplicate ID from the same portal.
            if not any(x.get("id") == key for x in state["listings"]):
                state["listings"].append(record)
                accepted_now.append(record)

            state["seen"][key] = {
                "status": "accepted",
                "seen_at": now_iso,
                "average_minutes": avg,
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
            "sources": list(scrapers.keys()),
            "routing_method": "Official Belgrade GTFS; schedule-based, no live traffic",
        },
        destinations=destinations,
    )

    if cfg.get("notifications", {}).get("telegram", {}).get("enabled", False):
        for record in accepted_now:
            try:
                notify_telegram(record)
            except Exception as exc:
                print(f"[WARN] Telegram: {exc}")

    print("Izvori:", source_counts)
    print(
        f"Gotovo. parsed={len(parsed)}, routable={len(routable)}, "
        f"accepted_now={len(accepted_now)}, total={len(state['listings'])}, "
        f"new_geocodes={geocoder.new_requests}"
    )


if __name__ == "__main__":
    main()
