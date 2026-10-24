"""Browser-assisted sign-in and apply, using a visible Playwright Chromium window.

Hard rules this module follows:
- It never sees, asks for, stores or logs job-board passwords. "Sign in" just
  opens the board's login page in a persistent profile (data/browser_profile);
  you type your credentials into the site yourself, and the site's own
  session cookies stay in that profile folder on your computer.
- It NEVER clicks a final Submit/Send button. The only click it performs is on
  the posting's Apply / Easy Apply button, and `safe_click` refuses anything
  that looks like a submit control. After uploading the resume and filling
  name, email and phone, it stops. You review, answer screening questions,
  solve any CAPTCHA and submit yourself.
- It does not try to evade bot detection, solve CAPTCHAs, or retry on blocks.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SIGNIN_URLS = {
    "linkedin": ("LinkedIn", "https://www.linkedin.com/login"),
    "indeed": ("Indeed", "https://secure.indeed.com/auth"),
    "dice": ("Dice", "https://www.dice.com/dashboard/login"),
}

APPLY_BUTTON = re.compile(
    r"^\s*(easy apply|apply now|apply for this (job|position|role)|apply on company (site|website)|"
    r"apply to this job|i'?m interested|apply)\b", re.I)
SUBMIT_LIKE = re.compile(
    r"\b(submit|send|finish|complete|confirm|review( your)? application|done)\b", re.I)
SENSITIVE_FIELD = re.compile(r"pass(word|code)?|pwd|otp|one[- ]time|captcha|verification code|ssn|social security",
                             re.I)


def is_submit_like(text: str, input_type: str = "") -> bool:
    """True for any control that could send the application. Those are never clicked."""
    return input_type.lower() == "submit" or bool(SUBMIT_LIKE.search(text or ""))


def classify_field(desc: str, input_type: str) -> str | None:
    """Map an input to the contact field it wants, or None. Password-like fields are never touched."""
    d = (desc or "").lower()
    t = (input_type or "text").lower()
    if t == "password" or SENSITIVE_FIELD.search(d):
        return None
    if t == "email" or re.search(r"e-?mail", d):
        return "email"
    if t == "tel" or re.search(r"phone|mobile|tel\b|telephone|cell", d):
        return "phone"
    if re.search(r"first[\s_-]?name|given[\s_-]?name|fname|firstname", d):
        return "first_name"
    if re.search(r"last[\s_-]?name|family[\s_-]?name|surname|lname|lastname", d):
        return "last_name"
    if re.search(r"full[\s_-]?name|your name|^name\b|\bname$|legal name|candidate name", d) \
            and not re.search(r"company|employer|school|user ?name|reference|manager", d):
        return "full_name"
    return None


CANDIDATE_INPUTS = ("input:not([type=hidden]):not([type=password]):not([type=file]):not([type=checkbox])"
                    ":not([type=radio]):not([type=submit]):not([type=button]):not([type=image])"
                    ":not([type=reset])")

_DESCRIBE_JS = """(sel) => Array.from(document.querySelectorAll(sel)).map((el, i) => {
  const labels = el.labels ? Array.from(el.labels).map(l => l.innerText) : [];
  const lb = el.getAttribute('aria-labelledby');
  if (lb) lb.split(/\\s+/).forEach(id => { const n = document.getElementById(id); if (n) labels.push(n.innerText); });
  const desc = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.autocomplete, ...labels]
    .filter(Boolean).join(' ');
  const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  return {i, type: el.type || 'text', desc, empty: !el.value, visible, disabled: el.disabled || el.readOnly};
})"""

_FILE_JS = """() => Array.from(document.querySelectorAll('input[type=file]')).map((el, i) => {
  const labels = el.labels ? Array.from(el.labels).map(l => l.innerText) : [];
  return {i, desc: [el.name, el.id, el.getAttribute('aria-label'), el.accept, ...labels].filter(Boolean).join(' ')};
})"""


@dataclass
class Contact:
    name: str = ""
    email: str = ""
    phone: str = ""

    def value_for(self, kind: str) -> str:
        parts = self.name.split()
        return {
            "email": self.email, "phone": self.phone, "full_name": self.name,
            "first_name": parts[0] if parts else "",
            "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
        }.get(kind, "")


def safe_click(locator, note: Callable[[str], None]) -> bool:
    """Click an Apply-type control. Refuses anything that looks like a submit button, and any button
    that belongs to a form (clicking it could send that form)."""
    text = (locator.inner_text(timeout=2000) or "").strip()
    kind, value, aria, in_form = locator.evaluate(
        "el => [(el.getAttribute('type') || '').toLowerCase(), el.value || '', "
        "el.getAttribute('aria-label') || '', !!(el.form || el.closest('form'))]")
    label = " ".join(x for x in (text, aria, value) if x).strip()
    submits_form = in_form and kind in ("", "submit") and locator.evaluate("el => el.tagName") in ("BUTTON", "INPUT")
    if is_submit_like(label, kind) or submits_form or not APPLY_BUTTON.search(text or aria or value):
        note(f"Didn't click '{label[:60]}' because JobPilot never clicks buttons that could submit a form.")
        return False
    locator.click(timeout=5000)
    return True


def _find_apply_button(page):
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=APPLY_BUTTON)
        for i in range(min(loc.count(), 6)):
            cand = loc.nth(i)
            try:
                if cand.is_visible():
                    return cand
            except Exception:
                continue
    return None


def fill_application(page, resume_path: Path, cover_path: Path | None, contact: Contact,
                     note: Callable[[str], None]) -> dict:
    """Upload documents and fill contact fields on every frame of `page`. Never submits."""
    summary = {"uploaded_resume": False, "uploaded_cover": False, "filled": []}
    for frame in page.frames:
        try:
            files = frame.evaluate(_FILE_JS)
        except Exception:
            continue
        file_inputs = frame.locator("input[type=file]")
        resume_slot = next((f for f in files if re.search(r"resume|cv\b|curriculum", f["desc"], re.I)), None)
        cover_slot = next((f for f in files if re.search(r"cover", f["desc"], re.I)), None)
        if resume_slot is None and files and files[0] is not cover_slot:
            resume_slot = files[0]
        if resume_slot is not None and not summary["uploaded_resume"]:
            file_inputs.nth(resume_slot["i"]).set_input_files(str(resume_path))
            summary["uploaded_resume"] = True
            note("Uploaded your tailored resume.")
        if cover_slot is not None and cover_path and cover_slot is not resume_slot and not summary["uploaded_cover"]:
            file_inputs.nth(cover_slot["i"]).set_input_files(str(cover_path))
            summary["uploaded_cover"] = True
            note("Uploaded your cover letter.")

        fields = frame.evaluate(_DESCRIBE_JS, CANDIDATE_INPUTS)
        inputs = frame.locator(CANDIDATE_INPUTS)
        for f in fields:
            if not f["visible"] or f["disabled"] or not f["empty"]:
                continue
            kind = classify_field(f["desc"], f["type"])
            value = contact.value_for(kind) if kind else ""
            if not value or kind in summary["filled"]:
                continue
            inputs.nth(f["i"]).fill(value)  # fill() types the value; it never presses Enter
            summary["filled"].append(kind)
            note(f"Filled {kind.replace('_', ' ')}.")
    return summary


def run_prefill(context, url: str, resume_path: Path, cover_path: Path | None, contact: Contact,
                note: Callable[[str], None]) -> dict:
    """Open the posting, click Apply/Easy Apply if present, upload and fill, then stop."""
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    note("Opened the job posting.")
    button = _find_apply_button(page)
    clicked = False
    if button is not None:
        before = list(context.pages)
        clicked = safe_click(button, note)
        if clicked:
            note("Clicked the Apply button.")
            page.wait_for_timeout(2500)
            new_pages = [p for p in context.pages if p not in before]
            if new_pages:  # Apply opened the employer's site in a new tab
                page = new_pages[-1]
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                note("Apply opened a new tab; continuing there.")
    else:
        note("No Apply button found on the page; filling whatever form is visible.")
    page.bring_to_front()
    summary = fill_application(page, resume_path, cover_path, contact, note)
    if not summary["uploaded_resume"]:
        note("Couldn't find a resume upload field on this step. Upload resume.docx from the app yourself.")
    note("Stopped. Review everything, answer any questions, and click Submit yourself. "
         "Then press \"I submitted this application\" in JobPilot.")
    summary["clicked_apply"] = clicked
    return summary


@dataclass
class BrowserStatus:
    state: str = "closed"         # closed | open | working | waiting_for_you | error
    message: str = ""
    application_id: int | None = None
    log: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"state": self.state, "message": self.message, "application_id": self.application_id,
                "log": list(self.log[-40:])}


class BrowserManager:
    """Owns the single persistent Chromium context on a dedicated thread (Playwright's sync API is
    thread-bound). The window stays open until you close it."""

    def __init__(self, profile_dir: Path, headless: bool = False, executable_path: str = ""):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.executable_path = executable_path
        self.status = BrowserStatus()
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.on_result: Callable[[int, str, str], None] | None = None  # (app_id, state, detail)

    def _note(self, msg: str) -> None:
        self.status.log.append(time.strftime("%H:%M:%S ") + msg)
        self.status.message = msg
        log.info("browser: %s", msg)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="jobpilot-browser", daemon=True)
                self._thread.start()

    def open_signin(self, board: str) -> None:
        if board not in SIGNIN_URLS:
            raise ValueError(f"Unknown board {board!r}")
        self._ensure_thread()
        self._q.put(("signin", board))

    def start_prefill(self, application_id: int, url: str, resume_path: Path, cover_path: Path | None,
                      contact: Contact) -> None:
        if self.status.state == "working":
            raise RuntimeError("The browser is busy with another application. Wait for it to finish.")
        self.status = BrowserStatus(state="working", application_id=application_id)
        self._ensure_thread()
        self._q.put(("prefill", (application_id, url, Path(resume_path), cover_path and Path(cover_path), contact)))

    # ---- thread side ----
    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.status.state = "error"
            self._note("Playwright isn't installed. Run: pip install playwright && playwright install chromium")
            return
        ctx = None
        try:
            with sync_playwright() as pw:
                while True:
                    try:
                        cmd, arg = self._q.get(timeout=0.25)
                    except queue.Empty:
                        cmd = None
                    if cmd:
                        try:
                            if ctx is None:
                                self.profile_dir.mkdir(parents=True, exist_ok=True)
                                ctx = pw.chromium.launch_persistent_context(
                                    str(self.profile_dir), headless=self.headless, no_viewport=True,
                                    executable_path=self.executable_path or None)
                            ctx = self._handle(ctx, cmd, arg)
                        except Exception as e:  # report and keep the thread alive
                            self._fail(e)
                    if ctx is not None:
                        ctx = self._pump(ctx)
                        if ctx is None and self.status.state != "error":
                            self.status.state = "closed"
                            self._note("Browser window closed. Your sign-in sessions are saved in the profile.")
        except Exception as e:
            self._fail(e)

    def _fail(self, e: Exception) -> None:
        msg = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            msg = "Chromium for Playwright isn't installed. Run: playwright install chromium"
        app_id = self.status.application_id
        self.status.state = "error"
        self._note(f"Browser error: {msg}")
        if app_id and self.on_result:
            self.on_result(app_id, "failed", msg)

    def _pump(self, ctx):
        """Let Playwright process events; detect when you close the window."""
        try:
            pages = ctx.pages
            if not pages:
                ctx.close()
                return None
            pages[0].wait_for_timeout(100)
            return ctx
        except Exception:
            return None

    def _handle(self, ctx, cmd: str, arg: Any):
        if cmd == "signin":
            label, url = SIGNIN_URLS[arg]
            page = ctx.pages[0] if ctx.pages and ctx.pages[0].url == "about:blank" else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.bring_to_front()
            self.status.state = "open"
            self._note(f"Opened {label} sign-in. Sign in yourself, then close the window.")
        elif cmd == "prefill":
            app_id, url, resume, cover, contact = arg
            blank = [p for p in ctx.pages if p.url == "about:blank"]
            summary = run_prefill(ctx, url, resume, cover, contact, self._note)
            for p in blank:
                try:
                    p.close()
                except Exception:
                    pass
            self.status.state = "waiting_for_you"
            if self.on_result:
                self.on_result(app_id, "in_progress",
                               f"Pre-filled: resume {'uploaded' if summary['uploaded_resume'] else 'not uploaded'}, "
                               f"fields {', '.join(summary['filled']) or 'none'}")
        return ctx
