"""Resume tailoring with a truthfulness guard enforced in code.

The model gets your parsed resume and the posting, and returns a tailored
resume, a short cover letter, its own list of changes, and the gaps (posting
requirements your resume doesn't show). It may reorder and reword your real
experience and mirror the posting's terminology where it truthfully applies.

Then `apply_truthfulness_guard` runs, whatever the model said:
- name, contact info, education and certifications are copied verbatim from
  your original resume;
- each experience entry is matched to an original job; employer, title,
  location and dates are restored from the original; entries that match no
  original job are dropped; original jobs the model left out are re-added;
- any skill whose text doesn't appear in your original resume is removed;
- reworded bullets and the summary are checked for numbers and specialised
  terms (tools, acronyms, proper nouns) that aren't in your original; a bullet
  that adds one is removed, and a summary that adds one reverts to yours;
- projects that don't match an original project are dropped, and their names
  and technology lists are restored from the original.
Every correction goes into the change list you see before applying.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from .llm import LLMBackend
from .resume_parser import RESUME_SCHEMA, normalize_resume

_S = {"type": "string"}
_SA = {"type": "array", "items": _S}

_tailored_resume_schema = copy.deepcopy(RESUME_SCHEMA)
_exp_item = _tailored_resume_schema["properties"]["experience"]["items"]
_exp_item["properties"] = {"source_index": {"type": "integer"}, **_exp_item["properties"]}
_exp_item["required"] = ["source_index"] + _exp_item["required"]

TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "resume": _tailored_resume_schema,
        "cover_letter": _S,
        "changes": _SA,
        "gaps": _SA,
    },
    "required": ["resume", "cover_letter", "changes", "gaps"],
    "additionalProperties": False,
}

TAILOR_SYSTEM = """You tailor a candidate's resume to one job posting and write a short cover letter.
The candidate will send both to a real employer, so honesty comes first.

Return:
- resume: the tailored resume in the same JSON shape as the original. Each experience entry carries the
  source_index of the original job it comes from.
  You MAY: reorder skills, bullets and projects; reword bullets and the summary to foreground what this job
  needs; use the posting's terminology where it truthfully describes what the candidate already did; leave
  out irrelevant bullets, skills or projects.
  You may NOT: invent or change employers, job titles, dates, locations, degrees, certifications, skills,
  tools, technologies or metrics. Keep every number exactly as written in the original. Never add a skill or
  tool the original doesn't list. Keep every job.
- cover_letter: 3 short paragraphs, under 300 words, plain text, using only facts from the resume. Open with
  "Dear Hiring Team," and sign off with the candidate's name. No placeholders.
