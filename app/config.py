"""Environment and mode resolution. Secrets are read from the environment (.env via python-dotenv) and never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

Mode = Literal["test", "live"]


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    traces_dir: Path = ROOT / "traces"
    state_dir: Path = ROOT / "state"
    fixtures_dir: Path = ROOT / "fixtures"
    test_key: str = os.environ.get("RECEIPT_TEST_KEY", "sk_test_receipt_demo")
    live_key: str = os.environ.get("RECEIPT_LIVE_KEY", "sk_live_receipt_demo")
    openrouter_api_key: Optional[str] = os.environ.get("OPENROUTER_API_KEY") or None
    openrouter_model: str = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
    discord_webhook_url: Optional[str] = os.environ.get("DISCORD_WEBHOOK_URL") or None
    sf_cli_alias: str = os.environ.get("SF_CLI_ALIAS", "dev")
    sf_api_version: str = os.environ.get("SF_API_VERSION", "67.0")
    airtable_pat: Optional[str] = os.environ.get("AIRTABLE_PAT") or None
    airtable_base_id: Optional[str] = os.environ.get("AIRTABLE_BASE_ID") or None

    def secrets(self) -> list[str]:
        """Values the tracer must redact wherever they appear."""
        out = [v for v in (self.openrouter_api_key, self.airtable_pat) if v]
        if self.discord_webhook_url:
            out.append(self.discord_webhook_url.rsplit("/", 1)[-1])  # webhook token
        return out


settings = Settings()


def resolve_mode(api_key: Optional[str]) -> Optional[Mode]:
    """Stripe-style: the key prefix decides which adapters run. Unknown key -> None (401)."""
    if not api_key:
        return None
    if api_key == settings.test_key and api_key.startswith("sk_test_"):
        return "test"
    if api_key == settings.live_key and api_key.startswith("sk_live_"):
        return "live"
    return None
