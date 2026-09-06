# StanFinder Beograd — besplatna verzija

Automatski monitor oglasa za izdavanje stanova u Beogradu.

## Filter

Stan prolazi samo ako:

- cena je **≤ 400 €**;
- prosečno vreme do dve ciljne lokacije je **≤ 60 min**;
- nijedno od osam uzorkovanih putovanja nije **> 75 min**.

Uzorkovanje: radni dan u 07:30, 09:00, 14:00 i 17:00.

## Bez Google billing-a

Ova verzija ne koristi Google Maps API.

Koristi:

- zvanični GTFS Grada Beograda za red vožnje;
- koordinate mape oglasa kada ih portal objavi;
- sopstveni mali GTFS routing model;
- Leaflet + OpenStreetMap samo za prikaz dashboard mape.

Vreme putovanja je schedule-based procena. Nema live gužvu. Pešačke deonice se
procenjuju geometrijski, pa rezultat služi za automatsko rangiranje lokacija,
a ne kao navigacija u realnom vremenu.

## Izvor oglasa

Prva verzija prati 4zida. Novi portali se mogu dodavati kao novi scraper moduli.

## GitHub Actions

Workflow radi na svakih 15 minuta i može se pokrenuti ručno iz Actions taba.

## GitHub Pages

Dashboard ostaje u folderu `site/` i prikazuje sve stanove koji su prošli filter.
Klik na marker otvara originalni oglas.
