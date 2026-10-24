"""FastAPI app: routes, flash messages and local-only request checks.

Run with `python -m jobpilot` (binds to 127.0.0.1 only).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import job_sources
from .applier import SIGNIN_URLS, BrowserManager, Contact
from .config import HOST, Settings, load_settings
from .db import APPLICATION_STATUSES, Database
from .job_sources import SearchQuery
from .job_sources.importer import ImportNeedsText, import_job
from .llm import LLM, LLMBackend, LLMError
from .matcher import default_preferences, passes_filters, score_jobs, split_list
from .resume_parser import SUPPORTED_EXTENSIONS, ResumeError, extract_text, normalize_resume, parse_resume
from .tailor import application_folder, tailor, write_application_files

log = logging.getLogger("jobpilot")
PKG_DIR = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------- flash messages

def flash(response: Response, kind: str, text: str) -> Response:
    """Queue a message for the next page view (stored in a short-lived cookie)."""
    messages = getattr(response, "_flashes", [])
    messages.append({"kind": kind, "text": text})
    response._flashes = messages
    response.raw_headers = [(k, v) for k, v in response.raw_headers
                            if not (k == b"set-cookie" and v.startswith(b"jp_flash="))]
    payload = base64.urlsafe_b64encode(json.dumps(messages[-6:]).encode()).decode()
    response.set_cookie("jp_flash", payload, httponly=True, samesite="strict")
    return response


def _read_flashes(request: Request) -> list[dict]:
    raw = request.cookies.get("jp_flash")
    if not raw:
        return []
    try:
        return json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:
        return []


def redirect(url: str, kind: str | None = None, text: str | None = None) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if kind and text:
        flash(resp, kind, text)
    return resp


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- app factory

def create_app(settings: Settings | None = None, llm: LLMBackend | None = None,
               browser: BrowserManager | None = None, sources: dict | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    llm = llm or LLM(settings)
    browser = browser or BrowserManager(settings.browser_profile_dir, executable_path=settings.chromium_path)
    browser.on_result = lambda app_id, state, detail: db.set_application_status(app_id, state, detail)

    app = FastAPI(title="JobPilot", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.db, app.state.llm, app.state.browser = settings, db, llm, browser
    app.mount("/static", StaticFiles(directory=PKG_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PKG_DIR / "templates")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Only serve this machine, and refuse cross-site form posts (so a web page you visit
        can't drive JobPilot through your browser)."""
        client = request.client.host if request.client else ""
        host = (request.headers.get("host") or "").split(":")[0]
        allowed_hosts = {"127.0.0.1", "localhost", "testserver"}
        if client not in {"127.0.0.1", "::1", "localhost", "testclient"} or host not in allowed_hosts:
            return Response("JobPilot only accepts requests from this computer.", status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if origin and (urlparse(origin).hostname or "") not in allowed_hosts:
                return Response("Cross-site request refused.", status_code=403)
        response = await call_next(request)
        if request.cookies.get("jp_flash") and response.status_code == 200 \
                and response.headers.get("content-type", "").startswith("text/html"):
            response.delete_cookie("jp_flash")
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        base = {"request": request, "flashes": _read_flashes(request), "settings": settings,
                "llm_ready": settings.llm_configured, "has_resume": bool((db.get_profile() or {}).get("resume")),
                "browser": browser.status.as_dict()}
        base.update(ctx)
        return templates.TemplateResponse(request, name, base)

    def require_resume() -> dict | None:
        profile = db.get_profile()
        return profile["resume"] if profile and profile.get("resume") else None

    def score_pending() -> tuple[int, str | None]:
        resume = require_resume()
        pending = db.unscored_jobs()
        if not pending:
            return 0, None
        if not resume:
            return 0, "Upload your resume in Setup so JobPilot can score these jobs."
        prefs = db.get_preferences()
        results = score_jobs(llm, pending, resume, prefs)
        for r in results:
            db.set_job_score(r["job_id"], r["score"], r["reason"], r["gaps"])
        return len(results), None

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return render(request, "dashboard.html", stats=db.stats(), recent=db.list_applications(limit=8),
                      top_jobs=db.list_jobs("open")[:5])

    # ------------------------------------------------------------ setup
    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        profile = db.get_profile() or {}
        resume = profile.get("resume")
        return render(request, "setup.html", profile=profile, resume=resume,
                      resume_json=json.dumps(resume, indent=2, ensure_ascii=False) if resume else "",
                      prefs=db.get_preferences(), sources=job_sources.available_sources(settings))

    @app.post("/setup/resume")
    async def upload_resume(request: Request, resume: UploadFile = File(...)):
        filename = Path(resume.filename or "").name
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return redirect("/setup", "error", "Upload a PDF, DOCX or TXT resume.")
        data = await resume.read()
        if not data:
            return redirect("/setup", "error", "That file is empty.")
        if len(data) > MAX_UPLOAD_BYTES:
            return redirect("/setup", "error", "That file is larger than 10 MB.")
        dest = settings.uploads_dir / f"resume{ext}"
        for old in settings.uploads_dir.glob("resume.*"):
            old.unlink()
        dest.write_bytes(data)
        try:
            text = extract_text(dest)
        except ResumeError as e:
            return redirect("/setup", "error", f"Couldn't read your resume: {e}")
        except Exception as e:
            log.warning("resume extraction failed: %s", e)
            return redirect("/setup", "error", "Couldn't read your resume. The file may be damaged or "
                                               "password-protected. Try exporting it again as PDF or DOCX.")
        try:
            parsed = parse_resume(llm, text)
        except LLMError as e:
            db.save_profile(filename=filename, path=str(dest), text=text, resume=None)
            return redirect("/setup", "error", f"Your resume text was extracted, but parsing failed: {e}")
        db.save_profile(filename=filename, path=str(dest), text=text, resume=parsed)
        db.clear_scores()
        return redirect("/setup", "success", f"Parsed {filename}. Check the result below and fix anything "
                                             "the parser got wrong.")

    @app.post("/setup/resume-json")
    def save_resume_json(resume_json: str = Form(...)):
        try:
            data = json.loads(resume_json)
        except json.JSONDecodeError as e:
            return redirect("/setup#parsed", "error", f"That isn't valid JSON: {e}")
        if not db.get_profile():
            return redirect("/setup", "error", "Upload a resume first.")
        db.update_resume_json(normalize_resume(data))
        db.clear_scores()
        return redirect("/setup#parsed", "success", "Saved your corrected resume data.")

    @app.post("/setup/preferences")
    def save_preferences(target_titles: str = Form(""), locations: str = Form(""),
                         remote_only: str | None = Form(None), min_salary: str = Form(""),
                         excluded_keywords: str = Form(""), email: str = Form(""), phone: str = Form(""),
                         sources: list[str] = Form([])):
        prefs = default_preferences()
        salary = "".join(ch for ch in min_salary if ch.isdigit())
        prefs.update({
            "target_titles": split_list(target_titles), "locations": split_list(locations),
            "remote_only": remote_only is not None, "min_salary": int(salary) if salary else None,
            "excluded_keywords": split_list(excluded_keywords),
            "email": email.strip(), "phone": phone.strip(),
            "sources": [s for s in sources if s in job_sources.REGISTRY],
        })
        db.save_preferences(prefs)
        db.clear_scores()
        return redirect("/setup#preferences", "success", "Preferences saved.")

    # ------------------------------------------------------------ jobs
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, tab: str = "open"):
        tab = tab if tab in ("open", "applied", "skipped") else "open"
        counts = {t: len(db.list_jobs(t)) for t in ("open", "applied", "skipped")}
        return render(request, "jobs.html", jobs=db.list_jobs(tab), tab=tab, counts=counts,
                      prefs=db.get_preferences(), sources=job_sources.available_sources(settings))

    @app.post("/jobs/find")
    def find_jobs():
        prefs = db.get_preferences()
        titles = prefs.get("target_titles") or []
        if not titles:
            return redirect("/setup#preferences", "error", "Add at least one target title first.")
        queries = [SearchQuery(keywords=t, location=(prefs.get("locations") or [""])[0],
                               remote_only=prefs.get("remote_only", False), min_salary=prefs.get("min_salary"),
                               limit=prefs.get("results_per_query", 20)) for t in titles[:5]]
        found, errors = job_sources.run_search(settings, db, prefs.get("sources") or ["remotive"], queries,
                                               sources=sources, polite_wait=sources is None)
        new = filtered = 0
        for posting in found:
            row = posting.to_row()
            ok, _ = passes_filters(row, prefs)
            if not ok:
                filtered += 1
                continue
            _, created = db.upsert_job(row)
            new += created
        msg = f"Found {len(found)} postings: {new} new, {filtered} hidden by your filters."
        resp = redirect("/jobs")
        try:
            scored, warn = score_pending()
            msg += f" Scored {scored}." if scored else ""
            if warn:
                flash(resp, "warning", warn)
        except LLMError as e:
            flash(resp, "error", f"Jobs were saved but couldn't be scored: {e}")
        flash(resp, "success", msg)
        for err in errors:
            flash(resp, "warning", err)
        return resp

    @app.post("/jobs/import")
    def import_posting(url: str = Form(""), text: str = Form("")):
        try:
            posting = import_job(llm, url=url, text=text)
        except (ImportNeedsText, ValueError) as e:
            return redirect("/jobs#import", "error", str(e))
        except LLMError as e:
            return redirect("/jobs#import", "error", f"Import failed: {e}")
        row = posting.to_row()
        job_id, created = db.upsert_job(row)
        resp = redirect(f"/jobs/{job_id}")
        ok, why = passes_filters(row, db.get_preferences())
        if not ok:
            flash(resp, "warning", f"Imported anyway, but note: this job {why}.")
        try:
            _, warn = score_pending()
            if warn:
                flash(resp, "warning", warn)
        except LLMError as e:
            flash(resp, "error", f"Imported, but scoring failed: {e}")
        flash(resp, "success", f"Imported '{posting.title}'" + ("" if created else " (already in your list)") + ".")
        return resp

    @app.post("/jobs/rescore")
    def rescore():
        db.clear_scores()
        try:
            scored, warn = score_pending()
        except LLMError as e:
            return redirect("/jobs", "error", f"Scoring failed: {e}")
        return redirect("/jobs", "warning" if warn else "success", warn or f"Re-scored {scored} jobs.")

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int):
        job = db.get_job(job_id)
        if not job:
            return redirect("/jobs", "error", "That job no longer exists.")
        apps = [a for a in db.list_applications() if a["job_id"] == job_id]
        return render(request, "job_detail.html", job=job, applications=apps)

    @app.post("/jobs/{job_id}/skip")
    def skip_job(job_id: int, back: str = Form("/jobs")):
        db.set_job_status(job_id, "skipped")
        return redirect(back if back.startswith("/") else "/jobs", "success", "Skipped.")

    @app.post("/jobs/{job_id}/unskip")
    def unskip_job(job_id: int):
        db.set_job_status(job_id, "open")
        return redirect("/jobs?tab=skipped", "success", "Moved back to Open.")

    @app.post("/jobs/{job_id}/tailor")
    def tailor_job(job_id: int):
        job = db.get_job(job_id)
        resume = require_resume()
        if not job:
            return redirect("/jobs", "error", "That job no longer exists.")
        if not resume:
            return redirect("/setup", "error", "Upload your resume before tailoring.")
        if len(job.get("description") or "") < 200:
            note = "This posting has only a short description; import the full text for better tailoring."
        else:
            note = None
        try:
            result = tailor(llm, resume, job, effort=settings.effort_tailor)
        except LLMError as e:
            return redirect(f"/jobs/{job_id}", "error", f"Tailoring failed: {e}")
        folder = application_folder(settings.applications_dir, job)
        paths = write_application_files(folder, job, result)
        app_id = db.create_application(
            job_id=job_id, folder=str(folder), resume_path=str(paths["resume"]),
            cover_letter_path=str(paths["cover_letter"]) if paths["cover_letter"] else None,
            job_description_path=str(paths["job_description"]), resume=result["resume"],
            cover_letter_text=result["cover_letter"], changes=result["changes"], gaps=result["gaps"])
        corrections = sum(1 for c in result["changes"] if c["kind"] == "correction")
        resp = redirect(f"/applications/{app_id}")
        if note:
            flash(resp, "warning", note)
        flash(resp, "success", "Tailored resume and cover letter are ready."
              + (f" The truthfulness guard made {corrections} correction(s); see the change list." if corrections else ""))
        return resp

    # ------------------------------------------------------------ applications
    @app.get("/applications", response_class=HTMLResponse)
    def applications_page(request: Request, status: str = ""):
        status = status if status in APPLICATION_STATUSES else ""
        return render(request, "applications.html", applications=db.list_applications(status or None),
                      status=status, statuses=APPLICATION_STATUSES)

    @app.get("/applications/{app_id}", response_class=HTMLResponse)
    def application_detail(request: Request, app_id: int):
        app_row = db.get_application(app_id)
        if not app_row:
            return redirect("/applications", "error", "That application doesn't exist.")
        files = []
        for kind, label in (("resume", "resume.docx"), ("cover_letter", "cover_letter.docx"),
                            ("job_description", "job_description.txt")):
            p = app_row.get(f"{kind}_path")
            if p and Path(p).exists():
                files.append({"kind": kind, "name": label, "bytes": Path(p).stat().st_size,
                              "sha256": app_row["file_hashes"].get(kind)})
        return render(request, "application_detail.html", app=app_row, files=files, statuses=APPLICATION_STATUSES)

    @app.get("/applications/{app_id}/files/{kind}")
    def download_file(app_id: int, kind: str):
        app_row = db.get_application(app_id)
        if not app_row or kind not in ("resume", "cover_letter", "job_description"):
            return Response("Not found", status_code=404)
        path = app_row.get(f"{kind}_path")
        if not path or not Path(path).exists():
            return Response("File missing", status_code=404)
        # Only serve files that live inside this application's folder.
        if Path(path).resolve().parent != Path(app_row["folder"]).resolve():
            return Response("Not found", status_code=404)
        company = "".join(c for c in (app_row.get("company") or "job") if c.isalnum() or c in " -")[:40].strip()
        download_name = f"{company} - {Path(path).name}" if company else Path(path).name
        return FileResponse(path, filename=download_name)

    @app.post("/applications/{app_id}/prefill")
    def prefill(app_id: int):
        app_row = db.get_application(app_id)
        if not app_row:
            return redirect("/applications", "error", "That application doesn't exist.")
        if app_row["status"] == "submitted":
            return redirect(f"/applications/{app_id}", "warning", "This application is already submitted.")
        if not app_row.get("url"):
            return redirect(f"/applications/{app_id}", "error",
                            "This job has no URL. Open the posting yourself and upload resume.docx.")
        prefs = db.get_preferences()
        contact_src = app_row["resume"].get("contact") or {}
        contact = Contact(name=app_row["resume"].get("name") or "",
                          email=prefs.get("email") or contact_src.get("email") or "",
                          phone=prefs.get("phone") or contact_src.get("phone") or "")
        try:
            browser.start_prefill(app_id, app_row["url"], Path(app_row["resume_path"]),
                                  Path(app_row["cover_letter_path"]) if app_row.get("cover_letter_path") else None,
                                  contact)
        except RuntimeError as e:
            return redirect(f"/applications/{app_id}", "error", str(e))
        db.set_application_status(app_id, "in_progress", "Opening the posting in the browser")
        return redirect(f"/applications/{app_id}", "success",
                        "Opening the posting in the browser window. JobPilot will upload and pre-fill, then stop. "
                        "You review and click Submit yourself.")

    @app.post("/applications/{app_id}/submitted")
    def mark_submitted(app_id: int):
        app_row = db.get_application(app_id)
        if not app_row:
            return redirect("/applications", "error", "That application doesn't exist.")
        hashes = {}
        for kind in ("resume", "cover_letter", "job_description"):
            p = app_row.get(f"{kind}_path")
            if p and Path(p).exists():
                hashes[kind] = sha256_file(Path(p))
                os.chmod(p, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # lock the files you sent
        db.mark_submitted(app_id, hashes)
        return redirect(f"/applications/{app_id}", "success",
                        "Recorded as submitted. The exact files are kept read-only in this application's folder.")

    @app.post("/applications/{app_id}/status")
    def update_status(app_id: int, status: str = Form(...)):
        if status not in APPLICATION_STATUSES:
            return redirect(f"/applications/{app_id}", "error", "Unknown status.")
        if status == "submitted":
            return mark_submitted(app_id)
        db.set_application_status(app_id, status, "Changed by you")
        return redirect(f"/applications/{app_id}", "success", f"Status set to {status.replace('_', ' ')}.")

    @app.post("/applications/{app_id}/notes")
    def update_notes(app_id: int, notes: str = Form("")):
        db.set_application_notes(app_id, notes)
        return redirect(f"/applications/{app_id}#notes", "success", "Notes saved.")

    # ------------------------------------------------------------ browser / sign-in
    @app.get("/signin", response_class=HTMLResponse)
    def signin_page(request: Request):
        return render(request, "signin.html", boards=SIGNIN_URLS)

    @app.post("/signin/{board}")
    def signin(board: str):
        if board not in SIGNIN_URLS:
            return redirect("/signin", "error", "Unknown job board.")
        browser.open_signin(board)
        return redirect("/signin", "success", f"Opening {SIGNIN_URLS[board][0]} in the browser window. "
                                              "Sign in there yourself, then close the window.")

    @app.get("/browser/status")
    def browser_status():
        return JSONResponse(browser.status.as_dict())

    return app


def run() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    print(f"JobPilot running at http://{HOST}:{settings.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(settings), host=HOST, port=settings.port, log_level="warning")
