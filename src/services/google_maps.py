from __future__ import annotations

from datetime import datetime
from typing import Iterable

import requests


GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"


class GoogleMapsClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("GOOGLE_MAPS_API_KEY nije podešen.")
        self.api_key = api_key
        self.session = requests.Session()

    def geocode(self, address: str) -> dict | None:
        query = f"{address}, Beograd, Srbija"
        r = self.session.get(
            GEOCODE_URL,
            params={
                "address": query,
                "region": "rs",
                "language": "sr",
                "key": self.api_key,
            },
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()

        if payload.get("status") != "OK" or not payload.get("results"):
            return None

        result = payload["results"][0]
        loc = result["geometry"]["location"]

        # Ako oglas nema broj ulice/nema tačnu adresu, marker je nužno približan.
        has_number = any(ch.isdigit() for ch in address)
        approximate = bool(result.get("partial_match")) or not has_number

        return {
            "lat": float(loc["lat"]),
            "lon": float(loc["lng"]),
            "formatted_address": result.get("formatted_address", query),
            "approximate": approximate,
        }

    @staticmethod
    def _duration_to_minutes(value: str | None) -> float | None:
        if not value or not value.endswith("s"):
            return None
        try:
            return round(float(value[:-1]) / 60.0, 1)
        except ValueError:
            return None

    def route_matrix(
        self,
        origins: list[dict],
        destinations: list[dict],
        departure_time: datetime,
    ) -> dict[int, dict[int, float | None]]:
        """
        Vraća:
          {origin_index: {destination_index: minutes_or_none}}
        """
        body = {
            "origins": [
                {
                    "waypoint": {
                        "location": {
                            "latLng": {
                                "latitude": o["lat"],
                                "longitude": o["lon"],
                            }
                        }
                    }
                }
                for o in origins
            ],
            "destinations": [
                {
                    "waypoint": {
                        "location": {
                            "latLng": {
                                "latitude": d["lat"],
                                "longitude": d["lon"],
                            }
                        }
                    }
                }
                for d in destinations
            ],
            "travelMode": "TRANSIT",
            "departureTime": departure_time.isoformat(),
            "languageCode": "sr-Latn",
            "regionCode": "RS",
        }

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": "originIndex,destinationIndex,duration,condition,status",
        }

        r = self.session.post(ROUTE_MATRIX_URL, json=body, headers=headers, timeout=60)
        r.raise_for_status()

        # API tipično vraća JSON niz; podržavamo i newline-delimited fallback.
        try:
            elements = r.json()
        except ValueError:
            elements = [
                __import__("json").loads(line)
                for line in r.text.splitlines()
                if line.strip()
            ]

        if isinstance(elements, dict):
            elements = [elements]

        out = {
            oi: {di: None for di in range(len(destinations))}
            for oi in range(len(origins))
        }

        for el in elements:
            oi = el.get("originIndex")
            di = el.get("destinationIndex")
            if oi is None or di is None:
                continue
            if el.get("condition") == "ROUTE_EXISTS":
                out[oi][di] = self._duration_to_minutes(el.get("duration"))

        return out
