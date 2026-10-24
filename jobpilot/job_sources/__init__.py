"""Job sources. Importing this package registers the built-in sources."""

from __future__ import annotations

import hashlib
import json
import logging
import time

from ..config import Settings
from ..db import Database
from . import adzuna, remotive  # noqa: F401  (registers sources)
from urllib.parse import urlparse

from .base import (REGISTRY, JobPosting, JobSource, SearchQuery, describe_network_error, html_to_text,
                   looks_remote, parse_salary)

log = logging.getLogger(__name__)
_last_call: dict[str, float] = {}

__all__ = ["REGISTRY", "JobPosting", "JobSource", "SearchQuery", "html_to_text", "looks_remote",
           "parse_salary", "available_sources", "run_search"]


def available_sources(settings: Settings) -> list[dict]:
    out = []
    for name, cls in REGISTRY.items():
        src = cls(settings)
        out.append({"name": name, "label": cls.label, "enabled": src.enabled(), "reason": src.disabled_reason()})
    return out


def run_search(settings: Settings, db: Database, source_names: list[str], queries: list[SearchQuery],
               sources: dict[str, JobSource] | None = None, polite_wait: bool = True) -> tuple[list[JobPosting], list[str]]:
    """Run each query against each chosen source. Identical queries are cached and live calls
    to one source are spaced out, so JobPilot stays well inside each API's rate limits."""
    results: list[JobPosting] = []
    errors: list[str] = []
    for name in source_names:
        cls = REGISTRY.get(name)
        if not cls:
            continue
        src = (sources or {}).get(name) or cls(settings)
        if not src.enabled():
            errors.append(f"{cls.label}: {src.disabled_reason()}")
            continue
        for q in queries:
            key = name + ":" + hashlib.sha1(json.dumps(q.__dict__, sort_keys=True).encode()).hexdigest()
            cached = db.cache_get(key, cls.cache_seconds)
            if cached is not None:
                results += [JobPosting(**j) for j in cached]
                continue
            wait = cls.min_interval_seconds - (time.monotonic() - _last_call.get(name, -1e9))
            if wait > 0 and polite_wait:
                time.sleep(wait)
            try:
                found = src.search(q)
            except Exception as e:  # network or API error: report it and keep going
                log.warning("Search failed on %s: %r", name, e)
                host = urlparse(getattr(cls, "API_URL", "")).hostname or cls.label
                errors.append(f"{cls.label}: search for '{q.keywords}' failed: {describe_network_error(e, host)}")
                continue
            finally:
                _last_call[name] = time.monotonic()
            db.cache_put(key, [j.__dict__ for j in found])
            results += found
    return results, errors
