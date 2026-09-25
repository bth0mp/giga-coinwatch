import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coinwatch.sources import scrape_source


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        if url not in self.pages:
            raise RuntimeError(f"fixture missing: {url}")
        return SimpleNamespace(url=url, text=self.pages[url])


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def source(adapter, url):
    return {"id": adapter, "name": adapter, "url": url, "adapter": adapter, "enabled": True}


def test_silbury_uses_persistent_product_id_price_and_reservation():
    url = "https://www.silburycoins.co.uk/product-category/roman-byzantine-coins/republican-290-41bc/"
    result = scrape_source(source("silbury", url), FixtureFetcher({url: fixture("silbury.html")}))
    assert result.complete and result.pages == 1
    assert [(x.external_id, x.price, x.currency, x.availability) for x in result.listings] == [
        ("77050", "185.00", "GBP", "available"),
        ("77053", "250.00", "GBP", "reserved"),
    ]
    assert result.listings[0].url.endswith("/roman-republic-a-p-pulcher-t-mallius-111-110-bc-silver-denarius/")
    assert result.listings[0].category == "Roman"


def test_vpuk_follows_next_page_and_excludes_mixed_or_lot_stock():
    url = "https://vpukcoins.com/collections/new-arrivals"
    second = url + "?page=2"
    result = scrape_source(source("vpuk", url), FixtureFetcher({url: fixture("vpuk-1.html"), second: fixture("vpuk-2.html")}))
    assert result.pages == 2 and not result.complete
    assert [x.external_id for x in result.listings] == ["16121508004220", "16121508004221"]
    assert [(x.price, x.currency) for x in result.listings] == [("28.00", "GBP"), ("24.00", "GBP")]
    assert all(x.category == "Roman" for x in result.listings)
    assert "page=3" in result.error


def test_premium_filters_lots_and_sold_rows_before_baseline():
    url = "https://premium-ancient-coins.com/en-gb/collections/roman-coins-for-sale"
    result = scrape_source(source("premium", url), FixtureFetcher({url: fixture("premium.html")}))
    assert result.pages == 1 and not result.complete
    assert [(x.external_id, x.price, x.currency, x.availability) for x in result.listings] == [
        ("9174757998824", "141.00", "GBP", "available"),
        ("9174757998999", "100.00", "GBP", "sold"),
    ]
    assert "page=2" in result.error


def test_silbury_follows_woocommerce_page_route():
    url = "https://www.silburycoins.co.uk/product-category/roman-byzantine-coins/republican-290-41bc/"
    first = fixture("silbury.html") + '<nav class="woocommerce-pagination"><a class="page-numbers" href="' + url + 'page/2/">2</a></nav>'
    second = url + "page/2/"
    fetcher = FixtureFetcher({url: first, second: fixture("silbury.html")})
    result = scrape_source(source("silbury", url), fetcher)
    assert result.complete and result.pages == 2
    assert fetcher.requests == [url, second]


def test_historynumis_traverses_two_pages_and_normalizes_euro_price():
    url = "https://historynumis.com/en/35-ancient-coins"
    result = scrape_source(source("historynumis", url), FixtureFetcher({url: fixture("historynumis-1.html"), url + "?page=2": fixture("historynumis-2.html")}))
    assert result.complete and result.pages == 2
    assert [(x.external_id, x.price, x.currency) for x in result.listings] == [
        ("624", "780.00", "EUR"), ("625", "3330.00", "EUR")
    ]
    assert [x.category for x in result.listings] == ["Byzantine", "Roman"]


def test_historynumis_accepts_public_json_rendered_catalog_from_fetcher():
    url = "https://historynumis.com/en/35-ancient-coins"
    body = json.dumps({"rendered_products": fixture("historynumis-1.html"), "pagination": {"pages_count": 2}})
    second = json.dumps({"rendered_products": fixture("historynumis-2.html"), "pagination": {"pages_count": 2}})
    result = scrape_source(source("historynumis", url), FixtureFetcher({url: body, url + "?page=2": second}))
    assert result.complete and result.pages == 2
    assert [x.external_id for x in result.listings] == ["624", "625"]


