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

    def start_scan(self, kind="manual", mode="both", search_id=None, web_queries=1, web_minutes=10):
        self.scan_requested = True
        self.scan_request = {"kind": kind, "mode": mode, "search_id": search_id, "web_queries": web_queries, "web_minutes": web_minutes}
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


def test_source_status_filter_explains_unsupported_without_fake_baseline(tmp_path):
    client, db, _ = setup_catalog(tmp_path)
    db.initialize([dict(id='blocked', name='Blocked shop', url='https://blocked.example',
                        adapter='', enabled=False, support_status='access_blocked',
                        support_checked='2026-09-26', note='HTTP 403 from the public catalog.')])
    page = client.get('/sources?status=access_blocked').text
    assert 'Blocked shop' in page and 'Access blocked' in page
    assert 'HTTP 403 from the public catalog.' in page and '2026-09-26' in page
    assert 'Test &amp; &lt;dealer&gt;' not in page
    assert 'Monitoring support pending' not in page and 'Baseline pending' not in page
    assert '/sources/blocked/enabled' not in page
    assert '1 of 2 sources' in page and 'Not monitored' in page


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
        assert runtime.scan_request == {"kind": "manual", "mode": mode, "search_id": None, "web_queries": 1, "web_minutes": 10}
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
    assert "For sale on wider web" in detail.text
    assert "My denarii" in client.get("/wanted").text
    response = client.post(f"/wanted/{search['id']}/scan", data=token(db))
    assert response.status_code == 303
    assert runtime.scan_request == {"kind": "manual", "mode": "coins", "search_id": search["id"], "web_queries": 1, "web_minutes": 10}
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


def test_delete_search_is_visible_and_confirmation_only_removes_selected_search(tmp_path):
    from bs4 import BeautifulSoup
    client, db, _ = setup_catalog(tmp_path)
    selected = db.save_search({"name": "Athens & <owl>", "mint": "Athens", "include_web": True})
    other = db.save_search({"name": "Rome", "mint": "Rome"})
    db.record_web_search(selected, [{"url": "https://example.com/athens", "title": "Athens coin"}])
    listing_id = db.list_listings(view="all")[0][0]["id"]
    client.post(f"/listings/{listing_id}/save", data=token(db))
    catalog_before = db.list_listings(view="all")
    saved_before = db.list_listings(view="saved")
    delete_url = f"/wanted/{selected}/delete"
    for path in ("/search", f"/wanted/{selected}"):
        soup = BeautifulSoup(client.get(path).text, "html.parser")
        link = soup.select_one(f'a[href="{delete_url}"]')
        assert link is not None and link.find_parent("details") is None
    confirmation = client.get(delete_url)
    assert confirmation.status_code == 200
    assert "Athens &amp; &lt;owl&gt;" in confirmation.text
    soup = BeautifulSoup(confirmation.text, "html.parser")
    cancel = soup.find("a", string="Cancel")
    assert client.get(cancel["href"]).status_code == 200
    assert len(db.search_results(selected)["results"]) == 1
    assert len(db.list_searches()) == 2
    assert client.post(delete_url).status_code == 403
    response = client.post(delete_url, data=token(db))
    assert response.status_code == 303 and response.headers["location"] == "/search"
    assert [row["id"] for row in db.list_searches()] == [other]
    assert client.get(delete_url).status_code == 404
    assert db.list_listings(view="all") == catalog_before
    assert db.list_listings(view="saved") == saved_before
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM search_web_results WHERE search_id=?", (selected,)).fetchone()[0] == 0


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


