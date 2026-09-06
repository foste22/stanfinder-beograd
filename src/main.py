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
            c["search_url"],
            float(c.get("request_delay_seconds", 1.5)),
            int(c.get("pages_per_category", 4)),
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


def parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def is_due(value: str | None, hours: float, now: datetime) -> bool:
    dt = parse_dt(value)
    if dt is None:
        return True
    try:
        return now - dt >= timedelta(hours=hours)
    except TypeError:
        # Defensive fallback if an old record had an offset-naive timestamp.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
        return now - dt >= timedelta(hours=hours)


def unique_images(values) -> list[str]:
    if not isinstance(values, list):
        values = [values] if values else []

    out = []
    seen = set()

    for value in values:
        if not value:
            continue
        url = str(value).strip()
        low = url.lower()

        if not (low.startswith("http://") or low.startswith("https://")):
            continue
        if any(x in low for x in (
            "logo", "avatar", "icon", "sprite",
            "banner", "placeholder", "favicon",
        )):
            continue

        clean = url.split("#", 1)[0]
        if clean not in seen:
            seen.add(clean)
            out.append(clean)

    return out


def merge_images(old_values, new_values) -> list[str]:
    # Prefer fresh order, then preserve any older URLs that disappeared
    # from current markup.
    return unique_images(list(new_values or []) + list(old_values or []))


def retry_hours(status: str | None, maintenance_cfg: dict) -> float | None:
    mapping = {
        "no_valid_photos": float(maintenance_cfg.get("retry_no_photos_hours", 12)),
        "missing_map_coordinates": float(maintenance_cfg.get("retry_missing_coords_hours", 4)),
        "parse_failed": float(maintenance_cfg.get("retry_parse_failed_hours", 6)),
        "insufficient_route_data": float(maintenance_cfg.get("retry_route_missing_hours", 6)),
        "inactive": float(maintenance_cfg.get("retry_inactive_hours", 24)),
    }
    return mapping.get(status)


def retry_is_due(previous: dict, maintenance_cfg: dict, now: datetime) -> bool:
    if not isinstance(previous, dict):
        return True

    status = previous.get("status")
    hours = retry_hours(status, maintenance_cfg)

    if hours is None:
        # Final statuses: accepted listing already exists in state, or the ad
        # was definitively outside user filters.
        return status not in {
            "accepted",
            "over_price",
            "over_average_time",
            "over_single_trip_time",
        }

    return is_due(previous.get("seen_at"), hours, now)


def refresh_existing_gallery(scraper, rec: dict) -> dict:
    source = rec.get("source")
    url = rec.get("url", "")
    source_id = rec.get("source_id", "")

    if source == "4zida":
        return scraper.refresh_details(url, source_id)

    fn = getattr(scraper, "refresh_existing", None)
    if callable(fn):
        return fn(url, source_id)

    return {}


