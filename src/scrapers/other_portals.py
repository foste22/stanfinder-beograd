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
        """
        Čitamo prve dve strane aktuelne pretrage do ~400 €.

        Nekretnine.rs ume da rasporedi "novo" oglase između premium/standard/lite
        grupa, pa jedna jedina strana može da propusti deo novih jeftinih stanova.
        Dve strane su i dalje mali, umeren broj zahteva.
        """
        found = {}

        for page in (1, 2):
            page_url = self._page_url(self.search_url, page)
            html = self._get(page_url)
            soup = BeautifulSoup(html, "html.parser")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.search(r"/oglasi/(\d+)/?", href)
                if not m:
                    continue

                sid = m.group(1)
                url = urljoin(self.base_url, href)
                text = " ".join(a.stripped_strings)

                found.setdefault(
                    sid,
                    ListingCandidate(sid, url, text),
                )

            if page == 1:
                time.sleep(self.delay_seconds)

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
    """
    Halo Oglasi adapter bez zaobilaženja Cloudflare zaštite.

    Desktop www.halooglasi.com trenutno vraća 403 automatizovanim requests
    klijentima, ali Halo ima javni alternativni prikaz:
        https://smsprint.halooglasi.com/

    Taj prikaz sadrži listu oglasa, cene, lokacije, kvadrature, sobnost,
    broj fotografija i javne linkove. Koristimo ga za čitanje podataka,
    dok korisniku uvek čuvamo normalan www.halooglasi.com URL.
    """

    source = "halo_oglasi"
    base_url = "https://smsprint.halooglasi.com"
    public_base_url = "https://www.halooglasi.com"

    # Razdvajanje po strukturi daje mnogo bolju pokrivenost jeftinih stanova
    # nego jedna opšta stranica puna premium oglasa.
    STRUCTURES = (
        "garsonjera",
        "jednosoban",
        "jednoiposoban",
        "dvosoban",
        "dvoiposoban",
        "trosoban",
    )

    def __init__(self, search_url: str, delay_seconds: float = 1.5):
        # Čak i ako stari config slučajno sadrži www host, prebaci na javni
        # smsprint prikaz.
        search_url = search_url.replace(
            "https://www.halooglasi.com",
            "https://smsprint.halooglasi.com",
        )
        super().__init__(search_url, delay_seconds)
        self.card_cache: dict[str, dict] = {}

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
                if img.get(attr):
                    candidates.append(img.get(attr))

            srcset = img.get("srcset")
            if srcset:
                candidates.extend(
                    x.strip().split(" ")[0]
                    for x in srcset.split(",")
                    if x.strip()
                )

        result, seen = [], set()
        for raw in candidates:
            if not raw:
                continue

            url = urljoin("https://smsprint.halooglasi.com", str(raw).strip())
            p = urlparse(url)
            host = p.netloc.lower()

            if not (
                host == "img.halooglasi.com"
                or host.endswith(".img.halooglasi.com")
                or "halooglasi.com" in host
            ):
                continue

            low = url.lower()
            if any(x in low for x in (
                "logo", "avatar", "icon", "sprite", "banner",
                "googletagmanager", "placeholder"
            )):
                continue

            # Halo slike ponekad nemaju klasičnu ekstenziju u URL-u,
            # zato je host važniji od ekstenzije.
            clean = p._replace(fragment="").geturl()
            if clean not in seen:
                seen.add(clean)
                result.append(clean)

        return result[:40]

    @staticmethod
    def _candidate_card(anchor):
        """
        Na smsprint listi pronađi najmanjeg pretka koji izgleda kao jedna
        kartica oglasa: ima cenu + lokaciju/kvadraturu.

        Ne oslanjamo se na CSS klase jer Halo može da ih promeni.
        """
        node = anchor
        best = None

        for _ in range(9):
            node = getattr(node, "parent", None)
            if node is None:
                break

            text = " ".join(node.stripped_strings)
            if len(text) > 5000:
                break

            has_price = bool(re.search(r"\b\d[\d.\s]*\s*€", text))
            has_property = (
                "Kvadratura" in text
                or "Broj soba" in text
                or "Opština" in text
            )

            if has_price and has_property:
                best = node
                # Nastavi još malo samo ako je trenutno ekstremno mali;
                # inače je ovo verovatno kartica.
                if len(text) >= 80:
                    break

        return best

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
            m = re.fullmatch(r"([\d.\s]+)\s*€", line)
            if m:
                raw = re.sub(r"[.\s]", "", m.group(1))
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

        # Stabilan obrazac liste:
        # Beograd
        # Opština X
        # Naselje
        # Ulica
        try:
            idx = next(i for i, x in enumerate(lines) if x == "Beograd")
            loc = lines[idx + 1: idx + 7]

            # odbaci opštinu iz kandidata za naselje/ulicu
            useful = []
            for x in loc:
                if x.startswith("Opština "):
                    municipality = x[len("Opština "):].strip()
                    continue
                if "Kvadratura" in x or "Broj soba" in x:
                    break
                # preskoči očigledne numeričke/tehničke stavke
                if re.fullmatch(r"[\d./-]+", x):
                    continue
                useful.append(x)

            if useful:
                neighborhood = useful[0].strip()
            if len(useful) >= 2:
                address = useful[1].strip()

        except StopIteration:
            pass

        # Ako ulica nije dostupna, naselje je i dalje dovoljno za približan
        # geocoding; routing će na sajtu biti označen sa ≈ lokacija.
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

    def _structure_urls(self) -> list[str]:
        root = self.search_url.rstrip("/")
        # Ako je u config-u već /beograd, samo dodaj strukturu.
        return [f"{root}/{s}" for s in self.STRUCTURES]

    def get_latest_candidates(self):
        found: dict[str, ListingCandidate] = {}
        self.card_cache = {}

        for i, page_url in enumerate(self._structure_urls()):
            if i:
                time.sleep(self.delay_seconds)

            html = self._get(page_url)
            soup = BeautifulSoup(html, "html.parser")

            for a in soup.find_all("a", href=True):
                href = a.get("href", "")

                if "/nekretnine/izdavanje-stanova/" not in href:
                    continue

                m = re.search(r"/(\d{10,})(?:\?|$)", href)
                if not m:
                    continue

                sid = m.group(1)
                title = " ".join(a.stripped_strings).strip()
                if not title:
                    continue

                card = self._candidate_card(a)
                if card is None:
                    continue

                parsed = self._parse_card_lines(card, title)
                price = parsed.get("price_eur")

                # Štedimo zahteve: oglasi preko našeg limita nas uopšte
                # ne zanimaju.
                if price is None or price > 400:
                    continue

                sms_url = urljoin(self.base_url, href)
                public_url = self._normal_public_url(sms_url)
                card_images = self._halo_images(card)

                self.card_cache[sid] = {
                    **parsed,
                    "title": title,
                    "sms_url": sms_url,
                    "public_url": public_url,
                    "images": card_images,
                }

                found.setdefault(
                    sid,
                    ListingCandidate(
                        sid,
                        public_url,
                        parsed.get("text", title),
                    ),
                )

        return list(found.values())

    def _detail_images(self, sms_url: str) -> list[str]:
        """
        Pokušaj da sa javnog smsprint detalja izvučemo sve Halo slike.
        Ako taj prikaz daje samo naslovnu sliku, kartična slika ostaje fallback.
        """
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

        # Javna detail stranica ne vraća 403; pokušaj da proširimo galeriju.
        detail_images = self._detail_images(card["sms_url"])
        seen = set(images)
        for url in detail_images:
            if url not in seen:
                seen.add(url)
                images.append(url)

        # Korisnik je eksplicitno tražio da oglasi bez fotografija ne ulaze.
        if not images:
            return None

        text = card.get("text", "")
        furnished = None
        low = text.lower()
        if "namešten" in low or "namesten" in low:
            furnished = True
        if "nenamešten" in low or "nenamesten" in low:
            furnished = False

        heating = None
        for label in ("CG", "EG", "TA", "Gas", "Podno"):
            if re.search(rf"\b{re.escape(label)}\b", text, re.I):
                heating = label
                break

        # smsprint lista već daje dovoljno podataka. Koordinate obično nisu
        # javne, pa će zajednički cached Nominatim fallback iz main.py
        # geokodirati ulicu + naselje veoma ograničenom brzinom.
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
        """
        Provera preko javnog smsprint URL-a, ne preko 403 desktop hosta.
        """
        sms_url = self._sms_url(url)
        time.sleep(self.delay_seconds)

        try:
            r = self._request(sms_url, allow_status=True)
        except requests.RequestException:
            return None

        if r.status_code in (404, 410):
            return False
        if r.status_code in (403, 429) or r.status_code >= 500:
            return None
        if r.status_code != 200:
            return None

        low = r.text.lower()
        inactive = (
            "oglas nije aktivan",
            "oglas više nije aktivan",
            "oglas vise nije aktivan",
            "oglas je istekao",
            "oglas nije dostupan",
        )
        if any(x in low for x in inactive):
            return False

        # Ako se ID oglasa i dalje nalazi u URL-u/finalnoj stranici,
        # tretiramo ga kao aktivan. U nejasnom slučaju ne brišemo.
        if source_id in r.url or source_id in r.text:
            return True

        return None
