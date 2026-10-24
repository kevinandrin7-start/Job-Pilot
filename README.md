# JobPilot

A local web app that helps you find jobs, tailor your resume to each one without inventing anything,
pre-fill applications (you always click Submit yourself), and keep a record of every application with
the exact files you sent.

It runs only on your computer, at http://127.0.0.1:8000.

## Setup

You need Python 3.11 or newer.

```bash
cd jobpilot
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
playwright install chromium        # the browser used for sign-in and pre-fill

cp .env.example .env               # Windows: copy .env.example .env
```

Open `.env` and set at least `ANTHROPIC_API_KEY` (from https://console.anthropic.com/). Optionally add
`ADZUNA_APP_ID` and `ADZUNA_APP_KEY` (free at https://developer.adzuna.com/) to search Adzuna as well
as Remotive.

Start the app:

```bash
python -m jobpilot
```

Then open http://127.0.0.1:8000.

## Using it

1. **Setup**: upload your resume (PDF, DOCX or TXT). JobPilot extracts the text locally and Claude
   structures it into JSON, copying facts exactly. Check the parsed result and fix anything in the JSON
   box; that JSON is the source of truth for all tailoring. Then fill in preferences: target titles,
   locations, remote-only, salary floor, exclude keywords, and the email and phone to use on applications.
2. **Jobs**: "Find and rank jobs" searches Remotive (and Adzuna if configured) for each target title.
   Your filters run first (exclude keywords, remote-only, and salary floor when a salary is listed), then
   each remaining job is scored 0-100 against your resume with a short reason and a list of gaps.
   Jobs are deduplicated by source and ID. Use the Open, Applied and Skipped tabs, and Skip or Tailor.
3. **Import**: for LinkedIn, Indeed, Dice or any other site, paste the job URL and/or the description
   text. JobPilot fetches the URL once (if you gave only a URL); if the site requires sign-in or blocks it,
   you'll be asked to paste the text instead. Claude extracts title, company, location, salary and
   description.
4. **Tailor**: generates a tailored resume and a short cover letter, then runs the truthfulness guard
   (below). Files are saved to `data/applications/<timestamp>_<company>_<role>/` as `resume.docx`,
   `cover_letter.docx` and `job_description.txt`.
5. **Apply**: on the application page, "Open in browser and pre-fill" opens the posting in JobPilot's
   browser window, clicks Apply / Easy Apply if present, uploads that exact resume, and fills name, email
   and phone. Then it stops. You review, answer screening questions, solve any CAPTCHA, and click Submit
   yourself. Then press "I submitted this application".
6. **Record**: marking it submitted stores the timestamp and a SHA-256 fingerprint of each file, and
   makes the files read-only, so you always have the exact documents you sent. Add notes and update the
   status (prepared, in progress, submitted, failed) any time.

### Signing in to LinkedIn, Indeed and Dice

Go to **Sign in** and press the button for a board. A visible browser opens on that board's login page,
using a persistent profile in `data/browser_profile`. Sign in yourself and close the window. The board's
session is reused the next time you pre-fill. JobPilot never asks for, sees, stores, logs or sends your
passwords. To sign out everywhere, delete `data/browser_profile`.

## Safety rules (built into the code)

- **No passwords.** Sign-in is done by you, in the board's own page. The pre-fill code skips password,
  one-time-code, CAPTCHA and similar fields entirely.
- **Never submits.** The only thing the browser automation clicks is an Apply / Easy Apply control.
  `safe_click` refuses anything labelled like submit/send/finish/review, any `type=submit` control, and
  any button inside a form. Filling fields never presses Enter.
- **No scraping of LinkedIn, Indeed or Dice search pages**, no CAPTCHA solving, no bot-detection
  workarounds, no retries on blocks. Direct search uses only public APIs (Remotive, Adzuna), with
  cached results and spacing between calls.
- **Local only.** The server binds to 127.0.0.1, rejects requests whose Host isn't localhost, and
  rejects cross-site form posts.

## The truthfulness guard

The model may reorder and reword your real experience and mirror the posting's terminology where it
truthfully applies. After it responds, `jobpilot/tailor.py` enforces, in code:

- Name, contact info, education and certifications are copied verbatim from your original resume.
- Each experience entry is matched to an original job. Employer, title, location and dates are restored
  from the original. Entries that match no original job are dropped. Original jobs the model left out are
  re-added. Jobs stay in their original order.
- Any skill whose text doesn't appear in your original resume is removed.
- Each reworded bullet is checked: any number, or any specialised term (tools, acronyms, proper nouns),
  not found in that job's original entry means the bullet is swapped back to your original wording (or
  removed if no original is close). A summary that adds facts reverts to your original summary.
- Projects not in your original resume are dropped; their names and technology lists are restored.
- Requirements you don't meet are listed as **gaps**, never added to the resume.

Every correction appears in the application's change list, next to the model's own description of its
edits. The cover letter is checked the same way and anything unverifiable is flagged for you to review.

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Required for parsing, import, scoring and tailoring |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Model used for all LLM calls |
| `LLM_EFFORT_EXTRACT` | `low` | Effort for parsing, import and scoring |
| `LLM_EFFORT_TAILOR` | `high` | Effort for tailoring |
| `LLM_FALLBACKS` | `default` | Server-side refusal fallback; set empty to disable |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | | Enable Adzuna search |
| `ADZUNA_COUNTRY` | `us` | Adzuna country code (gb, ca, au, ...) |
| `JOBPILOT_PORT` | `8000` | Port on 127.0.0.1 |
| `JOBPILOT_DATA_DIR` | `data` | Where the database, uploads, profile and applications live |
| `PLAYWRIGHT_CHROMIUM_PATH` | | Use an existing Chrome/Chromium binary instead of Playwright's |

## Tests

```bash
pytest
```

Tests run offline: the LLM is stubbed with monkeypatch, job-source HTTP uses `httpx.MockTransport`, and
the browser tests use a local HTML page in headless Chromium (they skip if Chromium isn't installed).
They cover the full flow (upload, filters, find and rank, dedupe, import, score, tailor, files on disk,
download, pre-fill hand-off, mark submitted, status and notes), the truthfulness guard (fabricated skill,
changed title, fake employer, dropped job, invented metric), DOCX rendering, resume extraction for
TXT/DOCX/PDF, HTML-to-text, and the never-submit and never-touch-passwords rules.

## Project layout

```
jobpilot/
  config.py          settings from .env
  db.py              SQLite schema and queries
  llm.py             Anthropic SDK wrapper (structured JSON output)
  resume_parser.py   PDF/DOCX/TXT extraction and LLM parsing
  job_sources/       pluggable sources: base.py, remotive.py, adzuna.py, importer.py
  matcher.py         preferences, filters, LLM scoring
  tailor.py          tailoring, truthfulness guard, .docx rendering, application folders
  applier.py         Playwright sign-in and pre-fill (never submits)
  main.py            FastAPI routes
  templates/, static/
tests/
```

### Adding a job source

Create `jobpilot/job_sources/mysource.py` with a `JobSource` subclass decorated with `@register`,
implement `search(query) -> list[JobPosting]`, and import it in `job_sources/__init__.py`. It appears in
Setup automatically. Only add sources that offer a public API and allow this use.
