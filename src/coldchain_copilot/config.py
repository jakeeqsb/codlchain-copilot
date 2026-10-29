"""Runtime settings read from environment variables and ``.env``.

Only ``MONGODB_URI`` is required at startup. API keys are checked when the
code that needs them runs, so seeding and detection work without Voyage AI or
OpenAI credentials.

Typical usage:

    settings = get_settings()
    client = MongoClient(settings.mongodb_uri)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """A required environment variable is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one process.

    Attributes:
        mongodb_uri: Connection string for the MongoDB deployment.
        db_name: Database that holds all project collections.
        voyage_api_key: Voyage AI key for embeddings, if set.
        voyage_embed_model: Voyage model used for documents and queries.
        openai_api_key: OpenAI key for the briefing LLM, if set.
        openai_model: OpenAI chat model with tool calling.
        max_window_hours: Upper bound for fleet- or reefer-scoped sensor
            queries. Shipment-scoped queries use the shipment period instead.
        data_dir: Directory of the catalog JSON files. Relative paths
            resolve against the working directory.
        docs_dir: Directory of the source Markdown documents. Relative paths
            resolve against the working directory.
    """

    mongodb_uri: str
    db_name: str = "coldchain_copilot"
    voyage_api_key: str | None = None
    voyage_embed_model: str = "voyage-4"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    max_window_hours: int = 72
    data_dir: Path = Path("data")
    docs_dir: Path = Path("docs")

    def require_voyage_key(self) -> str:
        """Returns the Voyage AI key.

        Raises:
            ConfigError: ``VOYAGE_API_KEY`` is not set.
        """
        return _require("VOYAGE_API_KEY", self.voyage_api_key)

    def require_openai_key(self) -> str:
        """Returns the OpenAI key.

        Raises:
            ConfigError: ``OPENAI_API_KEY`` is not set.
        """
        return _require("OPENAI_API_KEY", self.openai_api_key)


def _require(name: str, value: str | None) -> str:
    """Returns ``value`` or raises a ConfigError naming the missing variable."""
    if not value:
        raise ConfigError(f"{name} is not set. Add it to .env (see .env.example).")
    return value


def _positive_int(name: str, raw: str | None, default: int) -> int:
    """Parses an optional positive integer variable.

    Args:
        name: Variable name, used in error messages.
        raw: Raw value, or None when unset.
        default: Value used when ``raw`` is unset or blank.

    Returns:
        The parsed integer, or ``default``.

    Raises:
        ConfigError: ``raw`` is not an integer or is not positive.
    """
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from None
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}.")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Builds Settings from environment variables.

    Args:
        env: Variables to read. Defaults to ``os.environ`` after loading
            ``.env`` from the working directory. Tests pass a dict.

    Returns:
        Settings with defaults filled in for unset optional variables.

    Raises:
        ConfigError: ``MONGODB_URI`` is missing or ``MAX_WINDOW_HOURS`` is
            invalid.
    """
    if env is None:
        load_dotenv()
        env = os.environ

    def get(name: str) -> str | None:
        value = env.get(name)
        return value.strip() if value and value.strip() else None

    defaults = Settings(mongodb_uri="")
    return Settings(
        mongodb_uri=_require("MONGODB_URI", get("MONGODB_URI")),
        db_name=get("DB_NAME") or defaults.db_name,
        voyage_api_key=get("VOYAGE_API_KEY"),
        voyage_embed_model=get("VOYAGE_EMBED_MODEL") or defaults.voyage_embed_model,
        openai_api_key=get("OPENAI_API_KEY"),
        openai_model=get("OPENAI_MODEL") or defaults.openai_model,
        max_window_hours=_positive_int(
            "MAX_WINDOW_HOURS", env.get("MAX_WINDOW_HOURS"), defaults.max_window_hours
        ),
        data_dir=Path(get("DATA_DIR") or defaults.data_dir),
        docs_dir=Path(get("DOCS_DIR") or defaults.docs_dir),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Returns process-wide settings, loading them on first call.

    Raises:
        ConfigError: See :func:`load_settings`.
    """
    return load_settings()