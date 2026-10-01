"""Environment-driven settings. No hardcoded paths or hosts (the repo is cloned into the intranet)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    env: str  # "development" | "test" | "production" (anything else is treated as production)
    data_dir: Path
    auth_provider: str  # "dev" | "saml"
    llm_base_url: str  # OpenAI-compatible endpoint, intranet only
    llm_model: str
    mask_pii_default: bool

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self.data_dir / "wiki"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wiki.db"


_TRUE = {"1", "true", "yes", "on"}


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Load settings. WIKI_ENV is normalized (strip+lower) and defaults to "production" (fail closed).

    Local development must set WIKI_ENV=development (or "test") explicitly to use the dev auth provider.
    """
    e = os.environ if environ is None else environ
    return Settings(
        env=(e.get("WIKI_ENV") or "production").strip().lower() or "production",
        data_dir=Path(e.get("WIKI_DATA_DIR", "./data")),
        auth_provider=e.get("WIKI_AUTH_PROVIDER", "dev"),
        llm_base_url=e.get("WIKI_LLM_BASE_URL", "http://127.0.0.1:8000/v1"),
        llm_model=e.get("WIKI_LLM_MODEL", "local-model"),
        mask_pii_default=e.get("WIKI_MASK_PII", "0").strip().lower() in _TRUE,
    )
