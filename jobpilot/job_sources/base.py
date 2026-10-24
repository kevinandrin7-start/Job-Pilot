"""Pluggable job-source layer.

To add a source: subclass `JobSource`, implement `search()`, and decorate the
class with `@register`. It then shows up on the Jobs page automatically.

Only sources with public APIs belong here. LinkedIn, Indeed and Dice search
pages are deliberately not supported (their terms prohibit scraping); use the
Import feature for postings found there.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import ClassVar

import httpx

from ..config import Settings

USER_AGENT = "JobPilot/1.0 (personal job-search assistant; local use)"


@dataclass
class SearchQuery:
    keywords: str
    location: str = ""
    remote_only: bool = False
    min_salary: int | None = None
    limit: int = 25


@dataclass
class JobPosting:
    source: str
    external_id: str
    title: str
    company: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    remote: bool | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_text: str = ""
    employment_type: str = ""
    posted_at: str = ""
    extra: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        row = asdict(self)
        row.pop("extra")
        row["remote"] = None if self.remote is None else int(self.remote)
        return row


class JobSource(ABC):
    name: ClassVar[str]
    label: ClassVar[str]
    min_interval_seconds: ClassVar[int] = 10   # politeness gap between live calls
    cache_seconds: ClassVar[int] = 3600        # identical queries are served from cache

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT},
                                             follow_redirects=True)

    def enabled(self) -> bool:
        return True

    def disabled_reason(self) -> str:
        return ""

    @abstractmethod
    def search(self, query: SearchQuery) -> list[JobPosting]: ...


REGISTRY: dict[str, type[JobSource]] = {}


def register(cls: type[JobSource]) -> type[JobSource]:
    REGISTRY[cls.name] = cls
    return cls


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def describe_network_error(e: Exception, host: str) -> str:
    """Turn an httpx error into a message that says what to do about it."""
    chain, cur = [], e
    while cur is not None and len(chain) < 6:
        chain.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    detail = " ".join(chain)
    if "CERTIFICATE_VERIFY_FAILED" in detail or "SSLCertVerificationError" in detail:
        return (f"couldn't verify {host}'s security certificate. Antivirus software (such as Norton) or a "
                "proxy is probably inspecting HTTPS. Run `pip install truststore` in JobPilot's environment "
                "and restart; JobPilot will then trust the same certificates as your computer.")
    if isinstance(e, httpx.TimeoutException):
        return f"{host} took too long to answer. Try again in a minute."
    if isinstance(e, httpx.HTTPStatusError):
        return f"{host} answered with HTTP {e.response.status_code}."
    if isinstance(e, (httpx.ConnectError, httpx.NetworkError)):
        return (f"couldn't connect to {host}. Check your internet connection, and whether a firewall or "
                "antivirus is blocking Python.")
    return f"{type(e).__name__}: {str(e)[:200]}"


def html_to_text(html: str) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_MONEY = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kK])?")


def parse_salary(text: str) -> tuple[float | None, float | None]:
    """Best-effort parse of free-text salary like '$90k - $120k' or '100,000 USD'."""
    if not text:
        return None, None
    values = []
    for num, k in _MONEY.findall(text):
        n = float(num.replace(",", ""))
        if k:
            n *= 1000
        if n >= 1000:
            values.append(n)
    if not values:
        return None, None
    return min(values), max(values)


def looks_remote(*texts: str) -> bool:
    return any(re.search(r"\bremote\b|\banywhere\b|work from home|\bwfh\b", t or "", re.I) for t in texts)
