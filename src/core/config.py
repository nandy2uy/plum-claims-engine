"""Runtime settings and fail-fast loading of the policy / interpretation files.

Component contract
-------------------
- get_settings() -> Settings            env vars / .env (secrets and infra knobs only)
- llm_api_key / llm_model / llm_base_url / llm_key_env(settings)
                                        the active vision provider's key, model, endpoint and the
                                        name of the env var that holds its key (gemini | openai)
- get_policy() -> PolicyConfig          parsed + validated data/policy_terms.json
- get_interpretation() -> Interpretation parsed + validated data/interpretation.json
- config_fingerprint(filename) -> str   short sha256 of the file bytes, written into
                                        every trace so a decision can be tied to the
                                        exact policy version that produced it.
Errors: ConfigError if a file is missing or does not validate. The API calls these
at startup, so a broken policy stops the service from booting instead of
mis-adjudicating claims with silent defaults (the v1 behaviour was to print a
warning and run with an empty dict).
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.models.policy import Interpretation, PolicyConfig

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
ENGINE_VERSION = "2.0.0"


class ConfigError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")

    # Vision model provider. Both are called through the OpenAI-compatible Chat Completions API
    # (Gemini exposes one at generativelanguage.googleapis.com/v1beta/openai/), so the extractor
    # code is identical; only the key, model and base URL change.
    llm_provider: Literal["gemini", "openai"] = "gemini"
    gemini_api_key: str = ""
    openai_api_key: str = ""
    llm_model: str = ""       # empty -> provider default below
    llm_base_url: str = ""    # empty -> provider default below
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_max_concurrency: int = 4

    # Upper bound on one whole pipeline stage (per-document LLM retries included).
    extraction_stage_timeout_seconds: float = 120.0
    stage_timeout_seconds: float = 10.0

    max_upload_mb: float = 10.0
    max_pdf_pages: int = 5
    store_max_records: int = 5000

    # Fault injection (ClaimSubmission.simulate_component_failure) lets a caller
    # force a component to fail. It is OFF by default: left on in production it
    # would let anyone switch off the fraud check on their own claim.
    allow_fault_injection: bool = False

    # Fixture documents (DocumentSource.pre_extracted_data) are the eval harness's way of
    # supplying extraction output without images. OFF by default: left on in production, any
    # caller could skip extraction and submit invented amounts and diagnoses.
    allow_fixture_documents: bool = False

    policy_file: str = "policy_terms.json"
    interpretation_file: str = "interpretation.json"
    test_cases_file: str = "test_cases.json"


PROVIDER_DEFAULTS = {
    "gemini": {"model": "gemini-3.8-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "key_env": "GEMINI_API_KEY"},
    "openai": {"model": "gpt-4o-mini", "base_url": "", "key_env": "OPENAI_API_KEY"},
}


def llm_api_key(settings: Settings) -> str:
    return settings.gemini_api_key if settings.llm_provider == "gemini" else settings.openai_api_key


def llm_model(settings: Settings) -> str:
    return settings.llm_model or PROVIDER_DEFAULTS[settings.llm_provider]["model"]


def llm_base_url(settings: Settings) -> str:
    return settings.llm_base_url or PROVIDER_DEFAULTS[settings.llm_provider]["base_url"]


def llm_key_env(settings: Settings) -> str:
    return PROVIDER_DEFAULTS[settings.llm_provider]["key_env"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _read_json(filename: str) -> dict:
    path = DATA_DIR / filename
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Required config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc


@lru_cache
def get_policy() -> PolicyConfig:
    filename = get_settings().policy_file
    try:
        return PolicyConfig.model_validate(_read_json(filename))
    except ValidationError as exc:
        raise ConfigError(f"{filename} failed validation:\n{exc}") from exc


@lru_cache
def get_interpretation() -> Interpretation:
    filename = get_settings().interpretation_file
    try:
        return Interpretation.model_validate(_read_json(filename))
    except ValidationError as exc:
        raise ConfigError(f"{filename} failed validation:\n{exc}") from exc


@lru_cache
def config_fingerprint(filename: str) -> str:
    return hashlib.sha256((DATA_DIR / filename).read_bytes()).hexdigest()[:12]


def load_test_cases() -> list:
    return _read_json(get_settings().test_cases_file).get("test_cases", [])


def reset_caches() -> None:
    """For tests that change env vars / settings."""
    for fn in (get_settings, get_policy, get_interpretation, config_fingerprint):
        fn.cache_clear()
