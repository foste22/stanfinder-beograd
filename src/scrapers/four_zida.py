from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, unquote, urlparse, urlunparse

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
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0 Safari/537.36"
            ),
            "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def _request(self, url: str, *, allow_status: bool = False) -> requests.Response:
        r = self.session.get(url, timeout=30, allow_redirects=True)
        if not allow_status:
            r.raise_for_status()
        return r

    def _get(self, url: str) -> str:
        r = self._request(url)
        if len(r.text) < 1000:
            raise RuntimeError(f"Neočekivano kratak odgovor sa {url}")
        return r.text

    def get_latest_candidates(self) -> list[ListingCandidate]:
        html = self._get(self.search_url)
        soup = BeautifulSoup(html, "html.parser")
        found: dict[str, ListingCandidate] = {}

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/izdavanje-stanova/" not in href:
                continue
            m = LISTING_ID_RE.search(href)
            if not m:
                continue

            source_id = m.group(1).lower()
            url = urljoin(BASE_URL, href.split("#")[0])
            text = " ".join(a.stripped_strings)
            if "na dan" in text.lower():
                continue

            found.setdefault(
                source_id,
                ListingCandidate(source_id=source_id, url=url, search_text=text),
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
        names = [
            "Garsonjera", "Jednosoban", "Jednoiposoban", "Dvosoban",
            "Dvoiposoban", "Trosoban", "Troiposoban", "Četvorosoban",
        ]
        hay = f"{title} {text}"
        for name in names:
            if name.lower() in hay.lower():
                return name
        m = re.search(r"\b(\d(?:[.,]5)?)\s+soba\b", hay, re.I)
        return f"{m.group(1).replace(',', '.')} soba" if m else None

    @staticmethod
    def _guess_heating(text: str) -> str | None:
        for label in [
            "Centralno", "Etažno", "TA peć", "Na gas", "Na struju",
            "Podno", "Norveški radijatori",
        ]:
            if label.lower() in text.lower():
                return label
        return None

    @staticmethod
    def _valid_belgrade(lat: float, lon: float) -> bool:
        return 44.3 <= lat <= 45.2 and 19.8 <= lon <= 21.2

    @classmethod
    def _extract_coordinates(cls, soup: BeautifulSoup, html: str):
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
            for m in re.finditer(pattern, html, re.I | re.S):
                a, b = float(m.group(1)), float(m.group(2))
                lat, lon = (b, a) if reverse else (a, b)
                if cls._valid_belgrade(lat, lon):
                    return lat, lon
        return None, None

    # -----------------------------------------------------------------
    # FOTOGRAFIJE
    # -----------------------------------------------------------------

    @staticmethod
    def _normalise_resizer_url(raw: str | None) -> str | None:
        if not raw:
            return None

        value = unquote(str(raw).strip())
        value = value.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("http://"):
            value = "https://" + value[7:]

        try:
            p = urlparse(value)
        except Exception:
            return None

        # Samo CDN koji služi fotografije oglasa.
        if p.scheme != "https" or p.netloc.lower() != "resizer2.4zida.rs":
            return None

        # Fragmenti #3840/#1920/#640/#256 nisu deo HTTP resursa.
        p = p._replace(fragment="")
        value = urlunparse(p)

        # Isključi sve što nije image format.
        path_low = p.path.lower()
        if not re.search(r"\.(?:jpe?g|png|webp)(?:$|\?)", path_low):
            return None

        return value

    @staticmethod
    def _original_photo_info(url: str) -> tuple[str | None, str | None]:
        """
        Iz 4zida resizer URL-a dobija:
          (listing_id, photo_id)

        Npr:
          local:///6a9189af15c8c84d4707a693/7f563d2f1f_wm
        postaje:
          ("6a9189af15c8c84d4707a693", "7f563d2f1f")

        Tako:
        - različite rezolucije iste fotografije postaju jedan zapis;
        - fotografije drugih oglasa / preporuka ne mogu da upadnu u galeriju.
        """
        try:
            p = urlparse(url)
            last = p.path.rsplit("/", 1)[-1]
            encoded = last.rsplit(".", 1)[0]
            padding = "=" * ((4 - len(encoded) % 4) % 4)
            decoded = base64.urlsafe_b64decode(encoded + padding).decode("utf-8", "ignore")

            if not decoded.startswith("local:///"):
                return None, None

            relative = decoded[len("local:///"):].strip("/")
            parts = [x for x in relative.split("/") if x]
            if len(parts) < 2:
                return None, None

            listing_id = parts[0].lower()
            photo_key = parts[-1]
            photo_key = re.sub(r"_wm$", "", photo_key, flags=re.I).strip().lower()

            if not re.fullmatch(r"[0-9a-f]{24}", listing_id, re.I):
                return None, None

            return listing_id, photo_key or None
        except Exception:
            return None, None

    @classmethod
    def _original_photo_key(cls, url: str) -> str | None:
        return cls._original_photo_info(url)[1]

    @staticmethod
    def _quality_score(url: str) -> tuple[int, int, int]:
        """
        Biramo jednu najbolju verziju originala.
        Veća površina je bolja; fit je poželjniji od fill; JPEG dobija
        malu prednost pri istoj dimenziji zbog jednostavnijeg prikaza.
        """
        p = urlparse(url)
        m = re.search(r"/rs:(fit|fill):(\d+):(\d+):", p.path, re.I)
        if m:
            mode = m.group(1).lower()
            w, h = int(m.group(2)), int(m.group(3))
        else:
            mode, w, h = "", 0, 0

        area = w * h
        mode_bonus = 1 if mode == "fit" else 0
        jpeg_bonus = 1 if re.search(r"\.jpe?g$", p.path, re.I) else 0
        return area, mode_bonus, jpeg_bonus

    @classmethod
    def clean_image_urls(
        cls,
        urls: list[str] | None,
        expected_listing_id: str | None = None,
    ) -> list[str]:
        """
        Zadržava jednu najbolju verziju po originalnoj fotografiji.

        Ako je prosleđen expected_listing_id, prihvataju se SAMO fotografije
        čiji interni 4zida put pripada tom konkretnom oglasu.
        """
        best: dict[str, tuple[tuple[int, int, int], str]] = {}
        order: list[str] = []
        expected = expected_listing_id.lower() if expected_listing_id else None

        for raw in urls or []:
            url = cls._normalise_resizer_url(raw)
            if not url:
                continue

            listing_id, key = cls._original_photo_info(url)
            if not listing_id or not key:
                continue

            if expected and listing_id != expected:
                continue

            score = cls._quality_score(url)

            if key not in best:
                best[key] = (score, url)
                order.append(key)
            elif score > best[key][0]:
                best[key] = (score, url)

        return [best[key][1] for key in order if key in best]

    @classmethod
    def _extract_images(cls, soup: BeautifulSoup, html: str, source_id: str) -> list[str]:
        candidates: list[str] = []

        # Ono što browser stvarno renderuje.
        for img in soup.find_all("img"):
            for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                if img.get(attr):
                    candidates.append(img[attr])

            if img.get("srcset"):
                candidates.extend(
                    part.strip().split(" ")[0]
                    for part in img["srcset"].split(",")
                    if part.strip()
                )

        # URL-ovi u Next/React state-u, gde se često nalaze fotografije
        # koje nisu još renderovane u DOM.
        decoded = html.replace("\\u002F", "/").replace("\\/", "/")
        candidates.extend(
            re.findall(
                r'https?://resizer2\.4zida\.rs/[^\s"\'<>\\]+',
                decoded,
                flags=re.I,
            )
        )

        return cls.clean_image_urls(candidates, expected_listing_id=source_id)

    def refresh_gallery(self, url: str, source_id: str) -> list[str]:
        time.sleep(self.delay_seconds)
        html = self._get(url)
        return self._extract_images(
            BeautifulSoup(html, "html.parser"),
            html,
            source_id,
        )

    def check_active(self, url: str, source_id: str) -> bool | None:
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

        low = r.text.lower()
        for phrase in [
            "oglas nije aktivan", "oglas više nije aktivan",
            "oglas vise nije aktivan", "oglas je istekao",
            "oglas nije dostupan", "nekretnina više nije dostupna",
        ]:
            if phrase in low:
                return False

        return True

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

        furnished = (
            True if "namešteno" in full_text.lower()
            else False if "prazno" in full_text.lower()
            else None
        )

        lat, lon = self._extract_coordinates(soup, html)
        images = self._extract_images(soup, html, candidate.source_id)

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
