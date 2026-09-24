# Security review and manual rollout — 24 September 2026

This review covers the local Flask/React source and dependency manifests. The DigitalOcean VPS was not accessed or changed. The app is not serving customers yet. The templates assume Ubuntu/Debian, systemd, nginx, and PostgreSQL; adapt them to the installed OS and existing paths before applying.

Round 2 implementation, verification and outstanding launch risks: **[SECURITY_ROUND_2.md](SECURITY_ROUND_2.md)**. VPS work below is still deferred.

## Prioritized findings

| Priority | Risk in this app | Change / remaining work |
| --- | --- | --- |
| High | Anyone possessing an evidence photo URL could download it without logging in. | `/uploads/` now requires an allowlisted admin session and a matching request record. The admin UI fetches private blobs. Remove any nginx upload alias/static location or this protection can be bypassed. |
| High | Customer lookup selected the first Shopify search result, allowing a wrong-account association if search semantics broadened a match. | Strict email input, quoted search, and exact normalized email comparison before accepting a customer. |
| High | Customer OTP request bypassed resend cooldown; older OTPs remained usable; verification was not serialized across workers; cheap hashes allowed offline six-digit guessing from a database leak. | Shared send/resend cooldown, old-code invalidation, keyed HMAC hashes, PostgreSQL transaction locks, and single-use consumption. Admin issuance also serialized. |
| High | OTP/email flooding and admin-secret guessing had no shared per-IP/account budget. | Database-backed atomic counters across workers: 30 auth POSTs/IP/minute, 20 auth POSTs/email/15 minutes, 5 sends/email/15 minutes, and 2 customer resyncs/5 minutes. nginx adds edge limits. |
| High | Known vulnerabilities in old package versions, including the image parser, CORS library and build server. | Upgraded affected packages, refreshed frontend lockfile and added a resolved backend lockfile. Post-upgrade audits returned no known vulnerabilities on review date. |
| High if deployed insecurely | Default signing key and forced Flask debug mode; production could log OTPs. | Strong secret checks, independent admin secret, production configuration checks, explicit development mode, debug off. Production refuses console OTP delivery and missing email/webhook configuration. |
| Medium | Customer JWT accepted insufficient claims and lasted a week; customer tokens persisted in localStorage. | Round 2 replaces bearer tokens with opaque, revocable, eight-hour HttpOnly cookies, role-specific CSRF protection, and logout-all. Legacy bearer authentication is rejected. |
| Medium | Browser framing, caching of customer data, and weak static-site response policy. | API no-store/nosniff/frame protections. nginx template adds CSP, HSTS, TLS, and matching frontend policy. Credentialed CORS uses explicit origins; same-origin frontend deployment is required. |
| Medium | Upload parsing and public services can exhaust a small VPS. | Existing image re-encoding and size/pixel limits retained; decoded format checked; form parts/memory and login body size bounded. nginx request/connection limits and systemd resource caps supplied. Load-test against actual Droplet capacity. |

Authorization checks on customer orders/submissions, parameterized ORM queries, admin email allowlisting, cryptographic OTP generation, and webhook HMAC verification already existed. Existing ownership tests still pass. This was not an incident investigation.

## Round 1 verification (19 September; round 2 results linked above)

- 64 backend tests passed against the updated dependencies, including 11 new security regressions and the schema-preservation migration test. Tests use isolated SQLite databases and mocked external providers.
- Production frontend build passed with Vite 8.3.0 and React Router 7.18.4. Browser checks confirmed the customer login and admin login render without reported console errors, and unauthenticated admin navigation redirects to login.
- The npm audit of the updated lockfile reported zero vulnerabilities. `pip-audit` reported no known vulnerabilities for all 27 resolved entries in `requirements.lock`. The backend audit checked the supplied resolved list (`--no-deps --disable-pip`); it did not scan the VPS or system libraries.
- nginx/systemd execution, real PostgreSQL concurrency, authenticated browser photo rendering and live provider integration still require the VPS/staging acceptance checks below.

## Changes you must apply on the VPS

### 1. Preserve recovery and restrict access

Take a database backup and a Droplet snapshot before migration. Keep a copy of the current nginx and systemd configuration. Verify you can restore the database into a separate staging database; a snapshot alone is not a restore test.

In DigitalOcean Networking → Firewalls, attach rules to this Droplet:

| Inbound port | Allowed sources |
| --- | --- |
| TCP 22 (or your actual SSH port) | Your administrator public IP/VPN only, including the appropriate IPv6 source if used |
| TCP 80 and 443 | Public IPv4 and IPv6 |
| 5000, 8000, 5432, 6379 and all other app/database ports | No public inbound rule |

Do not blindly replace rules if the Droplet hosts other services. Keep working outbound DNS, HTTPS, NTP, package repositories and any explicitly needed external database connectivity. The app needs Shopify, Resend and delivery-provider HTTPS access.

