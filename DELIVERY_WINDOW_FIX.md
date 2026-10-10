# Five days after confirmed delivery

Return and exchange eligibility starts at each physical unit's confirmed Shopify
fulfillment `deliveredAt`, never the fulfillment creation date. The window lasts
five days (120 hours), inclusive of its exact deadline. An October 6 delivery
expires October 11 at the same time, so it is eligible on October 10. Split
shipments have separate deadlines. Items without delivery confirmation show
"Awaiting delivery". Replacement units keep their exchange delivery date.

Deploy backend and frontend together. Before starting the new backend, run:

```sh
python -m flask --app run db upgrade
python -m flask --app run backfill-deliveries
```

Migration `g18c72d4a901` adds delivery columns, preserves confirmed replacement
delivery timestamps, and sets existing return-window settings to five days.
Original order delivery dates are populated from Shopify during the backfill;
fulfillment timestamps are never copied as delivery timestamps. The backfill is
safe to repeat. It requires the app's existing read_orders access and Shopify
credentials. Shopify must contain delivery confirmation; a delivered status or
an estimated date alone cannot supply the actual delivery time.

Subscribe Shopify's `fulfillments/update` topic to
`/api/webhooks/shopify/fulfillments-update`, with the same store and HMAC secret
as the existing webhooks. The endpoint fetches the current order and delivery
timestamps, including when order `updated_at` has not changed. Existing
orders/updated and fulfillments/create hooks also refresh delivery dates.
Customer login refreshes units missing delivery dates; the customer Refresh
action also resyncs them. Provider failures remain retryable.

## Store coverage and stale delivery correction

Migration `h29d83e5b012` adds store import checkpoints and delivery-check timestamps.
The previous backfill only covered customers already cached in the app. The admin
Refresh button also only reloaded the cache. Neither guaranteed that all store
orders were represented. The new import reads all Shopify order pages from the
last 60 days, covering customers who never logged into the returns portal.

The admin page automatically starts/resumes an import when the last complete sync
is absent or more than 15 minutes old. **Sync Shopify orders** runs the same import
manually. Each page commits atomically and saves its next-page cursor; a failed
page is retried without losing prior pages or reporting a successful full sync.
Delivery dates are read in batches and product-image reads are omitted from store
imports to keep page processing bounded. Customer browsing still loads images.
The page shows the source Shopify domain, last complete sync, import progress,
orders missing delivery confirmation, and orders skipped for missing customer email.
Search/page changes still use the current cache while an import proceeds.

Customer order history now follows every Shopify pagination link. Customer lists,
item details, and direct return/exchange submissions recheck imported delivery data
after 15 minutes. A failed recheck returns a temporary-unavailability message rather
than using stale delivery data to reject a request. Fulfillments marked delivered
but lacking `deliveredAt` use the actual `DELIVERED` event's `happenedAt`, following
event pages as needed. A bare delivered status without a timestamp remains awaiting
confirmation. Creation, fulfillment, estimated, and event-created timestamps are
never substituted for actual delivery.

After deploying the new source and applying the migration, run:

```sh
python -m flask --app run db upgrade
python -m flask --app run sync-store-orders
```

`backfill-deliveries` now performs the same store-wide import. Shopify's standard
`read_orders` scope provides the recent order-access window; older orders require
approved `read_all_orders` access and an expanded import period. Existing cached
older orders and delivered replacement units still participate in eligibility.

For automatic reconciliation even when no admin has the page open, install the
provided timer using the existing service user, backend path, and production
environment. Adjust `deploy/tanoti-store-orders.service` if those differ:

```sh
sudo cp deploy/tanoti-store-orders.service deploy/tanoti-store-orders.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tanoti-store-orders.timer
```

Production verification should confirm the Shopify domain shown in the admin panel,
let the complete import finish, then search an affected order and compare its
delivery and deadline with Shopify's confirmed delivery event. The read-only audit
`python scripts/check_delivery_data.py --created-on 2026-09-26` prints order numbers
and delivery events, with no customer names/emails or credentials. The local `.env`
points to a test store with no orders on that date, so it cannot verify the reported
live order. The initial audit checks one page and flags whether more pages exist.

An order placed September 26 and delivered September 30 expires October 5 at the
delivery time under the five-day policy. It is eligible on October 1, and expired
on October 10. Shipping 30+ orders does not mean all 30+ are already delivered and
inside that five-day window; the sync and missing-delivery counts distinguish them.

The migration, import, and timer have not been applied to production by this change.
