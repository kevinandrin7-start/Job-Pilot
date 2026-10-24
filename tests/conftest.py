"""Shared fixtures. Every test runs offline: the LLM is a stub (monkeypatched into the app),
job-source HTTP goes through httpx.MockTransport, and the browser is a recording fake."""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobpilot import main as main_module  # noqa: E402
from jobpilot.config import Settings  # noqa: E402
from jobpilot.job_sources.remotive import RemotiveSource  # noqa: E402

RESUME_TEXT = """Jordan Rivera
jordan@example.com | (555) 010-2000 | Austin, TX

Summary
Data analyst with 5 years of experience turning messy data into decisions.

Skills
SQL, Python, Tableau, Excel, A/B testing

Experience
Data Analyst, Acme Corp, Austin, TX, Jan 2021 - Present
- Built 12 Tableau dashboards used by 300 sales staff
- Automated weekly reporting in Python, saving 6 hours per week
Junior Analyst, Beta LLC, Dallas, TX, Jun 2019 - Dec 2020
- Cleaned and joined CRM data in SQL for quarterly reviews

Education
B.S. Statistics, University of Texas, 2019

Certifications
Tableau Desktop Specialist, Tableau, 2022
"""

PARSED_RESUME = {
    "name": "Jordan Rivera",
    "contact": {"email": "jordan@example.com", "phone": "(555) 010-2000", "location": "Austin, TX", "links": []},
    "summary": "Data analyst with 5 years of experience turning messy data into decisions.",
    "skills": ["SQL", "Python", "Tableau", "Excel", "A/B testing"],
    "experience": [
        {"title": "Data Analyst", "company": "Acme Corp", "location": "Austin, TX", "start_date": "Jan 2021",
         "end_date": "Present", "bullets": ["Built 12 Tableau dashboards used by 300 sales staff",
                                            "Automated weekly reporting in Python, saving 6 hours per week"]},
        {"title": "Junior Analyst", "company": "Beta LLC", "location": "Dallas, TX", "start_date": "Jun 2019",
         "end_date": "Dec 2020", "bullets": ["Cleaned and joined CRM data in SQL for quarterly reviews"]},
    ],
    "education": [{"institution": "University of Texas", "degree": "B.S.", "field": "Statistics",
                   "start_date": "", "end_date": "2019", "details": []}],
    "projects": [],
    "certifications": [{"name": "Tableau Desktop Specialist", "issuer": "Tableau", "date": "2022"}],
}

JOB_TEXT = """Senior Data Analyst at Globex. Remote (US). $110,000 - $130,000.
We need SQL, Python, Tableau and dbt experience. You will build dashboards for sales leadership,
automate reporting, and partner with product on A/B testing. Snowflake experience is a plus."""


def fabricated_tailoring() -> dict:
    """What a misbehaving model might return: a fake skill, a changed title, a fake employer,
    a dropped job, an invented metric and a changed degree."""
    r = copy.deepcopy(PARSED_RESUME)
    r["skills"] = ["Tableau", "SQL", "Python", "dbt", "Snowflake", "A/B testing"]
    r["summary"] = "Data analyst with 5 years of experience building Tableau dashboards and automated Python reporting."
    acme = r["experience"][0]
    acme["source_index"] = 0
    acme["title"] = "Senior Data Analyst"  # changed title
    acme["bullets"] = [
        "Built 12 Tableau dashboards for 300 sales staff, giving leadership a daily view of pipeline",
        "Automated weekly reporting in Python, saving 6 hours per week",
        "Migrated the warehouse to Snowflake, cutting costs 40%",  # invented tool + metric
    ]
    fake = {"source_index": 5, "title": "Lead Analyst", "company": "Initech", "location": "Remote",
            "start_date": "2015", "end_date": "2019", "bullets": ["Led analytics for 40 clients"]}
    r["experience"] = [acme, fake]  # Beta LLC dropped
    r["education"][0]["degree"] = "M.S."
    return {
        "resume": r,
        "cover_letter": "Dear Hiring Team,\n\nI build Tableau dashboards and automate reporting in Python.\n\n"
                        "Best regards,\nJordan Rivera",
        "changes": ["Led the Acme role with dashboard work to match the posting"],
        "gaps": ["dbt", "Snowflake"],
    }