def test_wanted_for_sale_web_listings_escape_titles_and_omit_search_snippets(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    response = client.post("/wanted", data={**token(db), "name": "Athens", "mint": "Athens", "include_web": "true"})
    assert response.status_code == 303
    runtime.web_results = {"status": "complete", "results": [
        {"title": "Owl <script>bad()</script>", "url": "https://shop.example/coin", "snippet": "A silver <b>coin</b>",
         "sale_status": "available", "price": "275.00", "currency": "EUR", "sale_checked_at": "2026-09-25T11:30:00+00:00"},
        {"title": "Unsafe result", "url": "javascript:alert(1)", "snippet": "Unverified price",
         "sale_status": "available", "price": "300.00", "currency": "EUR", "sale_checked_at": "2026-09-25T11:30:00+00:00"},
    ], "error": "", "checked_at": "2026-09-25T12:00:00+00:00"}
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert 'href="https://shop.example/coin"' in detail.text
    assert "Owl &lt;script&gt;bad()&lt;/script&gt;" in detail.text
    assert "A silver" not in detail.text
    assert "EUR 275.00" in detail.text
    assert "javascript:" not in detail.text
    assert "Availability checked 25 Sep 2026, 12:30" in detail.text
    assert db.list_listings(view="all")[1] == 2


def test_wanted_scan_accepts_custom_query_count_and_rejects_invalid_budgets(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    search_id = db.save_search({"name": "Athens", "mint": "Athens", "include_web": True})
    response = client.post(f"/wanted/{search_id}/scan", data={**token(db), "web_queries": "17"})
    assert response.status_code == 303
    assert runtime.scan_request["web_queries"] == 17
    for value in ("0", "51", "-1", "1.5", "all"):
        runtime.scan_request = None
        response = client.post(f"/wanted/{search_id}/scan", data={**token(db), "web_queries": value})
        assert response.status_code == 400
        assert runtime.scan_request is None
    assert client.post(f"/wanted/{search_id}/scan", data={"web_queries": "17"}).status_code == 403


def test_search_tab_runs_selected_watch_with_chosen_budget(tmp_path):
    from bs4 import BeautifulSoup

    client, db, runtime = setup_catalog(tmp_path)
    db.save_search({"name": "Athens", "mint": "Athens", "include_web": True})
    selected = db.save_search({"name": "Boiotian", "keywords": "Boiotian", "include_web": True})
    response = client.get("/search")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select_one('nav a[href="/search"]').get("aria-current") == "page"
    form = page.select_one('form[action="/search/scan"]')
    assert form.select_one(f'select[name="search_id"] option[value="{selected}"]')
    assert form.select_one('input[name="web_queries"]').get("max") == "50"
    response = client.post("/search/scan", data={**token(db), "search_id": selected, "web_queries": "17"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/wanted/{selected}?scan=started"
    assert runtime.scan_request == {"kind": "manual", "mode": "coins", "search_id": selected, "web_queries": 17, "web_minutes": 10}
    runtime.scan_request = None
    assert client.post("/search/scan", data={"search_id": selected, "web_queries": "17"}).status_code == 403
    assert client.post("/search/scan", data={**token(db), "search_id": selected, "web_queries": "51"}).status_code == 400
    assert client.post("/search/scan", data={**token(db), "search_id": 9999, "web_queries": "5"}).status_code == 404
    assert runtime.scan_request is None


def test_search_tab_without_saved_searches_does_not_offer_empty_scan(tmp_path):
    from bs4 import BeautifulSoup

    client, _, runtime = setup_catalog(tmp_path)
    response = client.get("/search")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    form = page.select_one('form[action="/search/scan"]')
    assert form is None or form.select_one('button[type="submit"]').has_attr("disabled")
    assert page.select_one('form[action="/wanted"] input[name="mint"]')
    assert runtime.scan_request is None


def test_wanted_scan_disables_web_budget_when_web_search_is_off(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    search_id = db.save_search({"name": "Athens", "mint": "Athens", "include_web": False})
    from bs4 import BeautifulSoup
    document = BeautifulSoup(client.get(f"/wanted/{search_id}").text, "html.parser")
    assert document.select_one('input[name="web_queries"]').has_attr("disabled")
    assert document.select_one('input[name="web_minutes"]').has_attr("disabled")
    response = client.post(f"/wanted/{search_id}/scan", data={**token(db), "web_queries": "5"})
    assert response.status_code == 400
    assert runtime.scan_requested is False
    assert client.post(f"/wanted/{search_id}/scan", data={**token(db), "web_minutes": "30"}).status_code == 400
    assert client.post(f"/wanted/{search_id}/scan", data=token(db)).status_code == 303


def test_manual_search_time_control_reaches_worker_without_increasing_query_limit(tmp_path):
    from bs4 import BeautifulSoup
    client, db, runtime = setup_catalog(tmp_path)
    search_id = db.save_search({"name": "Athens", "mint": "Athens", "include_web": True})
    for page_url in ("/search", f"/wanted/{search_id}"):
        document = BeautifulSoup(client.get(page_url).text, "html.parser")
        control = document.select_one('input[name="web_minutes"]')
        assert control is not None
        assert (control["min"], control["max"], control["value"]) == ("1", "120", "10")
    for endpoint in ("/search/scan", f"/wanted/{search_id}/scan"):
        for minutes in (1, 30, 120):
            response = client.post(endpoint, data={**token(db), "search_id": search_id,
                                                   "web_queries": "3", "web_minutes": str(minutes)})
            assert response.status_code == 303
            assert runtime.scan_request["web_minutes"] == minutes
            assert runtime.scan_request["web_queries"] == 3
        runtime.scan_request = None
        for invalid in ("0", "121", "-1", "1.5", "unlimited"):
            response = client.post(endpoint, data={**token(db), "search_id": search_id, "web_minutes": invalid})
            assert response.status_code == 400
            assert runtime.scan_request is None
        assert client.post(endpoint, data={"search_id": search_id, "web_minutes": "30"}).status_code == 403


def test_wanted_web_results_count_for_sale_domains_and_show_availability_check_dates(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    search_id = db.save_search({"name": "Athens", "mint": "Athens", "include_web": True})
    runtime.web_results = {"status": "complete", "results": [
        {"title": "Owl A", "url": "https://shop.example/a", "snippet": "Coin", "last_seen": "2026-09-25T12:00:00+00:00",
         "sale_status": "available", "price": "275.00", "currency": "GBP", "sale_checked_at": "2026-09-25T10:30:00+00:00"},
        {"title": "Owl B", "url": "https://www.shop.example/b", "snippet": "Coin",
         "sale_status": "available", "price": "300.00", "currency": "GBP", "sale_checked_at": "2026-09-25T10:30:00+00:00"},
        {"title": "Owl C", "url": "https://other.example/c", "snippet": "Coin",
         "sale_status": "available", "price": "325.00", "currency": "GBP", "sale_checked_at": "2026-09-25T10:30:00+00:00"},
    ], "error": "", "checked_at": "2026-09-25T12:00:00+00:00"}
    response = client.get(f"/wanted/{search_id}")
    assert response.status_code == 200
    assert "3 for-sale listings across 2 domains" in response.text
    assert "Availability checked 25 Sep 2026, 11:30" in response.text
    assert "Availability checked 25 Sep 2026, 13:00" not in response.text


def test_for_sale_template_hides_old_unchecked_results_during_runtime_upgrade(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    search_id = db.save_search({"name": "Athens", "mint": "Athens", "include_web": True})
    runtime.web_results = {"status": "complete", "results": [
        {"title": "Old unchecked lead", "url": "https://shop.example/unchecked", "snippet": "Old search result"},
        {"title": "Sold owl", "url": "https://shop.example/sold", "sale_status": "sold"},
    ], "error": "", "checked_at": "2026-09-25T12:00:00+00:00"}
    response = client.get(f"/wanted/{search_id}")
    assert response.status_code == 200
    assert "Old unchecked lead" not in response.text
    assert "Sold owl" not in response.text
    assert "No verified for-sale listings yet" in response.text