Create/test a non-root sudo administrator with an SSH key. Keep your current SSH session open and DigitalOcean console recovery available. Only after a second key-based SSH session succeeds, configure an sshd drop-in:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
```

Validate with `sudo sshd -t`, inspect effective settings with `sudo sshd -T`, then reload the OS's SSH service. Test another new connection before closing the original session. If you use keyboard-interactive MFA, preserve that authentication policy instead. Apply equivalent host firewall restrictions after allowing your actual SSH port. Enable OS security updates and plan reboots for kernel fixes. Enable MFA on DigitalOcean, Shopify, email and source-control accounts.

### 2. Files, credentials and database

Use a dedicated non-login service user named `tanoti`. The supplied paths are:

```text
/srv/tanoti/backend             backend release and Python virtualenv
/srv/tanoti/frontend/dist       compiled frontend ONLY (nginx document root)
/etc/tanoti/backend.env         production environment, root-owned, mode 0600
/var/lib/tanoti/uploads         private images, owned by tanoti, mode 0700
```

Keep application code and its virtualenv owned by your deployment user/root and read-only to the service user. Do not make the entire application tree writable to `tanoti`. nginx needs read access only to compiled frontend files. Copy existing images into the private upload directory while preserving their `returns/<request-number>/<filename>.jpg` structure; confirm ownership before restart. Do not delete the old copy until verification succeeds, but remove all public routes to it immediately.

Generate two independent secrets with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`, run separately for each secret. Do not reuse the admin login secret as the server SECRET_KEY. Store credentials only in the production environment file, never in frontend `VITE_*` variables, Git, deploy archives or chat.

Required security settings in `/etc/tanoti/backend.env`:

```dotenv
APP_ENV=production
TESTING_MODE=false
TRUST_PROXY=true
SECRET_KEY=<first-generated-secret>
ADMIN_SECRET_KEY=<second-generated-secret>
CORS_ORIGINS=https://returns.tanotiofficial.com
PHOTOS_STORAGE_DIR=/var/lib/tanoti/uploads
MAX_PHOTO_STORAGE_MB=500
PHOTO_RETENTION_DAYS=120
DATABASE_URL=postgresql://<app-user>:<password>@127.0.0.1:5432/<database>
RESEND_API_KEY=<configured-key>
EMAIL_FROM=<verified-sender>
SHOPIFY_WEBHOOK_SECRET=<actual-webhook-signing-secret>
SHOPIFY_STORE_DOMAIN=<your-store>.myshopify.com
SHOPIFY_CLIENT_ID=<configured-id>
SHOPIFY_CLIENT_SECRET=<configured-secret>
```

Retain your verified Shopify API version and delivery settings. Placeholder values above must be replaced. Startup checks verify configuration presence, not whether the provider accepts the credentials. Exercise real email delivery in staging.

`TRUST_PROXY=true` is safe only with exactly the supplied nginx ingress overwriting forwarded headers and Gunicorn bound to loopback. Do not enable it with a publicly reachable backend. If Cloudflare/a DigitalOcean load balancer sits ahead of nginx, configure trusted proxy IP ranges and original client IP handling for that topology; do not trust arbitrary incoming X-Forwarded-For. The template assumes direct client → nginx → Gunicorn.

PostgreSQL should listen on loopback for a same-host database and use SCRAM password authentication. The app role must not be a superuser and must not have CREATEDB/CREATEROLE. Separate a schema-owning migration role from the runtime role where practical; grant the runtime role only needed table/sequence permissions, including `security_rate_limits`. For a managed database, use private networking, its trusted-source rules and TLS certificate verification instead of this loopback example.

### 3. Deploy backend and frontend together

Use Python 3.12 for the tested dependency resolution and Node 22.12+ or another version satisfying Vite's engine requirement for the frontend build. Install from the lockfiles:

```sh
# Run in the backend release directory using your deployment environment.
python3 -m venv venv
venv/bin/python -m pip install -r requirements.lock

# Run in the frontend source directory (build locally or in CI if preferred).
npm ci
npm run build
```

Build with `VITE_API_BASE_URL=/api`. The production frontend requires the API on the same origin. Deploy the new `dist` and backend together: old frontend builds cannot display the newly protected photos. Do not reuse the old `tanoti-returns-frontend.tar.gz`; it predates this security work.

Before starting the new API workers, run the migration using the same environment as the service. A one-time transient unit can load the root-only environment file without exposing its contents to a shell:

```sh
sudo systemd-run --wait --pipe --collect \
  --property=User=tanoti --property=Group=tanoti \
  --property=WorkingDirectory=/srv/tanoti/backend \
  --property=EnvironmentFile=/etc/tanoti/backend.env \
  /srv/tanoti/backend/venv/bin/flask --app run db upgrade
```

If you separate migration and runtime database roles, use a separate migration environment file and role for this command. Run through migration `c94a8e2fd061`, following `78ad903bc612`. These add abuse counters, sessions, webhook receipts, gift-card issuance records, admin audit records and order update timestamps. Deploying code without the tables breaks login. Deploy both components together; all legacy bearer sessions are rejected and users must sign in again. Rotating `SECRET_KEY` invalidates all sessions and outstanding OTPs.

