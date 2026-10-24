"""Truthfulness guard: corrections happen in code regardless of what the model returns."""

from __future__ import annotations

import copy

from conftest import PARSED_RESUME, fabricated_tailoring

from jobpilot.tailor import apply_truthfulness_guard, check_cover_letter, unsupported_facts


def _guard():
    return apply_truthfulness_guard(PARSED_RESUME, fabricated_tailoring()["resume"])


def test_fabricated_skill_removed():
    resume, corrections = _guard()
    assert "dbt" not in resume["skills"] and "Snowflake" not in resume["skills"]
    assert resume["skills"][:3] == ["Tableau", "SQL", "Python"]  # reordering allowed
    assert any("removed 'dbt'" in c for c in corrections)


def test_changed_title_restored():
    resume, corrections = _guard()
    acme = resume["experience"][0]
    assert acme["title"] == "Data Analyst" and acme["company"] == "Acme Corp"
    assert any("restored title" in c and "Senior Data Analyst" in c for c in corrections)


def test_fake_employer_dropped_and_missing_job_readded():
    resume, corrections = _guard()
    companies = [e["company"] for e in resume["experience"]]
    assert companies == ["Acme Corp", "Beta LLC"]
    assert any("dropped 'Lead Analyst at Initech'" in c for c in corrections)
    assert any("re-added Junior Analyst at Beta LLC" in c for c in corrections)


def test_invented_metric_and_tool_in_bullet():
    resume, corrections = _guard()
    bullets = resume["experience"][0]["bullets"]
    assert not any("Snowflake" in b or "40" in b for b in bullets)
    assert "Built 12 Tableau dashboards for 300 sales staff, giving leadership a daily view of pipeline" in bullets
    assert any("'40'" in c and "'Snowflake'" in c for c in corrections)


def test_education_certs_contact_verbatim():
    resume, corrections = _guard()
    assert resume["education"] == PARSED_RESUME["education"]
    assert resume["certifications"] == PARSED_RESUME["certifications"]
    assert resume["contact"] == PARSED_RESUME["contact"]
    assert any(c.startswith("Education: restored") for c in corrections)


def test_dates_and_locations_restored():
    proposed = copy.deepcopy(PARSED_RESUME)
    proposed["experience"][1]["start_date"] = "Jan 2018"
    proposed["experience"][1]["location"] = "Remote"
    resume, corrections = apply_truthfulness_guard(PARSED_RESUME, proposed)
    assert resume["experience"][1]["start_date"] == "Jun 2019"
    assert resume["experience"][1]["location"] == "Dallas, TX"
    assert len([c for c in corrections if "restored" in c]) == 2


def test_honest_tailoring_has_no_corrections():
    proposed = copy.deepcopy(PARSED_RESUME)
    proposed["skills"] = ["Tableau", "SQL", "Python"]
    proposed["experience"][0]["bullets"].reverse()
    resume, corrections = apply_truthfulness_guard(PARSED_RESUME, proposed)
    assert corrections == []
    assert resume["experience"][0]["bullets"][0].startswith("Automated")


def test_summary_with_new_facts_reverts():
    proposed = copy.deepcopy(PARSED_RESUME)
    proposed["summary"] = "Data analyst with 8 years of Snowflake experience."
    resume, corrections = apply_truthfulness_guard(PARSED_RESUME, proposed)
    assert resume["summary"] == PARSED_RESUME["summary"]
    assert corrections and corrections[0].startswith("Summary")


def test_unsupported_facts_detection():
    allowed = "Built 12 Tableau dashboards"
    assert unsupported_facts("Built 12 Tableau dashboards quickly", allowed) == []
    assert unsupported_facts("Built 15 Looker dashboards", allowed) == ["15", "Looker"]


def test_cover_letter_check():
    job = {"title": "Analyst", "company": "Globex", "description": "SQL"}
    assert check_cover_letter("Dear Hiring Team,\n\nI like Globex.\n\nJordan Rivera", PARSED_RESUME, job) == []
    assert check_cover_letter("I led Kubernetes migrations at Globex.", PARSED_RESUME, job)
