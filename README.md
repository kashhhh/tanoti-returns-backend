Latest customer features and deployment steps: **[FEATURE_UPDATE.md](FEATURE_UPDATE.md)**. Native Shopify return/restock sync is now disabled; order imports and gift cards remain active.

Current security/deployment instructions: **[SECURITY.md](SECURITY.md)**. They supersede the older mock-mode, public-upload, development-secret and production setup examples below. Deploy backend and frontend together, install from `requirements.lock`, and apply migrations through `e51a93bc820d`. Production now fails closed on unsafe/missing configuration.

For local development, explicitly set `APP_ENV=development`, `TRUST_PROXY=false`, a randomly generated `SECRET_KEY` of at least 32 characters, and `TESTING_MODE=true` if console OTP delivery is needed. Real Shopify operations remain live.

## Request dashboard and customer navigation

The admin UI opens on Needs review, oldest first, with 25 records per page. The
request list is paginated in SQL across returns and exchanges together. Query options
for `GET /api/admin/requests`: `page`, `per_page` (10/25/50), `stage`, `type`
(all/return/exchange), `q` (order/request number or email), `from`/`to` (inclusive
submission dates, YYYY-MM-DD in UTC), and `sort` (oldest/newest). Responses include
`requests`, `page`, `pages`, `per_page`, `total`, and `counts`. Stage counts reflect
search/type/date filters but include all stages so switching stages remains useful.
The API defaults to all stages; the UI explicitly requests pending. Filters live in
the URL, so refresh and browser Back preserve the view.

Statuses describe recorded outcomes: arrange pickup, awaiting parcel, inspect parcel,
refund approved, gift card issued, arrange replacement, shipment booked, rejected.
A booking is not described as delivery, and a manual refund approval is not described
as a completed transfer. Customer Past requests contains final review decisions;
individual status descriptions explain remaining refund/shipping work. Active requests
are those still in review, pickup, or parcel inspection. The underlying approval
workflow and database status values are unchanged; no migration is needed for this UI.

Customer requests use cards with outcome details and collapsible timelines. Admins
see a table on desktop and cards on mobile, with review actions inside the details
dialog alongside the evidence and addresses.

## September 2026 update: admin OTP, addresses, photos and emails

Admin login requires an allowlisted email, the shared admin secret, then an email OTP.
The allowlist is in `app/admin_setup.py`; it includes rajpurkaraakash@gmail.com and
tanotiofficial@gmail.com. Set `ADMIN_SECRET_KEY` and a strong `SECRET_KEY` in the backend
`.env`. Existing `ADMIN_API_KEY` is used as the login secret only if `ADMIN_SECRET_KEY`
is unset. The old X-Admin-Key API authentication no longer works. Admin sessions expire
after eight hours and are stored only for the browser tab session. Removing an email
from the configured allowlist also invalidates its sessions after the backend restarts.

Configure `RESEND_API_KEY` and a verified `EMAIL_FROM`. For isolated local testing only,
`TESTING_MODE=true` makes both customer and admin OTP delivery console-only, even
when a Resend key is configured. It also logs status notifications locally instead of
sending them. Set this one flag in backend `.env`, then restart the backend. Both login
screens identify console delivery automatically. Debug mode no longer controls OTP logging;
`ADMIN_LOCAL_EMAIL` is replaced by `TESTING_MODE`. Use `TESTING_MODE=false` in production.
This flag does not bypass OTP verification, admin secrets/allowlists, or customer order
ownership; Shopify always uses the real store.
Admin OTPs are separate from customer OTPs, use cryptographic randomness, expire after
10 minutes, allow five guesses, and have a 30-second send/resend cooldown.

With the backend virtual environment active, run from `backend/` before starting the app:

```sh
python -m flask --app run db upgrade
```

The new migration adds columns and tables without resetting existing data. Existing
orders are resynced on the next customer login if shipping addresses are missing; existing requests
without address snapshots fall back to the synced order address. Customer pickup edits
are saved only on the request. Replacement shipments still use the original shipping address.

Both request forms require one `photo_front` and one `photo_back` file, plus a JSON-encoded
`pickup_address` in multipart form data. Each image is limited to 10 MB and 25 megapixels,
then compressed locally. Both request types participate in 120-day retention and storage
quota cleanup. Quota cleanup preserves active requests; if there is no room after cleaning
completed/rejected requests, new uploads fail clearly instead of discarding active evidence.

Milestone emails cover submission, approval/pickup booking, parcel receipt, rejection with
reason, gift card issuance with code, manual refund approval, and replacement booking or
manual arrangement. No additional confirmation buttons or WhatsApp calls were added.
Manual refund emails say approved, not paid; shipment booking emails do not claim dispatch.
Live Delhivery booking and live Shopify gift cards still require account-level verification;
the existing shipping integration is not certified by these local tests.

Emails are queued in the same database transaction as the request/status change and sent
after commit. Failed sends remain queued. Configure a single scheduled worker on your VPS
(e.g. every minute) to run the first command below, and photo cleanup daily:

```sh
python -m flask --app run retry-emails
python -m flask --app run cleanup-photos
```

