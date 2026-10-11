# JURIS Production Deployment & Operations Guide

## Overview

JURIS is the AI and legal intelligence backend for Lawyer's E-Diary (LED).
This document details the production deployment architecture, secrets management, HTTPS configuration, and phone testing connectivity.

---

## 1. Architecture

```
Android Device (LED App)
         │
         ├─── (Firebase Callable) ───► LED Cloud Functions (Holds eCourts API Key)
         │                                       │
         │                                       ▼
         │                                eCourts India API
         │                                       │
         │                                       ▼
         │                               Firestore (cases/)
         │                                       ▲
         │ (HTTPS / TLS 1.3)                     │
         ▼                                       │
https://juris-backend.onrender.com               │
         │                                       │
    ┌────┴───────────────────────────┐           │
    │       JURIS FastAPI API        │           │
    │  - Firebase Auth Token Verify  │           │
    │  - Server Permission Engine    │           │
    │  - Automatic Case Monitoring   │───────────┘ (Reads LED Firestore)
    │  - Legal Context Resolution    │
    └────┬───────────────────────────┘
         │
         ▼
    Configured JURIS AI provider (Gemini, OpenAI-compatible, Hugging Face, or self-hosted endpoint)
```

---

## 2. Recommended Deployment Platform: Render (Free Tier with HTTPS)

Render provides:
- Fully managed Docker runtime
- Automatic free SSL/TLS certificate (HTTPS)
- Zero infrastructure maintenance
- Native healthcheck monitoring (`/health`)
- Secure environment secrets management

### Step-by-Step Deployment:

1. **Push repository to GitHub**:
   Ensure `Juris` is pushed to your private GitHub repository.
   Verify that `.env` is **NOT** tracked (already enforced by `.gitignore`).

2. **Log into Render Dashboard** ([render.com](https://render.com)):
   - Click **New +** → **Web Service**
   - Connect your GitHub repository (`Juris`)
   - Select **Docker** as the Environment (it will automatically detect `Dockerfile`)
   - Plan: **Free**

3. **Configure Environment Variables / Secrets**:
   Under the **Environment** tab on Render, add the following variables:

   | Variable | Value / Notes |
   |---|---|
   | `JURIS_ENV` | `production` |
   | `JURIS_DEBUG` | `false` |
   | `JURIS_AUTH_MODE` | `firebase` |
   | `JURIS_DEV_ALLOW_UNAUTHENTICATED` | `false` |
   | `JURIS_GATEWAY` | `echo` for credential-free startup, or `gemini` / `openai_compatible` when configured |
   | `JURIS_GEMINI_MODEL` | `gemini-3.8-flash` |
   | `JURIS_GEMINI_API_KEY` | *(Only when Gemini is selected)* |
   | `JURIS_FIREBASE_PROJECT_ID` | `lawyers-e-diary` *(or your Firebase project ID)* |
   | `JURIS_MONITORING_ENABLED` | `true` |
   | `JURIS_MONITOR_INTERVAL_MINUTES` | `180` |
   | `JURIS_ALLOWED_ORIGINS_RAW` | `https://app.lawyersediary.com` |

   *(NOTE: JURIS does NOT hold an eCourts credential. The eCourts key lives strictly in the LED-side Cloud Function secrets).*

4. **Click "Create Web Service"**:
   Render will build the Docker image, run dependency installations, and publish the HTTPS endpoint:
   `https://juris-backend.onrender.com`

---

## 3. Connecting the Android Phone (LED Flutter App)

To test on a physical Android phone:

1. In the Flutter application ([`lib/services/juris_service.dart`](file:///c:/Users/Zafarullah%20khan/lawyers_e_diary/lib/services/juris_service.dart)):
   Set the production backend URL:
   ```dart
   final jurisService = JurisService(baseUrl: 'https://juris-backend.onrender.com');
   ```
   Or set it dynamically based on build flavor/environment.

2. On the phone, sign into Lawyer's E-Diary with a real Firebase account.
3. The app will obtain the user's Firebase ID token and send it via `Authorization: Bearer <id_token>` to `https://juris-backend.onrender.com/chat`.
4. The production JURIS backend verifies the cryptographic signature with Google's public JWKS keys, resolves the `JurisUser`, enforces permissions, and calls Gemini `gemini-3.8-flash`.

---

## 4. Cold-Start Note (Render Free Tier)

On the free tier, Render automatically spins down services after 15 minutes of inactivity:
- The first request after a sleep period takes ~30–45 seconds to spin up.
- Subsequent requests respond in sub-second speeds.
- The Flutter `JurisService` timeout is set to 45 seconds (`Duration(seconds: 45)`) to accommodate this startup window safely.

---

## 5. Security Checklist

- [x] No API keys in source code
- [x] No `.env` files in git
- [x] Strict CORS configured (no wildcard `*` allowed in production)
- [x] Unauthenticated dev fallback disabled in production (`dev_allow_unauthenticated=false`)
- [x] JURIS does not hold or import any eCourts credentials
- [x] eCourts API key is held exclusively in LED-side server infrastructure (Cloud Functions)
- [x] JURIS reads only user-authorized case data from Firestore
- [x] Server-authoritative entitlement and permission checks
