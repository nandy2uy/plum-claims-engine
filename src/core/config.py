import json
from pathlib import Path

# We resolve paths relative to the project root so it never breaks
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

def load_json_file(filename: str) -> dict:
    file_path = DATA_DIR / filename
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"CRITICAL: Could not find {filename} at {file_path}")
        return {}

def get_policy_config() -> dict:
    return load_json_file("policy_terms.json")

def get_interpretation_config() -> dict:
    return load_json_file("interpretation.json")