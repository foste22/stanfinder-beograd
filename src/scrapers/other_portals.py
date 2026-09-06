from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
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

    @staticmethod
    def _page_url(url: str, page: int) -> str:
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        if page <= 1:
            params.pop("pag", None)
        else:
            params["pag"] = str(page)
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(params),
            parts.fragment,
        ))

    def get_latest_candidates(self):
        # Prve dve strane filtrirane pretrage, plus raw HTML fallback.
        found = {}
        pages_ok = 0
        anchors_seen = 0
        raw_matches = 0

        for page in (1, 2):
            page_url = self._page_url(self.search_url, page)

            try:
                html = self._get(page_url)
            except Exception as exc:
                print(f"[WARN] Nekretnine.rs list page {page_url}: {exc}")
                continue

            pages_ok += 1
            soup = BeautifulSoup(html, "html.parser")

            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                m = re.search(r"/oglasi/(\d+)/?", href)
                if not m:
                    continue

                anchors_seen += 1
                sid = m.group(1)
                url = urljoin(self.base_url, href)
                text = " ".join(a.stripped_strings).strip()

                found.setdefault(
                    sid,
                    ListingCandidate(sid, url, text),
                )

            raw_pattern = r'href=["\']([^"\']*/oglasi/(\d+)/?[^"\']*)["\']'
            for m in re.finditer(raw_pattern, html, flags=re.I):
                raw_matches += 1
                href, sid = m.group(1), m.group(2)
                href = href.replace("&amp;", "&")
                url = urljoin(self.base_url, href)
                found.setdefault(
                    sid,
                    ListingCandidate(sid, url, ""),
                )

            if page == 1:
                time.sleep(self.delay_seconds)

        print(
            "[NEKRETNINE DISCOVERY] "
            f"pages={pages_ok} anchors={anchors_seen} "
            f"raw_matches={raw_matches} unique={len(found)}"
        )
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

        # H1 je tipično:
        # "Jednosoban stan Ulica Dusana Radovica, Mirijevo - Novo Mirijevo, Beograd"
        neighborhood = None
        address = None

        m = re.search(
            r"^.+?\s+(.+?),\s*([^,]+),\s*Beograd\s*$",
            title,
            re.I,
        )
        if m:
            address = m.group(1).strip()
            # Odseci tip nekretnine sa početka adrese, ako je ostao.
            address = re.sub(
                r"^(?:stan|garsonjera|jednosoban|jednoiposoban|dvosoban|"
                r"dvoiposoban|trosoban|troiposoban|četvorosoban)\s+",
                "",
                address,
                flags=re.I,
            ).strip()
            neighborhood = m.group(2).strip()

        # Dodatni pouzdan trag na detaljnoj strani:
        # "Beograd Mirijevo - Novo Mirijevo Ulica Dusana Radovica"
        if not neighborhood:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for ln in lines[:20]:
                if not ln.lower().startswith("beograd "):
                    continue
                tail = ln[len("Beograd "):].strip()
                um = re.search(r"\b(Ulica\s+.+)$", tail, re.I)
                if um:
                    address = um.group(1).strip()
                    neighborhood = tail[:um.start()].strip(" ,-")
                    break

        # Fallback iz naslova: poslednji segment pre Beograda je naselje.
        if not neighborhood:
            m = re.search(r",\s*([^,]+),\s*Beograd\s*$", title, re.I)
            if m:
                neighborhood = m.group(1).strip()

        # Ako u naslovu nije data ulica, pokušaj opis.
        if not address or address == neighborhood:
            street_patterns = [
                r"\bu ulici\s+([A-ZČĆŠĐŽ][^,.\n]{2,70})",
                r"\b(Ulica\s+[A-ZČĆŠĐŽ][^,.\n]{2,70})",
            ]
            for pat in street_patterns:
                sm = re.search(pat, text, re.I)
                if sm:
                    address = sm.group(1).strip()
                    break

        address = address or neighborhood or title

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
    base_url = "https://smsprint.halooglasi.com"
    public_base_url = "https://www.halooglasi.com"

    STRUCTURES = (
        "garsonjera",
        "jednosoban",
        "jednoiposoban",
        "dvosoban",
        "dvoiposoban",
        "trosoban",
    )

    def __init__(
        self,
        search_url: str,
        delay_seconds: float = 1.5,
        pages_per_category: int = 4,
    ):
        search_url = search_url.replace(
            "https://www.halooglasi.com",
            "https://smsprint.halooglasi.com",
        )
        super().__init__(search_url, delay_seconds)
        self.pages_per_category = max(1, int(pages_per_category))
        self.card_cache: dict[str, dict] = {}
        self.discovery_stats = {}

    @staticmethod
    def _normal_public_url(url: str) -> str:
        return url.replace(
            "https://smsprint.halooglasi.com",
            "https://www.halooglasi.com",
        )

    @staticmethod
    def _sms_url(url: str) -> str:
        return url.replace(
            "https://www.halooglasi.com",
            "https://smsprint.halooglasi.com",
        )

    @staticmethod
    def _with_page(url: str, page: int) -> str:
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        if page <= 1:
            params.pop("page", None)
        else:
            params["page"] = str(page)
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(params),
            parts.fragment,
        ))

    def _listing_pages(self) -> list[str]:
        root = self.search_url.rstrip("/")
        bases = [root] + [f"{root}/{s}" for s in self.STRUCTURES]
        pages = []
        for base in bases:
            for page in range(1, self.pages_per_category + 1):
                pages.append(self._with_page(base, page))
        return pages

    @staticmethod
    def _halo_images(node) -> list[str]:
        candidates = []

        for img in node.find_all("img"):
            for attr in (
                "src",
                "data-src",
                "data-original",
                "data-lazy-src",
                "data-image",
            ):
                value = img.get(attr)
                if value:
                    candidates.append(value)

            srcset = img.get("srcset")
            if srcset:
                candidates.extend(
                    x.strip().split(" ")[0]
                    for x in srcset.split(",")
                    if x.strip()
                )

        result, seen = [], set()

        for raw in candidates:
            url = urljoin(
                "https://smsprint.halooglasi.com",
                str(raw).strip(),
            )
            parsed = urlparse(url)
            host = parsed.netloc.lower()

            if not (
                host == "img.halooglasi.com"
                or host.endswith(".img.halooglasi.com")
                or host.endswith("halooglasi.com")
            ):
                continue

            low = url.lower()
            if any(x in low for x in (
                "logo", "avatar", "icon", "sprite", "banner",
                "googletagmanager", "placeholder",
            )):
                continue

            clean = parsed._replace(fragment="").geturl()
            if clean not in seen:
                seen.add(clean)
                result.append(clean)

        return result[:40]

    @staticmethod
    def _candidate_card(anchor):
        node = anchor

        for _ in range(12):
            node = getattr(node, "parent", None)
            if node is None:
                break

            text = " ".join(node.stripped_strings)

            if len(text) > 7000:
                break

            has_price = bool(
                re.search(r"\b\d[\d.\s\xa0]*\s*€", text)
            )
            has_property = (
                "Kvadratura" in text
                or "Broj soba" in text
                or "Opština" in text
            )

            if has_price and has_property:
                return node

        return None

    @staticmethod
    def _parse_card_lines(card, title: str) -> dict:
        lines = [
            re.sub(r"\s+", " ", x).strip()
            for x in card.stripped_strings
            if re.sub(r"\s+", " ", x).strip()
        ]

        joined = "\n".join(lines)

        price = None
        for line in lines:
            m = re.fullmatch(r"([\d.\s\xa0]+)\s*€", line)
            if not m:
                continue

            raw = re.sub(r"[.\s\xa0]", "", m.group(1))
            if raw.isdigit():
                price = int(raw)
                break

        area = None
        rooms = None

        for line in lines:
            if "Kvadratura" in line:
                m = re.search(r"(\d+(?:[.,]\d+)?)", line)
                if m:
                    area = float(m.group(1).replace(",", "."))

            if "Broj soba" in line:
                m = re.search(r"(\d+(?:[.,]\d+)?)", line)
                if m:
                    rooms = m.group(1).replace(",", ".") + " soba"

        neighborhood = None
        address = None
        municipality = None

        try:
            idx = next(i for i, x in enumerate(lines) if x == "Beograd")
            loc = lines[idx + 1: idx + 8]
            useful = []

            for x in loc:
                if x.startswith("Opština "):
                    municipality = x[len("Opština "):].strip()
                    continue

                if "Kvadratura" in x or "Broj soba" in x:
                    break

                if re.fullmatch(r"[\d./-]+", x):
                    continue

                useful.append(x)

            if useful:
                neighborhood = useful[0]

            if len(useful) >= 2:
                address = useful[1]

        except StopIteration:
            pass

        address = address or neighborhood or municipality or title

        return {
            "price_eur": price,
            "area_m2": area,
            "rooms": rooms,
            "neighborhood": neighborhood,
            "address": address,
            "municipality": municipality,
            "text": joined,
        }

    def get_latest_candidates(self):
        found: dict[str, ListingCandidate] = {}
        self.card_cache = {}

        pages_ok = 0
        links_seen = 0
        cards_parsed = 0
        under_price = 0
        raw_id_matches = 0

        for i, page_url in enumerate(self._listing_pages()):
            if i:
                time.sleep(self.delay_seconds)

            try:
                html = self._get(page_url)
            except Exception as exc:
                print(f"[WARN] Halo list page {page_url}: {exc}")
                continue

            pages_ok += 1
            soup = BeautifulSoup(html, "html.parser")
            page_ids = set()

            raw_id_matches += len(re.findall(
                r'/nekretnine/izdavanje-stanova/[^"\'<> ]+/(\d{10,})',
                html,
                flags=re.I,
            ))

            for a in soup.find_all("a", href=True):
                href = a.get("href", "")

                if "/nekretnine/izdavanje-stanova/" not in href:
                    continue

                m = re.search(r"/(\d{10,})(?:/?(?:\?|$))", href)
                if not m:
                    continue

                links_seen += 1
                sid = m.group(1)

                if sid in page_ids:
                    continue

                page_ids.add(sid)

                card = self._candidate_card(a)
                if card is None:
                    continue

                title = " ".join(a.stripped_strings).strip()

                if not title:
                    h = card.find(["h1", "h2", "h3", "h4"])
                    if h:
                        title = " ".join(h.stripped_strings).strip()

                if not title:
                    links = [
                        " ".join(x.stripped_strings).strip()
                        for x in card.find_all("a", href=True)
                    ]
                    title = next(
                        (x for x in links if len(x) >= 4),
                        f"Halo oglas {sid}",
                    )

                data = self._parse_card_lines(card, title)
                cards_parsed += 1
                price = data.get("price_eur")

                if price is None or price > 400:
                    continue

                under_price += 1

                sms_url = urljoin(self.base_url, href)
                public_url = self._normal_public_url(sms_url)
                images = self._halo_images(card)

                record = {
                    **data,
                    "title": title,
                    "sms_url": sms_url,
                    "public_url": public_url,
                    "images": images,
                }

                old = self.card_cache.get(sid)

                if old:
                    old_score = (
                        len(old.get("images") or []),
                        bool(old.get("address")),
                        len(old.get("text") or ""),
                    )
                    new_score = (
                        len(images),
                        bool(data.get("address")),
                        len(data.get("text") or ""),
                    )
                    if new_score > old_score:
                        self.card_cache[sid] = record
                    continue

                self.card_cache[sid] = record

                found[sid] = ListingCandidate(
                    sid,
                    public_url,
                    data.get("text", title),
                )

        self.discovery_stats = {
            "pages": pages_ok,
            "raw_ids": raw_id_matches,
            "links": links_seen,
            "cards": cards_parsed,
            "under_price": under_price,
            "unique": len(found),
        }

        print(
            "[HALO DISCOVERY] "
            f"pages={pages_ok} raw_ids={raw_id_matches} "
            f"links={links_seen} cards={cards_parsed} "
            f"price<=400={under_price} unique={len(found)}"
        )

        return list(found.values())

    def _detail_images(self, sms_url: str) -> list[str]:
        try:
            time.sleep(self.delay_seconds)
            html = self._get(sms_url)
            soup = BeautifulSoup(html, "html.parser")
            return self._halo_images(soup)
        except Exception as exc:
            print(f"[WARN] Halo galerija nije pročitana: {exc}")
            return []

    def get_listing(self, c: ListingCandidate):
        card = self.card_cache.get(c.source_id)
        if not card:
            return None

        images = list(card.get("images") or [])
        detail_images = self._detail_images(card["sms_url"])

        seen = set(images)

        for url in detail_images:
            if url not in seen:
                seen.add(url)
                images.append(url)

        if not images:
            return None

        text = card.get("text", "")
        low = text.lower()

        furnished = None
        if "namešten" in low or "namesten" in low:
            furnished = True
        if "nenamešten" in low or "nenamesten" in low:
            furnished = False

        heating = None
        for label in ("CG", "EG", "TA", "Gas", "Podno"):
            if re.search(rf"\b{re.escape(label)}\b", text, re.I):
                heating = label
                break

        return Listing(
            source=self.source,
            source_id=c.source_id,
            url=card["public_url"],
            title=card["title"],
            price_eur=int(card["price_eur"]),
            address=card["address"],
            neighborhood=card.get("neighborhood"),
            area_m2=card.get("area_m2"),
            rooms=card.get("rooms"),
            furnished=furnished,
            heating=heating,
            lat=None,
            lon=None,
            approximate_location=True,
            images=images,
        )

    def check_active(self, url: str, source_id: str) -> bool | None:
        sms_url = self._sms_url(url)
        time.sleep(self.delay_seconds)

        try:
            response = self._request(sms_url, allow_status=True)
        except requests.RequestException:
            return None

        if response.status_code in (404, 410):
            return False

        if response.status_code in (403, 429) or response.status_code >= 500:
            return None

        if response.status_code != 200:
            return None

        low = response.text.lower()

        inactive = (
            "oglas nije aktivan",
            "oglas više nije aktivan",
            "oglas vise nije aktivan",
            "oglas je istekao",
            "oglas nije dostupan",
        )

        if any(x in low for x in inactive):
            return False

        if source_id in response.url or source_id in response.text:
            return True

        return None
