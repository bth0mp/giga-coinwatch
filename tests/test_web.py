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
        self.scan_request = None
        self.last_error = ""
        self.web_results = {"status": "unconfigured", "results": [], "error": "", "checked_at": ""}

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def start_scan(self, kind="manual", mode="both", search_id=None):
        self.scan_requested = True
        self.scan_request = {"kind": kind, "mode": mode, "search_id": search_id}
        return True

    def search_results(self, search_id):
        return self.web_results

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


def test_scan_modes_validate_scope_and_return_to_current_page(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    for mode in ("coins", "dealers", "both"):
        response = client.post("/scan", data={**token(db), "mode": mode, "return_to": "/discoveries"})
        assert response.status_code == 303
        assert response.headers["location"] == "/discoveries?scan=started"
        assert runtime.scan_request == {"kind": "manual", "mode": mode, "search_id": None}
    assert client.post("/scan", data={**token(db), "mode": "everything"}).status_code == 400
    response = client.post("/scan", data={**token(db), "mode": "coins", "return_to": "https://other.example"})
    assert response.headers["location"] == "/?scan=started"


def test_wanted_search_create_edit_match_and_scan(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    response = client.post("/wanted", data={**token(db), "name": "My denarii", "coin_type": "denarius",
                                           "currency": "GBP", "max_price": "50", "enabled": "true", "include_web": "true"})
    assert response.status_code == 303
    search = db.list_searches()[0]
    assert search["name"] == "My denarii"
    assert search["include_web"] == 1
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "Denarius &lt;script&gt;alert(1)&lt;/script&gt;" in detail.text
    assert "Old coin" not in detail.text
    assert "Wider web" in detail.text
    assert "My denarii" in client.get("/wanted").text
    response = client.post(f"/wanted/{search['id']}/scan", data=token(db))
    assert response.status_code == 303
    assert runtime.scan_request == {"kind": "manual", "mode": "coins", "search_id": search["id"]}
    response = client.post(f"/wanted/{search['id']}", data={**token(db), "name": "Lower budget", "coin_type": "denarius",
                                                         "currency": "GBP", "max_price": "40", "enabled": "true"})
    assert response.status_code == 303
    assert db.get_search(search["id"])["include_web"] == 0
    assert "Denarius &lt;script&gt;" not in client.get(response.headers["location"]).text


def test_wanted_search_mutations_require_csrf_and_missing_search_returns_404(tmp_path):
    client, db, _ = setup_catalog(tmp_path)
    assert client.post("/wanted", data={"name": "Unauthorised", "keywords": "athens"}).status_code == 403
    response = client.post("/wanted", data={**token(db), "name": "Athens", "mint": "Athens", "enabled": "true"})
    assert response.status_code == 303
    search_id = db.list_searches()[0]["id"]
    assert client.post(f"/wanted/{search_id}/enabled", data={**token(db), "enabled": "false"}).status_code == 303
    assert db.get_search(search_id)["enabled"] == 0
    assert client.post(f"/wanted/{search_id}/enabled", data={**token(db), "enabled": "perhaps"}).status_code == 400
    assert client.post(f"/wanted/{search_id}/delete").status_code == 403
    assert client.post(f"/wanted/{search_id}/delete", data=token(db)).status_code == 303
    assert db.list_searches() == []
    assert client.get(f"/wanted/{search_id}").status_code == 404
    assert client.post(f"/wanted/{search_id}/scan", data=token(db)).status_code == 404


def test_web_search_key_is_saved_locally_never_rendered_and_clearable(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    client, db, _ = setup_catalog(tmp_path)
    secret = "tvly-test-secret-not-for-html"
    assert client.post("/settings/web-search", data={"api_key": secret}).status_code == 403
    response = client.post("/settings/web-search", data={**token(db), "api_key": secret, "action": "save"})
    assert response.status_code == 303
    from coinwatch.web_search import load_api_key, provider_settings
    assert load_api_key(tmp_path) == secret
    assert secret not in client.get("/settings").text
    assert secret not in client.get("/api/status").text
    assert client.post("/settings/web-search", data={**token(db), "action": "clear"}).status_code == 303
    assert provider_settings(tmp_path)["configured"] is False


def test_wanted_web_leads_are_escaped_and_not_misrepresented_as_catalog_coins(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    response = client.post("/wanted", data={**token(db), "name": "Athens", "mint": "Athens", "include_web": "true"})
    assert response.status_code == 303
    runtime.web_results = {"status": "complete", "results": [
        {"title": "Owl <script>bad()</script>", "url": "https://shop.example/coin", "snippet": "A silver <b>coin</b>"},
        {"title": "Unsafe result", "url": "javascript:alert(1)", "snippet": "Unverified price"},
    ], "error": "", "checked_at": "2026-09-25T12:00:00+00:00"}
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert 'href="https://shop.example/coin"' in detail.text
    assert "Owl &lt;script&gt;bad()&lt;/script&gt;" in detail.text
    assert "A silver &lt;b&gt;coin&lt;/b&gt;" in detail.text
    assert "javascript:" not in detail.text
    assert "Unverified web lead" in detail.text
    assert db.list_listings(view="all")[1] == 2
