"""Preferences, filters and match scoring.

Filters (excluded keywords, remote-only, salary floor) run first, in code.
Jobs that pass are scored 0-100 against the resume by the LLM, in small
batches, with a one-line reason and a list of gaps (requirements the resume
doesn't show).
"""

from __future__ import annotations

import json
import re

from .llm import LLMBackend

BATCH_SIZE = 8
DESCRIPTION_CHARS = 3500


def default_preferences() -> dict:
    return {
        "target_titles": [],
        "locations": [],
        "remote_only": False,
        "min_salary": None,
        "excluded_keywords": [],
        "email": "",
        "phone": "",
        "sources": ["remotive", "adzuna"],
        "results_per_query": 20,
    }


def split_list(value: str) -> list[str]:
    """Split a comma- or newline-separated form field into a clean list."""
    return [p.strip() for p in re.split(r"[,\n]", value or "") if p.strip()]


def _contains(haystack: str, needle: str) -> bool:
    needle = needle.lower().strip()
    return bool(needle) and re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                                      haystack.lower()) is not None


def is_remote(job: dict) -> bool:
    if job.get("remote") in (1, True):
        return True
    text = f"{job.get('location') or ''} {job.get('title') or ''}"
    return re.search(r"\bremote\b|\banywhere\b|work from home", text, re.I) is not None


def passes_filters(job: dict, prefs: dict) -> tuple[bool, str]:
    """Return (ok, reason). Runs before any scoring."""
    text = " ".join(str(job.get(k) or "") for k in ("title", "company", "description"))
    for kw in prefs.get("excluded_keywords") or []:
        if _contains(text, kw):
            return False, f"contains excluded keyword '{kw}'"
    if prefs.get("remote_only") and not is_remote(job):
        return False, "not remote"
    floor = prefs.get("min_salary")
    top = job.get("salary_max") or job.get("salary_min")
    if floor and top and float(top) < float(floor):
        return False, f"salary {int(top):,} is below your floor"
    return True, ""


_S = {"type": "string"}
SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer"},
                "score": {"type": "integer"},
                "reason": _S,
                "gaps": {"type": "array", "items": _S},
            },
            "required": ["job_id", "score", "reason", "gaps"],
            "additionalProperties": False,
        }},
    },
    "required": ["scores"],
    "additionalProperties": False,
}

SCORE_SYSTEM = """You score how well a candidate fits job postings, 0-100.

90-100: meets essentially every requirement at the right level. 70-89: strong fit with minor gaps.
50-69: plausible, with real gaps. Below 50: weak fit or wrong field/level.
Consider the candidate's target titles and locations too.

For each job return:
- reason: one short sentence (under 25 words) explaining the score, citing concrete resume evidence.
- gaps: up to 5 short items naming requirements in the posting that the resume does not show. Be honest;
  an empty list is fine if there are none.
Score every job you are given, using its job_id."""


def _resume_brief(resume: dict) -> str:
    lines = [f"Summary: {resume.get('summary') or ''}", "Skills: " + ", ".join(resume.get("skills") or [])]
    for e in resume.get("experience") or []:
        lines.append(f"- {e.get('title')} at {e.get('company')} ({e.get('start_date')} - {e.get('end_date')}): "
                     + " ".join(e.get("bullets") or [])[:900])
    for ed in resume.get("education") or []:
        lines.append(f"- Education: {ed.get('degree')} {ed.get('field')}, {ed.get('institution')}")
    for c in resume.get("certifications") or []:
        lines.append(f"- Certification: {c.get('name')}")
    for p in resume.get("projects") or []:
        lines.append(f"- Project: {p.get('name')}: {p.get('description')}")
    return "\n".join(lines)


def score_jobs(llm: LLMBackend, jobs: list[dict], resume: dict, prefs: dict) -> list[dict]:
    """Score jobs in batches. Returns [{job_id, score, reason, gaps}] for the jobs the model scored."""
    brief = _resume_brief(resume)
    wants = (f"Target titles: {', '.join(prefs.get('target_titles') or []) or 'not set'}. "
             f"Locations: {', '.join(prefs.get('locations') or []) or 'any'}. "
             f"Remote only: {'yes' if prefs.get('remote_only') else 'no'}.")
    ids = {j["id"] for j in jobs}
    results: list[dict] = []
    for i in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[i:i + BATCH_SIZE]
        postings = "\n\n".join(
            f"<job job_id=\"{j['id']}\">\nTitle: {j.get('title')}\nCompany: {j.get('company')}\n"
            f"Location: {j.get('location')}\nSalary: {j.get('salary_text') or 'not listed'}\n"
            f"{(j.get('description') or '')[:DESCRIPTION_CHARS]}\n</job>" for j in batch)
        user = (f"CANDIDATE\n<resume>\n{brief}\n</resume>\nPreferences: {wants}\n\nJOBS\n{postings}\n\n"
                "Score each job.")
        data = llm.json(SCORE_SYSTEM, user, SCORE_SCHEMA)
        for s in data.get("scores") or []:
            try:
                jid = int(s.get("job_id"))
            except (TypeError, ValueError):
                continue
            if jid not in ids:
                continue
            results.append({
                "job_id": jid,
                "score": max(0, min(100, int(s.get("score") or 0))),
                "reason": str(s.get("reason") or "").strip(),
                "gaps": [str(g).strip() for g in (s.get("gaps") or []) if str(g).strip()][:5],
            })
    return results


def resume_json_for_prompt(resume: dict) -> str:
    return json.dumps({k: v for k, v in resume.items() if not k.startswith("_")}, ensure_ascii=False)
