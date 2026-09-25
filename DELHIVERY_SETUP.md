# Delhivery setup

The portal now creates both customer reverse shipments and replacement forward
shipments using the Delhivery B2C manifestation API. Photo approval books collection
for either a return or exchange. Accepting an inspected exchange books the replacement.
Replacements are prepaid: no cash is collected from the customer.

Each parcel is fixed at **30 cm long, 20 cm wide, 4 cm high, 200 g**, with one clothing
item per request. Reverse parcels return to the same registered warehouse used for
dispatch. No extra warehouse pickup requests are created: Tanoti has regular Delhivery
warehouse collection. Customers' saved collection addresses are used for reverse
shipments; replacements use the original order's delivery address.

## Configure the VPS

Put these in the environment used by the running `tanoti` service. Check
`sudo systemctl cat tanoti` to locate its EnvironmentFile; do not assume it uses the
same `.env` as a local checkout. If the service relies on `run.py` loading `.env`, use
`/var/www/tanoti-returns/backend/.env`.

```dotenv
DELHIVERY_API_TOKEN=your-production-token
DELHIVERY_PICKUP_LOCATION=Exact registered warehouse name
DELHIVERY_ENVIRONMENT=production
```

The warehouse name is case-sensitive. Configure the correct GST and clothing HSN
details for your account with Delhivery; if your account requires them in the payload,
also set `DELHIVERY_SELLER_GST` and `DELHIVERY_HSN_CODE`. Confirm reverse pickup is
enabled on the account, the warehouse address is correct, and the wallet/account is
funded. A staging token must only be used with `DELHIVERY_ENVIRONMENT=staging` in a
development app. The live app blocks accidental staging bookings.

Deploy both backend and frontend changes. With the same environment as the live
service loaded, run from the backend directory:

```bash
cd /var/www/tanoti-returns/backend
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m flask --app run.py db upgrade
sudo systemctl restart tanoti
```

The new migration is `f63d029c471a`; it adds a booking ledger. Apply it before serving
this version. This work has not changed the VPS environment or deployed the migration.

## Tracking and labels

The admin detail view shows the waybill and public tracking link. **Refresh Delhivery
tracking** reads the carrier's authenticated tracking API. For automatic updates,
install the included systemd service and timer after adjusting `User`, `Group`, paths
and EnvironmentFile to match the existing `tanoti` service:

```bash
sudo cp deploy/tanoti-delhivery.service deploy/tanoti-delhivery.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tanoti-delhivery.timer
sudo systemctl start tanoti-delhivery.service
sudo journalctl -u tanoti-delhivery -n 30 --no-pager
```

The timer polls every 15 minutes after the last run. You can also run
`.venv/bin/flask --app run.py sync-delhivery` with the service environment loaded.
The job never creates shipments. Confirmed forward delivery records the carrier's
delivery date, creates the replacement's new return-eligibility record, and queues
the delivery email once. Reverse tracking never approves an inspection: staff still
mark the parcel received and inspect it before issuing money or dispatching a replacement.

Print replacement labels in Delhivery One using the saved forward waybill; the admin
view links there. The portal does not generate its own carrier labels.

## Failure recovery

- Missing configuration or invalid addresses are detected before creating a shipment.
  Fix the problem, then use **Retry Delhivery booking** in the request detail view.
- A confirmed shipment is saved before finalizing the request. **Restore confirmed
  booking** repairs interrupted local finalization without contacting the create API.
- A timeout, malformed result, ambiguous response, or partial provider failure is
  never blindly retried. Search Delhivery One using the reference shown in the admin
  view, then use **Verify Delhivery waybill**. The server checks its exact reference
  and waybill via Delhivery before linking it. If no shipment can be found, ask
  Delhivery to investigate the reference; the app deliberately does not reset an
  ambiguous attempt automatically. Manual overrides cannot bypass this fence.
- Authentication rejections (HTTP 401/403) are safe to retry after correcting the
  token/environment. Customer emails are queued only after an actual waybill is
  confirmed. Processing errors do not expose tokens or customer addresses.
- References end in `-R` for collection and `-F` for replacements, so both legs of an
  exchange have independent waybills. A later return of a replacement gets a new
  request and new booking references. Earlier manually created shipments are kept;
  they are not automatically rebooked or included in automated delivery processing.

## Validation

Tests use mocked Delhivery HTTP responses and an isolated database, covering all
three booking flows, fixed parcel details, addresses, unique references, credential
failures, duplicate prevention, timeout reconciliation, tracking identity, and
one-time delivery notifications. No real pickup was booked during development.
After configuring a staging account, validate the account-specific payload in
Delhivery's API test environment before enabling real approval-triggered bookings.

Provider references: [manifestation](https://delhivery-express-api-doc.readme.io/reference/order-creation-api),
[pickup and tracking FAQ](https://one.delhivery.com/developer-portal/document/b2c/detail/faq).