def test_shanna_sold_items_never_appear_available():
    url = "https://www.shannaschmidt.com/greek-coins"
    result = scrape_source(source("shanna", url), FixtureFetcher({url: fixture("shanna.html")}))
    assert result.complete and result.pages == 1
    assert [(x.external_id, x.price, x.currency, x.availability) for x in result.listings] == [
        ("69798891d42a8175ab7a8cf1", "385000.00", "USD", "available"),
        ("693c623c144d875d2d92da4c", "65000.00", "USD", "sold"),
    ]
    assert all(x.category == "Greek" for x in result.listings)


def test_shanna_retains_later_valid_products_and_reports_each_untitled_card():
    url = "https://www.shannaschmidt.com/greek-coins"
    result = scrape_source(source("shanna", url), FixtureFetcher({url: fixture("shanna-untitled.html")}))
    assert not result.complete and result.pages == 1
    assert [(x.external_id, x.availability) for x in result.listings] == [
        ("69798891d42a8175ab7a8cf1", "available"),
        ("693c623c144d875d2d92da4c", "sold"),
    ]
    assert "2 unreadable product cards" in result.error
    assert "6ab15e2bd5e67563c3b314c1: missing title" in result.error
    assert "6a4d67b1c2a02a027d2166cf: missing title" in result.error


def test_palmyra_roman_category_uses_usd_and_purchase_control():
    url = "https://palmyraheritagegallery.com/product-category/ancient-coins/roman/"
    result = scrape_source(source("palmyra", url), FixtureFetcher({url: fixture("palmyra.html")}))
    assert result.complete and result.pages == 1
    assert [(x.external_id, x.price, x.currency, x.category, x.availability) for x in result.listings] == [
        ("33808", "3300.00", "USD", "Roman", "available"),
        ("10204", "175.00", "USD", "Roman", "available"),
    ]


def test_roman_republic_uses_discount_price_and_excludes_bulk_coins():
    url = "https://www.romancoinshop.com/en/roman-republic/"
    result = scrape_source(source("roman-republic", url), FixtureFetcher({url: fixture("roman-republic.html")}))
    assert result.pages == 1 and not result.complete
    assert [(x.external_id, x.price, x.currency, x.availability) for x in result.listings] == [
        ("16344", "269.00", "EUR", "available"),
        ("16177", "199.00", "EUR", "sold"),
    ]
    assert "page=2" in result.error


def test_mrb_verifies_each_product_is_still_addable_without_submitting_form():
    url = "https://mrbcoins.com/cgi-bin/category.pl?id=701"
    detail1 = "https://mrbcoins.com/cgi-bin/lotinfo.pl?id=68683"
    detail2 = "https://mrbcoins.com/cgi-bin/lotinfo.pl?id=68670"
    fetcher = FixtureFetcher({url: fixture("mrb-category.html"), detail1: fixture("mrb-detail.html"), detail2: fixture("mrb-detail.html").replace("68683", "68670")})
    result = scrape_source(source("mrb", url), fetcher)
    assert result.complete and result.pages == 1
    assert [(x.external_id, x.price, x.currency, x.category, x.availability) for x in result.listings] == [
        ("68683", "20.00", "USD", "Roman", "available"),
        ("68670", "30.00", "USD", "Roman", "available"),
    ]
    assert fetcher.requests == [url, detail1, detail2]


def test_coincom_republican_category_parses_fixed_prices_and_purchase_links():
    url = "https://www.coin.com/cgi-local/findCoins.cgi?grp=7"
    result = scrape_source(source("coincom", url), FixtureFetcher({url: fixture("coincom.html")}))
    assert result.complete and result.pages == 1
    assert [(x.external_id, x.price, x.currency, x.category) for x in result.listings] == [
        ("57675", "8500.00", "USD", "Roman"), ("57547", "700.00", "USD", "Roman")
    ]
    assert result.listings[0].url == "https://www.coin.com/cgi-local/find1Coins.cgi?inventory=57675"


