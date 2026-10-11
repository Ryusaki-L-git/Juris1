# JURIS Security Patch Notes

This patch is based on `Juris-FINAL.zip`. It does not include a live `.env` file and does not change any external LED or Firebase deployment.

## Implemented

1. **Write confirmations** (`core/action_confirmations.py`, `agent/orchestrator.py`)
   - Replaced predictable deterministic confirmation IDs with cryptographically random, expiring tokens.
   - Tokens are bound to the authenticated user, chat session, tool name, and exact tool arguments.
   - Tokens are single-use. Production uses a Firestore transaction to consume a token atomically; development uses an in-memory store.
   - Tokens expire after five minutes. Configure Firestore TTL on `_juris_pending_actions.expires_at` to clean up expired documents automatically.

2. **Action-specific permissions** (`core/permission_engine.py`)
   - Missing case IDs no longer authorize update, delete, draft, or arbitrary actions.
   - Document drafting now requires an accessible case and a valid `JURIS_ACCESS` entitlement.
   - Client reads verify the owner-scoped client exists and is not soft-deleted.
   - Non-read actions are denied for clients, hearings, teams, and legal research.

3. **LED data-access consistency** (`core/led_access.py`)
   - Team-owner and assigned-case access require an active membership in an explicitly identified team.
   - Team case listings require the requesting user's active membership and still filter each case through case-level access rules.
   - Direct owned-case reads and lists filter soft-deleted cases.
   - Direct client reads reject soft-deleted clients.

4. **Monitoring authorization** (`api/monitoring.py`, `monitoring/scheduler.py`)
   - Removed the development-mode bypass for paid monitoring entitlements.
   - Monitor creation checks the authenticated user's case access and requires the supplied CNR to match that case's canonical stored CNR.
   - Each scheduled check revalidates case access, CNR binding, and current server-owned entitlement before reading case state or dispatching a notification.
   - Invalid or mismatched provider CNR data pauses monitoring.

5. **Monitoring store isolation and limits** (`monitoring/store.py`, `monitoring/firestore_store.py`)
   - In-memory monitor reads/deletes enforce the supplied user ID.
   - The active-monitor limit counts active records, matching the API's message.
   - Firestore scheduler queries filter collection-group results to the `_juris_monitors` path and validate the record user against the path.

6. **Regression tests** (`tests/test_security_regressions.py`, `tests/test_monitoring.py`, `tests/test_juris_end_to_end.py`)
   - Added tests for missing-resource permission bypasses, deleted-client access, unverified identities, user-bound single-use confirmations, monitoring access loss, CNR mismatch, and user-scoped monitor deletion.

## Verification performed

- Python syntax compilation: passed.
- Application import: passed.
- Automated test suite: **92 passed** in the audit environment.

## Required before production use

- Configure `JURIS_FIREBASE_PROJECT_ID` and server credentials for the intended Firebase project.
- Configure Firestore TTL for `_juris_pending_actions.expires_at`.
- Ensure client-facing Firestore security rules deny client reads/writes to server-owned collections, including `_juris_pending_actions`, `_server_entitlements`, and finance/ledger collections. Admin SDK access bypasses Firestore rules, but client SDK access does not.
- Confirm the LED Firestore field names, team membership schema, case assignment policy, and deployed security rules against the actual LED application.
- Test with separate Firebase users and non-production records before enabling monitoring or any write integration.
- Review Firebase token revocation policy, production CORS allowlist, rate limits, logging, and deployment secrets separately; this patch does not certify those controls.

## Important boundary

This archive is a patched code copy, not an update to the local `C:\Users\Zafarullah khan\Juris` folder or to any deployed service. Review the diff and merge/copy changes into the working project only after backing it up. No live Firebase project or real client/case records were accessed during these tests.


## Official-source retrieval implementation

- Added `legal_research/official_web.py`, a provider using a code-owned allowlist of official URLs; it does not accept caller-supplied URLs or perform arbitrary open-web search.
- Initial coverage: the Supreme Court of India's Chief Justice and Judges page for supported CJI/current-office-holder queries.
- Results include the source URL/name, retrieval timestamp, official HTTPS host, and verification method.
- Requests use bounded timeouts, response-size limits, and redirect-host revalidation. Failed/unsupported retrieval returns no result rather than inventing authority.
- Added `JURIS_OFFICIAL_RESEARCH_ENABLED` and `JURIS_OFFICIAL_RESEARCH_TIMEOUT_SECONDS` configuration.
- Updated tool descriptions and system policy to require retrieval for current legal facts and to treat source text as untrusted evidence.
- Added regression tests for a successful CJI retrieval, unsupported queries, and redirects outside the allowlist.
- Verification in the available Python 3.13 environment: **95 passed**. The archive's configured Python 3.14 interpreter was not available in this test environment, so `uv run` could not be used here; the test suite was run directly with Python 3.13 and installed dependencies.

Coverage limitation: this is the first narrow source integration, not a comprehensive Indian-law research engine. More official sources and act/judgment search adapters need separate review and tests.
