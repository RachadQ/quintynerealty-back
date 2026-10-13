# Quintyne Realty inc Backend (MongoDB)

FastAPI backend for Quintyne Realty inc, with MongoDB Atlas.

## Security

- Signed cookie sessions and permission checks; external sign-in is not configured yet
- Short-lived signed JWT sessions in `HttpOnly` cookies
- CSRF double-submit validation for cookie-authenticated mutations
- Token revocation on logout, MongoDB TTL cleanup, RBAC/PBAC permission dependencies
- Exact-origin credentialed CORS, trusted-host validation, secure response headers and rate limiting
- Soft deletion, immutable audit events, unique identities, production startup checks
- Secrets are environment-only; Swagger is disabled in production

## Run locally

```powershell
Copy-Item .env.example .env
# Set the Atlas URI, public HTTPS origins, strong secrets.
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
python -m uvicorn server:app --host 127.0.0.1 --port 8002
```

Production mode fails startup if MongoDB, secure cookie settings, HTTPS origins, or strong secrets are missing or invalid. API documentation endpoints are disabled.

For localhost development only, `APP_ENV=development` and `DEV_AUTH_BYPASS=true` may be used with loopback-only frontend, CORS, and trusted-host values. This enables local development access but never bypasses MongoDB. The application rejects public development origins and rejects the bypass whenever `APP_ENV=production`.

Seed the first administrator after configuring MongoDB:

```powershell
$env:ADMIN_EMAIL="admin@example.com"
python scripts/seed_admin.py
```

MongoDB creates the required collections and indexes during application startup. Use a replica set in production for durability and transactions, TLS-enabled `mongodb+srv://` credentials from a secret manager, network allowlists, encryption at rest, and a least-privilege database user.

Microsoft sign-in has been removed. Google sign-in is planned but is not implemented. Until a replacement is configured, new production sign-ins are unavailable; existing valid sessions still use the same authentication and permission checks. Protected API routes return 401 when no valid session is provided.

## Deploy to Vercel

Use this backend directory as the Vercel project root and select the FastAPI
framework preset. The existing `server.py` exports `app` from `app.main` and is a
supported entrypoint; no `api/index.py` wrapper is needed. Routes keep their
existing paths, such as `/health` and `/receipts`, without adding `/api`.

The committed `vercel.json` selects FastAPI, excludes local-only files from the
function bundle, sets a 300-second function duration, and selects RapidOCR.
`.python-version` pins Python 3.12. OCR imports are deferred until needed; on
Vercel, downloaded OCR models are cached in temporary writable storage.

Keep the existing `requirements.txt`. Do not replace it with
`requirements-local.txt`: that file includes the large PaddleOCR stack and
references `requirements.txt` itself.

Leave build/install overrides at their framework defaults. Do not configure a
Uvicorn start command or run the local virtual-environment commands during
deployment. Configure the production environment variables from `.env.example`
in Vercel, with `OCR_ENGINE=rapid` and the actual frontend/backend origins and
hostnames. Redeploy without the build cache after dependency changes.

