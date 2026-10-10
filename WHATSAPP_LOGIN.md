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

Only Shopify's customer account `phone` is used, in international format such
as `+919876543210`. Order/shipping/billing contact numbers and client-supplied
numbers cannot redirect login codes. Formatting spaces, parentheses, dots and
hyphens are removed; ambiguous numbers without a country prefix are not used.
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
No database migration is required.

Template format: [Meta authentication template example](https://www.postman.com/meta/whatsapp-business-platform/request/6vkv46u/create-authentication-template-w-otp-copy-code-button).
