"""Local, server-rendered collector interface."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .fetch import FetchError
from .web_search import clear_api_key, provider_settings, save_api_key


PACKAGE_DIR = Path(__file__).resolve().parent
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


def _safe_url(value: object) -> str:
    """Only expose ordinary seller links and images in browser markup."""
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return value.strip()
    except ValueError:
        pass
    return ""


def _local_time(value: object, timezone: str) -> str:
    if not value:
        return "—"
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return str(value)
        return instant.astimezone(ZoneInfo(timezone)).strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError, KeyError):
        return str(value)


def _return_to(value: str | None, fallback: str = "/") -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return fallback


def _scan_redirect(value: str, started: bool) -> RedirectResponse:
    destination = urlsplit(_return_to(value))
    params = dict(parse_qsl(destination.query))
    params["scan"] = "started" if started else "running"
    return RedirectResponse(urlunsplit(("", "", destination.path, urlencode(params), "")), status_code=303)


def create_app(db, runtime) -> FastAPI:
    """Create the private UI using the storage and scan runtime contracts."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title="giga-coinwatch", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters["safe_url"] = _safe_url
    templates.env.filters["local_time"] = _local_time

    @app.middleware("http")
    async def local_requests_only(request: Request, call_next):
        if request.url.hostname not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Local access only"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    expected = urlsplit(str(request.base_url))
                    if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
                        return JSONResponse({"detail": "Foreign origin"}, status_code=403)
                except ValueError:
                    return JSONResponse({"detail": "Foreign origin"}, status_code=403)
        return await call_next(request)

    def context(request: Request, *, page: str, **extra):
        settings = db.settings()
        return {
            "request": request,
            "page": page,
            "settings": settings,
            "csrf_token": settings["csrf_token"],
            "stats": db.stats(),
            "runtime": runtime.snapshot(),
            "scan_return_to": request.url.path + ("?" + request.url.query if request.url.query else ""),
            **extra,
        }

    def require_csrf(token: str):
        from secrets import compare_digest

        actual = str(db.settings()["csrf_token"])
        if not token or not compare_digest(token, actual):
            raise HTTPException(status_code=403, detail="Invalid form token")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/status")
    def status():
        return {**runtime.snapshot(), "stats": db.stats(), "settings": {k: db.settings().get(k) for k in ("timezone", "scan_time")}}

    @app.get("/")
    def listings(
        request: Request,
        q: str = "",
        source: str = "",
        category: str = "",
        currency: str = "",
        max_price: str = "",
        view: str = "new",
        page: int = 1,
    ):
        if view not in {"new", "all", "saved"}:
            raise HTTPException(status_code=400, detail="Unknown listing view")
        if page < 1:
            raise HTTPException(status_code=400, detail="Page must be positive")
        if max_price and not currency:
            raise HTTPException(status_code=400, detail="Select a currency to use a maximum price")
        price = None
        if max_price:
            from decimal import Decimal, InvalidOperation

            try:
                price = Decimal(max_price)
                if not price.is_finite() or price < 0:
                    raise InvalidOperation
            except InvalidOperation:
                raise HTTPException(status_code=400, detail="Maximum price must be a positive amount") from None
        rows, total = db.list_listings(
            q=q.strip(), source=source, category=category.strip(), currency=currency.strip().upper(),
            max_price=price, view=view, page=page, per_page=30,
        )
        params = {"q": q, "source": source, "category": category, "currency": currency,
                  "max_price": max_price, "view": view}
        params = {k: v for k, v in params.items() if v}
        return templates.TemplateResponse(
            request,
            "listings.html",
            context(
                request, page="listings", rows=rows, total=total, sources=db.list_sources(), filters=params,
                view=view, current_page=page, has_next=page * 30 < total,
                prev_url="/?" + urlencode({**params, "page": page - 1}),
                next_url="/?" + urlencode({**params, "page": page + 1}),
                return_to=str(request.url.path) + ("?" + request.url.query if request.url.query else ""),
            ),
        )

    @app.get("/sources")
    def sources(request: Request):
        from .source_support import describe_support
        sources = [dict(s, support=describe_support(s)) for s in db.list_sources()]
        counts = {}
        for s in sources:
            key = s['support']['key']
            if key not in counts:
                counts[key] = dict(s['support'], count=0)
            counts[key]['count'] += 1
        selected = request.query_params.get('status', '')
        visible = [s for s in sources if not selected or s['support']['key'] == selected]
        return templates.TemplateResponse(request, "sources.html", context(
            request, page="sources", sources=visible, source_total=len(sources),
            support_counts=list(counts.values()), selected_status=selected))

    def find_search(search_id: int):
        try:
            return db.get_search(search_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Wanted search not found") from None

    @app.get("/search")
    @app.get("/wanted")
    def wanted(request: Request):
        return templates.TemplateResponse(request, "wanted.html", context(
            request, page="wanted", searches=db.list_searches(), search={"enabled": True, "include_web": True},
        ))

    @app.get("/wanted/{search_id}")
    def wanted_detail(request: Request, search_id: int, page: int = 1):
        if page < 1:
            raise HTTPException(status_code=400, detail="Page must be positive")
        search = find_search(search_id)
        rows, total = db.search_matches(search_id, page=page, per_page=30)
        web_results = runtime.search_results(search_id)
        web_domains = {urlsplit(_safe_url(lead.get("url"))).hostname.removeprefix("www.")
                       for lead in web_results["results"] if _safe_url(lead.get("url"))}
        return templates.TemplateResponse(request, "wanted_detail.html", context(
            request, page="wanted", search=search, rows=rows, total=total,
            web_results=web_results, web_domain_count=len(web_domains), current_page=page, has_next=page * 30 < total,
            prev_url=f"/wanted/{search_id}?page={page - 1}", next_url=f"/wanted/{search_id}?page={page + 1}",
            return_to=request.url.path + ("?" + request.url.query if request.url.query else ""),
        ))

    async def save_wanted(request: Request, search_id: int | None = None):
        form = await request.form()
        require_csrf(str(form.get("csrf_token", "")))
        if search_id is not None:
            find_search(search_id)
        values = {field: str(form.get(field, "")).strip() for field in (
            "name", "keywords", "coin_type", "mint", "ruler", "exclude_terms", "category", "currency", "max_price",
        )}
        values.update(enabled=form.get("enabled") == "true", include_web=form.get("include_web") == "true")
        try:
            saved_id = db.save_search(values, search_id=search_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse(f"/wanted/{saved_id}", status_code=303)

    @app.post("/wanted")
    async def wanted_create(request: Request):
        return await save_wanted(request)

    @app.post("/wanted/{search_id}")
    async def wanted_update(request: Request, search_id: int):
        return await save_wanted(request, search_id)

    @app.post("/wanted/{search_id}/enabled")
    def wanted_enabled(search_id: int, csrf_token: str = Form(""), enabled: str = Form(""), return_to: str = Form("/wanted")):
        require_csrf(csrf_token)
        find_search(search_id)
        if enabled not in {"true", "false"}:
            raise HTTPException(status_code=400, detail="Invalid search state")
        db.set_search_enabled(search_id, enabled == "true")
        return RedirectResponse(_return_to(return_to, "/wanted"), status_code=303)

    @app.get("/wanted/{search_id}/delete")
    def wanted_delete_confirmation(request: Request, search_id: int):
        return templates.TemplateResponse(request, "wanted_delete.html", context(
            request, page="wanted", search=find_search(search_id),
        ))

    @app.post("/wanted/{search_id}/delete")
    def wanted_delete(search_id: int, csrf_token: str = Form("")):
        require_csrf(csrf_token)
        find_search(search_id)
        db.delete_search(search_id)
        return RedirectResponse("/search", status_code=303)

    @app.post("/wanted/{search_id}/scan")
    def wanted_scan(search_id: int, csrf_token: str = Form(""), web_queries: str = Form("1"), web_minutes: str = Form("10")):
        require_csrf(csrf_token)
        search = find_search(search_id)
        try:
            count = int(web_queries)
            if not 1 <= count <= 50:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Choose a whole number of web queries from 1 to 50") from None
        try:
            minutes = int(web_minutes)
            if not 1 <= minutes <= 120:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Choose a whole number of web check minutes from 1 to 120") from None
        if count > 1 and not search["include_web"]:
            raise HTTPException(status_code=400, detail="Enable wider-web checking under Edit search before choosing multiple web queries")
        if minutes != 10 and not search["include_web"]:
            raise HTTPException(status_code=400, detail="Enable wider-web checking under Edit search before choosing a web check time")
        try:
            started = runtime.start_scan(mode="coins", search_id=search_id, web_queries=count, web_minutes=minutes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return _scan_redirect(f"/wanted/{search_id}", started)

    @app.post("/search/scan")
    def search_scan(search_id: int = Form(...), csrf_token: str = Form(""), web_queries: str = Form("1"), web_minutes: str = Form("10")):
        return wanted_scan(search_id, csrf_token, web_queries, web_minutes)

    @app.get("/discoveries")
    def discoveries(request: Request):
        return templates.TemplateResponse(
            request, "discoveries.html", context(request, page="discoveries", candidates=db.list_candidates(status="pending"))
        )

    @app.get("/settings")
    def settings(request: Request):
        return templates.TemplateResponse(request, "settings.html", context(
            request, page="settings", web_search=provider_settings(db.path.parent),
        ))

    @app.get("/history")
    def history(request: Request):
        return templates.TemplateResponse(request, "history.html", context(request, page="history", runs=db.runs(limit=20)))

    @app.post("/scan")
    def scan(csrf_token: str = Form(""), mode: str = Form("both"), return_to: str = Form("/")):
        require_csrf(csrf_token)
        if mode not in {"coins", "dealers", "both"}:
            raise HTTPException(status_code=400, detail="Choose coins, dealers, or both")
        return _scan_redirect(return_to, runtime.start_scan(mode=mode))

    @app.post("/listings/{listing_id}/save")
    def save(listing_id: int, csrf_token: str = Form(""), return_to: str = Form("/")):
        require_csrf(csrf_token)
        try:
            db.toggle_saved(listing_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Listing not found") from None
        return RedirectResponse(_return_to(return_to), status_code=303)

    @app.post("/sources/{source_id}/enabled")
    def source_enabled(source_id: str, csrf_token: str = Form(""), enabled: str = Form("")):
        require_csrf(csrf_token)
        if enabled not in {"true", "false"}:
            raise HTTPException(status_code=400, detail="Invalid source state")
        try:
            db.get_source(source_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Source not found") from None
        try:
            db.set_source_enabled(source_id, enabled == "true")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/add")
    def source_add(csrf_token: str = Form(""), url: str = Form(""), name: str = Form("")):
        require_csrf(csrf_token)
        if not _safe_url(url):
            raise HTTPException(status_code=400, detail="Enter an HTTP or HTTPS dealer URL")
        try:
            db.add_source(url.strip(), name.strip())
        except (ValueError, FetchError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse("/sources", status_code=303)

    @app.post("/discoveries/{candidate_id}/decision")
    def discovery_decision(candidate_id: int, csrf_token: str = Form(""), decision: str = Form("")):
        require_csrf(csrf_token)
        if decision not in {"accepted", "dismissed"}:
            raise HTTPException(status_code=400, detail="Invalid discovery decision")
        try:
            db.decide_candidate(candidate_id, decision)
        except ValueError:
            raise HTTPException(status_code=404, detail="Discovery not found") from None
        return RedirectResponse("/discoveries", status_code=303)

    @app.post("/settings")
    def settings_update(csrf_token: str = Form(""), timezone: str = Form(""), scan_time: str = Form("")):
        require_csrf(csrf_token)
        try:
            ZoneInfo(timezone)
            parsed_time = datetime.strptime(scan_time, "%H:%M")
            if parsed_time.strftime("%H:%M") != scan_time:
                raise ValueError
            db.update_settings({"timezone": timezone, "scan_time": scan_time})
        except (ValueError, KeyError):
            raise HTTPException(status_code=400, detail="Choose a valid time zone and 24-hour scan time") from None
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/web-search")
    def web_search_settings(csrf_token: str = Form(""), api_key: str = Form(""), action: str = Form("save")):
        require_csrf(csrf_token)
        try:
            if action == "save":
                save_api_key(db.path.parent, api_key.strip())
            elif action == "clear":
                clear_api_key(db.path.parent)
            else:
                raise ValueError("Choose save or clear for the web-search key")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse("/settings?saved=1#web-search", status_code=303)

    return app
