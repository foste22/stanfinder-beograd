from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
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
    lat: float | None
    lon: float | None
    approximate_location: bool
    image_url: str | None


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
        m = re.search(
            r"za izdavanje,\s*(.+?),\s*\d[\d.\s]*\s*€",
            title,
            re.I,
        )
        if m:
            return m.group(1).strip()

        parts = [p.strip() for p in title.split(",")]
        if len(parts) >= 3:
            return parts[1]
        return title.strip()

    @staticmethod
    def _guess_rooms(title: str, text: str) -> str | None:
        candidates = [
            "Garsonjera", "Jednosoban", "Jednoiposoban", "Dvosoban",
            "Dvoiposoban", "Trosoban", "Troiposoban", "Četvorosoban",
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
            "Centralno", "Etažno", "TA peć", "Na gas", "Na struju",
            "Podno", "Norveški radijatori",
        ]
        for label in labels:
            if label.lower() in text.lower():
                return label
        return None

    @staticmethod
    def _valid_belgrade(lat: float, lon: float) -> bool:
        return 44.3 <= lat <= 45.2 and 19.8 <= lon <= 21.2

    @classmethod
    def _extract_coordinates(cls, soup: BeautifulSoup, html: str) -> tuple[float | None, float | None]:
        for script in soup.find_all("script"):
            raw = script.string or script.get_text("", strip=False)
            if not raw:
                continue

            if script.get("type") == "application/ld+json":
                try:
                    obj = json.loads(raw)
                    objects = obj if isinstance(obj, list) else [obj]
                    stack = list(objects)
                    while stack:
                        x = stack.pop()
                        if isinstance(x, dict):
                            geo = x.get("geo")
                            if isinstance(geo, dict):
                                lat = geo.get("latitude") or geo.get("lat")
                                lon = geo.get("longitude") or geo.get("lng") or geo.get("lon")
                                if lat is not None and lon is not None:
                                    lat, lon = float(lat), float(lon)
                                    if cls._valid_belgrade(lat, lon):
                                        return lat, lon
                            stack.extend(x.values())
                        elif isinstance(x, list):
                            stack.extend(x)
                except Exception:
                    pass

        patterns = [
            r'["\']latitude["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)["\']?.{0,250}?'
            r'["\']longitude["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)',
            r'["\']longitude["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)["\']?.{0,250}?'
            r'["\']latitude["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)',
            r'["\']lat["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)["\']?.{0,250}?'
            r'["\'](?:lng|lon)["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)',
        ]

        for idx, pattern in enumerate(patterns):
            for m in re.finditer(pattern, html, flags=re.I | re.S):
                a, b = float(m.group(1)), float(m.group(2))
                lat, lon = (a, b) if idx != 1 else (b, a)
                if cls._valid_belgrade(lat, lon):
                    return lat, lon

        return None, None

    @staticmethod
    def _normalise_image_url(value: str | None) -> str | None:
        if not value:
            return None
        value = value.strip()
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("http://"):
            value = "https://" + value[len("http://"):]
        if value.startswith("https://"):
            return value
        return None

    @classmethod
    def _extract_primary_image(cls, soup: BeautifulSoup) -> str | None:
        # Najstabilniji izvor je social-preview slika oglasa.
        selectors = [
            ('meta[property="og:image"]', "content"),
            ('meta[property="og:image:secure_url"]', "content"),
            ('meta[name="twitter:image"]', "content"),
            ('meta[name="twitter:image:src"]', "content"),
        ]
        for selector, attr in selectors:
            tag = soup.select_one(selector)
            if tag:
                url = cls._normalise_image_url(tag.get(attr))
                if url and "4zida" in url:
                    return url

        # Fallback: prva fotografija sa 4zida resizer domena koja ne liči
        # na avatar, logo ili dekorativnu pozadinu.
        for img in soup.find_all("img"):
            alt = (img.get("alt") or "").lower()
            if any(x in alt for x in ("avatar", "pozadinska", "inspira", "logo")):
                continue

            candidates = [
                img.get("src"),
                img.get("data-src"),
                img.get("data-lazy-src"),
            ]
            srcset = img.get("srcset")
            if srcset:
                candidates.extend(
                    part.strip().split(" ")[0]
                    for part in srcset.split(",")
                    if part.strip()
                )

            for raw in candidates:
                url = cls._normalise_image_url(raw)
                if url and "resizer2.4zida.rs" in url:
                    return url

        return None

    def get_primary_image_url(self, url: str) -> str | None:
        time.sleep(self.delay_seconds)
        html = self._get(url)
        soup = BeautifulSoup(html, "html.parser")
        return self._extract_primary_image(soup)

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

        if "na dan" in candidate.search_text.lower():
            return None

        if "namešteno" in full_text.lower():
            furnished = True
        elif "prazno" in full_text.lower():
            furnished = False
        else:
            furnished = None

        lat, lon = self._extract_coordinates(soup, html)
        image_url = self._extract_primary_image(soup)

        # Portal može namerno da prikazuje približnu lokaciju.
        approximate = True

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
            lat=lat,
            lon=lon,
            approximate_location=approximate,
            image_url=image_url,
        )
