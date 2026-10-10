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

The migration and backfill have not been applied to production by this change.