class FakeLLM:
    def __init__(self, settings=None):
        self.calls: list[str] = []
        self.tailoring = fabricated_tailoring()

    def json(self, system, user, schema, *, effort=None):
        if "convert resume text" in system:
            self.calls.append("parse")
            return copy.deepcopy(PARSED_RESUME)
        if "extract a job posting" in system:
            self.calls.append("import")
            return {"is_job_posting": True, "title": "Senior Data Analyst", "company": "Globex",
                    "location": "Remote (US)", "remote": "remote", "salary_text": "$110,000 - $130,000",
                    "salary_min": 110000, "salary_max": 130000, "employment_type": "Full-time",
                    "description": JOB_TEXT}
        if "score how well" in system:
            self.calls.append("score")
            ids = [int(x) for x in re.findall(r'job_id="(\d+)"', user)]
            return {"scores": [{"job_id": i, "score": 82, "reason": "Strong SQL and Tableau match.",
                                "gaps": ["dbt"]} for i in ids]}
        if "tailor a candidate" in system:
            self.calls.append("tailor")
            return copy.deepcopy(self.tailoring)
        raise AssertionError("unexpected prompt: " + system[:60])

    def text(self, system, user, *, effort=None):
        return "ok"


class FakeBrowser:
    def __init__(self):
        from jobpilot.applier import BrowserStatus
        self.status = BrowserStatus()
        self.prefills: list[tuple] = []
        self.signins: list[str] = []
        self.on_result = None

    def start_prefill(self, application_id, url, resume_path, cover_path, contact):
        self.prefills.append((application_id, url, resume_path, cover_path, contact))

    def open_signin(self, board):
        self.signins.append(board)


REMOTIVE_PAYLOAD = {"jobs": [
    {"id": 101, "url": "https://remotive.com/remote-jobs/data/senior-data-analyst-101",
     "title": "Senior Data Analyst", "company_name": "Initrode", "job_type": "full_time",
     "publication_date": "2026-10-20T10:00:00", "candidate_required_location": "USA",
     "salary": "$100k - $120k", "description": "<p>We use <b>SQL</b> and Tableau.</p><ul><li>Dashboards</li></ul>"},
    {"id": 102, "url": "https://remotive.com/remote-jobs/data/analyst-102", "title": "Data Analyst (Clearance)",
     "company_name": "Gov Co", "job_type": "contract", "publication_date": "2026-10-19T10:00:00",
     "candidate_required_location": "USA", "salary": "", "description": "<p>Requires security clearance.</p>"},
]}


def remotive_with_mock(settings: Settings) -> RemotiveSource:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "remotive.com"
        return httpx.Response(200, json=REMOTIVE_PAYLOAD)
    return RemotiveSource(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(anthropic_api_key="test-key", data_dir=tmp_path / "data")
    s.ensure_dirs()
    return s


@pytest.fixture
def fake_llm(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(main_module, "LLM", lambda settings: llm)
    return llm


@pytest.fixture
def fake_browser():
    return FakeBrowser()


@pytest.fixture
def client(settings, fake_llm, fake_browser):
    from fastapi.testclient import TestClient
    app = main_module.create_app(settings, browser=fake_browser, sources={"remotive": remotive_with_mock(settings)})
    with TestClient(app) as c:
        yield c


def write_minimal_pdf(path: Path, lines: list[str]) -> None:
    """Write a tiny valid PDF with text, without extra dependencies."""
    content = "BT /F1 11 Tf 50 750 Td 14 TL " + " ".join(
        "(" + l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") '" for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)
