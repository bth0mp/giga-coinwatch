"""Browser behavior against the real catalog database."""

from fastapi.testclient import TestClient

from coinwatch.db import Database
from coinwatch.models import Listing
from coinwatch.web import create_app


class RuntimeStub:
    """The scan worker is external to route behavior; storage stays real."""

    def __init__(self):
        self.started = False
        self.stopped = False
        self.scan_requested = False
        self.last_error = ""

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def start_scan(self):
        self.scan_requested = True
        return True

    def snapshot(self):
        return {"running": False, "phase": "idle", "source": None, "last_error": self.last_error}


def setup_catalog(tmp_path):
    db = Database(tmp_path / "coinwatch.db")
    db.initialize([{"id": "shop", "name": "Test & <dealer>", "url": "https://example.com",
                    "adapter": "test", "enabled": True}])
    run = db.claim_run("manual")
    db.record_source_result(run, db.get_source("shop"), [
        Listing("old", "https://example.com/old", "Old coin", "20.00", "EUR")
    ], True, 1, None)
    db.record_source_result(run, db.get_source("shop"), [
        Listing("new", "https://example.com/coin/4", "Denarius <script>alert(1)</script>",
                "45.00", "GBP", image_url="https://example.com/coin.jpg", category="Roman")
    ], True, 1, None)
    db.save_candidate({"domain": "other.example", "url": "https://other.example",
                       "name": "Other dealer", "reason": "Ancient coins with prices",
                       "evidence_url": "https://directory.example/other", "discovered_from": "Dealer directory"})
    runtime = RuntimeStub()
    return TestClient(create_app(db, runtime), follow_redirects=False), db, runtime


def token(db):
    return {"csrf_token": db.settings()["csrf_token"]}


def test_catalog_filters_real_listings_and_escapes_dealer_text(tmp_path):
    client, _, _ = setup_catalog(tmp_path)
    response = client.get("/?q=denarius&view=all&currency=GBP&max_price=50")
    assert response.status_code == 200
    assert "Denarius &lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "Test &amp; &lt;dealer&gt;" in response.text
    assert "GBP 45.00" in response.text
    assert "Old coin" not in response.text
    assert client.get("/?view=all&currency=GBP&max_price=40").text.find("Denarius") == -1
    assert client.get("/?view=all&max_price=40").status_code == 400


def test_save_and_pause_change_real_catalog_state(tmp_path):
    client, db, _ = setup_catalog(tmp_path)
    listing_id = db.list_listings(view="new")[0][0]["id"]
    assert client.post(f"/listings/{listing_id}/save").status_code == 403
    assert client.post(f"/listings/{listing_id}/save", data=token(db)).status_code == 303
    assert db.list_listings(view="saved")[1] == 1
    assert "Denarius" in client.get("/?view=saved").text
    assert client.post("/sources/shop/enabled", data={**token(db), "enabled": "false"}).status_code == 303
    assert db.get_source("shop")["enabled"] == 0


def test_foreign_origin_host_and_wrong_token_cannot_mutate(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    assert client.post("/scan", data=token(db), headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/scan", data=token(db), headers={"host": "evil.example"}).status_code == 403
    assert client.post("/scan", data={"csrf_token": "wrong"}).status_code == 403
    assert runtime.scan_requested is False


def test_discovery_settings_history_and_scan_use_persistent_state(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    candidate_id = db.list_candidates()[0]["id"]
    assert "other.example" in client.get("/discoveries").text
    assert client.post(f"/discoveries/{candidate_id}/decision", data={**token(db), "decision": "dismissed"}).status_code == 303
    assert db.list_candidates() == []
    assert db.list_candidates("dismissed")[0]["domain"] == "other.example"
    assert client.post("/settings", data={**token(db), "timezone": "Europe/London", "scan_time": "10:30"}).status_code == 303
    assert db.settings()["scan_time"] == "10:30"
    assert "manual" in client.get("/history").text
    assert client.post("/scan", data=token(db)).status_code == 303
    assert runtime.scan_requested is True
    assert client.get("/api/status").json()["running"] is False
    assert client.get("/health").json()["status"] == "ok"


def test_unsupported_added_source_cannot_be_enabled(tmp_path):
    client, db, _ = setup_catalog(tmp_path)
    assert client.post("/sources/add", data={**token(db), "url": "https://another.example", "name": "Another"}).status_code == 303
    source = next(source for source in db.list_sources() if source["name"] == "Another")
    assert source["enabled"] == 0
    assert client.post(f"/sources/{source['id']}/enabled", data={**token(db), "enabled": "true"}).status_code == 400
    assert f'/sources/{source["id"]}/enabled' not in client.get("/sources").text


def test_private_source_url_returns_client_error(tmp_path):
    client, db, _ = setup_catalog(tmp_path)
    response = client.post("/sources/add", data={**token(db), "url": "http://127.0.0.1/coins"})
    assert response.status_code == 400
    assert "Private network addresses" in response.text


def test_app_lifespan_starts_and_stops_runtime(tmp_path):
    client, _, runtime = setup_catalog(tmp_path)
    with client:
        assert runtime.started
        assert client.get("/health").status_code == 200
    assert runtime.stopped


def test_scan_error_banner_links_to_history_without_dumping_external_detail(tmp_path):
    client, _, runtime = setup_catalog(tmp_path)
    runtime.last_error = "Dealer directory failed at <script>alert(1)</script>; " + "details " * 300
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/history"' in response.text
    assert "Dealer directory failed" not in response.text
    assert "details details details" not in response.text
