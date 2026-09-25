> This document describes the retained legacy integration. Native return/restock sync is disabled and its admin controls are removed. See [FEATURE_UPDATE.md](FEATURE_UPDATE.md) for current behavior and deployment instructions.

# Unit cards and Shopify record synchronization

## Behavior

- Each purchased unit has a separate card and stable local ID. A quantity of two becomes Unit 1 of 2 and Unit 2 of 2. Re-syncing does not reset its request history.
- A rejection, gift card or paid refund closes that physical unit to further applications. An in-progress request also blocks another return or exchange, enforced in the backend with an atomic claim.
- Mark delivered creates a linked replacement unit with the requested variant/size. Its return window starts at the recorded delivery timestamp, using the configured number of days. Returning or exchanging it displays a Replacement return/exchange tag beside the customer. The original unit remains closed.
- Partial fulfillment dates are assigned per unit. The original unit and its replacement retain separate history. Existing delivered exchanges can be backfilled without duplicating cards.

## Shopify writes

Pending and rejected requests are recorded in a per-request JSON order metafield in the `tanoti_returns` namespace, plus order tags. Full customer notes and recorded outcomes are included; gift-card codes are not.

Photo approval creates one native Shopify Return for one fulfilled unit. Exchanges include the exact replacement variant. Accepting the inspected parcel processes the native return and releases its exchange fulfillment order. The owner explicitly chooses restock at the original fulfillment location or do not restock. A rejected, unprocessed native return is canceled. Subsequent payment/delivery status changes update the order record.

The application does not issue a gateway refund during record sync. Manual payments and newly issued gift cards are recorded in metadata, not used to settle Shopify's outstanding refund/payment balance. Reconcile those financial balances manually until a gateway-aware financial integration is implemented. Exchange price/discount differences can create an outstanding balance in Shopify. Do not issue another refund merely to clear that balance.

Replacement shipping/fulfillment is still managed manually while carrier integration is pending. The replacement must be fulfilled in Shopify before Shopify will accept a native return of it, even if this app's owner already marked it delivered. The local request is kept and a sync error is displayed if Shopify cannot yet accept it.

## Setup (no .env was read or changed)

Use the existing Shopify app installation. Add scopes `read_returns`, `write_returns`, and `write_orders`; retain `read_orders`, `read_customers`, `read_products`, and gift-card scopes. Access to fulfillment locations may also require `read_locations`. For orders older than Shopify's standard order-access window, request `read_all_orders` if needed. Reauthorize the installation after changing scopes.

This implementation targets stable Admin API version `2026-07`; the source default is updated. If your environment explicitly overrides `SHOPIFY_API_VERSION`, set it to `2026-07` yourself.

Back up the database, stop workers during this data migration, then run from backend with its virtual environment active:

```powershell
python -m flask --app run db upgrade
python -m flask --app run backfill-replacements
```

The migration preserves old unit-1 IDs and request associations and creates remaining unit cards. It deliberately cannot collapse those records on downgrade; restore a pre-migration backup if rollback is needed.

Migration sources remain ignored by the existing .gitignore because the earlier request to change that rule was declined. Include every file in migrations/versions in the deployment artifact, especially `f482b17a61e0_units_and_shopify_sync.py` and its predecessors.

## Recovery and scheduling

New submissions and status actions attempt sync after the local database commit. Failures remain visible in admin details and on the queue. Use Retry Shopify sync for a specific request. Schedule this command to retry pending/error records:

```powershell
python -m flask --app run sync-shopify-returns
```

This retry command also picks up older records. Review historic data before running a full backfill against production. Older approved cases require an explicit restock decision in their admin details.

A request identifier is embedded in the native return's reason note. If creation times out, a retry searches for that identifier before linking it. If no matching record can be found after an uncertain attempt, it stays Needs review and will not blindly create a second return. Investigate the order in Shopify; do not manually clear the attempted marker without checking Shopify. Processing retries read already-processed quantities first. External changes to native return lines or cancellation produce a visible error rather than overwriting them.

## Validation limits

Tests use isolated SQLite and mocked Shopify responses; no live store was accessed. The UI was built with environment-file loading disabled. Validate scopes and an end-to-end staging return/exchange before production. A real PostgreSQL concurrency run is still required before launch. The source review's remaining refund valuation, gift-card issuance recovery, OTP and infrastructure findings are not resolved by this feature.

Official references:
- https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/returnCreate
- https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/returnProcess
- https://shopify.dev/docs/api/admin-graphql/2026-07/queries/returnableFulfillments