Resend retries use a stable Idempotency-Key. Resend retains these keys for 24 hours;
a send accepted by Resend followed by a local crash could duplicate if retried after that
window. See https://resend.com/docs/dashboard/emails/idempotency-keys.
These commands are provided but no VPS scheduler has been installed by this change.

Verification (isolated SQLite database and mocked external providers):

```sh
python -m unittest discover -s tests -v
```

---

# Tanoti Returns App — Backend (Phase 1)

Flask + Postgres backend for `returns.tanotiofficial.com`. This is Phase 1:
schema, auth, and the core customer/admin API routes. No React frontend or
Shopify order-sync job yet — see "Not built yet" below.

## Structure

```
app/
  models.py            All tables: Customer, Order, OrderItem,
                        ReturnRequest, ExchangeRequest, AdminSettings, OTPToken
  config.py             Env-driven config
  extensions.py          db / migrate instances
  auth/routes.py         POST /api/auth/request-otp, /resend-otp, /verify-otp
  customer/routes.py      GET  /api/customer/orders  (eligible + expired buckets)
                          GET  /api/customer/orders/search?order_number=...
                          GET  /api/customer/my-requests  (pending + completed)
                          GET  /api/customer/order-items/<id>
                          POST /api/customer/order-items/<id>/exchange
                          POST /api/customer/order-items/<id>/return  (multipart, photos[])
  admin/routes.py         GET  /api/admin/requests   (the owner's table, with repeat-returner flag)
                          POST /api/admin/requests/<kind>/<number>/accept
                          POST /api/admin/requests/<kind>/<number>/reject
                          GET/PUT /api/admin/settings  (return window, deduction toggle)
  services/
    shopify_client.py     Customer lookup, order fetch, gift card issuance
    email_service.py      Resend: OTP, rejection, status-update emails
    storage_service.py    DO Spaces photo upload + retention cleanup job
    delhivery_service.py  Reverse pickup scheduling (auto-fires on accept)
  utils/
    decorators.py          Customer JWT auth (login_required)
    admin_auth.py           Admin shared-key auth (admin_required)
    numbering.py            RET-000123 / EXC-000123 generator
```

## How the pieces fit your decisions

- **OTP login, validated against Shopify** — `request_otp()` calls
  `shopify_client.find_customer_by_email()` first; no OTP is sent if the
  email has no matching Shopify customer.
- **Return window starts at fulfillment** — `Order.fulfilled_at` drives
  `is_within_return_window()`. This field needs to be set by the order-sync
  job (Phase 2) from Shopify's `fulfillments/create` webhook.
- **Items, not orders, are the clickable unit** — `list_orders()` flattens
  every order into per-item entries, split into `eligible` / `expired`.
- **"Pending" state + a status page** — `OrderItem.active_request()` hides
  an item from "eligible" once it has an open request; `/my-requests`
  gives the customer their full history.
- **Exchange blocks out-of-stock sizes** — `available_sizes` in
  `get_order_item()` is currently a stub (`[]`); wire it to a live Shopify
  variant inventory check in Phase 2.
- **"To account" refund is manual** — no payment gateway reversal call
  anywhere; `refund_mode = account` just gets stored and shown to you in
  the admin table for you to action yourself.
- **Global deduction toggle** — `AdminSettings.deduction_enabled` /
  `deduction_amount`; applied and *snapshotted* onto the ReturnRequest at
  submit time (`deduction_applied`, `net_refund_amount`), so it's visible
  to the customer on the final return screen and doesn't retroactively
  change if you flip the toggle later.
- **Repeat-returner signal, never automatic** — `_repeat_returner_flag()`
  is just a boolean on each admin table row; accept/reject always requires
  the explicit POST from you.
- **Rejection reason → email** — `reject_request()` requires a
  `rejection_reason` from the fixed dropdown (`RejectionReason` enum) and
  sends it via Resend.
- **Reverse pickup auto-fires on accept**, with a try/except fallback —
  if the Delhivery call fails, the accept still succeeds and
  `delhivery_pickup_status` is set to `manual_scheduling_required` so you
  know to book it yourself.
- **Photos on DO Spaces, not the VPS**, auto-deleted after
  `PHOTO_RETENTION_DAYS` (120 days ≈ 4 months) via
  `storage_service.cleanup_old_photos()` — schedule this as a daily cron
  or APScheduler job.

## Phase 2 — now built

- **Shopify webhook receiver** (`app/webhooks/routes.py`) — handles
  `orders/create`, `orders/updated`, `fulfillments/create`. HMAC-verified
  using `SHOPIFY_WEBHOOK_SECRET` (skipped automatically in mock mode).
- **Sync service** (`app/services/sync_service.py`) — turns a Shopify
  order payload into local `Customer`/`Order`/`OrderItem` rows,
  idempotently. Also does a **full backfill pull** for a customer
  (`sync_orders_for_customer`), which fires automatically the first time
  someone logs in, and is exposed as `POST /api/customer/resync` for a
  manual "refresh my orders" action.
- **Live exchange-size stock check** — `get_order_item()` now calls
  `shopify_client.get_variants_for_product()` and only returns in-stock
  sizes, so out-of-stock sizes never appear in the exchange dropdown.