### 4. Enable the service and nginx policy

Review `deploy/tanoti.service`, adjust paths and worker/memory limits to the Droplet, then install under `/etc/systemd/system/`. Create the service user and `/var/lib/tanoti/uploads` first. The service binds only `127.0.0.1:8000`, runs without root/capabilities, and can write only its private data/temp directories. Stop/disable the old API service before starting the new one to avoid conflicting listeners.

Review `deploy/nginx.conf`, adapting it to your existing site. It is an http-context include for `sites-available`, not a complete replacement for `/etc/nginx/nginx.conf`. Obtain/retain a valid certificate before enabling the HTTPS block. For a new certificate, initially enable only the port-80 ACME location and request a webroot certificate; the port-443 block cannot pass `nginx -t` until the files exist. Verify certificate auto-renewal with the installed ACME client's dry-run.

Remove any old `/uploads/` alias/root/static location in **all** enabled server blocks. That path must proxy to Flask. Serve only `frontend/dist` as the web root. Do not expose the repository, `.env`, backups, source maps, virtualenv or uploads directory. The CSP allows Google Fonts, HTTPS product images, and local blob previews, but no third-party scripts. If extra scripts are later added, review the policy instead of disabling it.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now tanoti.service
sudo nginx -t
# Only after nginx -t succeeds:
sudo systemctl reload nginx
sudo systemctl status tanoti.service --no-pager
sudo ss -lntp
```

No public listener should exist for Gunicorn or PostgreSQL. Keep the new security headers at server level; adding nginx `add_header` directives inside a location can change inheritance on older nginx versions, so recheck every affected response afterward. Do not add `includeSubDomains` or HSTS preload without reviewing the other subdomains.

### 5. Cleanup, updates and recovery

Install `deploy/tanoti-cleanup.service` and `.timer` under `/etc/systemd/system/`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now tanoti-cleanup.timer
sudo systemctl list-timers tanoti-cleanup.timer
```

This purges expired OTPs/counters/sessions and applies the existing photo retention policy. Separately retain/configure the email retry and Shopify sync jobs described in the backend README. Monitor service failures, 429/5xx rates, disk space, pending emails, certificate expiry and database backup success. Avoid logging Authorization headers, OTPs, request bodies or raw secrets; nginx's template access log omits query strings. Protect/rotate access logs, which still contain IPs and request identifiers.

Enable encrypted off-Droplet database backups with limited access and a defined retention policy. Include private photos if they are needed for dispute recovery. Periodically restore both to a staging environment. Keep secrets out of ordinary backup/deploy archives. Re-run package audits before deployment and regularly afterward; no-known-vulnerabilities is a point-in-time result.

## Acceptance checks before opening to customers

1. `https://returns.tanotiofficial.com` loads, HTTP redirects to HTTPS, certificates validate, and login/admin navigation works.
2. Inspect response headers: the frontend has CSP/HSTS/frame protection; customer/admin API responses have `Cache-Control: no-store`.
3. Open an actual stored `/uploads/...jpg` URL in a signed-out browser: expect 401, never the image. Admin preview/full-size view must work. A customer session must also be refused on this route.
4. Request/resend a code twice rapidly: the second send is throttled. Try five wrong codes, then the correct one: verification remains blocked. Resend after the cooldown: old code fails; new code works once. Verify real email receipt with `TESTING_MODE=false`.
5. Verify two different customers cannot access each other's items or requests. Try a customer session on an admin route and vice versa: both must fail.
6. Run a PostgreSQL staging concurrency check using multiple workers: simultaneous verifications of one OTP produce at most one successful login; simultaneous sends cannot bypass the cooldown. Local SQLite tests do not prove PostgreSQL locking behavior.
7. Verify from outside the Droplet that only intended public ports respond. Confirm `.env`, archives and private database ports are inaccessible. Inspect IPv6 rules as well as IPv4.
8. Verify invalid webhook signatures fail; valid staging webhooks still sync. Check failed external-provider calls never expose credentials in client responses.
9. Test photo upload, admin preview, return/exchange submission, logout and sign-in on a mobile browser under the real nginx CSP. Watch console/CSP errors. Test renewal, cleanup, and restore once.

## Remaining work and limits

See the current prioritized risks and acceptance checks in [SECURITY_ROUND_2.md](SECURITY_ROUND_2.md). VPS templates remain unverified on the actual server; no infrastructure changes have been made.

## References

The controls follow [OWASP authentication guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html) and [Flask security guidance](https://flask.palletsprojects.com/web-security/). Infrastructure references: [DigitalOcean firewall rules](https://docs.digitalocean.com/products/networking/firewalls/how-to/configure-rules/), [nginx request limits](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html), and [systemd sandbox settings](https://manpages.debian.org/trixie/systemd/systemd.exec.5.en.html).
