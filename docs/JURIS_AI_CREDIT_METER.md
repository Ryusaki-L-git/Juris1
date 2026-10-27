# JURIS AI Credit estimate and approval flow

## User experience

1. LED sends the message to the authenticated JURIS /chat endpoint without an approval token.
2. For managed Gemini requests, JURIS estimates a conservative per-call budget from the serialized model request, the configured output-token cap, the reviewed model rate card, USD/INR planning rate, margin, and reservation buffer.
3. JURIS multiplies the per-call reservation by the configured maximum tool/model rounds and returns status=confirmation_required with a server-issued, single-use quote token. No model call or Credit debit occurs during this preview.
4. LED displays the maximum approved Credit budget and requires the user to select Continue.
5. LED resubmits the same message, session and selected model with the quote token. JURIS verifies the token is unexpired and bound to the same authenticated user, session, message and model, then consumes it once.
6. Each managed Gemini call reserves no more than the remaining approved budget. Final charge is reconciled from provider usage metadata, rounded up to paise, and capped at the approved remaining budget. Unused reservations are released by the existing settlement flow.
7. If usage reaches the approved budget, JURIS stops before another model call. It does not silently charge beyond the approved maximum. A new request requires a fresh estimate and approval.

## Security and limitations

- Quotes expire after five minutes and are single-use.
- Production quotes are stored server-side in the _juris_pending_ai_usage Firestore collection; client-supplied prices or balances are never trusted.
- The collection is accessed by the server's Firestore client, not directly by LED clients. Configure Firestore rules to deny client access to underscore-prefixed server collections.
- The estimator uses a conservative character-to-token approximation; it is a budget ceiling, not a provider guarantee. The actual amount charged is based on reported provider usage when available.
- If a provider reports usage that exceeds the current per-call reservation, JURIS caps that charge at the reserved amount and stops before executing requested tools or making another model call. This can under-recover provider cost, but prevents a surprise charge beyond the user's approval.
- BYOK and non-Gemini requests remain outside managed Gemini billing. eCourts Case Tokens remain separate.
- Payment/top-up activation, production deployment and live provider testing are not part of this change.
