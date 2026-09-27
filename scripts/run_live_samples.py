"""Run the mock documents in samples/ through the REAL vision extractor and write docs/LIVE_EXTRACTION.md.

  python scripts/make_samples.py                  # once
  python scripts/run_live_samples.py              # all six documents
  python scripts/run_live_samples.py --only 04_handwritten_prescription.png 05_altered_pharmacy_bill.png
                                                  # re-run just these; other results are kept

Uses LLM_PROVIDER and its key from .env (GEMINI_API_KEY by default).

Free-tier friendly: documents are sent one at a time with a pause between them (--delay, default 13s,
which fits a 5-requests-per-minute quota), and a document that hits a temporary provider error
(429 quota, 503 overloaded) is retried after waiting (--attempts, default 3). Results accumulate in
samples/live_results.json, so a re-run with --only merges into the earlier results and the report
always shows every document.
"""

import argparse
import asyncio
import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.config import get_policy, get_settings, llm_api_key, llm_key_env, llm_model  # noqa: E402
from src.core.llm import ExtractionError, VisionExtractor, retry_wait_seconds  # noqa: E402
from src.evals.samples import compare  # noqa: E402
from src.models.claim import DocumentSource  # noqa: E402

SAMPLES = ROOT / "samples"
RESULTS = SAMPLES / "live_results.json"
REPORT = ROOT / "docs" / "LIVE_EXTRACTION.md"


def short_error(message: str) -> str:
    """One readable line for the summary table; the full text goes in the Errors section."""
    lowered = message.lower()
    if "429" in message or "quota" in lowered or "resource_exhausted" in lowered:
        return "Rate limit / quota (429)"
    if "503" in message or "unavailable" in lowered or "high demand" in lowered:
        return "Provider overloaded (503)"
    if "not configured" in lowered:
        return "No API key"
    return message.split(":")[0][:60]


