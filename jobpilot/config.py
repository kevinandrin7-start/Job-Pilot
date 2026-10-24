"""Settings loaded from .env and the environment.

Secrets (the Anthropic and Adzuna keys) are read here and nowhere else. They are
never written to the database, rendered in templates, or logged. Job-board
passwords are never handled by JobPilot at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"  # JobPilot only ever binds to loopback.


def use_system_certificates() -> bool:
    """Verify HTTPS against the operating system's certificate store instead of Python's bundled one.

    Antivirus tools (Norton, Avast, Kaspersky...) and company proxies inspect HTTPS by re-signing it with
    their own root certificate, which they add to the Windows/macOS store. Python's bundled store doesn't
    have it, so every request fails with CERTIFICATE_VERIFY_FAILED. truststore makes Python trust exactly
    what your OS trusts. Verification stays fully on. Set JOBPILOT_SYSTEM_CERTS=0 to turn this off.
    """
    if os.getenv("JOBPILOT_SYSTEM_CERTS", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        import truststore
    except ImportError:
        return False
    truststore.inject_into_ssl()
    return True


@dataclass
class Settings:
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    effort_extract: str = "low"
    effort_tailor: str = "high"
    llm_fallbacks: str = "default"
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    adzuna_country: str = "us"
    port: int = 8000
    chromium_path: str = ""
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobpilot.db"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser_profile"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def tailored_dir(self) -> Path:
        return self.data_dir / "tailored"

    @property
    def applications_dir(self) -> Path:
        return self.data_dir / "applications"

    @property
    def llm_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def adzuna_configured(self) -> bool:
        return bool(self.adzuna_app_id and self.adzuna_app_key)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.browser_profile_dir, self.uploads_dir,
                  self.tailored_dir, self.applications_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    data_dir = Path(os.getenv("JOBPILOT_DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5").strip() or "claude-opus-5-5",
        effort_extract=os.getenv("LLM_EFFORT_EXTRACT", "low").strip() or "low",
        effort_tailor=os.getenv("LLM_EFFORT_TAILOR", "high").strip() or "high",
        llm_fallbacks=os.getenv("LLM_FALLBACKS", "default").strip(),
        adzuna_app_id=os.getenv("ADZUNA_APP_ID", "").strip(),
        adzuna_app_key=os.getenv("ADZUNA_APP_KEY", "").strip(),
        adzuna_country=os.getenv("ADZUNA_COUNTRY", "us").strip().lower() or "us",
        port=int(os.getenv("JOBPILOT_PORT", "8000")),
        chromium_path=os.getenv("PLAYWRIGHT_CHROMIUM_PATH", "").strip(),
        data_dir=data_dir,
    )
