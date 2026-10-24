"""Apply-assist safety: never clicks submit, never touches password fields."""

from __future__ import annotations

import os

import pytest

from jobpilot.applier import Contact, classify_field, is_submit_like, run_prefill


def test_submit_detection():
    for label in ("Submit", "Submit application", "Send application", "Review your application", "Finish"):
        assert is_submit_like(label)
    assert is_submit_like("Apply", "submit")
    for label in ("Easy Apply", "Apply now", "Apply"):
        assert not is_submit_like(label)


def test_field_classification():
    assert classify_field("email Email address", "email") == "email"
    assert classify_field("phoneNumber Mobile phone", "tel") == "phone"
    assert classify_field("first_name First name", "text") == "first_name"
    assert classify_field("Company name", "text") is None
    assert classify_field("password", "password") is None
    assert classify_field("Email verification code", "text") is None
    assert classify_field("username", "text") is None


FORM_HTML = """<!doctype html><html><body>
<h1>Data Analyst</h1>
<button id="apply" onclick="document.getElementById('f').style.display='block'">Easy Apply</button>
<form id="f" style="display:none" onsubmit="window.SUBMITTED = true; return false;">
  <label for="fn">First name</label><input id="fn" name="first_name">
  <label for="ln">Last name</label><input id="ln" name="last_name">
  <label for="em">Email</label><input id="em" type="email" name="email">
  <label for="ph">Phone</label><input id="ph" type="tel" name="phone">
  <label for="pw">Password</label><input id="pw" type="password" name="password">
  <label for="cv">Resume / CV</label><input id="cv" type="file" name="resume">
  <label for="q">Why do you want this job?</label><input id="q" name="why">
  <button type="submit" id="submit">Submit application</button>
</form></body></html>"""


@pytest.fixture
def browser_context(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as e:
            # Fall back to a system Chromium if one is configured (e.g. in CI images).
            exe = os.getenv("JOBPILOT_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
            try:
                browser = pw.chromium.launch(headless=True, executable_path=exe)
            except Exception:
                pytest.skip(f"Chromium not available (run `playwright install chromium`): {e}")
        ctx = browser.new_context()
        yield ctx
        browser.close()


def test_prefill_stops_before_submit(browser_context, tmp_path):
    page_path = tmp_path / "job.html"
    page_path.write_text(FORM_HTML)
    resume = tmp_path / "resume.docx"
    resume.write_bytes(b"PK fake docx")
    log: list[str] = []
    summary = run_prefill(browser_context, page_path.as_uri(), resume, None,
                          Contact(name="Jordan Rivera", email="j@example.com", phone="555-0100"), log.append)
    page = browser_context.pages[-1]
    assert summary["clicked_apply"] and summary["uploaded_resume"]
    assert page.input_value("#fn") == "Jordan" and page.input_value("#ln") == "Rivera"
    assert page.input_value("#em") == "j@example.com" and page.input_value("#ph") == "555-0100"
    assert page.input_value("#pw") == ""          # never touched
    assert page.input_value("#q") == ""           # screening questions are left to you
    assert page.evaluate("document.getElementById('cv').files[0].name") == "resume.docx"
    assert page.evaluate("window.SUBMITTED") is None  # Submit was never clicked
    assert "Stopped." in log[-1]


TRAP_HTML = """<!doctype html><html><body>
<form onsubmit="window.SUBMITTED = true; return false;">
  <label for="em">Email</label><input id="em" type="email" name="email">
  <button>Apply</button>
</form></body></html>"""


def test_apply_button_inside_form_is_not_clicked(browser_context, tmp_path):
    page_path = tmp_path / "trap.html"
    page_path.write_text(TRAP_HTML)
    resume = tmp_path / "resume.docx"
    resume.write_bytes(b"PK")
    log: list[str] = []
    summary = run_prefill(browser_context, page_path.as_uri(), resume, None, Contact(email="j@example.com"), log.append)
    page = browser_context.pages[-1]
    assert not summary["clicked_apply"]
    assert page.input_value("#em") == "j@example.com"
    assert page.evaluate("window.SUBMITTED") is None
    assert any("never clicks" in line for line in log)


def test_browser_manager_prefill_thread(tmp_path):
    """The background manager drives a persistent profile and reports back without submitting."""
    pytest.importorskip("playwright.sync_api")
    import time

    from jobpilot.applier import BrowserManager
    page_path = tmp_path / "job.html"
    page_path.write_text(FORM_HTML)
    resume = tmp_path / "resume.docx"
    resume.write_bytes(b"PK")
    exe = os.getenv("JOBPILOT_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
    mgr = BrowserManager(tmp_path / "profile", headless=True, executable_path=exe if os.path.exists(exe) else "")
    results = []
    mgr.on_result = lambda app_id, state, detail: results.append((app_id, state, detail))
    mgr.start_prefill(7, page_path.as_uri(), resume, None, Contact(name="A B", email="a@b.co", phone="1"))
    deadline = time.time() + 40
    while not results and time.time() < deadline:
        time.sleep(0.2)
    if results and results[0][1] == "failed" and "isn't installed" in results[0][2]:
        pytest.skip("Chromium not installed")
    assert results and results[0][0] == 7 and results[0][1] == "in_progress", results
    assert mgr.status.state == "waiting_for_you"
    assert (tmp_path / "profile").exists()
