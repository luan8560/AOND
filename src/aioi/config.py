"""Runtime configuration, read from environment (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # optional dependency; harmless if missing
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


@dataclass(frozen=True)
class Settings:
    provider: str
    anthropic_api_key: str | None
    chat_model: str
    vision_model: str
    extract_model: str = "claude-haiku-4-5"  # structured extraction/ops — cheap model is enough
    google_api_key: str | None = None  # for Google Drive folder listing (optional)
    db_path: str = "aioi.db"           # local persistent store (projects, later listings)
    azure_tenant_id: str | None = None    # SharePoint media store (kho gốc) — Azure AD app
    azure_client_id: str | None = None
    azure_client_secret: str | None = None
    sharepoint_site_id: str | None = None
    max_photos_per_listing: int = 20      # cap photos KEPT per listing (storage cost control)

    @property
    def effective_provider(self) -> str:
        """Fall back to the offline mock provider when Claude is selected but no key is present."""
        if self.provider == "claude" and not self.anthropic_api_key:
            return "mock"
        return self.provider

    @property
    def sharepoint_configured(self) -> bool:
        return all((self.azure_tenant_id, self.azure_client_id,
                    self.azure_client_secret, self.sharepoint_site_id))


def load_settings() -> Settings:
    return Settings(
        provider=os.getenv("AIOI_LLM_PROVIDER", "claude").strip().lower(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        chat_model=os.getenv("AIOI_CHAT_MODEL", "claude-haiku-4-5"),
        vision_model=os.getenv("AIOI_VISION_MODEL", "claude-opus-4-8"),
        extract_model=os.getenv("AIOI_EXTRACT_MODEL", "claude-haiku-4-5"),

        google_api_key=os.getenv("GOOGLE_API_KEY") or None,
        db_path=os.getenv("AIOI_DB_PATH", "aioi.db"),
        azure_tenant_id=os.getenv("AZURE_TENANT_ID") or None,
        azure_client_id=os.getenv("AZURE_CLIENT_ID") or None,
        azure_client_secret=os.getenv("AZURE_CLIENT_SECRET") or None,
        sharepoint_site_id=os.getenv("SHAREPOINT_SITE_ID") or None,
        max_photos_per_listing=_int_env("AIOI_MAX_PHOTOS_PER_LISTING", 20),
    )


def _int_env(name: str, default: int) -> int:
    """Read an int from the environment, falling back to `default` on missing/invalid."""
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default
