from __future__ import annotations

import time
import requests


class CachedNominatimGeocoder:
    """
    Very small personal-project fallback.

    Public Nominatim policy:
    - cache every result;
    - no repeated geocoding of the same query;
    - recurring scripts: stay at or below 4 requests/minute.

    We therefore use a 16 second minimum interval and max 4 NEW requests/run.
    """

    URL = "https://nominatim.openstreetmap.org/search"

    def __init__(self, cache: dict, max_new_requests: int = 4, min_interval_seconds: float = 16.0):
        self.cache = cache
        self.max_new_requests = max_new_requests
        self.min_interval_seconds = min_interval_seconds
        self.new_requests = 0
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "StanFinder-Beograd/1.0 personal-apartment-search",
            "Accept-Language": "sr,en;q=0.7",
        })

    def geocode(self, address: str, neighborhood: str | None = None):
        parts = []
        if address:
            parts.append(address)
        if neighborhood and neighborhood.lower() not in address.lower():
            parts.append(neighborhood)
        parts.extend(["Beograd", "Srbija"])
        query = ", ".join(parts)

        if query in self.cache:
            value = self.cache[query]
            if not value:
                return None
            return value

        if self.new_requests >= self.max_new_requests:
            return None

        elapsed = time.monotonic() - self.last_request
        if self.last_request and elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)

        try:
            r = self.session.get(
                self.URL,
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": 1,
                    "countrycodes": "rs",
                },
                timeout=35,
            )
            self.last_request = time.monotonic()
            self.new_requests += 1
            r.raise_for_status()
            rows = r.json()
        except Exception as exc:
            print(f"[WARN] Nominatim geocoding nije uspeo za {query}: {exc}")
            return None

        if not rows:
            self.cache[query] = None
            return None

        row = rows[0]
        result = {
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "display_name": row.get("display_name", query),
        }
        self.cache[query] = result
        return result