See the [Vercel FastAPI documentation](https://vercel.com/docs/frameworks/backend/fastapi).

If logs show `ValidationError` with `input_value=''`, the deployment environment
variables were saved with empty values. In Vercel project Settings → Environment
Variables, populate `MONGODB_URI` (including the database name), `JWT_SECRET` and
`SESSION_SECRET` (different random secrets, each at least 32 characters),
`FRONTEND_URL`, `CORS_ALLOWED_ORIGINS`, and `TRUSTED_HOSTS`. Use the frontend HTTPS origin for the frontend and CORS
values, and `api.example.com` for this backend's trusted host.
Set these for the deployment's environment, then redeploy. Never paste secrets
into logs or commit them. Blank optional settings use defaults; secure cookies
default to `true` and SameSite defaults to `lax`. Required settings still fail
closed when missing.

## Receipts, OCR, and Google Drive

### Deployment size

`requirements.txt` is the smaller serverless install: it includes RapidOCR and
ONNX Runtime, but excludes PaddlePaddle and PaddleOCR and their large dependency
tree. Set `OCR_ENGINE=rapid` in the deployment environment. `.vercelignore`
excludes local virtual environments, model caches, uploads, and development files.
Redeploy without the previous build cache after changing the dependency set;
verify the resulting function size in the deployment build output.

Drive storage uses the REST API with `google-auth[requests]` for service-account
authentication. The large `google-api-python-client` discovery library is not
needed for folder creation, file uploads, or downloads and is excluded from dependencies.

Local development uses `requirements-local.txt`, which adds PaddleOCR
and keeps `OCR_ENGINE=paddle` as the primary engine with RapidOCR as its fallback.
Installing into an existing environment does not uninstall old dependencies;
use a fresh environment when measuring the smaller install.

Receipt image uploads use lightweight PaddleOCR PP-OCRv5 mobile text detection and recognition locally, with RapidOCR as an automatic fallback; no paid OCR service is required. Optional orientation, document-layout, table, formula, seal, and page-unwarping models are not loaded. Documents can be categorized as gas, restaurant/client, maintenance, job expense, or other, and assigned a business activity. The reporting endpoint returns weekly, monthly, yearly, category, and profit/loss totals.

OCR previews run as durable background jobs. `POST /receipts/preview` returns a job ID immediately, and `GET /receipts/preview/{job_id}` returns `queued`, `processing`, `completed`, or `failed`. Temporary uploads remain in MongoDB GridFS so work survives a browser refresh; they are deleted after confirmation or automatically after one hour.

Without Drive configuration, files are stored under `uploads/Receipts/<year>/<category>` . To use Google Drive:

1. Create a Google Cloud service account and enable the Google Drive API.
2. Create a Drive folder and share it with the service account email as an editor.
3. Put the service account JSON on one line in `GOOGLE_DRIVE_CREDENTIALS_JSON` and set the shared folder ID in `GOOGLE_DRIVE_ROOT_FOLDER_ID`.

The backend then creates `Receipts/<year>/<category>` automatically.

Receipt endpoints require the `RECEIPTS_READ`, `RECEIPTS_CREATE`, `RECEIPTS_UPDATE`, and `RECEIPTS_DELETE` permissions. Super administrators automatically have access.

## Business receipts

`POST /receipts` and `POST /receipts/from-preview/{job_id}` accept `business_activity` as a multipart field. Supported values are `vending_machine`, `third_party_sales`, `rentals`, and `other` (the default). Invalid values return 422. The preview job ID identifies OCR processing only.

`PATCH /receipts/{receipt_id}` can update `business_activity` with `RECEIPTS_UPDATE`. `GET /receipts` and `GET /receipts/reports/summary` accept an optional `business_activity` query parameter. Filtering by `other` includes older receipts with a missing or null business type. Business activity is independent of receipt category and income/expense classification.

Jobs, job-linked uploads, quotes, drawings, and RFI endpoints have been removed. Existing records and stored files are retained; existing receipts remain visible in the standalone receipt list.


## HST tracking

Receipts store an optional `hst_amount`, separate from the tax-inclusive receipt total. OCR suggests explicitly labelled HST amounts for user review; no tax is inferred from a rate or total. Enter 0 when no HST was charged, and leave unknown amounts blank. Existing receipts can be updated in Receipt details. Financial reports total CAD HST paid on expenses and HST collected on income separately, using the selected year and business activity, and count expense receipts missing HST. These totals support review; they do not determine input tax credit eligibility. Original receipts remain available.

API clients can send `hst_reviewed=true` with a blank HST amount to preserve an explicitly unknown value rather than re-extracting it from OCR. `PATCH /receipts/{receipt_id}` accepts `hst_amount: null` to mark HST as unknown. Negative, non-finite, or above-total HST amounts are rejected.


## Vehicle catalog performance

Canadian vehicle ratings are cached in memory and in a temporary disk cache reusable for 24 hours across backend restarts. Concurrent requests share one dataset download. The year lookup returns makes and model configurations together. The frontend caches each selected year, shows loading/errors, and offers manual entry. Vehicle mutations refresh only vehicle data instead of reloading receipt OCR work.


Annual vehicle kilometres are calculated from non-deleted trips dated in the current calendar year, and update after logging a trip. Trips can be business or personal; older trips without a purpose are treated as business. The insurance business-use percentage uses logged business kilometres divided by all logged kilometres. Insurance estimates assume twelve months at the saved monthly cost and a complete driving log. Historical manually entered annual totals are retained in storage but no longer used for this calculation.


Gas receipt usage is automatic FIFO per vehicle: receipt date first, upload time to break ties. Trips are allocated chronologically and can consume fuel across multiple receipts. Existing trip distances are included regardless of their old manual receipt selection; stored history is retained. Exhausted receipts have zero remaining litres. Fuel used beyond recorded purchases remains visible in the vehicle overdrawn warning and is allocated when another receipt is added.


## OneDrive receipt storage

Personal OneDrive is supported with delegated Microsoft Graph access. Configure `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_REDIRECT_URI`, and `MICROSOFT_TENANT_ID=consumers` in the real backend environment. For Microsoft 365, use the tenant ID or `organizations`; `common` supports both where the app registration permits it. Register the callback as a **Web** redirect URI, enable personal Microsoft accounts in Supported account types, and grant delegated `Files.ReadWrite` plus `offline_access`. The callback path is `/auth/microsoft/callback`, and the callback host must be in `TRUSTED_HOSTS`.

For this localhost setup the configured callback is `http://localhost:8002/auth/microsoft/callback`. `SESSION_COOKIE_SECURE=false` is needed for plain HTTP local development; production requires secure cookies and HTTPS. A super administrator opens Receipts and selects **Connect OneDrive**, signs in, and approves access. This connects storage and does not replace application sign-in. Tokens stay encrypted in MongoDB with a key derived from `SESSION_SECRET`; rotating that secret requires reconnecting OneDrive.

After connection, new receipts upload to `Quintyne Realty/<year>/<category>/<MM - Month>`. Unique filenames prevent overwriting. Existing receipt files retain their original storage location. OneDrive takes priority over Google Drive/local storage after it is connected; connection failures fail the upload instead of silently switching storage. Receipt previews remain in MongoDB GridFS. Receipt download routes retain app permission checks and fetch OneDrive bytes through the backend.

Reference: https://learn.microsoft.com/en-us/graph/api/driveitem-put-content and https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow.

OneDrive year and month folders use the reviewed receipt date, not the upload date. For example, a gas receipt dated October 12, 2026 is stored under `Quintyne Realty/2026/Gas/10 - October`. Missing folders are created automatically during upload; existing files are not moved.


## OneDrive trip spreadsheet

Each saved trip updates one app-managed `Trip Log <year>.csv` spreadsheet in `Quintyne Realty/<year>`. CSV opens in Excel. The log includes all non-deleted trips across vehicles and months for that calendar year, with date, vehicle, business/personal type, From, To, kilometres, estimated litres used, notes, and a stable trip ID. The first upload uses a temporary unique filename, then renames the managed file to `Trip Log <year>.csv`. Existing name conflicts are reported without overwriting another file. Later updates reuse the same OneDrive file ID.

The spreadsheet is rebuilt from saved trip records, so retries do not duplicate rows. It is a generated report; make corrections in the app because later syncs replace its contents. If OneDrive is disconnected or an update fails, the trip remains saved and the form shows a retry action. `POST /vehicles/{vehicle_id}/trips/{trip_id}/sync-spreadsheet` retries without creating another trip. Concurrent updates use a shared MongoDB lease. Receipt folders remain organized by year/category/month; each annual trip log stays directly in its year folder.
