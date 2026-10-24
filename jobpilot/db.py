"""SQLite storage (stdlib sqlite3).

One connection per call keeps things simple and thread-safe for a single-user
local app. JSON columns hold structured data (resume, preferences, match info).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    resume_filename TEXT,
    resume_path TEXT,
    resume_text TEXT,
    resume_json TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    remote INTEGER,
    salary_min REAL,
    salary_max REAL,
    salary_text TEXT,
    employment_type TEXT,
    description TEXT,
    posted_at TEXT,
    match_score INTEGER,
    match_reason TEXT,
    match_gaps_json TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    folder TEXT NOT NULL,
    resume_path TEXT NOT NULL,
    cover_letter_path TEXT,
    job_description_path TEXT NOT NULL,
    resume_json TEXT NOT NULL,
    cover_letter_text TEXT,
    changes_json TEXT NOT NULL DEFAULT '[]',
    gaps_json TEXT NOT NULL DEFAULT '[]',
    file_hashes_json TEXT,
    status TEXT NOT NULL DEFAULT 'prepared',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS search_cache (
    key TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

APPLICATION_STATUSES = ["prepared", "in_progress", "submitted", "failed"]
JOB_STATUSES = ["open", "skipped"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- generic helpers ----------
    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid

    # ---------- profile ----------
    def get_profile(self) -> dict | None:
        row = self.one("SELECT * FROM profile WHERE id = 1")
        if row and row.get("resume_json"):
            row["resume"] = json.loads(row["resume_json"])
        elif row:
            row["resume"] = None
        return row

    def save_profile(self, *, filename: str, path: str, text: str, resume: dict | None) -> None:
        self.execute(
            """INSERT INTO profile (id, resume_filename, resume_path, resume_text, resume_json, updated_at)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET resume_filename=excluded.resume_filename,
                 resume_path=excluded.resume_path, resume_text=excluded.resume_text,
                 resume_json=excluded.resume_json, updated_at=excluded.updated_at""",
            (filename, path, text, json.dumps(resume) if resume is not None else None, now_iso()),
        )

    def update_resume_json(self, resume: dict) -> None:
        self.execute("UPDATE profile SET resume_json = ?, updated_at = ? WHERE id = 1",
                     (json.dumps(resume), now_iso()))

    # ---------- preferences ----------
    def get_preferences(self) -> dict:
        from .matcher import default_preferences
        row = self.one("SELECT data FROM preferences WHERE id = 1")
        prefs = default_preferences()
        if row:
            prefs.update(json.loads(row["data"]))
        return prefs

    def save_preferences(self, prefs: dict) -> None:
        self.execute(
            """INSERT INTO preferences (id, data, updated_at) VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
            (json.dumps(prefs), now_iso()),
        )

    # ---------- jobs ----------
    def upsert_job(self, job: dict) -> tuple[int, bool]:
        """Insert a job, deduped on (source, external_id). Returns (id, created).
        An existing row keeps its status and score; only listing fields are refreshed."""
        cols = ["source", "external_id", "url", "title", "company", "location", "remote",
                "salary_min", "salary_max", "salary_text", "employment_type", "description", "posted_at"]
        values = tuple(job.get(c) for c in cols)
        with self.connect() as conn:
            existing = conn.execute("SELECT id FROM jobs WHERE source = ? AND external_id = ?",
                                    (job["source"], job["external_id"])).fetchone()
            if existing:
                conn.execute(f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in cols[2:])} WHERE id = ?",
                             values[2:] + (existing["id"],))
                return existing["id"], False
            cur = conn.execute(
                f"INSERT INTO jobs ({', '.join(cols)}, created_at) VALUES ({', '.join('?' for _ in cols)}, ?)",
                values + (now_iso(),))
            return cur.lastrowid, True

    @staticmethod
    def _job_out(job: dict) -> dict:
        job["gaps"] = json.loads(job["match_gaps_json"]) if job.get("match_gaps_json") else []
        return job

    def get_job(self, job_id: int) -> dict | None:
        job = self.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._job_out(job) if job else None

    def list_jobs(self, tab: str = "open") -> list[dict]:
        """tab: open (not skipped, no submitted application), applied, skipped."""
        submitted = "EXISTS (SELECT 1 FROM applications a WHERE a.job_id = jobs.id AND a.status = 'submitted')"
        if tab == "applied":
            where = submitted
        elif tab == "skipped":
            where = "status = 'skipped'"
        else:
            where = f"status = 'open' AND NOT {submitted}"
        rows = self.all(
            f"""SELECT jobs.*, (SELECT MAX(id) FROM applications a WHERE a.job_id = jobs.id) AS latest_application_id
                FROM jobs WHERE {where}
                ORDER BY match_score IS NULL, match_score DESC, created_at DESC""")
        return [self._job_out(r) for r in rows]

    def unscored_jobs(self) -> list[dict]:
        return [self._job_out(r) for r in
                self.all("SELECT * FROM jobs WHERE match_score IS NULL AND status = 'open' ORDER BY id")]

    def set_job_score(self, job_id: int, score: int, reason: str, gaps: list[str]) -> None:
        self.execute("UPDATE jobs SET match_score = ?, match_reason = ?, match_gaps_json = ? WHERE id = ?",
                     (score, reason, json.dumps(gaps), job_id))

    def clear_scores(self) -> None:
        self.execute("UPDATE jobs SET match_score = NULL, match_reason = NULL, match_gaps_json = NULL")

    def set_job_status(self, job_id: int, status: str) -> None:
        if status not in JOB_STATUSES:
            raise ValueError(f"Unknown job status {status!r}")
        self.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))

    # ---------- applications ----------
    def create_application(self, *, job_id: int, folder: str, resume_path: str, cover_letter_path: str | None,
                           job_description_path: str, resume: dict, cover_letter_text: str | None,
                           changes: list[dict], gaps: list[str]) -> int:
        ts = now_iso()
        app_id = self.execute(
            """INSERT INTO applications (job_id, folder, resume_path, cover_letter_path, job_description_path,
               resume_json, cover_letter_text, changes_json, gaps_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)""",
            (job_id, folder, resume_path, cover_letter_path, job_description_path, json.dumps(resume),
             cover_letter_text, json.dumps(changes), json.dumps(gaps), ts, ts))
        self.add_event(app_id, "prepared", "Tailored resume and cover letter generated")
        return app_id

    @staticmethod
    def _app_out(app: dict) -> dict:
        app["resume"] = json.loads(app["resume_json"])
        app["changes"] = json.loads(app["changes_json"] or "[]")
        app["gaps"] = json.loads(app["gaps_json"] or "[]")
        app["file_hashes"] = json.loads(app["file_hashes_json"]) if app.get("file_hashes_json") else {}
        return app

    def get_application(self, app_id: int) -> dict | None:
        app = self.one("""SELECT a.*, j.title, j.company, j.url, j.location, j.source FROM applications a
                          JOIN jobs j ON j.id = a.job_id WHERE a.id = ?""", (app_id,))
        if not app:
            return None
        app = self._app_out(app)
        app["events"] = self.all("SELECT * FROM application_events WHERE application_id = ? ORDER BY id",
                                 (app_id,))
        return app

    def list_applications(self, status: str | None = None, limit: int | None = None) -> list[dict]:
        sql = """SELECT a.*, j.title, j.company, j.url, j.location, j.source FROM applications a
                 JOIN jobs j ON j.id = a.job_id"""
        params: list[Any] = []
        if status:
            sql += " WHERE a.status = ?"
            params.append(status)
        sql += " ORDER BY a.id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._app_out(r) for r in self.all(sql, tuple(params))]

    def set_application_status(self, app_id: int, status: str, detail: str = "") -> None:
        if status not in APPLICATION_STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        self.execute("UPDATE applications SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), app_id))
        self.add_event(app_id, "status", f"{status}{': ' + detail if detail else ''}")

    def set_application_notes(self, app_id: int, notes: str) -> None:
        self.execute("UPDATE applications SET notes = ?, updated_at = ? WHERE id = ?", (notes, now_iso(), app_id))

    def mark_submitted(self, app_id: int, file_hashes: dict) -> None:
        ts = now_iso()
        self.execute("""UPDATE applications SET status = 'submitted', submitted_at = ?, updated_at = ?,
                        file_hashes_json = ? WHERE id = ?""", (ts, ts, json.dumps(file_hashes), app_id))
        self.add_event(app_id, "submitted", "You marked this application as submitted")

    def add_event(self, app_id: int, kind: str, detail: str = "") -> None:
        self.execute("INSERT INTO application_events (application_id, at, kind, detail) VALUES (?, ?, ?, ?)",
                     (app_id, now_iso(), kind, detail))

    # ---------- search cache ----------
    def cache_get(self, key: str, max_age_seconds: int) -> Any | None:
        row = self.one("SELECT fetched_at, payload FROM search_cache WHERE key = ?", (key,))
        if not row:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
        if age.total_seconds() > max_age_seconds:
            return None
        return json.loads(row["payload"])

    def cache_put(self, key: str, payload: Any) -> None:
        self.execute(
            """INSERT INTO search_cache (key, fetched_at, payload) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload""",
            (key, now_iso(), json.dumps(payload)),
        )

    def stats(self) -> dict:
        count = lambda sql: self.one(sql)["c"]
        return {
            "jobs": count("SELECT COUNT(*) c FROM jobs WHERE status = 'open'"),
            "prepared": count("SELECT COUNT(*) c FROM applications WHERE status IN ('prepared', 'in_progress')"),
            "submitted": count("SELECT COUNT(*) c FROM applications WHERE status = 'submitted'"),
            "failed": count("SELECT COUNT(*) c FROM applications WHERE status = 'failed'"),
        }
