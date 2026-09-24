# Security round 2 — 24 September 2026

Implemented locally in backend and frontend. No VPS, cloud, live customer data or provider configuration was changed. Infrastructure work remains deferred to the manual [VPS guide](SECURITY.md).

## Changes implemented

| Risk | Protection added |
| --- | --- |
| Browser-readable credentials and copied tokens surviving logout | Opaque database-backed sessions replace JWT bearer authentication. Production uses Secure, HttpOnly, SameSite=Strict, host-only cookies. Only keyed credential digests are stored in the session table. Eight-hour absolute expiry, session rotation on login, server-side logout and logout-all are supported separately for customers and admins. |
| Cross-site actions after switching to cookies | Role/session-bound CSRF tokens on authenticated writes; explicit origin checks; bounded JSON object parsing. The frontend keeps CSRF values in memory, restores them through the session endpoint, and uses same-origin requests. Legacy browser token storage is removed. |
| Replayed or out-of-order Shopify callbacks | HMAC verification followed by signed-body fingerprint deduplication, store/topic/identifier checks and per-order update timestamps. Receipt and order changes commit together; failures roll back and return retryable errors. PostgreSQL advisory locks serialize order updates. |
| Duplicate gift cards after timeouts or process crashes | One durable issuance record per return is committed before calling Shopify. An uncertain attempt blocks repeat creation and conflicting rejection. Admins can verify an existing Shopify card by ID; the server checks amount, currency, refund reference, code suffix and unique provider assignment before completing the request. |
| Malformed financial settings and limited accountability | Strict bounds/type checks for settings and rejection notes. Successful admin workflow milestones, settings changes and gift-card attempts/reconciliation are recorded with actor and timestamp. Audit records are available through authenticated GET /api/admin/audit?page=1. |

The gift-card fence favors preventing duplicate payments over automatic recovery. A timeout, provider error or crash can leave a pending/uncertain record even when no card was created. Investigate in Shopify; do not delete the record or blindly retry. Legacy ambiguous gift-card decisions also require investigation. Reconciliation reads an existing card and never creates one. The app does not expose pending gift-card codes in dashboard responses.

## Required application deployment steps

1. Back up and test recovery of the database. Apply `flask --app run db upgrade` through migration `c94a8e2fd061` using the deployment environment. It follows `78ad903bc612` and creates sessions, webhook receipts, gift-card issuance records, admin audit records and an order update timestamp. The new backend requires these tables.
2. Deploy the backend and freshly built frontend together. Existing bearer sessions are deliberately rejected, so everyone must log in again. Production needs HTTPS with frontend, `/api` and protected `/uploads` on the same origin. Build with `VITE_API_BASE_URL=/api`; never expose uploads through an nginx static alias.
3. Set `SHOP_CURRENCY` to the actual Shopify store currency (default INR). Verify gift-card API access and the create/read response contract in staging. Local tests mock Shopify; no real gift card was issued.
4. Keep `SECRET_KEY` strong and stable. Production refuses insecure session cookies. For emergency global logout, run `flask --app run revoke-sessions` in the backend environment. This revokes all current customer/admin sessions; already executing requests may still finish. Rotating SECRET_KEY also invalidates sessions and OTPs.
5. Keep the security cleanup job enabled; it now removes expired session rows too. Webhook receipts, issuance evidence and audit records are retained; plan database storage and retention without discarding unresolved financial evidence.

Local development requires explicit `APP_ENV=development`. Vite proxies `/api` and `/uploads` to `http://127.0.0.1:5000`; `TANOTI_DEV_API_TARGET` overrides this target. Development uses non-Secure cookies for localhost HTTP only.

## Verification