- changes: a short list describing what you changed and why (e.g. "Moved API work to the top of the
  Acme role to match the posting's focus on integrations").
- gaps: requirements in the posting that the resume does not show. These are shown to the candidate as
  gaps; never paper over them in the resume."""


# ---------------------------------------------------------------- fact checks

_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#./&-]*[A-Za-z0-9+#]|[A-Za-z]")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text or "")}


def _special_terms(text: str) -> set[str]:
    """Words that carry factual weight: capitalised mid-sentence words, acronyms, and words with digits or
    symbols (Kubernetes, AWS, C++, Node.js, S3). A sentence's first word is ignored."""
    terms = set()
    for sentence in re.split(r"(?<=[.!?;:])\s+|\n", text or ""):
        sentence = re.sub(r"^\s*[-•*]\s*", "", sentence)
        for i, tok in enumerate(_TOKEN.findall(sentence)):
            core = tok.strip(".-/&")
            if len(core) < 2:
                continue
            has_symbol = bool(re.search(r"[0-9+#./&]", core))
            upper_inside = any(c.isupper() for c in core[1:])
            capitalised = core[0].isupper() and i > 0
            if has_symbol or upper_inside or capitalised:
                terms.add(core)
    return terms


def _word_in(term: str, text_norm: str) -> bool:
    t = re.escape(term.lower().strip())
    if not t:
        return False
    return re.search(r"(?<![a-z0-9])" + t + r"(?:e?s)?(?![a-z0-9])", text_norm) is not None or (
        term.lower().endswith("s") and re.search(r"(?<![a-z0-9])" + re.escape(term.lower()[:-1]) +
                                                 r"(?![a-z0-9])", text_norm) is not None)


def unsupported_facts(text: str, allowed: str) -> list[str]:
    """Numbers and specialised terms in `text` that don't appear in `allowed`."""
    allowed_norm, allowed_nums = _norm(allowed), _numbers(allowed)
    problems = [n for n in sorted(_numbers(text)) if n not in allowed_nums]
    problems += [t for t in sorted(_special_terms(text)) if not _word_in(t, allowed_norm)]
    return problems


def _same(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)


def _entry_text(entry: dict) -> str:
    parts = [str(v) for k, v in entry.items() if isinstance(v, str)]
    for key in ("bullets", "technologies", "details"):
        parts += [str(x) for x in entry.get(key) or []]
    return "\n".join(parts)


# ---------------------------------------------------------------- the guard

def _match_experience(model_entries: list[dict], originals: list[dict]) -> list[tuple[dict, int | None]]:
    """Pair each model entry with an original job index (or None)."""
    used: set[int] = set()
    pairs: list[tuple[dict, int | None]] = []

    def similarity(m: dict, o: dict) -> int:
        score = 0
        if _same(m.get("company", ""), o.get("company", "")):
            score += 3
        if _same(m.get("title", ""), o.get("title", "")):
            score += 1
        if _same(m.get("start_date", ""), o.get("start_date", "")) and o.get("start_date"):
            score += 1
        if _same(m.get("end_date", ""), o.get("end_date", "")) and o.get("end_date"):
            score += 1
        return score

    for m in model_entries:
        best, best_score = None, 0
        idx = m.get("source_index")
        if isinstance(idx, int) and 0 <= idx < len(originals) and idx not in used:
            # Trust source_index only if the entry still resembles that job (same employer, or same title
            # and dates). A fabricated job that claims a real index doesn't get through.
            if similarity(m, originals[idx]) >= 2:
                best, best_score = idx, 99
        if best is None:
            for i, o in enumerate(originals):
                if i in used:
                    continue
                s = similarity(m, o)
                if s > best_score and (s >= 3 or s >= 2 and not m.get("company")):
                    best, best_score = i, s
        if best is not None:
            used.add(best)
        pairs.append((m, best))
    return pairs


def _overlap(a: str, b: str) -> float:
    ta, tb = set(re.findall(r"[a-z0-9]+", a.lower())), set(re.findall(r"[a-z0-9]+", b.lower()))
    return len(ta & tb) / max(1, len(ta | tb))


def _guard_bullets(bullets: list[str], originals: list[str], allowed: str, label: str,
                   corrections: list[str]) -> list[str]:
    """Keep bullets that pass the fact check. A failing bullet is swapped for the original bullet it was
    most likely based on, or removed if none is close."""
    kept: list[str] = []
    for b in bullets:
        b = str(b).strip()
        if not b:
            continue
        bad = unsupported_facts(b, allowed)
        if not bad:
            kept.append(b)
            continue
        added = ", ".join(repr(x) for x in bad)
        candidates = [o for o in originals if o not in kept]
        best = max(candidates, key=lambda o: _overlap(b, o), default=None)
        if best is not None and _overlap(b, best) >= 0.25:
            kept.append(best)
            corrections.append(f"{label}: a reworded bullet added {added}, which isn't in your original "
                               f"resume, so your original wording was restored (\"{best[:100]}\").")
        else:
            corrections.append(f"{label}: removed a bullet that added {added}, which isn't in your original "
                               f"resume (\"{b[:100]}\").")
    return kept


def apply_truthfulness_guard(original: dict, proposed: dict) -> tuple[dict, list[str]]:
    original = normalize_resume(original)
    raw_experience = (proposed or {}).get("experience") or []
    proposed = normalize_resume(proposed or {})
    for entry, raw in zip(proposed["experience"], raw_experience):
        entry["source_index"] = raw.get("source_index") if isinstance(raw, dict) else None
    corrections: list[str] = []
    full_text = json.dumps(original, ensure_ascii=False)
    full_norm = _norm(full_text)
    out = copy.deepcopy(proposed)

    # Identity, contact, education, certifications: always verbatim from the original.
    for key, label in (("name", "Name"), ("contact", "Contact info"), ("education", "Education"),
                       ("certifications", "Certifications")):
        if proposed[key] != original[key]:
            corrections.append(f"{label}: restored exactly as in your original resume.")
        out[key] = copy.deepcopy(original[key])

    # Summary
    bad = unsupported_facts(proposed["summary"], full_text)
    if bad:
        corrections.append(f"Summary: the tailored summary mentioned {', '.join(repr(x) for x in bad)}, which "
                           "isn't in your original resume, so your original summary was used.")
        out["summary"] = original["summary"]
    elif not proposed["summary"]:
        out["summary"] = original["summary"]

    # Skills: keep only those whose text appears in the original resume.
    skills, seen = [], set()
    for s in proposed["skills"]:
        key = _norm(s)
        if key in seen:
            continue
        seen.add(key)
        if _word_in(s, full_norm):
            skills.append(s)
        else:
            corrections.append(f"Skills: removed '{s}' because it doesn't appear in your original resume.")
    out["skills"] = skills or list(original["skills"])

    # Experience
    originals = original["experience"]
    matched: dict[int, dict] = {}
    for entry, idx in _match_experience(proposed["experience"], originals):
        label = f"{entry.get('title') or 'Role'} at {entry.get('company') or 'unknown employer'}"
        if idx is None:
            corrections.append(f"Experience: dropped '{label}' because it doesn't match any job in your "
                               "original resume.")
            continue
        orig = originals[idx]
        fixed = {k: orig[k] for k in ("title", "company", "location", "start_date", "end_date")}
        for k, name in (("company", "employer"), ("title", "title"), ("location", "location"),
                        ("start_date", "start date"), ("end_date", "end date")):
            if not _same(entry.get(k, ""), orig[k]):
                corrections.append(f"Experience: restored {name} for {orig['title']} at {orig['company']} "
                                   f"(model wrote '{entry.get(k, '')}', original is '{orig[k]}').")
        olabel = f"{orig['title']} at {orig['company']}"
        bullets = _guard_bullets(entry.get("bullets") or [], orig["bullets"], _entry_text(orig), olabel,
                                 corrections)
        if not bullets:
            bullets = list(orig["bullets"])
            if entry.get("bullets"):
                corrections.append(f"{olabel}: no reworded bullets passed the check, so your original "
                                   "bullets were used.")
        fixed["bullets"] = bullets
        matched[idx] = fixed
    experience = []
    for i, orig in enumerate(originals):
        if i in matched:
            experience.append(matched[i])
        else:
            corrections.append(f"Experience: re-added {orig['title']} at {orig['company']}, which the "
                               "tailored version left out.")
            experience.append(copy.deepcopy(orig))
    out["experience"] = experience  # original chronological order

    # Projects: may be omitted or reordered, never invented.
    by_name = {_norm(p["name"]): p for p in original["projects"] if p["name"]}
    projects, used = [], set()
    for p in proposed["projects"]:
        key = _norm(p.get("name", ""))
        orig = by_name.get(key)
        if not orig or key in used:
            if not orig:
                corrections.append(f"Projects: dropped '{p.get('name')}' because it isn't in your original resume.")
            continue
        used.add(key)
        label = f"Project {orig['name']}"
        item = {"name": orig["name"], "technologies": list(orig["technologies"])}
        if p.get("technologies") and [_norm(t) for t in p["technologies"]] != [_norm(t) for t in orig["technologies"]]:
            if any(not _word_in(t, _norm(_entry_text(orig))) for t in p["technologies"]):
                corrections.append(f"{label}: restored the technology list from your original resume.")
        desc = p.get("description", "")
        if desc and unsupported_facts(desc, _entry_text(orig)):
            corrections.append(f"{label}: restored the original description (the reworded one added details).")
            desc = orig["description"]
        item["description"] = desc or orig["description"]
        item["bullets"] = _guard_bullets(p.get("bullets") or [], orig["bullets"], _entry_text(orig), label,
                                         corrections) \
            or list(orig["bullets"])
        projects.append(item)
    out["projects"] = projects
    return normalize_resume(out), corrections


def check_cover_letter(text: str, resume: dict, job: dict) -> list[str]:
    allowed = json.dumps(resume, ensure_ascii=False) + "\n" + "\n".join(
        str(job.get(k) or "") for k in ("title", "company", "location", "description"))
    bad = unsupported_facts(text, allowed + "\nDear Hiring Team Sincerely Best Regards Thank I")
    if not bad:
        return []
    return ["Cover letter: please check " + ", ".join(repr(x) for x in bad[:12]) +
            "; these don't appear in your resume or the posting."]


# ---------------------------------------------------------------- orchestration

def _numbered_resume(resume: dict) -> str:
    data = copy.deepcopy(resume)
    for i, e in enumerate(data.get("experience") or []):
        e["source_index"] = i
    return json.dumps(data, ensure_ascii=False, indent=1)


def tailor(llm: LLMBackend, resume: dict, job: dict, effort: str | None = None) -> dict:
    """Run the model, then the guard. Returns resume, cover_letter, changes (list of dicts), gaps."""
    user = (f"JOB POSTING\nTitle: {job.get('title')}\nCompany: {job.get('company')}\n"
            f"Location: {job.get('location')}\n<posting>\n{job.get('description') or ''}\n</posting>\n\n"
            f"ORIGINAL RESUME (JSON)\n<resume>\n{_numbered_resume(resume)}\n</resume>")
    data = llm.json(TAILOR_SYSTEM, user, TAILOR_SCHEMA, effort=effort)
    guarded, corrections = apply_truthfulness_guard(resume, data.get("resume") or {})
    cover = (data.get("cover_letter") or "").strip()
    cover_warnings = check_cover_letter(cover, resume, job) if cover else []
    changes = ([{"kind": "edit", "text": str(c)} for c in data.get("changes") or [] if str(c).strip()]
               + [{"kind": "correction", "text": c} for c in corrections]
               + [{"kind": "warning", "text": w} for w in cover_warnings])
    gaps = [str(g).strip() for g in data.get("gaps") or [] if str(g).strip()]
    return {"resume": guarded, "cover_letter": cover, "changes": changes, "gaps": gaps}


def application_folder(base: Path, job: dict, when: datetime | None = None) -> Path:
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    slug = lambda s, n: re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")[:n] or "unknown"
    folder = base / f"{stamp}_{slug(job.get('company'), 40)}_{slug(job.get('title'), 50)}"
    n = 2
    while folder.exists():
        folder = folder.with_name(folder.name.rsplit("~", 1)[0] + f"~{n}")
        n += 1
    return folder


def write_application_files(folder: Path, job: dict, result: dict) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    resume_path = render_resume_docx(result["resume"], folder / "resume.docx")
    cover_path = render_cover_docx(result["cover_letter"], result["resume"], folder / "cover_letter.docx") \
        if result.get("cover_letter") else None
    jd = folder / "job_description.txt"
    header = [f"Title: {job.get('title') or ''}", f"Company: {job.get('company') or ''}",
              f"Location: {job.get('location') or ''}", f"Salary: {job.get('salary_text') or ''}",
              f"URL: {job.get('url') or ''}", f"Source: {job.get('source') or ''}",
              f"Saved: {datetime.now().isoformat(timespec='seconds')}", ""]
    jd.write_text("\n".join(header) + (job.get("description") or "") + "\n", encoding="utf-8")
    return {"resume": resume_path, "cover_letter": cover_path, "job_description": jd}


# ---------------------------------------------------------------- .docx output

def _heading(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text.upper())
    run.bold = True
    run.font.size = Pt(11)
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(2)


def _dates(item: dict) -> str:
    s, e = item.get("start_date") or "", item.get("end_date") or ""
    return f"{s} – {e}" if s and e else (s or e)


def render_resume_docx(resume: dict, path: Path) -> Path:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    for section in doc.sections:
        section.left_margin = section.right_margin = Pt(54)
        section.top_margin = section.bottom_margin = Pt(46)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(resume.get("name") or "")
    r.bold = True
    r.font.size = Pt(18)
    c = resume.get("contact") or {}
    contact_bits = [c.get("email"), c.get("phone"), c.get("location")] + list(c.get("links") or [])
    if any(contact_bits):
        p = doc.add_paragraph(" | ".join(b for b in contact_bits if b))
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    if resume.get("summary"):
        _heading(doc, "Summary")
        doc.add_paragraph(resume["summary"])
    if resume.get("skills"):
        _heading(doc, "Skills")
        doc.add_paragraph(", ".join(resume["skills"]))
    if resume.get("experience"):
        _heading(doc, "Experience")
        for e in resume["experience"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            run = p.add_run(" – ".join(x for x in (e.get("title"), e.get("company")) if x))
            run.bold = True
            meta = " | ".join(x for x in (e.get("location"), _dates(e)) if x)
            if meta:
                p.add_run(f"   {meta}").italic = True
            for b in e.get("bullets") or []:
                doc.add_paragraph(b, style="List Bullet")
    if resume.get("projects"):
        _heading(doc, "Projects")
        for pr in resume["projects"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            p.add_run(pr.get("name") or "").bold = True
            if pr.get("technologies"):
                p.add_run(f"   ({', '.join(pr['technologies'])})").italic = True
            if pr.get("description"):
                doc.add_paragraph(pr["description"])
            for b in pr.get("bullets") or []:
                doc.add_paragraph(b, style="List Bullet")
    if resume.get("education"):
        _heading(doc, "Education")
        for ed in resume["education"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            degree = ", ".join(x for x in (ed.get("degree"), ed.get("field")) if x)
            p.add_run(" – ".join(x for x in (degree, ed.get("institution")) if x)).bold = True
            if _dates(ed):
                p.add_run(f"   {_dates(ed)}").italic = True
            for d in ed.get("details") or []:
                doc.add_paragraph(d, style="List Bullet")
    if resume.get("certifications"):
        _heading(doc, "Certifications")
        for cert in resume["certifications"]:
            doc.add_paragraph(", ".join(x for x in (cert.get("name"), cert.get("issuer"), cert.get("date")) if x),
                              style="List Bullet")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def render_cover_docx(text: str, resume: dict, path: Path) -> Path:
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    head = doc.add_paragraph()
    head.add_run(resume.get("name") or "").bold = True
    c = resume.get("contact") or {}
    contact = " | ".join(x for x in (c.get("email"), c.get("phone"), c.get("location")) if x)
    if contact:
        doc.add_paragraph(contact)
    doc.add_paragraph(datetime.now().strftime("%B %d, %Y").replace(" 0", " "))
    for para in re.split(r"\n\s*\n", text.strip()):
        doc.add_paragraph(para.strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path
