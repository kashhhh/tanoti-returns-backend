# Production review - 18 September 2026

UPDATE: Unit cards, replacement lineage, permanent per-unit request blocking, server-side window/stock validation and native Shopify record sync are now implemented. See SHOPIFY_SYNC_SETUP.md for the current behavior, migration and limitations. The findings below describe the earlier audit; refund valuation, gift-card issuance recovery, customer OTP and deployment hardening still require work.

This is a source-code review, not a certification of the deployed store. No environment file or live store credentials were inspected, and no Shopify order was changed. Delivery-provider and Resend setup remain deferred as requested.

## Completed in this change

Optional customer notes on return and exchange submissions, limited to 2,000 characters on both client and server. Notes are separate from the required explanation for reason "Other". They appear in return confirmation, customer request history, and admin request details as plain text. Existing records have no note.

Apply migrations from the backend virtual environment before restarting the backend:

```powershell
python -m flask --app run db upgrade
```

Migration source files are currently ignored by `backend/.gitignore` (`migrations/`). The user declined the previous edit to that rule; it has not been changed. Include the full migration chain in the deployment artifact, including `d291c04fa832_customer_notes.py`; otherwise a clean deployment cannot upgrade its database.

## Current repeat-request behavior (not changed by the notes feature)

`OrderItem.active_request()` only blocks an unfinished request. A rejected request, paid refund, issued gift card, or delivered exchange permits another request on the same original order line. This is a real gap, not the intended permanent lockout described by the owner. Return and exchange submission routes also do not check the fulfillment date or return-window deadline. The order-list UI does, but a direct submission bypasses it.

Policy decisions pending: whether replacement items are final after delivery, and whether multiple units on a single order line must support separate quantities. Other items in the same order should remain independently eligible. Do not solve this with an order-wide lock.

## Release blockers to resolve

1. **Eligibility and duplicate submission.** Enforce fulfillment and window checks in both POST routes; enforce the chosen per-item/per-quantity repeat policy, including rejection. Serialize simultaneous return/exchange submissions. The photo-storage helper currently commits the DB session, which must be accounted for when implementing locks. Request numbers use `count()+1`, so simultaneous submissions on different items can collide.
2. **Money and quantities.** Refunds use `OrderItem.price` copied from Shopify line `price`, without discount allocations, prior refunded amounts, tax/currency accounting or requested quantity. The UI displays INR unconditionally. Define refundable paid value and shipping/tax treatment; snapshot it at submission. Add selected quantities if multiple units are sold. Existing requests must not silently have their amounts recalculated.
3. **Gift-card retry safety.** Parcel acceptance calls Shopify before committing its outcome and currently has no action lock or durable issuance/reconciliation record. Two admins or a timeout after Shopify succeeds can cause repeat issuance. Use a recoverable operation with a persisted reference and reconcile ambiguous responses before issuing again. A gift card is not proof that the original order's refund was recorded.
4. **Exchange fulfillment and stock.** The stock check only populates the UI dropdown; submission accepts an arbitrary size. It assumes size is option1 and does not retain the requested Shopify variant ID. Validate the exact variant at submission and acceptance, preserve other options such as colour, and manage stock/reservation or explicitly require manual allocation. Currently no Shopify replacement order/fulfillment or inventory adjustment is created.
5. **Shopify synchronization.** Decide on native return/exchange/refund recording versus an explicit manual reconciliation process. Current app writes only gift cards to Shopify. Paginate order pulls; track fulfillment per item for split shipments; handle canceled/already-refunded orders and deleted lines. The fulfillment webhook currently acknowledges a failed upstream fetch as success, so Shopify cannot retry that failure. Sync uses the earliest order fulfillment date for every item and removes timezone information without first converting to UTC.
6. **Customer OTP hardening.** Cooldown is only enforced on `/resend-otp`, so `/request-otp` bypasses it. Old unconsumed OTP rows can become usable again after a newer token is consumed, because verification selects the latest unconsumed row. Invalidate superseded codes and enforce shared request/verification throttling under concurrent workers. Admin OTP already has its own separate implementation; test both flows with production email behavior.
7. **Deployment and data handling.** Migrations must ship with the code. Verify production secrets, TESTING_MODE=false, HTTPS, restricted CORS, backups and restore, durable photo storage and cleanup/retry scheduling. Current photos are stored locally on disk, not DigitalOcean Spaces as in the original conversation, and are served by an unauthenticated URL. Decide whether that matches the intended storage/access policy. None of the runtime settings were checked because `.env` is off limits. The source default Shopify API version is 2026-10; select and verify a stable supported version for the deployment date.

## Shopify records

This app can track cases in its own database without native Shopify Return objects. However, its current actions do not reconcile Shopify return status, exchange fulfillment, inventory or original-order financial records. For the goal of an integrated production returns app, native synchronization is recommended before launch; otherwise staff must carry out and reconcile those operations manually. Do not issue a second gateway refund when recording a gift-card outcome.

Shopify documentation consulted:
- https://shopify.dev/docs/apps/build/orders-fulfillment/returns-apps
- https://shopify.dev/docs/api/admin-graphql/latest/mutations/returnCreate
- https://shopify.dev/docs/api/admin-graphql/latest/mutations/returnProcess

Shopify's return workflow supports creating approved returns and processing returned items, exchange items and financial outcomes. Restock decisions should follow physical inspection, not photo approval alone.

## Suggested implementation order

Resolve quantity/replacement policy; implement eligibility and concurrency; fix refund valuation and recoverable gift-card issuance; add Shopify return/exchange and inventory reconciliation; harden OTP and sync; run a staging end-to-end test with discounted orders, COD, rejected requests, concurrent clicks, multiple quantities and split fulfillment. Complete delivery and Resend integrations afterward, before opening the production customer portal. More dashboard features are not the priority.
