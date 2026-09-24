import json
from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

# We resolve paths relative to the project root so it never breaks
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    """Runtime configuration sourced from environment variables / .env.

    Policy and interpretation data live in JSON files (see load_json_file
    below) — this class is only for secrets and infra knobs, per the
    assignment's "don't hardcode policy logic" instruction.
    """

    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_json_file(filename: str) -> dict:
    file_path = DATA_DIR / filename
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"CRITICAL: Could not find {filename} at {file_path}")
        return {}


@lru_cache
def get_policy_config() -> dict:
    return load_json_file("policy_terms.json")


@lru_cache
def get_interpretation_config() -> dict:
    return load_json_file("interpretation.json")
