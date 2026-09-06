from __future__ import annotations

import csv
import io
import math
import os
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests


INF = 10**12


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_gtfs_time(value: str) -> int:
    h, m, s = map(int, value.strip().split(":"))
    return h * 3600 + m * 60 + s


@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    lat: float
    lon: float


class GTFSRouter:
    """
    Mali schedule-based router napravljen za ovaj StanFinder.

    Nije zamena za Google Maps/OTP:
    - koristi zvanični GTFS red vožnje;
    - pešačenje do stajališta, između bliskih stajališta i od stajališta
      do odredišta procenjuje pravolinijskom udaljenošću * faktor;
    - ne sadrži live gužvu.

    Za rangiranje stanova po tipičnoj dostupnosti ovo daje stabilan,
    potpuno besplatan kriterijum.
    """

    def __init__(
        self,
        gtfs_path: str,
        gtfs_url: str,
        max_age_hours: float,
        walking_speed_mps: float = 1.33,
        walking_distance_factor: float = 1.20,
        max_access_walk_m: float = 1200,
        max_egress_walk_m: float = 1200,
        max_transfer_walk_m: float = 350,
        max_transit_rides: int = 4,
    ):
        self.gtfs_path = Path(gtfs_path)
        self.gtfs_url = gtfs_url
        self.max_age_hours = max_age_hours
        self.walking_speed_mps = walking_speed_mps
        self.walking_factor = walking_distance_factor
        self.max_access_walk_m = max_access_walk_m
        self.max_egress_walk_m = max_egress_walk_m
        self.max_transfer_walk_m = max_transfer_walk_m
        self.max_transit_rides = max_transit_rides

        self._ensure_gtfs()
        self._zip = zipfile.ZipFile(self.gtfs_path)
        self.stops: list[Stop] = []
        self.stop_index: dict[str, int] = {}
        self.transfers: list[list[tuple[int, int]]] = []
        self._load_stops()
        self._build_walk_transfers()

        self._cache_date: date | None = None
        self._cache_trips: list[list[tuple[int, int, int]]] | None = None

    def _ensure_gtfs(self) -> None:
        self.gtfs_path.parent.mkdir(parents=True, exist_ok=True)
        fresh = False
        if self.gtfs_path.exists() and self.gtfs_path.stat().st_size > 1000:
            age_h = (time.time() - self.gtfs_path.stat().st_mtime) / 3600.0
            fresh = age_h <= self.max_age_hours

        if fresh:
            return

        print("Preuzimam svež zvanični GTFS Beograda...")
        tmp = self.gtfs_path.with_suffix(".tmp")
        r = requests.get(
            self.gtfs_url,
            timeout=120,
            headers={"User-Agent": "StanFinder-Beograd/1.0 personal-project"},
        )
        r.raise_for_status()
        tmp.write_bytes(r.content)

        # Provera da je zaista ZIP.
        with zipfile.ZipFile(tmp) as z:
            required = {"stops.txt", "trips.txt", "stop_times.txt"}
            missing = required - set(z.namelist())
            if missing:
                raise RuntimeError(f"GTFS nema obavezne fajlove: {sorted(missing)}")

        tmp.replace(self.gtfs_path)

    def _rows(self, name: str):
        with self._zip.open(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8-sig", newline="")
            yield from csv.DictReader(text)

    def _load_stops(self) -> None:
        for row in self._rows("stops.txt"):
            try:
                stop = Stop(
                    stop_id=row["stop_id"],
                    name=row.get("stop_name", ""),
                    lat=float(row["stop_lat"]),
                    lon=float(row["stop_lon"]),
                )
            except Exception:
                continue
            self.stop_index[stop.stop_id] = len(self.stops)
            self.stops.append(stop)

    def _build_walk_transfers(self) -> None:
        """
        Prostorna mreža ćelija da ne računamo sve parove stajališta.
        """
        cell_deg = 0.0045  # oko 500 m u pravcu S-J
        grid: dict[tuple[int, int], list[int]] = defaultdict(list)

        for i, s in enumerate(self.stops):
            key = (int(s.lat / cell_deg), int(s.lon / cell_deg))
            grid[key].append(i)

        self.transfers = [[] for _ in self.stops]

        for i, s in enumerate(self.stops):
            x, y = int(s.lat / cell_deg), int(s.lon / cell_deg)
            seen = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in grid.get((x + dx, y + dy), []):
                        if i == j or j in seen:
                            continue
                        seen.add(j)
                        t = self.stops[j]
                        d = haversine_m(s.lat, s.lon, t.lat, t.lon)
                        if d <= self.max_transfer_walk_m:
                            sec = self._walk_seconds(d)
                            self.transfers[i].append((j, sec))

    def _walk_seconds(self, straight_line_m: float) -> int:
        effective = straight_line_m * self.walking_factor
        return int(round(effective / self.walking_speed_mps))

    def _active_services(self, day: date) -> set[str]:
        active: set[str] = set()
        ymd = int(day.strftime("%Y%m%d"))
        weekday = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][day.weekday()]

        if "calendar.txt" in self._zip.namelist():
            for row in self._rows("calendar.txt"):
                try:
                    if (
                        int(row.get(weekday, "0")) == 1
                        and int(row["start_date"]) <= ymd <= int(row["end_date"])
                    ):
                        active.add(row["service_id"])
                except Exception:
                    continue

        if "calendar_dates.txt" in self._zip.namelist():
            for row in self._rows("calendar_dates.txt"):
                try:
                    if int(row["date"]) != ymd:
                        continue
                    sid = row["service_id"]
                    typ = int(row["exception_type"])
                    if typ == 1:
                        active.add(sid)
                    elif typ == 2:
                        active.discard(sid)
                except Exception:
                    continue

        return active

    def _trips_for_date(self, day: date) -> list[list[tuple[int, int, int]]]:
        if self._cache_date == day and self._cache_trips is not None:
            return self._cache_trips

        active_services = self._active_services(day)
        if not active_services:
            raise RuntimeError(f"GTFS nema aktivnu uslugu za {day.isoformat()}")

        active_trip_ids: set[str] = set()
        for row in self._rows("trips.txt"):
            if row.get("service_id") in active_services:
                active_trip_ids.add(row["trip_id"])

        by_trip: dict[str, list[tuple[int, int, int, int]]] = defaultdict(list)

        for row in self._rows("stop_times.txt"):
            trip_id = row.get("trip_id")
            if trip_id not in active_trip_ids:
                continue

            stop_id = row.get("stop_id")
            idx = self.stop_index.get(stop_id)
            if idx is None:
                continue

            arr_raw = row.get("arrival_time") or row.get("departure_time")
            dep_raw = row.get("departure_time") or row.get("arrival_time")
            if not arr_raw or not dep_raw:
                continue

            try:
                seq = int(row.get("stop_sequence") or 0)
                arr = parse_gtfs_time(arr_raw)
                dep = parse_gtfs_time(dep_raw)
            except Exception:
                continue

            by_trip[trip_id].append((seq, idx, arr, dep))

        trips: list[list[tuple[int, int, int]]] = []
        for rows in by_trip.values():
            rows.sort(key=lambda x: x[0])
            seq = [(idx, arr, dep) for _, idx, arr, dep in rows]
            if len(seq) >= 2:
                trips.append(seq)

        self._cache_date = day
        self._cache_trips = trips
        print(f"GTFS: {len(self.stops)} stajališta, {len(trips)} aktivnih polazaka za {day}")
        return trips

    def _nearby_stops(self, lat: float, lon: float, max_m: float) -> list[tuple[int, int]]:
        out = []
        for i, s in enumerate(self.stops):
            d = haversine_m(lat, lon, s.lat, s.lon)
            if d <= max_m:
                out.append((i, self._walk_seconds(d)))
        out.sort(key=lambda x: x[1])
        return out

    def _apply_transfers(self, arrivals: list[int]) -> list[int]:
        out = arrivals[:]
        # Dva prolaza su dovoljna za mala presedanja/komplekse stajališta
        # i sprečavaju da pešačenje "putuje" kilometrima kroz lanac stanica.
        for _ in range(2):
            changed = False
            snapshot = out[:]
            for i, ai in enumerate(snapshot):
                if ai >= INF:
                    continue
                for j, walk_sec in self.transfers[i]:
                    cand = ai + walk_sec
                    if cand < out[j]:
                        out[j] = cand
                        changed = True
            if not changed:
                break
        return out

    def travel_minutes(
        self,
        origin_lat: float,
        origin_lon: float,
        dest_lat: float,
        dest_lon: float,
        departure: datetime,
    ) -> float | None:
        trips = self._trips_for_date(departure.date())
        start_sec = departure.hour * 3600 + departure.minute * 60 + departure.second

        access = self._nearby_stops(origin_lat, origin_lon, self.max_access_walk_m)
        egress = self._nearby_stops(dest_lat, dest_lon, self.max_egress_walk_m)

        # Dozvoli i čisto pešačenje ako su tačke blizu.
        direct_d = haversine_m(origin_lat, origin_lon, dest_lat, dest_lon)
        best_dest = start_sec + self._walk_seconds(direct_d) if direct_d <= 3000 else INF

        if not access or not egress:
            return None if best_dest >= INF else round((best_dest - start_sec) / 60.0, 1)

        arrivals = [INF] * len(self.stops)
        for idx, walk_sec in access:
            arrivals[idx] = min(arrivals[idx], start_sec + walk_sec)
        arrivals = self._apply_transfers(arrivals)

        egress_map = {idx: walk for idx, walk in egress}

        def update_destination(arr: list[int], current_best: int) -> int:
            best = current_best
            for idx, walk_sec in egress_map.items():
                if arr[idx] < INF:
                    best = min(best, arr[idx] + walk_sec)
            return best

        best_dest = update_destination(arrivals, best_dest)

        # Jedan krug = najviše jedna nova vožnja javnim prevozom.
        for _round in range(self.max_transit_rides):
            previous = arrivals
            transit = previous[:]

            for trip in trips:
                boarded = False
                for stop_idx, arr_sec, dep_sec in trip:
                    if not boarded and previous[stop_idx] <= dep_sec:
                        boarded = True
                    if boarded and arr_sec < transit[stop_idx]:
                        transit[stop_idx] = arr_sec

            arrivals = self._apply_transfers(transit)
            best_dest = update_destination(arrivals, best_dest)

            # Ako ništa nije poboljšano, nema smisla dalje.
            if arrivals == previous:
                break

        if best_dest >= INF:
            return None

        minutes = (best_dest - start_sec) / 60.0
        if minutes < 0 or minutes > 240:
            return None
        return round(minutes, 1)
