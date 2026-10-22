# Customer login via email or WhatsApp

Customers can identify their account by email or order ID, then choose email
or WhatsApp for the login code. Clicking Continue checks `/api/auth/delivery-options`
before showing delivery choices. WhatsApp is shown only when the matched customer
has a usable registered phone and WhatsApp delivery is configured. Missing or
invalid phone numbers, unknown accounts and ineligible orders show email only.
Editing the identifier or changing sign-in methods resets the choices and checks
again. This lookup sends no code and has separate per-identifier limits plus the
shared login IP/email limits. Email remains the default. Requests and resends
include `channel: "email"` or `channel: "whatsapp"`; omitted channels retain
the previous email behavior. Admin login remains email-only.

Order ID login supports WhatsApp selection, sending, resending and code verification.
If the order response omits a usable customer phone, the backend looks up the
linked customer account by email and verifies that both its customer ID and email
match before using its registered phone. This applies to the delivery chooser and
WhatsApp sends/resends.

If no usable account phone exists, **Order ID** login automatically checks the
order's shipping address phone, then the matched customer's default address phone.
Only server-fetched Shopify addresses are used. Indian address numbers with ten
digits (starting 6–9), or twelve digits starting 91, are normalized to +91 format
only when the address country is India/IN. Other contacts require a +country code.
The chooser and sends/resends use the same resolution logic. Account phones take
priority; client-supplied phone numbers and order-level contact fields are ignored.

An address-phone OTP and its resulting session are restricted to that one order.
Lists, search, item details, return/exchange submission, request history, and
refresh enforce this restriction on the backend. Logout-all from this restricted
session logs out only that session. Email and customer-account-phone login retain
account-wide access. Email login does not use address-phone fallback; the screen
directs customers to Order ID when WhatsApp is unavailable for their email.

WhatsApp uses Meta Cloud API, matching the existing Tanoti WhatsApp app.
Configure these **backend-only** environment variables on every deployment:

```
WHATSAPP_ACCESS_TOKEN=<same token as the WhatsApp app>
WHATSAPP_PHONE_NUMBER_ID=<same phone number ID as the WhatsApp app>
WHATSAPP_API_VERSION=v23.0
WHATSAPP_OTP_TEMPLATE_NAME=tanoti_returns_login
WHATSAPP_OTP_TEMPLATE_LANGUAGE=en_US
```

The `tanoti_returns_login` template was submitted with user approval on
2026-10-09 and Meta returned `APPROVED`, category `AUTHENTICATION`.
It uses a copy-code button, security recommendation and ten-minute expiry notice.
The local ignored `backend/.env` is configured. Production environment variables
must also be configured when deploying; credentials are never shipped to the frontend.

Shopify's customer account `phone` must use international format such
as `+919876543210`. Formatting spaces, parentheses, dots and hyphens are removed.
Address-phone fallback follows the order-only rules above; billing contact numbers
and client-supplied numbers cannot redirect login codes.
The number must be reachable on WhatsApp. Customers without a suitable number
can select email. Responses never expose the registered phone. Showing available
channels before authentication necessarily reveals WhatsApp availability for a
matched identifier; the lookup is rate-limited, and unknown accounts receive the
same email-only result as accounts without a usable phone.

Both channels share per-account send limits, resend cooldown, expiry, failed
verification limits and single-use codes. A successfully issued replacement
invalidates previous codes across channels. Provider failures invalidate the
new code and return a retryable error. Meta accepting a message does not prove
device delivery; the UI permits resending or returning to select email.

`TESTING_MODE=true` prints WhatsApp codes locally and sends no WhatsApp messages,
just like existing console email delivery. Use `TESTING_MODE=false` with the
approved template for real delivery. No live customer OTP was sent during development.
Deploy the backend migration before starting the updated app:

```
flask --app run:app db upgrade
```

Migration `j41fa507d234` adds nullable `order_scope_id` fields to customer OTPs
and sessions. Existing credentials retain their previous behavior. Downgrading
deletes restricted credentials before dropping scope fields to prevent widening
their access. Rebuild and deploy the frontend with the updated sign-in guidance.

Template format: [Meta authentication template example](https://www.postman.com/meta/whatsapp-business-platform/request/6vkv46u/create-authentication-template-w-otp-copy-code-button).
