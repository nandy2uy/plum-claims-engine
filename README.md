# Plum AI Claims Processing Engine

A production-ready, deterministic claims processing pipeline featuring strict module boundaries between probabilistic LLM perception and deterministic financial adjudication. 

Built for the Plum AI Engineer Take-Home Assignment.

## 🏗️ Architecture Overview

The system is designed around a core principle: **Perception is probabilistic, judgment is deterministic.** 
LLMs are exclusively used to extract unstructured data from raw images into strictly typed Pydantic models. A deterministic Python rule engine calculates the exact financial step-down waterfalls and handles policy logic. 

1. **Intake & Document Gate:** Validates payloads and runs a cheap, deterministic check for document completeness and image blurriness. 
2. **Hybrid Extraction Router:** Toggles between live OpenAI Vision (`gpt-4o-mini`) for real-world document OCR, and local fixtures for zero-latency, deterministic CI/CD testing.
3. **Consistency Agent:** Cross-references extracted entities (e.g., patient names) using token-set matching to detect cross-document mismatches.
4. **Policy Adjudicator:** A pure Python rule engine enforcing limits, co-pays, and network discounts using `Decimal` math to prevent floating-point currency errors.
5. **Fraud & Risk Agent:** Tracks historical claim frequency and high-value thresholds, flagging suspicious claims for human escalation.

## 🛡️ Fault Tolerance & Observability

*   **Graceful Degradation:** Agents are wrapped in an `@isolate_fault` decorator. If an LLM times out or an OCR API crashes, the system degrades state, lowers the confidence score, and routes the claim to `MANUAL_REVIEW` instead of crashing the server.
*   **Trace Ledger:** Every single agent drops strongly typed `TraceEvent` objects into a central ledger. The final decision is perfectly explainable because the entire audit trail is returned in the API response.

## 🚀 Quickstart

**1. Install Dependencies**
```bash
python -m venv venv
source venv/bin/activate  # Or `venv\Scripts\activate` on Windows
pip install fastapi uvicorn pydantic requests openai 