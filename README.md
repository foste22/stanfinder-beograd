# StanFinder Beograd

Lični monitor oglasa za izdavanje stanova u Beogradu.

## Trenutna pravila

Stan prolazi samo ako:

- kirija je **≤ 400 € mesečno**;
- prosek svih uzorkovanih putovanja javnim prevozom do dve lokacije je **≤ 60 min**;
- nijedno od tipičnih uzorkovanih putovanja nije **> 75 min**.

Rute se proveravaju radnim danom u 07:30, 09:00, 14:00 i 17:00.

Destinacije su već upisane u `config.yaml`.

## Šta sistem radi

1. GitHub Actions pokreće proveru četiri puta na sat.
2. Čita 4zida pretragu `Beograd / izdavanje / do 400 € / najnoviji`.
3. Otvara samo nove oglase koje ranije nije video.
4. Iz naslova oglasa izvlači cenu, adresu i kvadraturu.
5. Google Geocoding API pretvara adresu u koordinate za mapu.
6. Google Routes API računa javni prevoz do obe destinacije u četiri termina.
7. Odbacuje oglase koji ne prolaze pravila.
8. Prihvaćene oglase upisuje u `site/data/listings.json`.
9. GitHub Pages objavljuje interaktivnu mapu.
10. Ako su podešeni Telegram secrets, šalje instant link za svaki novi prihvaćeni stan.

## Važna napomena o lokaciji

Ako oglas ne sadrži broj ulice ili tačnu adresu, geokodirana tačka je označena kao **približna** (`≈ lokacija`). To znači da vreme putovanja može malo odstupati.

## 1. Napravi GitHub repository

Napravi novi repository, npr. `stanfinder-beograd`, i ubaci kompletan sadržaj ovog foldera.

Primer komandama:

```bash
git init
git add .
git commit -m "Initial StanFinder"
git branch -M main
git remote add origin https://github.com/TVOJ_USERNAME/stanfinder-beograd.git
git push -u origin main
```

## 2. Google Maps API

U Google Cloud projektu uključi:

- **Geocoding API**
- **Routes API**

Napravi API key i u GitHub repository-ju idi na:

`Settings → Secrets and variables → Actions → New repository secret`

Dodaj:

- Name: `GOOGLE_MAPS_API_KEY`
- Secret: tvoj ključ

Nemoj unositi API ključ direktno u kod.

## 3. GitHub Pages

U repository-ju:

`Settings → Pages → Build and deployment → Source → GitHub Actions`

Workflow će nakon prvog uspešnog pokretanja objaviti dashboard.

## 4. Prvo ručno pokretanje

Idi na:

`Actions → Update StanFinder → Run workflow`

Posle uspešnog pokretanja `site/data/listings.json` će biti popunjen, a Pages link će se pojaviti u GitHub-u.

## 5. Telegram (opciono)

Ako želiš instant notifikacije:

1. Napravi Telegram bota preko `@BotFather`.
2. Pošalji svom botu jednu poruku.
3. Pronađi svoj `chat_id`.
4. U GitHub Actions secrets dodaj:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

Ako ova dva secrets-a nisu podešena, ostatak sistema i dalje radi normalno.

## Dashboard

Mapa koristi Leaflet + OpenStreetMap. Marker stana se može kliknuti i popup sadrži direktan link do originalnog oglasa.

Boje:

- zeleno: prosek ≤ 35 min
- žuto: 35–45 min
- narandžasto: 45–60 min
- plavo: dve ciljne lokacije

Lista pored mape može da se sortira po:

- najbržoj ruti;
- najnižoj ceni;
- najnovije pronađenom oglasu.

## Podešavanje kriterijuma

Sve glavne granice su u `config.yaml`:

```yaml
app:
  max_price_eur: 400
  max_average_minutes: 60
  max_single_trip_minutes: 75
```

## O 4zida monitoru

MVP koristi samo prvu stranu pretrage sortirane po najnovijim oglasima i pravi pauzu između detaljnih zahteva. Namenjen je ličnoj, nekomercijalnoj upotrebi.

Ako 4zida promeni HTML strukturu, potrebno je prilagoditi `src/scrapers/four_zida.py`.

## Sledeći portali

Arhitektura je namerno razdvojena po scraperima. Novi portal se dodaje kao novi modul u:

`src/scrapers/`

bez menjanja dashboarda ili sistema za rute.