def main():
    cfg = load_config()
    app_cfg = cfg["app"]
    route_cfg = cfg["routing"]
    destinations = cfg["destinations"]
    maintenance_cfg = cfg.get("maintenance", {})
    scrapers = build_scrapers(cfg)

    state = load_state()
    state.setdefault("seen", {})
    state.setdefault("listings", [])
    state.setdefault("geocode_cache", {})

    now = datetime.now(ZoneInfo(app_cfg["timezone"]))
    now_iso = now.isoformat()

    four = scrapers.get("4zida")

    # ============================================================
    # A) MIGRACIJA + ČIŠĆENJE JAVNE LISTE
    # ============================================================
    # 0 slika => oglas NE SME biti na sajtu.
    # Takav ID ostaje u seen bazi i može kasnije ponovo da se pokuša.
    cleaned = []
    hidden_no_photo = 0

    for rec in state["listings"]:
        key = rec.get("id") or listing_key(
            rec.get("source", "?"),
            rec.get("source_id", "?"),
        )
        rec["id"] = key

        old_images = rec.get("images")
        if not isinstance(old_images, list):
            old_images = [rec.get("image_url")] if rec.get("image_url") else []

        images = unique_images(old_images)

        if rec.get("source") == "4zida" and four:
            try:
                images = four.clean_image_urls(
                    images,
                    expected_listing_id=rec.get("source_id"),
                )
            except Exception:
                pass

        rec["images"] = images
        rec["image_url"] = images[0] if images else ""

        if not images:
            hidden_no_photo += 1
            state["seen"][key] = {
                **(state["seen"].get(key) or {}),
                "status": "no_valid_photos",
                "seen_at": now_iso,
                "url": rec.get("url"),
                "source": rec.get("source"),
                "source_id": rec.get("source_id"),
            }
            continue

        cleaned.append(rec)

    state["listings"] = cleaned

    # Existing accepted record = authoritative cache.
    # It will NEVER be parsed/geocoded/routed again just because it appears
    # on a search page.
    existing_by_id = {
        rec["id"]: rec
        for rec in state["listings"]
        if rec.get("id")
    }

    # ============================================================
    # B) DISCOVERY: LIST PAGES ONLY, DETAILS ONLY FOR TRULY NEW IDs
    # ============================================================
    parsed = []
    source_counts = {}
    pipeline_stats = {
        source: {
            "candidates": 0,
            "known_skipped": 0,
            "retry_backoff_skipped": 0,
            "unseen_selected": 0,
            "parsed_ok": 0,
            "no_photos": 0,
            "over_price": 0,
            "geocoded_or_coords": 0,
            "missing_coords": 0,
            "route_insufficient": 0,
            "over_average": 0,
            "over_single": 0,
            "accepted": 0,
        }
        for source in scrapers
    }

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

        pipeline_stats[source]["candidates"] = len(candidates)

        unseen = []

        for c in candidates:
            key = listing_key(source, c.source_id)

            # Crucial optimization: if accepted listing is already in public
            # state, stop here. No detail page, images, geocoding or GTFS.
            existing = existing_by_id.get(key)
            if existing is not None:
                existing["last_seen_in_search_at"] = now_iso
                pipeline_stats[source]["known_skipped"] += 1
                state["seen"][key] = {
                    **(state["seen"].get(key) or {}),
                    "status": "accepted",
                    "seen_at": now_iso,
                    "url": existing.get("url"),
                }
                continue

            previous = state["seen"].get(key, {})

            if previous and not retry_is_due(previous, maintenance_cfg, now):
                pipeline_stats[source]["retry_backoff_skipped"] += 1
                continue

            unseen.append(c)

        unseen = unseen[:max_new]
        source_counts[source] = {
            "candidates": len(candidates),
            "new": len(unseen),
        }
        pipeline_stats[source]["unseen_selected"] = len(unseen)

        for c in unseen:
            key = listing_key(source, c.source_id)

            try:
                item = scraper.get_listing(c)

                if item is None:
                    state["seen"][key] = {
                        "status": "parse_failed",
                        "seen_at": now_iso,
                        "url": c.url,
                    }
                    continue

                pipeline_stats[source]["parsed_ok"] += 1

                if item.price_eur > max_price:
                    pipeline_stats[source]["over_price"] += 1
                    state["seen"][key] = {
                        "status": "over_price",
                        "seen_at": now_iso,
                        "url": item.url,
                    }
                    continue

                item.images = unique_images(item.images)

                if not item.images:
                    pipeline_stats[source]["no_photos"] += 1
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

    # ============================================================
    # C) GEOCODING SAMO ZA NOVO PARSIRANE OGLASE
    # ============================================================
    geocoder = CachedNominatimGeocoder(
        state["geocode_cache"],
        max_new_requests=int(
            cfg.get("geocoding", {}).get("max_new_requests_per_run", 24)
        ),
        min_interval_seconds=float(
            cfg.get("geocoding", {}).get("min_interval_seconds", 16)
        ),
    )

    geocode_priority = {
        "halo_oglasi": 0,
        "nekretnine_rs": 1,
        "oglasi_rs": 2,
        "4zida": 3,
    }

    ordered_parsed = sorted(
        parsed,
        key=lambda x: (
            0 if (x.lat is None or x.lon is None) else 1,
            geocode_priority.get(x.source, 9),
        ),
    )

    routable = []

    for item in ordered_parsed:
        key = listing_key(item.source, item.source_id)

        if item.lat is None or item.lon is None:
            geo = geocoder.geocode(item.address, item.neighborhood)
            if geo:
                item.lat = geo["lat"]
                item.lon = geo["lon"]
                item.approximate_location = True

        if item.lat is None or item.lon is None:
            pipeline_stats[item.source]["missing_coords"] += 1
            state["seen"][key] = {
                "status": "missing_map_coordinates",
                "seen_at": now_iso,
                "address": item.address,
                "neighborhood": item.neighborhood,
                "url": item.url,
            }
            continue

        pipeline_stats[item.source]["geocoded_or_coords"] += 1
        routable.append(item)

    # ============================================================
    # D) GTFS ROUTING SAMO ZA NOVE OGLASE
    # ============================================================
    accepted_now = []

    if routable:
        router = GTFSRouter(
            gtfs_path=route_cfg["gtfs_path"],
            gtfs_url=route_cfg["gtfs_url"],
            max_age_hours=float(route_cfg.get("gtfs_max_age_hours", 24)),
            walking_speed_mps=float(route_cfg.get("walking_speed_mps", 1.33)),
            walking_distance_factor=float(
                route_cfg.get("walking_distance_factor", 1.20)
            ),
            max_access_walk_m=float(
                route_cfg.get("max_access_walk_m", 1200)
            ),
            max_egress_walk_m=float(
                route_cfg.get("max_egress_walk_m", 1200)
            ),
            max_transfer_walk_m=float(
                route_cfg.get("max_transfer_walk_m", 350)
            ),
            max_transit_rides=int(
                route_cfg.get("max_transit_rides", 4)
            ),
        )

        samples = sample_datetimes(
            app_cfg["timezone"],
            route_cfg["sample_times"],
        )

        for item in routable:
            key = listing_key(item.source, item.source_id)
            per_dest = {d: [] for d in range(len(destinations))}
            failed = False

            print(
                f"Računam NOVI [{item.source}]: "
                f"{item.neighborhood or ''} {item.address} / "
                f"{item.price_eur} € / {len(item.images)} slika"
            )

            for departure in samples:
                for di, dest in enumerate(destinations):
                    try:
                        minutes = router.travel_minutes(
                            item.lat,
                            item.lon,
                            dest["lat"],
                            dest["lon"],
                            departure,
                        )
                    except Exception as exc:
                        print(f"[WARN] ruta {item.source}: {exc}")
                        minutes = None
                        failed = True

                    if minutes is not None:
                        per_dest[di].append(minutes)

            min_samples = int(app_cfg["min_samples_per_destination"])

            if failed or any(
                len(per_dest[d]) < min_samples
                for d in per_dest
            ):
                pipeline_stats[item.source]["route_insufficient"] += 1
                state["seen"][key] = {
                    "status": "insufficient_route_data",
                    "seen_at": now_iso,
                    "url": item.url,
                }
                continue

            dest_avgs = [
                round(statistics.mean(per_dest[d]), 1)
                for d in range(len(destinations))
            ]
            all_samples = [
                value
                for values in per_dest.values()
                for value in values
            ]
            avg = round(statistics.mean(all_samples), 1)
            worst = round(max(all_samples), 1)

            if avg > float(app_cfg["max_average_minutes"]):
                pipeline_stats[item.source]["over_average"] += 1
                state["seen"][key] = {
                    "status": "over_average_time",
                    "seen_at": now_iso,
                    "average_minutes": avg,
                    "url": item.url,
                }
                continue

            if worst > float(app_cfg["max_single_trip_minutes"]):
                pipeline_stats[item.source]["over_single"] += 1
                state["seen"][key] = {
                    "status": "over_single_trip_time",
                    "seen_at": now_iso,
                    "worst_trip_minutes": worst,
                    "url": item.url,
                }
                continue

            expected = getattr(item, "expected_image_count", None)

            record = {
                "id": key,
                "source": item.source,
                "source_id": item.source_id,
                "title": item.title,
                "url": item.url,
                "images": item.images,
                "image_url": item.images[0],
                "expected_image_count": expected,
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
                "route_samples": {
                    str(d): per_dest[d]
                    for d in per_dest
                },
                "routing_method": (
                    "Belgrade GTFS + approximate walking"
                ),
                "first_seen_at": now_iso,
                "last_seen_in_search_at": now_iso,
                "last_active_check_at": now_iso,
                # Don't immediately re-open a new listing just because
                # its first parse produced one image.
                "last_gallery_refresh_at": now_iso,
            }

            state["listings"].append(record)
            existing_by_id[key] = record
            accepted_now.append(record)
            pipeline_stats[item.source]["accepted"] += 1

            state["seen"][key] = {
                "status": "accepted",
                "seen_at": now_iso,
                "average_minutes": avg,
                "url": item.url,
            }

    # ============================================================
    # E) GALERIJE: ODVOJEN, MALI MAINTENANCE BATCH
    # ============================================================
    gallery_interval = float(
        maintenance_cfg.get("gallery_refresh_interval_hours", 6)
    )
    gallery_batch_size = int(
        maintenance_cfg.get("gallery_refresh_batch_size", 8)
    )

    gallery_candidates = []

    for rec in state["listings"]:
        images = rec.get("images") or []
        expected = rec.get("expected_image_count")

        incomplete = (
            len(images) <= 1
            or (
                isinstance(expected, int)
                and expected > len(images)
            )
        )

        if not incomplete:
            continue

        if not is_due(
            rec.get("last_gallery_refresh_at"),
            gallery_interval,
            now,
        ):
            continue

        gallery_candidates.append(rec)

    gallery_candidates.sort(
        key=lambda rec: (
            0 if len(rec.get("images") or []) <= 1 else 1,
            -int(rec.get("expected_image_count") or 0),
        )
    )

    gallery_refreshed = 0
    gallery_improved = 0

    for rec in gallery_candidates[:gallery_batch_size]:
        scraper = scrapers.get(rec.get("source"))
        if not scraper:
            continue

        rec["last_gallery_refresh_at"] = now_iso
        gallery_refreshed += 1

        try:
            details = refresh_existing_gallery(scraper, rec)
        except Exception as exc:
            print(
                f"[WARN] gallery-refresh "
                f"{rec.get('source')} {rec.get('source_id')}: {exc}"
            )
            continue

        fresh = unique_images(details.get("images") or [])
        merged = merge_images(rec.get("images") or [], fresh)

        if len(merged) > len(rec.get("images") or []):
            gallery_improved += 1
            rec["images"] = merged
            rec["image_url"] = merged[0]

        expected = details.get("expected_image_count")
        if isinstance(expected, int) and expected > 0:
            rec["expected_image_count"] = max(
                expected,
                int(rec.get("expected_image_count") or 0),
            )

        neighborhood = details.get("neighborhood")
        if neighborhood and not rec.get("neighborhood"):
            rec["neighborhood"] = neighborhood

    # ============================================================
    # F) ACTIVE CHECK: NE SVAKIH 15 MIN ZA SVAKI STARI OGLAS
    # ============================================================
    active_interval = float(
        maintenance_cfg.get("active_check_interval_hours", 6)
    )
    active_batch_size = int(
        maintenance_cfg.get("active_check_batch_size", 12)
    )

    active_due = []

    for rec in state["listings"]:
        # If this ID was just seen on the portal's list page, it is active
        # enough for this run. No reason to open detail page again.
        if not is_due(
            rec.get("last_seen_in_search_at"),
            active_interval,
            now,
        ):
            continue

        if not is_due(
            rec.get("last_active_check_at"),
            active_interval,
            now,
        ):
            continue

        active_due.append(rec)

    remove_ids = set()
    active_checked = 0

    for rec in active_due[:active_batch_size]:
        scraper = scrapers.get(rec.get("source"))
        if not scraper:
            continue

        rec["last_active_check_at"] = now_iso
        active_checked += 1

        try:
            active = scraper.check_active(
                rec.get("url", ""),
                rec.get("source_id", ""),
            )
        except Exception as exc:
            print(
                f"[WARN] active-check "
                f"{rec.get('source')}: {exc}"
            )
            continue

        if active is False:
            remove_ids.add(rec["id"])
            state["seen"][rec["id"]] = {
                "status": "inactive",
                "seen_at": now_iso,
                "url": rec.get("url"),
            }

    if remove_ids:
        state["listings"] = [
            rec
            for rec in state["listings"]
            if rec.get("id") not in remove_ids
        ]

    # One final safety gate: public output can never contain a zero-photo ad.
    final_listings = []

    for rec in state["listings"]:
        images = unique_images(rec.get("images") or [])
        if not images:
            key = rec.get("id")
            if key:
                state["seen"][key] = {
                    **(state["seen"].get(key) or {}),
                    "status": "no_valid_photos",
                    "seen_at": now_iso,
                    "url": rec.get("url"),
                }
            continue

        rec["images"] = images
        rec["image_url"] = images[0]
        final_listings.append(rec)

    state["listings"] = final_listings

    state["listings"].sort(
        key=lambda x: (
            x.get("average_minutes", 999),
            x.get("price_eur", 9999),
        )
    )

    total_by_source = {}
    for rec in state["listings"]:
        src = rec.get("source", "?")
        total_by_source[src] = total_by_source.get(src, 0) + 1

    print("\n========== PIPELINE DIAGNOSTIKA ==========")
    for src, stats in pipeline_stats.items():
        print(
            f"[{src}] "
            f"candidates={stats['candidates']} "
            f"KNOWN_SKIPPED={stats['known_skipped']} "
            f"backoff={stats['retry_backoff_skipped']} "
            f"selected={stats['unseen_selected']} "
            f"parsed={stats['parsed_ok']} "
            f"no_photos={stats['no_photos']} "
            f"coords={stats['geocoded_or_coords']} "
            f"missing_coords={stats['missing_coords']} "
            f"route_missing={stats['route_insufficient']} "
            f"avg>limit={stats['over_average']} "
            f"worst>limit={stats['over_single']} "
            f"accepted_now={stats['accepted']} "
            f"TOTAL_ON_SITE={total_by_source.get(src, 0)}"
        )

    print(
        f"[MAINTENANCE] hidden_no_photo={hidden_no_photo} "
        f"gallery_checked={gallery_refreshed} "
        f"gallery_improved={gallery_improved} "
        f"active_checked={active_checked} "
        f"inactive_removed={len(remove_ids)}"
    )
    print("==========================================\n")

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
            "pipeline_stats": pipeline_stats,
            "total_by_source": total_by_source,
            "maintenance": {
                "hidden_no_photo": hidden_no_photo,
                "gallery_checked": gallery_refreshed,
                "gallery_improved": gallery_improved,
                "active_checked": active_checked,
                "inactive_removed": len(remove_ids),
            },
            "routing_method": (
                "Official Belgrade GTFS; schedule-based, no live traffic"
            ),
        },
        destinations=destinations,
    )

    if cfg.get("notifications", {}).get(
        "telegram", {}
    ).get("enabled", False):
        for record in accepted_now:
            try:
                notify_telegram(record)
            except Exception as exc:
                print(f"[WARN] Telegram: {exc}")

    print("Izvori:", source_counts)
    print(
        f"Gotovo. new_parsed={len(parsed)}, "
        f"new_routable={len(routable)}, "
        f"accepted_now={len(accepted_now)}, "
        f"total={len(state['listings'])}, "
        f"new_geocodes={geocoder.new_requests}"
    )


if __name__ == "__main__":
    main()
