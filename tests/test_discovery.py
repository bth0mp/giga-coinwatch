from types import SimpleNamespace

import pytest

import coinwatch.discovery as discovery
from test_sources import fixture


class FixtureFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        if url not in self.pages:
            raise RuntimeError("unavailable")
        page = self.pages[url]
        return page if hasattr(page, "text") else SimpleNamespace(url=url, text=page)


def test_discovery_requires_shop_evidence_and_deduplicates_domains(monkeypatch):
    directory = "https://directory.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory,))
    fetcher = FixtureFetcher({
        directory: fixture("dealers.html"),
        "https://new.example/ancient-coins": fixture("dealer-shop.html"),
        "https://auction.example/auction": fixture("dealer-auction.html"),
    })
    found = discovery.discover(fetcher, {"old.example", "later.example"}, limit=20)
    assert [(x["domain"], x["url"], x["evidence_url"], x["discovered_from"]) for x in found] == [
        ("new.example", "https://new.example/ancient-coins", "https://new.example/ancient-coins", directory)
    ]
    assert "fixed-price" in found[0]["reason"]
    assert "http://127.0.0.1/private" not in fetcher.requests
    assert "https://old.example/ancient-coins" not in fetcher.requests


def test_directory_failure_carries_partial_candidates_and_failure_detail(monkeypatch):
    directory = "https://directory.example/dealers"
    broken = "https://broken.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory, broken))
    fetcher = FixtureFetcher({directory: fixture("dealers.html"), "https://new.example/ancient-coins": fixture("dealer-shop.html"), "https://auction.example/auction": fixture("dealer-auction.html")})
    with pytest.raises(discovery.DiscoveryError) as error:
        discovery.discover(fetcher, {"old.example"}, limit=20)
    assert [x["domain"] for x in error.value.candidates] == ["new.example", "later.example"]
    assert {x["url"] for x in error.value.failures} == {broken, "https://later.example/ancient-coins"}


def test_candidate_fetch_failure_is_visible(monkeypatch):
    directory = "https://directory.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory,))
    fetcher = FixtureFetcher({directory: fixture("dealers.html")})
    with pytest.raises(discovery.DiscoveryError) as error:
        discovery.discover(fetcher, {"old.example"}, limit=1)
    assert [(x["domain"], x["evidence_url"]) for x in error.value.candidates] == [("new.example", "")]
    assert "inaccessible" in error.value.candidates[0]["reason"].lower()
    assert error.value.failures[0]["url"] == "https://new.example/ancient-coins"


def test_recording_unclear_first_batch_allows_later_domain_next_day(monkeypatch):
    directory = "https://directory.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory,))
    pages = {directory: fixture("dealers.html"), "https://new.example/ancient-coins": "<h1>Ancient coin dealer</h1>", "https://later.example/ancient-coins": fixture("dealer-shop.html")}
    first = discovery.discover(FixtureFetcher(pages), {"old.example"}, limit=1)
    assert [(x["domain"], x["evidence_url"]) for x in first] == [("new.example", "")]
    assert "unclear" in first[0]["reason"].lower()
    second = discovery.discover(FixtureFetcher(pages), {"old.example", "new.example"}, limit=1)
    assert [(x["domain"], x["evidence_url"]) for x in second] == [("later.example", "https://later.example/ancient-coins")]


def test_ancient_guide_and_separate_modern_product_are_not_shop_evidence(monkeypatch):
    directory = "https://directory.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory,))
    page = '<article><h1>Roman coin collecting guide</h1><p>How to identify denarii.</p></article><article><h2>Modern 50p</h2><span>£20.00</span><button>Buy now</button></article>'
    found = discovery.discover(FixtureFetcher({directory: '<a href="https://new.example/coins">New coin shop</a>', "https://new.example/coins": page}), set())
    assert found[0]["domain"] == "new.example"
    assert found[0]["evidence_url"] == ""
    assert "unclear" in found[0]["reason"].lower()


def test_cross_domain_redirect_does_not_claim_evidence_for_known_dealer(monkeypatch):
    directory = "https://directory.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory,))
    pages = {
        directory: '<a href="https://alias.example/ancient-coins">Ancient coin dealer</a>',
        "https://alias.example/ancient-coins": SimpleNamespace(url="https://known.example/ancient-coins", text=fixture("dealer-shop.html")),
    }
    found = discovery.discover(FixtureFetcher(pages), {"known.example"})
    assert [(x["domain"], x["evidence_url"]) for x in found] == [("alias.example", "")]
    assert "redirect" in found[0]["reason"].lower()


def test_catalog_product_detail_can_supply_purchase_evidence(monkeypatch):
    directory = "https://directory.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory,))
    catalog = "https://new.example/coin-catalog"
    detail = "https://new.example/product/vespasian-ae-diobol/"
    fetcher = FixtureFetcher({
        directory: f'<a href="{catalog}">Ancient coin shop</a>',
        catalog: fixture("discovery-catalog.html"),
        detail: fixture("discovery-product.html"),
    })
    found = discovery.discover(fetcher, set())
    assert [(x["domain"], x["evidence_url"]) for x in found] == [("new.example", detail)]
    assert fetcher.requests == [directory, catalog, detail]


def test_woo_card_with_wrapping_link_follows_coin_detail(monkeypatch):
    directory = "https://directory.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory,))
    catalog = "https://new.example/"
    detail = "https://new.example/shop/showcase/didius-sestertius/"
    fetcher = FixtureFetcher({
        directory: f'<a href="{catalog}">Ancient coin dealer</a>',
        catalog: fixture("discovery-woo-catalog.html"),
        detail: '<article class="product"><h1>Didius Iulianus AD 193 AE Sestertius</h1><span class="price">$995.00</span><button>Add to cart</button></article>',
    })
    found = discovery.discover(fetcher, set())
    assert found[0]["evidence_url"] == detail
    assert fetcher.requests == [directory, catalog, detail]


def test_ancient_artifact_with_price_and_cart_is_not_coin_evidence(monkeypatch):
    directory = "https://directory.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory,))
    catalog = "https://new.example/ancient-artifacts"
    page = '<article class="product"><h2>Ancient Roman glass flask</h2><span>$125.00</span><button>Add to cart</button></article>'
    found = discovery.discover(FixtureFetcher({directory: f'<a href="{catalog}">Ancient dealer</a>', catalog: page}), set())
    assert found[0]["evidence_url"] == ""


def test_product_detail_takes_priority_over_homepage_navigation(monkeypatch):
    directory = "https://directory.example/dealers"
    monkeypatch.setattr(discovery, "DIRECTORIES", (directory,))
    home = "https://new.example/"
    catalog = "https://new.example/coin-catalog"
    detail = "https://new.example/product/vespasian-ae-diobol/"
    fetcher = FixtureFetcher({
        directory: f'<a href="{home}">Ancient coin shop</a>',
        home: '<a href="/coin-catalog">Coin catalog</a><a href="/roman-history">Roman history</a><a href="/shop-info">Shop info</a>',
        catalog: fixture("discovery-catalog.html"),
        detail: fixture("discovery-product.html"),
    })
    found = discovery.discover(fetcher, set())
    assert found[0]["evidence_url"] == detail
    assert fetcher.requests == [directory, home, catalog, detail]
