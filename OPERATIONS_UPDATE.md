# Refunds, attention dashboard and cancellation

This update is implemented locally. It has not been deployed to the VPS. Pickup
rescheduling is excluded at the owner's request.

## Refund policy and review

The customer confirmation and submission use a fresh, read-only Shopify order
lookup. Refundable item value is the purchased unit's price after Shopify discount
allocations, including its item tax, minus the return deduction. Original delivery
charges are excluded. Tax is only added when Shopify prices exclude it. Decimal
arithmetic and deterministic paise allocation preserve totals for multi-unit lines.
Delivered replacements trace back to the original purchased unit's paid value.

The amount and calculation are saved with the return. Approval checks Shopify
again before issuing a gift card or approving a manual refund. A changed amount
blocks approval and appears under Needs attention. Use **Review refund amount**
in the request details to preview and explicitly confirm a correction. Existing
paid refunds, issued cards and uncertain/legacy card attempts cannot be revalued.
Older unresolved returns appear for review; the migration never rewrites their
amounts. Zero-value returns finish as **No refund due**, without issuing a card or
claiming money was transferred.

Orders must be marked paid (or partially refunded) in Shopify. This includes COD:
confirm collection/payment there first. Currency mismatch, adjusted quantities,
missing pricing data, existing refunds on the same line, and unallocated refunds
block automatic valuation. Resolve ambiguous external refunds manually; the app
does not guess which of several physical units was refunded. Refunds/credits made
outside Shopify cannot be detected, and a concurrent external refund between the
last check and issuance cannot be made atomic across providers. Staff should use
one refund process per request. Original-payment refunds still happen outside the
app; **Mark refund paid** records a payment already made and does not transfer money.

## Needs attention

The admin requests page opens on **Needs attention**. It includes:

- Photo review waiting more than 24 hours.
- Pickup still awaiting collection more than 3 days after approval.
- Inspection waiting more than 2 days after warehouse receipt.
- Replacement delivery outstanding more than 7 days after inspection approval.
- Missing, failed or uncertain shipment bookings; cancelled/returned shipments;
  unresolved cancellation actions; failed tracking refreshes.
- Unverified/changed refund amounts and uncertain gift-card issuance.
- Failed emails immediately, or unsent emails queued longer than 15 minutes.

Counts and pagination respect search, type and date filters. A request appears
once even when several reasons apply. Its reasons are shown beside the action and
in its detail view. **Retry unsent emails** retries only that request's queued
notifications. It never regenerates already-sent notifications. Existing Resend
idempotency handling remains in place. Completed requests can still appear if an
email needs attention. Use Refresh to update the view.

## Shipment cancellation

Staff can cancel a verified API-booked customer pickup or prepaid replacement
from request details. A browser confirmation precedes the call. The server checks
the original waybill, reference, environment and current carrier status. In-app
cancellation is limited to eligible pre-collection shipments; parcels already
moving, delivered or outside these states need handling in Delhivery One.

The action is saved before the carrier call to prevent duplicate attempts after a
timeout or worker restart. HTTP success means submitted, not cancelled. Only a new
authenticated carrier tracking scan confirms cancellation: Canceled/Cancelled for
reverse pickup, Returned for a prepaid replacement. An ambiguous action stays
visible and blocked from repeat submission. Use **Refresh Delhivery tracking** or
the existing tracking timer to reconcile it; contact Delhivery if it stays unclear.
Authentication rejection allows retry after credentials are corrected.

Cancellation retains the original waybill and staff action history. It does not
reject/close the return or exchange, refund money, cancel the Shopify order, or book
another shipment. Cancelled cases remain in Needs attention for staff follow-up.
No automatic rebooking or pickup rescheduling is included. No extra customer
status-change emails have been added.

## Deploy

Back up the database first. Deploy the backend and rebuilt frontend together.
Run with the same environment used by the live `tanoti` service:

```bash
cd /var/www/tanoti-returns/backend
.venv/bin/python -m flask --app run.py db upgrade
sudo systemctl restart tanoti
```

Migration **a74e13b95c02** adds refund evidence and carrier-action fields. It follows
f63d029c471a and is required before this backend starts serving requests. No new
credentials or environment variables are needed. Build the frontend with the
existing production settings and deploy its `dist` output. Keep the Delhivery
tracking timer and email retry job enabled.

Automated tests use isolated SQLite and mocked providers, including monetary
rounding, discounts/tax/deductions, stale quotes, external refunds, zero refunds,
gift-card fences, attention thresholds, cancellation timeouts, forward/reverse
status confirmation, authorization and migration preservation. They do not create
or cancel live shipments. PostgreSQL multi-worker and account-specific live carrier
acceptance remain deployment checks.

Provider references: [Shopify order fields](https://shopify.dev/docs/api/admin-rest/2026-07/resources/order)
and [Delhivery cancellation](https://delhivery-express-api-doc.readme.io/reference/cancel-order-api).
