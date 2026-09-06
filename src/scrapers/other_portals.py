from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


@dataclass
class ListingCandidate:
    source_id: str
    url: str
    search_text: str = ""


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    price_eur: int
    address: str
    neighborhood: str | None
    area_m2: float | None
    rooms: str | None
    furnished: bool | None
    heating: str | None
    lat: float | None
    lon: float | None
    approximate_location: bool
    images: list[str]


class BasePortalScraper:
    source = ""
    base_url = ""

    def __init__(self, search_url: str, delay_seconds: float = 1.3):
        self.search_url = search_url
        self.delay_seconds = delay_seconds
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "StanFinder-Beograd/1.0 (personal apartment search)",
            "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def _request(self, url: str, allow_status: bool = False):
        r = self.session.get(url, timeout=35, allow_redirects=True)
        if not allow_status:
            r.raise_for_status()
        return r

    def _get(self, url: str) -> str:
        r = self._request(url)
        if len(r.text) < 500:
            raise RuntimeError(f"Neočekivano kratak odgovor sa {url}")
        return r.text

    @staticmethod
    def _price(text: str) -> int | None:
        patterns = [
            r"€\s*([\d.\s]+)(?:/mesec)?",
            r"([\d.\s]+(?:,\d{2})?)\s*(?:EUR|€)",
            r"Cena\s*:?\s*([\d.\s]+(?:,\d{2})?)",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if not m:
                continue
            raw = m.group(1).split(",")[0]
            raw = re.sub(r"[.\s]", "", raw)
            if raw.isdigit():
                val = int(raw)
                if 50 <= val <= 100000:
                    return val
        return None

    @staticmethod
    def _area(text: str) -> float | None:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*m(?:²|2)\b", text, re.I)
        return float(m.group(1).replace(",", ".")) if m else None

    @staticmethod
    def _coords(soup: BeautifulSoup, html: str):
        # JSON-LD
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
                                    if 44.25 <= lat <= 45.25 and 19.7 <= lon <= 21.3:
                                        return lat, lon
                            stack.extend(x.values())
                        elif isinstance(x, list):
                            stack.extend(x)
                except Exception:
                    pass

        patterns = [
            (r'["\']latitude["\']\s*[:=]\s*["\']?(4[4-5]\.\d+).*?["\']longitude["\']\s*[:=]\s*["\']?(2[0-1]\.\d+)', False),
            (r'["\']longitude["\']\s*[:=]\s*["\']?(2[0-1]\.\d+).*?["\']latitude["\']\s*[:=]\s*["\']?(4[4-5]\.\d+)', True),
            (r'["\']lat["\']\s*[:=]\s*["\']?(4[4-5]\.\d+).*?["\'](?:lng|lon)["\']\s*[:=]\s*["\']?(2[0-1]\.\d+)', False),
        ]
        for pat, reverse in patterns:
            m = re.search(pat, html, re.I | re.S)
            if m:
                a, b = float(m.group(1)), float(m.group(2))
                return (b, a) if reverse else (a, b)
        return None, None

    @staticmethod
    def _jsonld_images(soup: BeautifulSoup) -> list[str]:
        result = []
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text("", strip=False)
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            stack = [obj]
            while stack:
                x = stack.pop()
                if isinstance(x, dict):
                    image = x.get("image")
                    if isinstance(image, str):
                        result.append(image)
                    elif isinstance(image, list):
                        result.extend(v for v in image if isinstance(v, str))
                    stack.extend(x.values())
                elif isinstance(x, list):
                    stack.extend(x)
        return result

    @staticmethod
    def _images(soup: BeautifulSoup, allowed_hosts: tuple[str, ...]) -> list[str]:
        candidates = []
        for selector, attr in [
            ('meta[property="og:image"]', "content"),
            ('meta[property="og:image:secure_url"]', "content"),
            ('meta[name="twitter:image"]', "content"),
        ]:
            for tag in soup.select(selector):
                if tag.get(attr):
                    candidates.append(tag.get(attr))

        candidates.extend(BasePortalScraper._jsonld_images(soup))

        for img in soup.find_all("img"):
            for attr in ("src", "data-src", "data-original", "data-lazy-src"):
                if img.get(attr):
                    candidates.append(img.get(attr))
            srcset = img.get("srcset")
            if srcset:
                candidates.extend(
                    part.strip().split(" ")[0]
                    for part in srcset.split(",")
                    if part.strip()
                )

        out, seen = [], set()
        for raw in candidates:
            if not raw:
                continue
            url = urljoin("https://example.invalid", str(raw).strip())
            p = urlparse(url)
            host = p.netloc.lower()
            if not any(host == h or host.endswith("." + h) for h in allowed_hosts):
                continue
            low = p.path.lower()
            if not re.search(r"\.(?:jpe?g|png|webp)(?:$|/)", low):
                continue
            if any(x in low for x in ("logo", "avatar", "icon", "banner", "placeholder")):
                continue
            clean = p._replace(fragment="").geturl()
            if clean not in seen:
                seen.add(clean)
                out.append(clean)
        return out[:40]

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

        text = r.text.lower()
        inactive = [
            "oglas nije aktivan",
            "oglas više nije aktivan",
            "oglas vise nije aktivan",
            "oglas je istekao",
            "nije u ponudi",
            "oglas nije dostupan",
        ]
        return False if any(x in text for x in inactive) else True


class NekretnineRSScraper(BasePortalScraper):
    source = "nekretnine_rs"
    base_url = "https://www.nekretnine.rs"

    def get_latest_candidates(self):
        html = self._get(self.search_url)
        soup = BeautifulSoup(html, "html.parser")
        found = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/oglasi/(\d+)/?", href)
            if not m:
                continue
            sid = m.group(1)
            url = urljoin(self.base_url, href)
            found.setdefault(sid, ListingCandidate(sid, url, " ".join(a.stripped_strings)))
        return list(found.values())

    def get_listing(self, c: ListingCandidate):
        time.sleep(self.delay_seconds)
        html = self._get(c.url)
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if not h1:
            return None
        title = " ".join(h1.stripped_strings)
        text = "\n".join(soup.stripped_strings)
        price = self._price(text)
        if price is None:
            return None

        # "Beograd Bežanijska kosa" / h1 "... Bežanijska kosa, Beograd"
        neighborhood = None
        m = re.search(r",\s*([^,]+),\s*Beograd\s*$", title, re.I)
        if m:
            neighborhood = m.group(1).strip()
        if not neighborhood:
            m = re.search(r"\bBeograd\s+([^\n]{2,50})", text)
            if m:
                neighborhood = m.group(1).strip().split("\n")[0]

        # Ulica je često navedena u naslovu ili opisu.
        address = neighborhood or title
        street_patterns = [
            r"\bu ulici\s+([A-ZČĆŠĐŽ][^,.\n]{2,70})",
            r"\bStan\s+([^,]{3,70}),\s*" + re.escape(neighborhood or "NEVERMATCH"),
        ]
        for pat in street_patterns:
            m = re.search(pat, text, re.I)
            if m:
                address = m.group(1).strip()
                break

        images = self._images(soup, ("pic.nekretnine.rs", "nekretnine.rs"))
        lat, lon = self._coords(soup, html)

        furnished = True if re.search(r"Namešteno\s+Da|Namešten", text, re.I) else None
        rooms = None
        rm = re.search(r"\b(Garsonjera|Jednosoban|Jednoiposoban|Dvosoban|Dvoiposoban|Trosoban|Troiposoban|Četvorosoban)", text, re.I)
        if rm:
            rooms = rm.group(1)

        heating = None
        hm = re.search(r"Grejanje\s+([^\n]{2,35})", text, re.I)
        if hm:
            heating = hm.group(1).strip()

        return Listing(
            source=self.source, source_id=c.source_id, url=c.url, title=title,
            price_eur=price, address=address, neighborhood=neighborhood,
            area_m2=self._area(text), rooms=rooms, furnished=furnished,
            heating=heating, lat=lat, lon=lon, approximate_location=(lat is None),
            images=images,
        )


class OglasiRSScraper(BasePortalScraper):
    source = "oglasi_rs"
    base_url = "https://www.oglasi.rs"

    def get_latest_candidates(self):
        html = self._get(self.search_url)
        soup = BeautifulSoup(html, "html.parser")
        found = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/oglas/([^/]+)/", href)
            if not m:
                continue
            sid = m.group(1)
            url = urljoin(self.base_url, href)
            found.setdefault(sid, ListingCandidate(sid, url, " ".join(a.stripped_strings)))
        return list(found.values())

    def get_listing(self, c: ListingCandidate):
        time.sleep(self.delay_seconds)
        html = self._get(c.url)
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if not h1:
            return None
        title = " ".join(h1.stripped_strings)
        text = "\n".join(soup.stripped_strings)
        price = self._price(text)
        if price is None:
            return None

        neighborhood = None
        m = re.search(r"Lokacija:\s*\|?\s*([^\n(]+)", text, re.I)
        if m:
            neighborhood = m.group(1).strip(" |")

        address = neighborhood or title
        m = re.search(r"Ulica i broj:\s*\|?\s*([^\n]+)", text, re.I)
        if m:
            address = m.group(1).strip(" |")

        images = self._images(soup, ("media.oglasi.rs", "oglasi.rs"))
        lat, lon = self._coords(soup, html)

        rooms = None
        rm = re.search(r"Sobnost:\s*\|?\s*([^\n]+)", text, re.I)
        if rm:
            rooms = rm.group(1).strip(" |")

        furnished = None
        fm = re.search(r"Opremljenost:\s*\|?\s*([^\n]+)", text, re.I)
        if fm:
            val = fm.group(1).lower()
            if "namešten" in val and "nenamešten" not in val:
                furnished = True
            elif "nenamešten" in val:
                furnished = False

        heating = None
        hm = re.search(r"Grejanje:\s*\|?\s*([^\n]+)", text, re.I)
        if hm:
            heating = hm.group(1).strip(" |")

        return Listing(
            source=self.source, source_id=c.source_id, url=c.url, title=title,
            price_eur=price, address=address, neighborhood=neighborhood,
            area_m2=self._area(text), rooms=rooms, furnished=furnished,
            heating=heating, lat=lat, lon=lon, approximate_location=(lat is None),
            images=images,
        )


class HaloOglasiScraper(BasePortalScraper):
    source = "halo_oglasi"
    base_url = "https://www.halooglasi.com"

    def get_latest_candidates(self):
        html = self._get(self.search_url)
        soup = BeautifulSoup(html, "html.parser")
        found = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/nekretnine/izdavanje-stanova/" not in href:
                continue

            # Halo detaljni URL tipično završava numeričkim ID-em.
            m = re.search(r"/(\d{10,})(?:\?|$)", href)
            if not m:
                continue

            sid = m.group(1)
            url = urljoin(self.base_url, href)
            found.setdefault(sid, ListingCandidate(sid, url, " ".join(a.stripped_strings)))
        return list(found.values())

    def get_listing(self, c: ListingCandidate):
        time.sleep(self.delay_seconds)
        html = self._get(c.url)
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if not h1:
            # Halo na nekim template-ima koristi h2.
            h1 = soup.find("h2")
        if not h1:
            return None

        title = " ".join(h1.stripped_strings)
        text = "\n".join(soup.stripped_strings)
        price = self._price(text)
        if price is None:
            return None

        # Tražimo tekstualne location stavke.
        neighborhood = None
        address = title
        loc_candidates = []
        for li in soup.find_all(["li", "span", "div"]):
            t = " ".join(li.stripped_strings)
            if 2 <= len(t) <= 80 and (
                "Opština" in t or
                re.search(r"\b(Borča|Ledine|Rakovica|Kumodraž|Zemun|Karaburma|Mirijevo|Voždovac|Zvezdara|Palilula|Novi Beograd)\b", t, re.I)
            ):
                loc_candidates.append(t)

        # Search result/detail text često daje redom: Beograd, opština, naselje, ulica.
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        for i, line in enumerate(lines):
            if line.lower() == "beograd":
                nearby = lines[i:i+6]
                useful = [x for x in nearby if not x.lower().startswith("opština") and x.lower() != "beograd"]
                if useful:
                    neighborhood = useful[0]
                    if len(useful) > 1 and len(useful[1]) < 80:
                        address = useful[1]
                    break

        images = self._images(soup, ("img.halooglasi.com", "halooglasi.com"))
        lat, lon = self._coords(soup, html)

        rooms = None
        rm = re.search(r"(\d(?:[.,]\d)?)\s*Broj soba", text, re.I)
        if rm:
            rooms = rm.group(1).replace(",", ".") + " soba"

        return Listing(
            source=self.source, source_id=c.source_id, url=c.url, title=title,
            price_eur=price, address=address, neighborhood=neighborhood,
            area_m2=self._area(text), rooms=rooms, furnished=None,
            heating=None, lat=lat, lon=lon, approximate_location=(lat is None),
            images=images,
        )
