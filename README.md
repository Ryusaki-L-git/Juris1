# JURIS

JURIS is the AI/backend layer for **Lawyer's E-Diary (LED)**.

This repository is the **architecture-complete backend baseline**. It is designed so the LED client, authorization layer, case data, document retrieval, legal research, monitoring, and AI providers remain separate concerns.

## Core rule

**LED owns the user's case library. JURIS reads only the LED data the authenticated user is authorized to access.**

JURIS is not a second case database and does not own the eCourts credential. If LED synchronizes new eCourts information into a case, JURIS can read that stored information afterward.

## Architecture

```text
LED Flutter
    │
    │ Firebase ID token + structured context
    ▼
FastAPI
    │
    ▼
Identity / Authentication
    │
    ▼
JURIS Orchestrator
    ├── Context Resolver
    ├── Permission Engine
    ├── Authorized LED Access
    ├── Tool Registry
    ├── Confirmation Gate
    ├── Grounding / response policy
    └── AI Model Router
            ├── Managed AI
            ├── User BYOK
            ├── Gemini
            ├── OpenAI-compatible
            ├── Hugging Face
            └── Local OpenAI-compatible endpoint
```

## Important boundaries

- The model is **never** an authorization authority.
- Every tool call is permission-checked server-side.
- Team case access follows LED's canonical `ownerUid`, `teamOwnerUid`, `teamId`, and `assignedTo` fields.
- `assignedUserUid` is legacy compatibility only.
- eCourts access is read-only and must resolve through an authorized LED case.
- Write tools require explicit user confirmation before execution.
- Raw API keys never enter prompts or normal responses.
- JURIS does not invent legal authorities, case facts, or document text.
- Local-first LED documents are unavailable to JURIS until an authorized document-sharing/ingestion boundary exposes their text.

## Local development

Requirements: Python 3.12+ and `uv`.

```powershell
uv sync
uv run uvicorn main:app --reload
```

Open:

- `/docs` — API documentation
- `/health` — service health

The default gateway is `echo`, which makes **no external AI request** and requires no API key. This is intentional: the project can boot and its security/tool pipeline can be tested without provider credentials.

To use Gemini, set `JURIS_GATEWAY=gemini` and provide `JURIS_GEMINI_API_KEY`.

To use an OpenAI-compatible provider, set `JURIS_GATEWAY=openai_compatible` and configure its base URL/key/model.

## Product BYOK

BYOK is disabled by default. A production deployment must configure:

- Firebase project access
- `JURIS_BYOK_ENABLED=true`
- a strong `JURIS_CREDENTIAL_ENCRYPTION_KEY` supplied by a deployment secret manager/KMS

Keys are encrypted before Firestore storage and decrypted only at the provider boundary.

## Official-source research (initial coverage)

JURIS can retrieve the Supreme Court of India's Chief Justice and Judges page for supported Chief Justice of India/current-office-holder questions. Results include the official URL, UTC retrieval timestamp, source host, and verification method. The provider uses a code-owned source registry, HTTPS-only requests, redirect allowlisting, a response-size cap, and a bounded timeout; user queries cannot supply arbitrary URLs.

Configuration:

- `JURIS_OFFICIAL_RESEARCH_ENABLED=true` enables the provider (default in this build).
- `JURIS_OFFICIAL_RESEARCH_TIMEOUT_SECONDS=8` bounds each source request (clamped to 1–15 seconds).

This is deliberately limited coverage, not general web search. A missing result means JURIS could not verify the requested fact from its configured sources; it must not fill the gap with model memory or invented citations. Add new official sources only through a reviewed code change and regression tests. Retrieved page text is evidence, never instructions.

## Tests

```powershell
uv run pytest -q
```

The repository's test suite covers authentication contracts, authorization, case access, eCourts read-only behavior, model gateways, entitlements/tokens, monitoring, and end-to-end orchestration.

## What still requires external integration

These are intentionally **interfaces**, not fake implementations:

1. LED document ingestion/OCR/retrieval implementation.
2. Expanded official legal-research coverage. A controlled provider now retrieves the Supreme Court of India's Chief Justice and Judges page for matching CJI/current-office-holder queries. Other laws and legal topics remain unsupported until reviewed official sources are added to the registry and tested.
3. Production Firebase credentials and deployment configuration.
4. Production managed-AI credentials.
5. LED-side write APIs for controlled actions such as reminders/drafts.

JURIS does not simulate any of these capabilities with fake legal or case data.
