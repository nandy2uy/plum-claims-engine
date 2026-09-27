"""Run every mock document in samples/ through the REAL vision extractor and write docs/LIVE_EXTRACTION.md.

  python scripts/make_samples.py          # once
  python scripts/run_live_samples.py      # uses LLM_PROVIDER and its key from .env (GEMINI_API_KEY by default)

The report lists, per document: detected type, readability, the model's quality flags, each expected
field vs what was extracted, and the call metadata (model, prompt version, schema mode, latency,
tokens). Raw results are also saved to samples/live_results.json so the report is reproducible
without a key. Six calls; costs cents (or nothing on a free-tier Gemini key).
"""

import asyncio
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.config import get_policy, get_settings, llm_api_key, llm_key_env, llm_model  # noqa: E402
from src.core.llm import ExtractionError, VisionExtractor  # noqa: E402
from src.evals.samples import compare  # noqa: E402
from src.models.claim import DocumentSource  # noqa: E402


async def main() -> int:
    settings = get_settings()
    if not llm_api_key(settings):
        print(f"{llm_key_env(settings)} is not set (LLM_PROVIDER={settings.llm_provider}); nothing to run.")
        return 1
    samples_dir = ROOT / "samples"
    manifest_path = samples_dir / "manifest.json"
    if not manifest_path.exists():
        print("samples/manifest.json missing; run python scripts/make_samples.py first.")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = get_policy()
    extractor = VisionExtractor(settings, condition_vocabulary=list(policy.waiting_periods.specific_conditions)
                                      + list(policy.exclusions.conditions))

    async def run_one(entry):
        raw = (samples_dir / entry["file_name"]).read_bytes()
        doc = DocumentSource(file_id=entry["file_name"], file_name=entry["file_name"], file_type=entry["declared_type"],
                             content_base64=base64.b64encode(raw).decode(), mime_type=entry["mime_type"])
        try:
            result = (await extractor.extract(doc)).model_dump(mode="json")
            return {"entry": entry, "result": result, "checks": [c.as_dict() for c in compare(entry["expected"], result)]}
        except ExtractionError as exc:
            return {"entry": entry, "error": f"{exc.kind}: {exc}"}

    runs = [await run_one(e) for e in manifest]  # sequential: friendlier to free-tier rate limits
    (samples_dir / "live_results.json").write_text(json.dumps(runs, indent=2, ensure_ascii=False), encoding="utf-8")

    total = sum(len(r.get("checks", [])) for r in runs)
    ok = sum(c["ok"] for r in runs for c in r.get("checks", []))
    out = ["# Live extraction results", "",
           f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} with `{settings.llm_provider}` / `{llm_model(settings)}` by "
           f"`python scripts/run_live_samples.py`. Documents are the mock files in `samples/` "
           f"(rendered by `scripts/make_samples.py` following sample_documents_guide.md).", "",
           f"**Field accuracy: {ok}/{total} expected fields extracted correctly.**", "",
           "| Document | Variations | Detected | Readability | Fields correct | Latency | Tokens in/out | Schema mode |",
           "|---|---|---|---|---|---|---|---|"]
    for r in runs:
        e = r["entry"]
        if "error" in r:
            out.append(f"| {e['file_name']} | {', '.join(e['variations']) or 'clean'} | error | — | — | — | — | {r['error']} |")
            continue
        res, meta = r["result"], r["result"]["meta"]
        good = sum(c["ok"] for c in r["checks"])
        out.append(f"| {e['file_name']} | {', '.join(e['variations']) or 'clean'} | {res['detected_document_type']} | "
                   f"{res['readability']} | {good}/{len(r['checks'])} | {meta['latency_ms']} ms | "
                   f"{meta['input_tokens']}/{meta['output_tokens']} | {meta['schema_mode']} |")
    for r in runs:
        if "error" in r:
            continue
        out += ["", f"## {r['entry']['file_name']}", "",
                f"Quality flags: {', '.join(r['result']['quality_flags']) or 'none'}. "
                f"Unreadable fields: {', '.join(r['result']['unreadable_fields']) or 'none'}. "
                f"Model confidence: {r['result']['confidence']}.", "",
                "| Field | Expected | Extracted | OK |", "|---|---|---|---|"]
        out += [f"| {c['field']} | {c['expected']} | {c['actual']} | {'✅' if c['ok'] else '❌'} |" for c in r["checks"]]
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "LIVE_EXTRACTION.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"docs/LIVE_EXTRACTION.md written: {ok}/{total} fields correct")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
