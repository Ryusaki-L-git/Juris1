# JURIS Architecture — Final Baseline

## 1. System ownership

```text
LED = source of truth for user case/client/hearing/document records
JURIS = authorized intelligence + orchestration layer
LED eCourts integration = external court-data ingestion/update owner
```

JURIS never creates a parallel case repository.

## 2. Request pipeline

```text
LED
 ↓
FastAPI
 ↓
Firebase identity verification
 ↓
RequestContext resolution
 ↓
AuthorizedLedAccess
 ↓
JurisOrchestrator
 ├─ server permission check
 ├─ tool selection/execution
 ├─ confirmation gate for writes
 ├─ grounded tool results
 └─ AIModelRouter
      ├─ Managed credentials
      ├─ BYOK vault
      └─ provider gateway
 ↓
ChatResponse
 ↓
LED
```

## 3. Case authorization

Canonical case fields:

- `ownerUid`
- `teamOwnerUid`
- `teamId`
- `assignedTo`
- `assignedToName`

Rules:

- owner → access
- team owner → access to team-owned cases
- assigned active member → access to the assigned case
- unassigned case → other members do not automatically gain access
- legacy `assignedUserUid` → never used for production authorization

The permission engine consumes `CaseAccess` evidence produced by the LED data-access layer. It does not trust model claims or client-supplied ownership claims.

## 4. eCourts boundary

```text
eCourts
   ↓
LED server-side eCourts integration
   ↓
LED case document / ecourts_data
   ↓
JURIS authorized case lookup
   ↓
JURIS analysis
```

JURIS has no eCourts API credential and does not write to eCourts.

A CNR request must first resolve to a case the caller can access. This prevents arbitrary CNR enumeration.

## 5. AI architecture

```text
AIModelSelection
       ↓
AIModelRouter
       ├── ManagedCredentialResolver
       ├── FirestoreBYOKVault
       └── provider gateway
             ├── GeminiGateway
             └── OpenAICompatibleGateway
                    ├── OpenAI-compatible services
                    ├── Hugging Face router
                    └── local/self-hosted endpoints
```

The orchestrator never imports a provider SDK.

## 6. BYOK security

The API key lifecycle is:

```text
LED authenticated request
       ↓
JURIS BYOK endpoint
       ↓
encrypt with deployment secret
       ↓
Firestore user/provider credential record
       ↓
decrypt only in provider resolver
       ↓
provider gateway
```

Keys are never placed in prompts, model metadata, debug responses, or Firestore case records.

## 7. Documents / RAG boundary

```text
LED local/cloud document
       ↓
authorized ingestion boundary
       ↓
text extraction / OCR
       ↓
chunking + metadata
       ↓
DocumentRetriever
       ↓
grounded context
       ↓
JURIS model
       ↓
source/citation verification
```

The current repository exposes the retrieval contract without inventing document text or silently reading local files.

## 8. Legal research boundary

Legal research uses a provider-neutral contract. A provider must return source-backed results. If no provider is configured, JURIS returns an explicit unavailable result rather than fabricating law or citations.

## 9. Controlled actions

The model may request a write tool, but it cannot authorize its own action.

```text
model requests write
      ↓
server permission check
      ↓
server-issued confirmation ID
      ↓
LED user confirms
      ↓
JURIS executes tool
```

## 10. Monitoring

Monitoring stores references to LED cases and is user-scoped. Firestore is used when configured; local development can use an in-memory repository.

Monitoring must never become a second source of truth for case facts.

## 11. Production principles

- fail closed on authorization errors
- no fake legal data
- no fake case data
- no direct model-to-Firestore access
- no direct model-to-eCourts access
- no provider credentials in source control
- bounded tool rounds
- bounded query limits
- explicit confirmation for side effects
- durable user-scoped monitoring
- provider/model abstraction independent of the orchestrator