- Backend: 82 tests pass, including migration preservation, cookie attributes, CSRF rejection, copied-cookie revocation, logout-all isolation, invalid settings, scoped admin audit, webhook deduplication/rollback/stale-event handling and gift-card timeout/crash/reconciliation cases.
- Frontend production build passes. Isolated browser checks cover customer/admin login, session restoration, authenticated front/back photo rendering, settings save and logout isolation.
- Tests use SQLite and mocked external providers. The optional `scripts/security_browser_fixture.py` serves temporary fake data and blocks external provider requests; it is for local testing only.
- No PostgreSQL multi-worker test, live provider transaction, production CSP/mobile acceptance test, penetration test or live infrastructure audit was performed. Dependencies were not changed this round; the clean vulnerability-audit result in SECURITY.md is from 19 September and must be refreshed before release.

## Remaining risks, in launch priority order

1. **High — financial correctness.** Gift-card duplicate creation is fenced, but refund valuation against discounts, taxes, partial refunds and prior external refunds needs dedicated reconciliation tests and authoritative provider checks. A correctly authenticated action can still refund the wrong amount. Keep financial outcomes under manual review until this is addressed; this is the next application priority.
2. **High — shipping side effects.** Reverse/forward shipping bookings still need durable idempotency and provider reconciliation across a successful remote call followed by a local crash. Do not blindly retry ambiguous bookings. Extend the durable-attempt approach to these operations with provider-specific lookup semantics.
3. **High before rollout — infrastructure and secrets.** Public port restrictions, private upload routing, TLS/CSP, OS updates, least-privilege database/service accounts, encrypted off-host backups and restore tests remain pending manual VPS work. Database compromise exposes customer PII and stored gift-card codes. Restrict and protect database/backups; application sessions alone do not address this.
4. **Medium — administrator compromise.** Admins still use a shared secret plus email OTP and an allowlist. Mailbox compromise, phishing and shared-secret distribution remain risks. Prefer individual phishing-resistant staff authentication. The new database audit is scoped and mutable by a database administrator; it is not a complete, independently retained security log.
5. **Medium — availability and abuse.** Per-account throttles can be abused to deny someone login; distributed floods, image processing and synchronous webhook/provider calls can exhaust a small VPS. Test real capacity, monitor failures and backlog, and consider an authenticated webhook queue and edge abuse controls. Timing-based account enumeration may remain.
6. **Validation gap — real concurrency and integration.** Exercise duplicate webhook deliveries, simultaneous OTP verification and gift-card action/reconciliation races against PostgreSQL with multiple workers. Verify real Shopify gift-card access, email delivery, shipping responses and response times. SQLite tests cannot establish those production properties.
7. **Residual browser risk.** HttpOnly prevents script access to session cookies but malicious code running in the app origin can still perform authenticated actions. Deploy the supplied CSP, review dependencies and avoid unsafe HTML rendering. Session revocation reduces exposure; it does not make phishing or XSS harmless.

## Staging acceptance additions

- Reload customer and admin pages after login; then logout and verify the old session cannot read protected data. Logout-all must revoke other sessions for that identity/role while preserving the other role.
- A request with a valid session but missing/wrong CSRF must fail without mutation. Cross-origin login/write attempts must fail. Customer cookies must not authorize admin APIs or evidence photos.
- Deliver the same signed webhook twice with different delivery IDs; only one receipt/effect should persist. An older update must not overwrite newer order state. An upstream failure must allow a later successful retry.
- Simulate a gift-card create timeout after provider success. Repeat actions must not create another card. Reconcile its existing ID; mismatched amount/reference/currency must be refused. Confirm one customer notification and the recorded staff actor.
- Verify actual HTTPS cookies, mobile browser behavior, frontend CSP and uploads routing after manual VPS deployment. Refresh dependency audits and perform a restore test before opening to customers.

## Design references

[OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), [OWASP CSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html), [Shopify webhooks](https://shopify.dev/docs/apps/build/webhooks), and [Shopify gift-card API](https://shopify.dev/docs/api/admin-rest/latest/resources/gift-card). Shopify exposes the full card code at creation and only its last characters on later reads, which informs the reconciliation checks.
