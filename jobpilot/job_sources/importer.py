"""Import a single job posting from a pasted URL and/or pasted description text.

This is how postings from LinkedIn, Indeed, Dice (or anywhere else) get into
JobPilot. A URL is fetched once, with an honest user agent, only because you
asked for it. If the site blocks that or shows a login wall, JobPilot asks you
to paste the description text instead; it never retries or works around blocks.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

import httpx

from ..llm import LLMBackend
from .base import USER_AGENT, JobPosting, describe_network_error, html_to_text, looks_remote

_S = {"type": "string"}
_N = {"type": "number"}
IMPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_job_posting": {"type": "boolean"},
        "title": _S, "company": _S, "location": _S,
        "remote": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]},
        "salary_text": _S,
        "salary_min": _N, "salary_max": _N,
        "employment_type": _S,
        "description": _S,
    },
    "required": ["is_job_posting", "title", "company", "location", "remote", "salary_text",
                 "salary_min", "salary_max", "employment_type", "description"],
    "additionalProperties": False,
}

IMPORT_SYSTEM = """You extract a job posting's details from raw page text or pasted text.

Rules:
- Use only what the text says. Never guess a company, salary or location that is not stated.
- salary_text: the salary/compensation exactly as written, or "".
- salary_min / salary_max: annual amounts as plain numbers if a salary is stated (convert "120k" to 120000;
  if only one number, use it for both). Use 0 when no salary is stated.
- description: the full job description (responsibilities, requirements, benefits), cleaned of navigation,
  cookie banners and unrelated page text, but otherwise kept word for word.
- is_job_posting: false if the text is a login page, an error page, or not a job posting."""

BLOCKED_HOST_HINTS = ("linkedin.com", "indeed.com", "dice.com")
LOGIN_WALL_HINTS = ("sign in to", "log in to", "join now", "authwall", "captcha", "verify you are human",
                    "access denied", "enable javascript")


class ImportNeedsText(ValueError):
    """Raised when the URL can't be read and the user should paste the description."""


def fetch_page_text(url: str, client: httpx.Client | None = None) -> str:
    client = client or httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    host = urlparse(url).hostname or ""
    board = next((h for h in BLOCKED_HOST_HINTS if host.endswith(h)), None)
    try:
        resp = client.get(url)
    except httpx.HTTPError as e:
        raise ImportNeedsText(f"Couldn't load that URL: {describe_network_error(e, host or 'the site')} "
                              "You can paste the job description text instead.")
    if resp.status_code != 200:
        raise ImportNeedsText(f"The site answered with HTTP {resp.status_code}. "
                              "Open the posting in your browser and paste the description text instead.")
    text = html_to_text(resp.text)
    lowered = text.lower()
    if len(text) < 400 or (board and any(h in lowered[:3000] for h in LOGIN_WALL_HINTS)):
        raise ImportNeedsText("That page needs a sign-in or blocks automated reading. "
                              "Open it in your browser, copy the job description, and paste it here.")
    return text[:60000]


def import_job(llm: LLMBackend, *, url: str = "", text: str = "",
               client: httpx.Client | None = None) -> JobPosting:
    url, text = url.strip(), text.strip()
    if not url and not text:
        raise ValueError("Paste a job URL, the job description text, or both.")
    if url and not urlparse(url).scheme.startswith("http"):
        raise ValueError("The URL should start with http:// or https://")
    source_text = text or fetch_page_text(url, client)
    prompt = (f"Job URL: {url or '(none)'}\n\nPosting text:\n<posting>\n{source_text}\n</posting>\n\n"
              "Extract the job details.")
    data = llm.json(IMPORT_SYSTEM, prompt, IMPORT_SCHEMA)
    if not data.get("is_job_posting", True) or not data.get("title"):
        raise ImportNeedsText("That doesn't look like a job posting. Paste the job description text instead.")
    digest = hashlib.sha1((url or source_text).encode()).hexdigest()[:16]
    remote = {"remote": True, "onsite": False}.get(data.get("remote"))
    if data.get("remote") == "unknown":
        remote = looks_remote(data.get("location", ""), data.get("title", "")) or None
    return JobPosting(
        source="import",
        external_id=digest,
        title=data["title"].strip(),
        company=(data.get("company") or "").strip(),
        location=(data.get("location") or "").strip() + (" (hybrid)" if data.get("remote") == "hybrid" else ""),
        url=url,
        description=(data.get("description") or source_text).strip(),
        remote=remote,
        salary_min=data.get("salary_min") or None,
        salary_max=data.get("salary_max") or None,
        salary_text=data.get("salary_text") or "",
        employment_type=data.get("employment_type") or "",
    )
