"""End-to-end flow through the web app with a stubbed LLM."""

from __future__ import annotations

import os
import re
from pathlib import Path

from docx import Document

from conftest import JOB_TEXT, RESUME_TEXT


def _app_id(resp) -> int:
    return int(re.search(r"/applications/(\d+)", resp.headers["location"]).group(1))


def test_full_flow(client, fake_llm, fake_browser, settings):
    # Upload resume
    r = client.post("/setup/resume", files={"resume": ("resume.txt", RESUME_TEXT.encode(), "text/plain")},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    page = client.get("/setup").text
    assert "Parsed resume.txt" in page and "Acme Corp" in page

    # Preferences (exclude keyword + remote only)
    client.post("/setup/preferences", data={"target_titles": "Data Analyst", "remote_only": "on",
                                            "excluded_keywords": "clearance", "email": "jordan.apply@example.com",
                                            "phone": "555-0100", "sources": ["remotive"]})

    # Find and rank: one Remotive job is filtered (clearance), the other is scored.
    r = client.post("/jobs/find", follow_redirects=True)
    assert "1 new, 1 hidden by your filters" in r.text
    assert "Initrode" in r.text and "Gov Co" not in r.text
    assert "Strong SQL and Tableau match." in r.text

    # Running it again dedupes on (source, external_id)
    r = client.post("/jobs/find", follow_redirects=True)
    assert "0 new" in r.text

    # Import pasted text
    r = client.post("/jobs/import", data={"url": "https://www.linkedin.com/jobs/view/123", "text": JOB_TEXT}, follow_redirects=False)
    assert r.status_code == 303
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/jobs/{job_id}").text
    assert "Globex" in page and "82" in page

    # Tailor
    r = client.post(f"/jobs/{job_id}/tailor", follow_redirects=False)
    app_id = _app_id(r)
    detail = client.get(f"/applications/{app_id}").text
    assert "Guard correction" in detail and "prepared" in detail

    app = client.app.state.db.get_application(app_id)
    folder = Path(app["folder"])
    assert folder.parent == settings.applications_dir
    assert re.match(r"\d{8}-\d{6}_Globex_Senior-Data-Analyst", folder.name)
    for name in ("resume.docx", "cover_letter.docx", "job_description.txt"):
        assert (folder / name).exists()
    assert "Snowflake experience is a plus" in (folder / "job_description.txt").read_text()
    assert app["gaps"] == ["dbt", "Snowflake"]

    # Downloads
    r = client.get(f"/applications/{app_id}/files/resume")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert r.content == (folder / "resume.docx").read_bytes()
    assert client.get(f"/applications/{app_id}/files/cover_letter").status_code == 200
    assert client.get(f"/applications/{app_id}/files/nope").status_code == 404

    # Pre-fill hands the exact files and contact info to the browser
    r = client.post(f"/applications/{app_id}/prefill", follow_redirects=True)
    assert "in progress" in r.text
    (_, url, resume_path, cover_path, contact) = fake_browser.prefills[-1]
    assert Path(resume_path) == folder / "resume.docx"
    assert contact.email == "jordan.apply@example.com" and contact.name == "Jordan Rivera"

    # Mark submitted: hashes recorded, files locked read-only, job moves to Applied tab
    r = client.post(f"/applications/{app_id}/submitted", follow_redirects=True)
    assert "Recorded as submitted" in r.text
    app = client.app.state.db.get_application(app_id)
    assert app["status"] == "submitted" and app["submitted_at"]
    assert len(app["file_hashes"]["resume"]) == 64
    assert not os.access(folder / "resume.docx", os.W_OK) or os.geteuid() == 0
    assert "Globex" in client.get("/jobs?tab=applied").text
    assert "Globex" not in client.get("/jobs?tab=open").text

    # Status updates and notes
    client.post(f"/applications/{app_id}/status", data={"status": "failed"})
    assert client.app.state.db.get_application(app_id)["status"] == "failed"
    client.post(f"/applications/{app_id}/notes", data={"notes": "Recruiter: Sam"})
    assert "Recruiter: Sam" in client.get(f"/applications/{app_id}").text
    assert client.post(f"/applications/{app_id}/status", data={"status": "bogus"},
                       follow_redirects=True).status_code == 200
    assert client.app.state.db.get_application(app_id)["status"] == "failed"

    # Dashboard counts
    dash = client.get("/").text
    assert "Open jobs" in dash and "Submitted" in dash


def test_skip_and_restore(client):
    client.post("/setup/resume", files={"resume": ("resume.txt", RESUME_TEXT.encode(), "text/plain")})
    client.post("/setup/preferences", data={"target_titles": "Data Analyst", "sources": ["remotive"]})
    client.post("/jobs/find")
    job = client.app.state.db.list_jobs("open")[0]
    client.post(f"/jobs/{job['id']}/skip", data={"back": "/jobs"})
    assert job["title"] in client.get("/jobs?tab=skipped").text
    assert all(j["id"] != job["id"] for j in client.app.state.db.list_jobs("open"))
    client.post(f"/jobs/{job['id']}/unskip")
    assert any(j["id"] == job["id"] for j in client.app.state.db.list_jobs("open"))


def test_docx_tables_and_bad_upload(client):
    r = client.post("/setup/resume", files={"resume": ("resume.png", b"x", "image/png")}, follow_redirects=True)
    assert "Upload a PDF, DOCX or TXT resume." in r.text
    r = client.post("/setup/resume", files={"resume": ("resume.pdf", b"not a pdf", "application/pdf")},
                    follow_redirects=True)
    assert "Couldn&#39;t read your resume" in r.text


def test_missing_api_key_banner(settings, monkeypatch, fake_browser):
    from fastapi.testclient import TestClient

    from jobpilot import main as m
    settings.anthropic_api_key = ""
    with TestClient(m.create_app(settings, browser=fake_browser)) as c:
        assert "ANTHROPIC_API_KEY is missing" in c.get("/").text
        r = c.post("/setup/resume", files={"resume": ("r.txt", RESUME_TEXT.encode(), "text/plain")},
                   follow_redirects=True)
        assert "ANTHROPIC_API_KEY is not set" in r.text


def test_rejects_remote_and_cross_site_requests(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    r = client.post("/jobs/find", headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_signin_opens_browser_only(client, fake_browser):
    r = client.post("/signin/linkedin", follow_redirects=True)
    assert fake_browser.signins == ["linkedin"]
    assert "password" not in r.text.lower().replace("never asks for, sees, stores, logs or sends your passwords", "")
