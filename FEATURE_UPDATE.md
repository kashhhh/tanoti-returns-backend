# Customer login, images, emails and gift cards

Deploy the backend and frontend together. Before restarting the backend, apply the
new migration with `flask --app run.py db upgrade` (head: `e51a93bc820d`). It adds a
nullable, uniquely indexed opaque OTP challenge; existing records are preserved.

## Store configuration

The read-only check against this workspace's configured store returned USD and
included both gift-card scopes. This is inconsistent with this INR returns portal.
No live gift cards were created, modified or redeemed during this work.

Configure the live store manually in the backend environment:

```dotenv
SHOPIFY_STORE_DOMAIN=your-live-store.myshopify.com
SHOPIFY_CLIENT_ID=your-live-app-client-id
SHOPIFY_CLIENT_SECRET=your-live-app-client-secret
SHOP_CURRENCY=INR
```

Alternatively use `SHOPIFY_ADMIN_API_TOKEN` for a legacy app. A nonempty static
token takes precedence over client credentials: remove any stale token when using
the client-credentials flow. The configuration now actually loads the static token
(previously it was documented but omitted from Config). Restart the backend after
editing its environment so the cached access token is cleared. Keep secrets out of
source control. Use the live store's webhook signing secret as well.

Required scopes for the active workflows are `read_customers`, `read_orders`,
`read_products`, `read_gift_cards`, and `write_gift_cards`. Historical orders outside
Shopify's standard access window additionally need approved `read_all_orders`.

## Behavior

- Login accepts email or an exact order name/number (with an optional #), or the
  numeric Shopify order ID. OTPs go only to the matched account email. Order login
  requires the order contact email to match the customer email, because a session
  grants access to that customer's order history. The email is never disclosed by
  the unauthenticated response. Both login modes share cooldowns and OTP limits.
- Product images prefer the ordered variant's image, then its image association,
  then the product image. Cached images refresh on login or order refresh, including
  orders whose Shopify update timestamp has not changed.
- Customer notifications are limited to request received, pickup arranged,
  replacement arranged, replacement delivered, gift card issued, and refund paid.
  Login OTPs continue as before. Intermediate reviews, parcel receipt, refund
  approval and rejection remain visible in the portal without an email. Unsent
  legacy notifications for those removed milestones are discarded on delivery.
- For bookings made outside the courier API, admins can record the carrier and
  tracking number. That confirmation sends the arranged milestone once. A pending
  manual booking never claims that a shipment has already been arranged.
- Native Shopify return processing, metadata/tag writes and restocking are disabled.
  The admin sync panel, retries and stock decision selector have been removed.
  Historical Shopify records are preserved. Imports/webhooks, variant selection,
  courier booking and gift cards are independent and remain active. Existing
  Shopify inventory availability checks for exchange sizes are unchanged.
- Gift cards use Shopify's GraphQL `giftCardCreate` and verify the returned code,
  amount, currency and note. Permissions and store currency are checked before
  creation. Definite preflight/provider rejections can be retried after fixing the
  problem; timeouts or ambiguous results still require reconciliation to prevent
  duplicate monetary issuance. Old codes/uncertain attempts are not automatically
  reissued when switching stores. Investigate those against the original store.

API references: [giftCardCreate](https://shopify.dev/docs/api/admin-graphql/latest/mutations/giftcardcreate)
and [order lookup](https://shopify.dev/docs/api/admin-rest/latest/resources/order).

## Verification

The backend regression suite covers opaque order login, email privacy, OTP expiry,
exact order matching, colour-specific images, notification selection, manual
booking idempotency, disabled return sync, currency preflight, safe provider
rejection retries and ambiguous issuance reconciliation. The frontend production
bundle builds successfully. Tests use isolated SQLite and mocked financial writes;
successful card issuance against the live INR store remains to be verified after
the store configuration is corrected.
