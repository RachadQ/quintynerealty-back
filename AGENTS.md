# Backend agent guidance

This FastAPI application serves the Quintyne Realty inc React client. It uses MongoDB, signed cookie sessions, Microsoft OAuth, CSRF protection, RBAC, and immutable audit events.

## Commands

- Run locally: `python -m uvicorn server:app --host 127.0.0.1 --port 8002`.
- Compile check: `python -m compileall -q app`.
- Temporary tests (when present): `python -m pytest -q`. Remove all test files and test artifacts after verification as described below.
- Install dependencies: `python -m pip install -r requirements.txt`.

## API and security

- Require `get_current_user` or a specific `require_permission()` dependency on non-public routes.
- Keep CSRF protection on cookie-authenticated mutations.
- Production must fail closed when authentication or MongoDB is unavailable. The local authentication bypass is permitted only with `APP_ENV=development` and loopback-only frontend, CORS, and trusted-host settings.
- Validate MIME type and `MAX_UPLOAD_BYTES` before processing uploads.
- Never log authentication tokens, service-account JSON, full OCR documents, or customer financial data.
- Audit create, update, delete, login, logout, receipt, and RFI operations.
- Use soft deletion for business records.
- Add indexes for new query patterns and serialize UUID values through `app.models.serialize`.

## Receipt, OCR, and Drive behavior

- `POST /receipts/preview` queues OCR and stores the upload temporarily in MongoDB GridFS so previews survive a browser refresh.
- Temporary GridFS bytes must be deleted after confirmation or expiration; abandoned preview data may live for at most one hour.
- Preview job lists, status, and files are exposed only to the user who created them.
- `POST /receipts` is the explicit confirmation step and may accept corrected OCR text from the client.
- PaddleOCR PP-OCRv5 mobile detection/recognition is the primary free local OCR implementation, with RapidOCR as its fallback. Do not reintroduce document-layout models for ordinary receipts.
- Google Drive storage uses a service account and creates `Receipts/<year>/<category>` or `RFI/<year>/<category>` below the configured root folder.
- Preserve local filesystem fallback when Drive is not configured.
- Never overwrite an existing Drive file based only on its display name.
- Receipt categories are `gas`, `client_meals`, `maintenance`, `job_expense`, and `other`.
- Work links use `link_type`, `link_id`, and `link_label` until first-class job, quote, and estimate collections exist.
- RFIs must be excluded from every financial aggregate.
- Financial reports must distinguish income, expenses, and net profit/loss and support weekly, monthly, and yearly views.

## Configuration

Document every new setting in `.env.example`. Do not commit a real `.env`, OAuth secret, JWT secret, session secret, or Google service-account credential.

## Cleanup after verification

- After verification, remove all project-owned test files, including test scripts, automated test suites, test-only fixtures, and test helpers, regardless of their name or location. This includes `test_receipt_upload.py` and files under `tests/`.
- Remove generated test caches, reports, and empty test directories. Do not remove tests bundled inside installed dependencies or virtual environments.
- Tests may be created temporarily for verification, but must not remain in the final working tree or be committed unless the user explicitly requests retaining them.
- Remove sample or seeded receipt records and files created during testing.
- Never commit real receipt images, OCR output, test credentials, or development-only authentication data.
