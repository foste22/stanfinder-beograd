from src.scrapers.four_zida import FourZidaScraper


def test_parse_title():
    title = "Garsonjera za izdavanje, Zvezdarska 11, 300€, 30m²"
    assert FourZidaScraper._parse_price(title) == 300
    assert FourZidaScraper._parse_area(title) == 30.0
    assert FourZidaScraper._parse_address(title) == "Zvezdarska 11"