def test_mrb_missing_product_detail_makes_baseline_incomplete():
    url = "https://mrbcoins.com/cgi-bin/category.pl?id=701"
    result = scrape_source(source("mrb", url), FixtureFetcher({url: fixture("mrb-category.html")}))
    assert not result.complete and result.pages == 1 and result.listings == []


def test_price_parser_prefers_woo_sale_and_handles_european_separators():
    url = "https://www.silburycoins.co.uk/product-category/roman-byzantine-coins/republican-290-41bc/"
    html = fixture("silbury.html").replace('<span class="price"><span class="woocommerce-Price-amount amount"><bdi><span class="woocommerce-Price-currencySymbol">£</span>185.00</bdi></span></span>', '<span class="price"><del>£249.00</del><ins>£199.00</ins></span>')
    result = scrape_source(source("silbury", url), FixtureFetcher({url: html}))
    assert result.listings[0].price == "199.00"
    euro = "https://historynumis.com/en/35-ancient-coins"
    html = fixture("historynumis-2.html").replace("€3,330.00", "€1.234,50")
    result = scrape_source(source("historynumis", euro), FixtureFetcher({euro: html}))
    assert result.listings[0].price == "1234.50"


def test_scoped_catalogs_skip_generic_lots_replicas_and_modern_insertions():
    url = "https://www.silburycoins.co.uk/product-category/roman-byzantine-coins/republican-290-41bc/"
    injected = fixture("silbury.html").replace("</ul>", '<li class="product post-99000 instock"><a class="woocommerce-LoopProduct-link" href="https://www.silburycoins.co.uk/product/replica/"><h2 class="woocommerce-loop-product__title">Roman Replica Coin Lot</h2><span class="price">£10.00</span></a></li></ul>')
    result = scrape_source(source("silbury", url), FixtureFetcher({url: injected}))
    assert result.complete
    assert [x.external_id for x in result.listings] == ["77050", "77053"]


def test_vpuk_rejects_modern_roman_themed_coin():
    url = "https://vpukcoins.com/collections/new-arrivals"
    modern = '<ul id="product-grid"><li class="grid__item"><div class="card-wrapper"><h3 class="card__heading"><a href="/products/roman-britain-50p">2024 Roman Britain 50p</a></h3><span class="price-item--regular">£12.00 GBP</span><input name="product-id" value="9999"><button>Add to cart</button></div></li></ul>'
    result = scrape_source(source("vpuk", url), FixtureFetcher({url: modern}))
    assert result.complete and result.listings == []


def test_premium_card_without_purchase_status_cannot_be_assumed_available():
    url = "https://premium-ancient-coins.com/en-gb/collections/roman-coins-for-sale"
    missing = '<div class="product-item"><a class="product-item__title" href="/en-gb/products/roman-denarius">Roman Denarius</a><span class="price">£100.00 GBP</span><input name="product-id" value="9999"></div>'
    result = scrape_source(source("premium", url), FixtureFetcher({url: missing}))
    assert not result.complete and result.listings == []
    assert "availability" in result.error.lower()


def test_missing_price_makes_page_incomplete_instead_of_erasing_baseline():
    url = "https://historynumis.com/en/35-ancient-coins"
    broken = '<div class="elementor-product-miniature"><a class="elementor-product-link" href="https://historynumis.com/en/accueil/625-roman-aureus.html"></a><h3 class="elementor-title">Aureus - Vespasian - 74</h3><form class="elementor-atc"><button>Add to cart</button></form></div>'
    result = scrape_source(source("historynumis", url), FixtureFetcher({url: broken}))
    assert not result.complete and result.pages == 1 and result.listings == []
    assert "price" in result.error.lower()


def test_unknown_adapter_reports_failure():
    result = scrape_source(source("unknown", "https://example.com/"), FixtureFetcher({}))
    assert not result.complete and result.pages == 0 and "adapter" in result.error.lower()