- **Returns/exchange analytics** — `GET /api/admin/analytics/overview`
  returns top-returned products, return reason breakdown, returns by
  size, and exchange reason breakdown.
- **Email testing:** `TESTING_MODE=true` prints OTPs and status emails locally.
  Shopify always uses the configured real store, including gift card issuance.
  Photos use local compressed storage.

## Still not built

1. **React frontend.**
2. **GoKwik gift card redemption check** — confirm gift card codes apply
   cleanly at your GoKwik checkout (flagged earlier: some merchants needed
   `write_gift_card_adjustments`/`read_gift_card_adjustments` scopes
   enabled on the GoKwik side) before relying on this as the refund path.
3. **Photo cleanup job scheduling** — `storage_service.cleanup_old_photos()`
   exists but needs a cron/APScheduler trigger once deployed.

## Running it locally

You need Python 3.11+ and a local Postgres running. Everything else (Shopify,
Resend, DO Spaces) can stay unconfigured thanks to the mock/local fallbacks
above — you're testing the real application logic, just with fake data
standing in for the three external services.

**1. Create the database**
```bash
createdb tanoti_returns
```
(If you don't have `createdb`/Postgres yet: `brew install postgresql` on Mac,
or `sudo apt install postgresql` on Linux, then start the service.)

**2. Set up the project**
```bash
cd tanoti-returns-app
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.local.example .env
```

**3. Create the schema**
```bash
export FLASK_APP=run.py         # Windows (cmd): set FLASK_APP=run.py
flask db init
flask db migrate -m "initial schema"
flask db upgrade
```

**4. Seed test data**
```bash
python scripts/seed.py
```
This creates a test customer (`test@example.com`) with:
- Order **1001** — fulfilled 3 days ago, prepaid, 2 items → shows as **eligible for return**
- Order **0987** — fulfilled 45 days ago, COD, 1 item → shows as **return window expired**

**5. Run the server**
```bash
python run.py
```
Server runs at `http://localhost:5000`.

**6. Walk through the flow with curl**

Request an OTP (watch the terminal running the server — the code prints there):
```bash
curl -X POST http://localhost:5000/api/auth/request-otp \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com"}'
```

Verify it (replace `123456` with the code from the console) and save the token:
```bash
curl -X POST http://localhost:5000/api/auth/verify-otp \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "otp": "123456"}'
```

List eligible/expired orders (replace `TOKEN` with the token from above):
```bash
curl http://localhost:5000/api/customer/orders \
  -H "Authorization: Bearer TOKEN"
```

Submit a return (grab an `id` from the orders response above; needs 2 real
image files on your machine):
```bash
curl -X POST http://localhost:5000/api/customer/order-items/1/return \
  -H "Authorization: Bearer TOKEN" \
  -F "reason=size_issue" \
  -F "refund_mode=gift_card" \
  -F "photos=@/path/to/photo1.jpg" \
  -F "photos=@/path/to/photo2.jpg"
```

View the admin table and accept the request (using `ADMIN_API_KEY` from `.env`):
```bash
curl http://localhost:5000/api/admin/requests \
  -H "X-Admin-Key: dev-admin-key"

curl -X POST http://localhost:5000/api/admin/requests/return/RET-000001/accept \
  -H "X-Admin-Key: dev-admin-key"
```

`TESTING_MODE=true` logs emails locally. Shopify calls always use your real store;
use a real customer email with orders. Gift card issuance and shipping remain live.
See the current setup instructions at the top of this file for admin OTP login.


### Confirmed outcomes and analytics

Apply the schema upgrade before running this version (from `backend`, with the virtual environment active):

```powershell
python -m flask --app run db upgrade
```

Manual refunds remain active until an admin selects **Mark refund paid** after making the payment. Approved exchanges remain active until **Mark delivered** is selected after confirming delivery. These actions record an admin email and UTC timestamp and queue a customer email; they do not transfer funds or fetch delivery confirmation. Gift card issuance and rejection move requests to history immediately. Existing approvals are not automatically treated as paid or delivered.

Analytics uses requests submitted in the selected inclusive UTC date range (up to 366 days), with current outcomes and an equal-length previous period for submission counts. Financial totals use net refund values and distinguish approved unpaid refunds, confirmed payments, and issued gift cards. Product/size/reason combinations and size exchange patterns count requests, including rejected requests, not store-wide return rates. Longest waits use the latest recorded process timestamp. Clicking reason, product, stage or financial summaries opens filtered requests.


### Optional customer notes

Returns and exchanges accept an optional `customer_note` field (maximum 2,000 characters, trimmed, blank stored as null). Notes are shown as plain text in customer history and admin details. Apply migration `d291c04fa832` with `python -m flask --app run db upgrade` before starting this version. See `PRODUCTION_REVIEW.md` for unresolved launch blockers; delivery and Resend setup remain deferred.


### Individual units, replacement returns and Shopify sync

See `SHOPIFY_SYNC_SETUP.md` for the unit-splitting migration, delivered replacement backfill, Shopify scopes, retry command and financial-reconciliation limitations. `.env` was not changed.