async def run_one(extractor: VisionExtractor, entry: dict, attempts: int, delay: float) -> dict:
    raw = (SAMPLES / entry["file_name"]).read_bytes()
    doc = DocumentSource(file_id=entry["file_name"], file_name=entry["file_name"], file_type=entry["declared_type"],
                         content_base64=base64.b64encode(raw).decode(), mime_type=entry["mime_type"])
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            result = (await extractor.extract(doc)).model_dump(mode="json")
            checks = [c.as_dict() for c in compare(entry["expected"], result)]
            ok = sum(c["ok"] for c in checks)
            print(f"  {entry['file_name']}: {ok}/{len(checks)} fields correct")
            return {"entry": entry, "result": result, "checks": checks, "attempts": attempt,
                    "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        except ExtractionError as exc:
            last_error = f"{exc.kind}: {exc}"
            if exc.kind != "SYSTEM" or attempt == attempts:
                break
            wait = max(delay, retry_wait_seconds(exc, attempt) + 2)
            print(f"  {entry['file_name']}: {short_error(str(exc))}; waiting {wait:.0f}s before attempt {attempt + 1}")
            await asyncio.sleep(wait)
    print(f"  {entry['file_name']}: failed ({short_error(last_error)})")
    return {"entry": entry, "error": last_error, "attempts": attempts,
            "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def render(runs: list, settings) -> str:
    total = sum(len(r.get("checks", [])) for r in runs)
    ok = sum(c["ok"] for r in runs for c in r.get("checks", []))
    failed = [r for r in runs if "error" in r]
    out = ["# Live extraction results", "",
           f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} with `{settings.llm_provider}` / "
           f"`{llm_model(settings)}` by `python scripts/run_live_samples.py`. Documents are the mock files in "
           f"`samples/` (rendered by `scripts/make_samples.py` following sample_documents_guide.md): a clean "
           f"prescription and bill, a skewed and shadowed phone photo, a handwritten-style prescription with "
           f"shorthand and a stamp, a pharmacy bill with a crossed-out amount, and a two-page scanned PDF.", "",
           f"**Field accuracy: {ok}/{total} expected fields extracted correctly across "
           f"{len(runs) - len(failed)} of {len(runs)} documents.**", "",
           "| Document | Variations | Detected | Readability | Fields correct | Latency | Tokens in/out | Schema mode |",
           "|---|---|---|---|---|---|---|---|"]
    for r in runs:
        e = r["entry"]
        variations = ", ".join(e["variations"]) or "clean"
        if "error" in r:
            out.append(f"| {e['file_name']} | {variations} | not processed | — | — | — | — | {short_error(r['error'])} |")
            continue
        res, meta = r["result"], r["result"]["meta"]
        good = sum(c["ok"] for c in r["checks"])
        out.append(f"| {e['file_name']} | {variations} | {res['detected_document_type']} | {res['readability']} | "
                   f"{good}/{len(r['checks'])} | {meta['latency_ms']} ms | {meta['input_tokens']}/{meta['output_tokens']} | "
                   f"{meta['schema_mode']} |")
    for r in runs:
        if "error" in r:
            continue
        res = r["result"]
        out += ["", f"## {r['entry']['file_name']}", "",
                f"Quality flags: {', '.join(res['quality_flags']) or 'none'}. "
                f"Unreadable fields: {', '.join(res['unreadable_fields']) or 'none'}. "
                f"Model confidence: {res['confidence']}.", "",
                "| Field | Expected | Extracted | OK |", "|---|---|---|---|"]
        out += [f"| {c['field']} | {c['expected']} | {c['actual']} | {'✅' if c['ok'] else '❌'} |" for c in r["checks"]]
    if failed:
        out += ["", "## Documents not processed", "",
                "These failed for provider-side reasons (quota or availability), not extraction accuracy. In the "
                "claims pipeline the same errors are classified as SYSTEM faults: the claim goes to manual review "
                "and the member is never asked to re-upload. Re-run them with "
                "`python scripts/run_live_samples.py --only <file>`.", ""]
        out += [f"- `{r['entry']['file_name']}` after {r.get('attempts', 1)} attempt(s): {r['error'][:300]}" for r in failed]
    return "\n".join(out) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", metavar="FILE", help="re-run only these sample files")
    parser.add_argument("--delay", type=float, default=13.0, help="seconds between documents (default 13)")
    parser.add_argument("--attempts", type=int, default=3, help="attempts per document on provider errors")
    args = parser.parse_args()

    settings = get_settings()
    if not llm_api_key(settings):
        print(f"{llm_key_env(settings)} is not set (LLM_PROVIDER={settings.llm_provider}); nothing to run.")
        return 1
    manifest_path = SAMPLES / "manifest.json"
    if not manifest_path.exists():
        print("samples/manifest.json missing; run python scripts/make_samples.py first.")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    names = [e["file_name"] for e in manifest]
    if args.only:
        unknown = [n for n in args.only if n not in names]
        if unknown:
            print(f"Unknown sample(s): {', '.join(unknown)}. Available: {', '.join(names)}")
            return 1
    targets = [e for e in manifest if not args.only or e["file_name"] in args.only]

    previous = {}
    if RESULTS.exists():
        previous = {r["entry"]["file_name"]: r for r in json.loads(RESULTS.read_text(encoding="utf-8"))}

    policy = get_policy()
    extractor = VisionExtractor(settings, condition_vocabulary=list(policy.waiting_periods.specific_conditions)
                                + list(policy.exclusions.conditions))
    print(f"Extracting {len(targets)} document(s) with {settings.llm_provider} / {llm_model(settings)}, "
          f"{args.delay:.0f}s apart...")
    for index, entry in enumerate(targets):
        if index:
            time.sleep(args.delay)
        previous[entry["file_name"]] = await run_one(extractor, entry, args.attempts, args.delay)

    runs = [previous[n] for n in names if n in previous]
    RESULTS.write_text(json.dumps(runs, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(render(runs, settings), encoding="utf-8")
    total = sum(len(r.get("checks", [])) for r in runs)
    ok = sum(c["ok"] for r in runs for c in r.get("checks", []))
    failed = [r["entry"]["file_name"] for r in runs if "error" in r]
    print(f"docs/LIVE_EXTRACTION.md written: {ok}/{total} fields correct"
          + (f"; not processed: {', '.join(failed)}" if failed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
