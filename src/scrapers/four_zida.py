from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, unquote

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
    images: list[str]


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

    def _request(self, url: str, *, allow_status: bool = False) -> requests.Response:
        response = self.session.get(url, timeout=30, allow_redirects=True)
        if not allow_status:
            response.raise_for_status()
        return response

    def _get(self, url: str) -> str:
        response = self._request(url)
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
        return float(m.group(1).replace(",", ".")) if m else None

    @staticmethod
    def _parse_address(title: str) -> str:
        m = re.search(r"za izdavanje,\s*(.+?),\s*\d[\d.\s]*\s*€", title, re.I)
        if m:
            return m.group(1).strip()
        parts = [p.strip() for p in title.split(",")]
        return parts[1] if len(parts) >= 3 else title.strip()

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
        return f"{m.group(1).replace(',', '.')} soba" if m else None

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
                    stack = list(obj if isinstance(obj, list) else [obj])
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
            (r'["\']latitude["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)["\']?.{0,250}?'
             r'["\']longitude["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)', False),
            (r'["\']longitude["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)["\']?.{0,250}?'
             r'["\']latitude["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)', True),
            (r'["\']lat["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)["\']?.{0,250}?'
             r'["\'](?:lng|lon)["\']\s*:\s*["\']?([0-9]{2}\.[0-9]+)', False),
        ]
        for pattern, reverse in patterns:
            for m in re.finditer(pattern, html, flags=re.I | re.S):
                a, b = float(m.group(1)), float(m.group(2))
                lat, lon = (b, a) if reverse else (a, b)
                if cls._valid_belgrade(lat, lon):
                    return lat, lon
        return None, None

    @staticmethod
    def _clean_image_url(value: str | None) -> str | None:
        if not value:
            return None
        value = unquote(value.strip().replace("\\u002F", "/").replace("\\/", "/"))
        value = value.replace("&amp;", "&")
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("http://"):
            value = "https://" + value[7:]
        if not value.startswith("https://"):
            return None
        # Fokus na stvarne 4zida fotografije, ne ikonice i spoljne reklame.
        if "resizer2.4zida.rs" not in value and "4zida.rs" not in value:
            return None
        return value

    @classmethod
    def _extract_images(cls, soup: BeautifulSoup, html: str) -> list[str]:
        raw_urls: list[str] = []

        # Meta preview obično sadrži jednu od glavnih fotografija.
        for selector, attr in [
            ('meta[property="og:image"]', "content"),
            ('meta[property="og:image:secure_url"]', "content"),
            ('meta[name="twitter:image"]', "content"),
        ]:
            for tag in soup.select(selector):
                if tag.get(attr):
                    raw_urls.append(tag.get(attr))

        # Sve slike koje su direktno renderovane u galeriji.
        for img in soup.find_all("img"):
            alt = (img.get("alt") or "").lower()
            if any(x in alt for x in ("avatar", "logo", "pozadinska", "reklam", "inspiracija")):
                continue
            for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                if img.get(attr):
                    raw_urls.append(img.get(attr))
            srcset = img.get("srcset")
            if srcset:
                raw_urls.extend(
                    part.strip().split(" ")[0]
                    for part in srcset.split(",")
                    if part.strip()
                )

        # React/Next state često sadrži URL-ove galerije i kada slike nisu sve
        # trenutno ubačene kao <img>.
        decoded = html.replace("\\u002F", "/").replace("\\/", "/")
        raw_urls.extend(
            re.findall(
                r'https?://[^\s"\'<>\\]+',
                decoded,
                flags=re.I,
            )
        )

        # Deduplikacija. CDN ponekad nudi istu fotografiju u više dimenzija;
        # biramo URL kako ga portal daje i ograničavamo galeriju na razuman broj.
        result: list[str] = []
        seen: set[str] = set()
        for raw in raw_urls:
            url = cls._clean_image_url(raw)
            if not url:
                continue

            low = url.lower()
            if any(x in low for x in ("logo", "avatar", "background", "banner", "icon")):
                continue

            # ukloni završnu interpunkciju koja ponekad upadne iz JS stringa
            url = url.rstrip("),;]}")

            if url not in seen:
                seen.add(url)
                result.append(url)

        return result[:60]

    def check_active(self, url: str, source_id: str) -> bool | None:
        """
        True = oglas je aktivan
        False = pouzdano je uklonjen / istekao
        None = privremeni problem, ne diraj oglas
        """
        time.sleep(self.delay_seconds)
        try:
            r = self._request(url, allow_status=True)
        except requests.RequestException:
            return None

        if r.status_code in (404, 410):
            return False
        if r.status_code in (403, 429) or r.status_code >= 500:
            return None
        if r.status_code != 200:
            return None

        final_url = r.url.lower()
        text = r.text.lower()

        inactive_phrases = [
            "oglas nije aktivan",
            "oglas više nije aktivan",
            "oglas vise nije aktivan",
            "oglas je istekao",
            "oglas nije dostupan",
            "ovaj oglas više nije",
            "nekretnina više nije dostupna",
        ]
        if any(p in text for p in inactive_phrases):
            return False

        # Ako je detaljni URL preusmeren na potpuno drugu stranicu bez ID-a,
        # najčešće oglas više ne postoji.
        if source_id.lower() not in final_url and "/izdavanje-stanova/" not in final_url:
            return False

        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        if not h1:
            return None

        title = " ".join(h1.stripped_strings).lower()
        if "za izdavanje" not in title:
            return None

        return True

    def refresh_gallery(self, url: str) -> list[str]:
        time.sleep(self.delay_seconds)
        html = self._get(url)
        return self._extract_images(BeautifulSoup(html, "html.parser"), html)

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
        images = self._extract_images(soup, html)

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
            approximate_location=True,
            images=images,
        )
