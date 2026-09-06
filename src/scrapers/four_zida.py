from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.4zida.rs"
LISTING_ID_RE = re.compile(r"/([0-9a-f]{24})(?:[/?#]|$)", re.I)


@dataclass
class ListingCandidate:
    source_id: str
    url: str
    search_text: str


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    price_eur: int
    address: str
    area_m2: float | None
    rooms: str | None
    furnished: bool | None
    heating: str | None


class FourZidaScraper:
    def __init__(self, search_url: str, delay_seconds: float = 1.2):
        self.search_url = search_url
        self.delay_seconds = delay_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/152.0 Safari/537.36"
                ),
                "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

    def _get(self, url: str) -> str:
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        if len(response.text) < 1000:
            raise RuntimeError(f"Neočekivano kratak odgovor sa {url}")
        return response.text

    def get_latest_candidates(self) -> list[ListingCandidate]:
        html = self._get(self.search_url)
        soup = BeautifulSoup(html, "html.parser")
        found: dict[str, ListingCandidate] = {}

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/izdavanje-stanova/" not in href:
                continue
            match = LISTING_ID_RE.search(href)
            if not match:
                continue

            source_id = match.group(1).lower()
            url = urljoin(BASE_URL, href.split("#")[0])
            search_text = " ".join(a.stripped_strings)

            # Search strana ume da sadrži i kratkoročne oglase ("NA DAN").
            if "na dan" in search_text.lower():
                continue

            found.setdefault(
                source_id,
                ListingCandidate(source_id=source_id, url=url, search_text=search_text),
            )

        return list(found.values())

    @staticmethod
    def _parse_price(title: str) -> int | None:
        m = re.search(r"(\d[\d.\s]*)\s*€", title)
        if not m:
            return None
        raw = re.sub(r"[.\s]", "", m.group(1))
        return int(raw) if raw.isdigit() else None

    @staticmethod
    def _parse_area(title: str) -> float | None:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", title, re.I)
        if not m:
            return None
        return float(m.group(1).replace(",", "."))

    @staticmethod
    def _parse_address(title: str) -> str:
        # Npr. "Garsonjera za izdavanje, Zvezdarska 11, 300€, 30m²"
        m = re.search(
            r"za izdavanje,\s*(.+?),\s*\d[\d.\s]*\s*€",
            title,
            re.I,
        )
        if m:
            return m.group(1).strip()

        # Fallback: uzmi deo između prve i pretposlednje zarezom odvojene stavke.
        parts = [p.strip() for p in title.split(",")]
        if len(parts) >= 3:
            return parts[1]
        return title.strip()

    @staticmethod
    def _guess_rooms(title: str, text: str) -> str | None:
        candidates = [
            "Garsonjera",
            "Jednosoban",
            "Jednoiposoban",
            "Dvosoban",
            "Dvoiposoban",
            "Trosoban",
            "Troiposoban",
            "Četvorosoban",
        ]
        haystack = f"{title} {text}"
        for c in candidates:
            if c.lower() in haystack.lower():
                return c

        m = re.search(r"\b(\d(?:[.,]5)?)\s+soba\b", haystack, re.I)
        if m:
            return f"{m.group(1).replace(',', '.')} soba"
        return None

    @staticmethod
    def _guess_heating(text: str) -> str | None:
        labels = [
            "Centralno",
            "Etažno",
            "TA peć",
            "Na gas",
            "Na struju",
            "Podno",
            "Norveški radijatori",
        ]
        for label in labels:
            if label.lower() in text.lower():
                return label
        return None

    def get_listing(self, candidate: ListingCandidate) -> Listing | None:
        time.sleep(self.delay_seconds)
        html = self._get(candidate.url)
        soup = BeautifulSoup(html, "html.parser")

        h1 = soup.find("h1")
        if not h1:
            return None

        title = " ".join(h1.stripped_strings)
        price = self._parse_price(title)
        if price is None:
            return None

        full_text = "\n".join(soup.stripped_strings)

        # Dodatna zaštita od dnevnog najma.
        if "na dan" in candidate.search_text.lower():
            return None

        furnished: bool | None
        if "Namešteno".lower() in full_text.lower():
            furnished = True
        elif "Prazno".lower() in full_text.lower():
            furnished = False
        else:
            furnished = None

        return Listing(
            source="4zida",
            source_id=candidate.source_id,
            url=candidate.url,
            title=title,
            price_eur=price,
            address=self._parse_address(title),
            area_m2=self._parse_area(title),
            rooms=self._guess_rooms(title, full_text),
            furnished=furnished,
            heating=self._guess_heating(full_text),
        )
