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

Receipt and RFI image uploads use lightweight PaddleOCR PP-OCRv5 mobile text detection and recognition locally, with RapidOCR as an automatic fallback; no paid OCR service is required. Optional orientation, document-layout, table, formula, seal, and page-unwarping models are not loaded. Documents can be categorized as gas, restaurant/client, maintenance, job expense, or other, and linked to a job, quote, or estimate reference. The reporting endpoint returns weekly, monthly, yearly, category, and profit/loss totals.

OCR previews run as durable background jobs. `POST /receipts/preview` returns a job ID immediately, and `GET /receipts/preview/{job_id}` returns `queued`, `processing`, `completed`, or `failed`. Temporary uploads remain in MongoDB GridFS so work survives a browser refresh; they are deleted after confirmation or automatically after one hour.

Without Drive configuration, files are stored under `uploads/Receipts/<year>/<category>` . To use Google Drive:

1. Create a Google Cloud service account and enable the Google Drive API.
2. Create a Drive folder and share it with the service account email as an editor.
3. Put the service account JSON on one line in `GOOGLE_DRIVE_CREDENTIALS_JSON` and set the shared folder ID in `GOOGLE_DRIVE_ROOT_FOLDER_ID`.

The backend then creates `Receipts/<year>/<category>` and `RFI/<year>/<category>` automatically.

Receipt endpoints require the `RECEIPTS_READ`, `RECEIPTS_CREATE`, `RECEIPTS_UPDATE`, and `RECEIPTS_DELETE` permissions. Super administrators automatically have access.

## Jobs

`GET /jobs` lists active job records, `POST /jobs` creates a job with a unique code, and `GET /jobs/{id}` groups its receipts, RFIs, quotes, and drawings. Receipt and RFI uploads attach to a job with `link_type=job` and `link_id=<job code>`; the API validates the job and normalizes the code. Quotes and drawings are PDF or image files uploaded to `POST /jobs/{id}/files` with `kind=quote` or `kind=drawing`. Local files are stored below `uploads/Jobs/<job ID>/<kind>`; Drive files are stored below `Jobs/<job code>/Quotes` or `Jobs/<job code>/Drawings`. Job routes use `JOBS_READ`, `JOBS_CREATE`, and `JOBS_UPDATE` permissions. Uploads keep the existing MIME and size limits, and RFIs remain excluded from financial reports.

Structured quotes can also be created with `POST /jobs/{id}/quotes` and edited with `PATCH /jobs/{id}/quotes/{quote_id}`. Each quote stores multiple items with name, description, quantity, unit price, source URL, and calculated totals. `POST /jobs/{id}/quotes/extract-item` accepts a public product URL and uses Beautiful Soup to read JSON-LD product data or page metadata for the item name, description, and price. The extractor rejects local/private hosts and oversized or non-HTML pages. Uploaded quote files remain available alongside structured quotes.

Product extraction also reads embedded JSON product records (including Best Buy and Home Hardware) when their product ID matches the requested URL. It fills fields independently, preserving recovered descriptions and prices when the name must be inferred from the URL. The response includes a warning for inferred names or missing details; unavailable prices stay `null`. Scripts are never executed, and blocked pages may still require manual entry.

Creating or editing a structured quote automatically generates a PDF containing the job, company, items, quantities, CAD unit prices, line totals, total, and notes. Quote fields and PDF bytes are saved atomically in the same MongoDB document so serverless restarts cannot lose the PDF. JSON responses omit PDF bytes and include `pdf_url`, `pdf_filename`, and `pdf_generated_at` for newly saved quotes. `GET /jobs/{job_id}/quotes/{quote_id}/pdf` requires `JOBS_READ`, downloads the saved PDF, and generates PDFs on demand for older quotes. Deleted jobs or quotes cannot be downloaded. Saves accept up to 500 items and an 8 MB generated PDF. PDF downloads are audited; quote edits retain CSRF and `JOBS_UPDATE` checks.

RFI functionality has been removed from the interface and backend endpoints. Existing RFI records and stored files are retained but are excluded from receipt lists and reports.
