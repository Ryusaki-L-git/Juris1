# JURIS Production Readiness Status

This document separates repository-level implementation from release verification. **This ZIP is not a certification that a live deployment is production-ready.** External configuration, live LED schema verification, security review, and deployment acceptance tests remain mandatory.

## Included in this handoff

- FastAPI JURIS backend and existing security/permission architecture.
- Gemini gateway tool-call signature preservation (`thoughtSignature`) and corrected HTTP 400 classification.
- Existing official-source research provider and regression suite.
- Deployment templates and environment-variable template.
- LED Flutter integration starter: authenticated `JurisService`, basic JURIS chat screen, and `/juris` route wired to that screen in the included LED snapshot.
- A production integration checklist and acceptance test plan.

## Verified in this build

- Python automated test suite: `96 passed` in the build environment.
- The Gemini gateway has unit tests for signature round-trip preservation.
- Python test results do not verify Flutter compilation, Firebase project configuration, external API credentials, or the live deployed service.

## Mandatory before real confidential data

1. Configure production environment secrets in the hosting provider. Never copy `.env` into this ZIP or commit it.
2. Set `JURIS_ENV=production`, `JURIS_AUTH_MODE=firebase`, `JURIS_DEV_ALLOW_UNAUTHENTICATED=false`, and the correct Firebase project ID. Verify the startup config actually reads these names.
3. Configure the production AI gateway and a model ID verified against the chosen provider/API key. Model IDs in templates are examples and may need updating.
4. Verify server-side authorization against the actual LED Firestore rules and canonical case/team/assignment fields. The supplied LED ZIP is a pre-implementation snapshot and may not match the current working repository.
5. Configure the LED build with `--dart-define=JURIS_BASE_URL=https://YOUR-REAL-HTTPS-HOST`.
6. Run `flutter pub get`, `flutter analyze`, and `flutter test` from the LED project on the actual development machine. This build environment did not run Flutter checks.
7. Deploy to staging and test valid/expired/revoked tokens, two-user case isolation, team removal, denied access, provider outage, legal research, rate limits, logs, and rollback.
8. Do not enable automatic monitoring, controlled write actions, BYOK, or document ingestion until each subsystem has its own tested production configuration and authorization review.

## Known integration limitations

- The starter chat sends `current_screen: unknown` and does not yet attach an active case or selected document. Case-aware context passing must be implemented in the existing LED case-detail flows after checking the current repository's navigation and model IDs.
- The UI displays the backend reply as text. Structured citations and clickable source cards are a follow-up UI task; raw backend output should not be treated as verified legal advice unless source provenance is present.
- The chat service currently uses a per-screen session ID and does not persist conversation history locally. Durable conversation history/retention must be designed and tested before claiming persistent memory.
- This snapshot's route change replaces the `/juris` placeholder only in the included LED archive; it does not modify the user's actual working folder.
- The JURIS backend must independently enforce authorization. The client must never be trusted to authorize case access.

## Final release gate

A release is blocked if any critical/high security finding remains open, any unauthorized cross-user access succeeds, the deployed legal-research flow fabricates a result after a tool failure, or rollback/secret rotation has not been rehearsed.
