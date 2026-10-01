# Complete VPS security rollout — 29 September 2026

This consolidates the earlier security rounds and the latest operational changes.
It supersedes the deployment assumptions in SECURITY.md and SECURITY_ROUND_2.md.
It is a checklist for manual application, not confirmation that the VPS is secure.
No VPS access, infrastructure changes, or fresh dependency audit were performed for this guide.

Known live setup from the server logs:

| Setting | Live value |
| --- | --- |
| Backend | /var/www/tanoti-returns/backend |
| Virtual environment | /var/www/tanoti-returns/backend/.venv |
| systemd service | tanoti-returns.service |
| Gunicorn listener | 127.0.0.1:8010 |
| Website | https://returns.tanotiofficial.com |

The installed OS version, nginx configuration, service user, firewall, environment
location and backup status have not been inspected. Confirm these before changes.
The existing deploy templates use /srv/tanoti, venv, port 8000 and tanoti.service;
do not copy them unchanged or start a second API service.

## 1. Recovery and safe access — do first

- [ ] Take a database backup and Droplet snapshot. Save nginx/systemd configuration
  and all migration files, including files created only on the VPS. Store copies
  outside the public web root with restricted access.
- [ ] Keep the current SSH session open and ensure DigitalOcean console recovery works.
- [ ] Create a personal non-root sudo account with an SSH key. Test a second login
  and sudo before restricting root/password login or changing firewall rules.
- [ ] Enable MFA on DigitalOcean, administrator email, Shopify and source control.
  Store recovery codes securely; remove former staff access and unused SSH keys.

## 2. Network exposure

- [ ] Attach a DigitalOcean Cloud Firewall to this Droplet. Permit public TCP 80/443;
  permit the actual SSH port only from administrator IPs or a trusted VPN. Cover
  IPv6 too. If your IP changes frequently, establish a stable VPN before restricting SSH.
- [ ] Allow no public access to 8010, 8000, 5000, 5173, 5432 or any unused service.
  Preserve other deliberately hosted services and required outbound HTTPS/DNS/NTP.
- [ ] If using UFW, match these restrictions and allow the actual SSH port/source
  before enabling it. Review existing rules; do not blindly reset a shared server.
- [ ] Keep Gunicorn on 127.0.0.1:8010. Same-host PostgreSQL must listen only locally.
  Do not expose Flask debug mode, Vite, database administration tools or test fixtures.
- [ ] Inspect `sudo ss -lntp` and verify externally that private ports cannot be reached.

Reference: [DigitalOcean firewall rules](https://docs.digitalocean.com/products/networking/firewalls/how-to/configure-rules/).

## 3. SSH and operating-system updates

- [ ] After testing the new administrator account, set effective SSH policy to:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
```

Preserve keyboard-interactive authentication if it is required for configured MFA.
Inspect existing sshd drop-ins: adding a later file may not override an earlier value.
Validate with `sudo sshd -t` and inspect `sudo sshd -T` (including relevant Match
conditions). Reload the installed SSH service and test a new connection before
closing the original session.

- [ ] Use a supported OS release, install security updates for the OS, OpenSSH,
  nginx, PostgreSQL and Python, and schedule necessary reboots.
- [ ] On Ubuntu/Debian, review `sudo apt update` and `sudo apt upgrade` during a
  maintenance window. Configure unattended security upgrades; verify they run and
  decide how required service restarts/reboots are handled.
- [ ] Optional: add fail2ban for SSH if SSH cannot be restricted to trusted sources.
  It supplements key authentication and firewall rules.

References: [Ubuntu SSH](https://ubuntu.com/server/docs/how-to/security/openssh-server/),
[automatic updates](https://ubuntu.com/server/docs/how-to/software/automatic-updates/).

## 4. Production credentials and configuration

- [ ] Use a dedicated non-login account, such as tanoti, for API and scheduled jobs.
  The API must not run as root.
- [ ] Keep code and .venv owned by root/deployment user, readable but not writable
  by the API account. Give it write access only to private application data/temp.
- [ ] Prefer /etc/tanoti/backend.env, owned by root, mode 0600, loaded by systemd
  EnvironmentFile. All jobs and migration commands must load the same settings.
  systemd can load this file before switching to the unprivileged service user.
  If retaining backend/.env instead, restrict it to the required account/group
  and ensure it is outside the nginx web root and inaccessible over HTTP.
- [ ] Review these production values; retain actual provider credentials:

```dotenv
APP_ENV=production
TESTING_MODE=false
CORS_ORIGINS=https://returns.tanotiofficial.com
SHOP_CURRENCY=INR
DELHIVERY_ENVIRONMENT=production
PHOTOS_STORAGE_DIR=/var/lib/tanoti/uploads
PHOTO_RETENTION_DAYS=120
MAX_PHOTO_STORAGE_MB=500
```

- [ ] SECRET_KEY and ADMIN_SECRET_KEY must be different random secrets, at least
  32 characters each. Generate each separately with
  `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` if replacing weak
  or exposed values. Keep a strong existing SECRET_KEY stable: changing it
  invalidates sessions and OTPs. Never put credentials in frontend VITE variables.
- [ ] Preserve the working live INR Shopify domain/token, verified API version,
  webhook signing secret, Resend sender/key, Delhivery token and registered pickup
  location. Do not replace working values with example-file defaults.
- [ ] Review the admin allowlist in app/admin_setup.py. Remove unused staff access.
- [ ] Rotate any secret exposed through Git, public files, logs or shared archives;
  deleting the exposed file alone does not revoke the credential. Scope provider
  access to the permissions this integration actually needs.

## 5. Private customer photos — high priority

- [ ] Every /uploads/ request must proxy through Flask authorization. Remove public
  upload aliases/static routes from every enabled nginx server block, CDN or old
  hostname. A static alias bypasses the app's staff-only protection.
- [ ] Store images outside the web root, preferably /var/lib/tanoti/uploads, owned
  by the API user with directory mode 0700. When moving existing photos, briefly
  pause writes, copy the full returns/<request>/<file>.jpg structure and verify it
  before changing PHOTOS_STORAGE_DIR. Keep the old copy private until verified.
- [ ] Test a real existing photo URL signed out and as a customer: neither may
  retrieve the image. An authorized administrator must still see the preview.
- [ ] Review 120-day retention and 500 MB storage budget against your dispute and
  traffic needs. Cleanup deletes eligible evidence; do not enable it without
  confirming retention and backup requirements.

## 6. nginx, HTTPS and trusted proxy settings

- [ ] Adapt deploy/nginx.conf into the existing site. Replace ALL upstream
  127.0.0.1:8000 occurrences with 127.0.0.1:8010. Use the actual frontend dist path
  (normally /var/www/tanoti-returns/frontend/dist; verify it). Serve only dist.
- [ ] Retain the valid certificate, redirect HTTP to HTTPS, use TLS 1.2/1.3, and
  verify renewal. If using Certbot, test `sudo certbot renew --dry-run`.
- [ ] Apply the template's CSP, HSTS, nosniff, frame denial, Referrer-Policy and
  Permissions-Policy. Do not add HSTS includeSubDomains/preload without reviewing
  other subdomains. Recheck header inheritance when editing nginx locations.
- [ ] Keep API/private-photo responses uncached by nginx and any CDN. Confirm
  Cache-Control: no-store. Never cache authenticated requests by URL alone.
- [ ] Enable login/API request limits and connection/time limits. Retain 4 KB auth
  bodies, 2 MB webhook bodies and 22 MB overall uploads: the app permits two 10 MB
  photos. Verify the UI displays oversized-upload errors, including proxy rejections.
- [ ] Deny dotfiles, database dumps, archives, source maps and other private files.
  Keep backups, repository source, .venv and .env outside the served directory.
- [ ] For direct client -> nginx -> Gunicorn, overwrite X-Forwarded-For with
  $remote_addr and X-Forwarded-Proto with $scheme, then enable TRUST_PROXY=true.
  Do not pass untrusted client-supplied forwarded headers through unchanged.
  If Cloudflare or another proxy is in front, configure trusted real-IP ranges
  for that topology first; otherwise rate limits can use the wrong client IP.
- [ ] Validate with `sudo nginx -t` before `sudo systemctl reload nginx`.
  Test customer/admin login, photos, mobile camera/file upload and checkout links.

## 7. Harden the existing API service

- [ ] Adapt deploy/tanoti.service to the existing tanoti-returns.service; preserve
  /var/www/tanoti-returns/backend, .venv and 127.0.0.1:8010.
- [ ] Apply User/Group, UMask=0077, NoNewPrivileges, PrivateTmp, PrivateDevices,
  ProtectSystem, ProtectHome, kernel/control-group restrictions, empty capability
  set and limited writable paths from the template. Create private data directories
  first. Test provider access and photo reads/writes after hardening.
- [ ] Match worker count, memory and task limits to actual Droplet RAM and load;
  the template's 768 MB cap is an example, not a measured requirement.
- [ ] Use daemon-reload after unit edits and restart tanoti-returns. Verify status,
  logs and listener address. Apply equivalent restrictions to background jobs.

## 8. Database and migration integrity

- [ ] Keep PostgreSQL private. Use SCRAM password authentication for password-based
  access; verify app connectivity before changing pg_hba.conf rules.
- [ ] The app role must not be superuser and should have no CREATEDB/CREATEROLE.
  Prefer a separate migration role with DDL rights; grant runtime access to all
  required tables/sequences, including new tables, with appropriate default grants.
- [ ] Preserve production-only revisions 6c9b3b3c56cc, 66ffdc22dd67, their required
  ancestors and the latest generated merge file in source control. Retrieve them
  from the VPS; the local checkout does not contain this complete history.
- [ ] Check db heads and db current in the actual service environment. After the
  merge discussed in this task, there should be one head and the database should
  be at it; a74e13b95c02 must be among its applied ancestors. The new merge ID,
  rather than a74e13b95c02 itself, becomes the head. Do not regenerate merges if
  already completed, delete history, reset the database or use stamp to skip DDL.
- [ ] All earlier security migrations and the newest refund/shipping migration
  are required. Back up before upgrades, deploy migrations first, and verify
  success before restarting workers. A healthy systemd status alone does not prove
  the application can query its database.

## 9. Application releases and dependency checks

- [ ] Install backend dependencies from requirements.lock using the deployed
  Python 3.12 .venv. Build the frontend with npm ci and npm run build, using
  VITE_API_BASE_URL=/api. Deploy backend and frontend together.
- [ ] Audit the current Python and npm lockfiles and the actual deployed packages.
  The earlier clean audit is historical, not assurance that today's packages are
  vulnerability-free. Review upgrades and run regression checks before release;
  do not blindly use force-upgrade commands.
- [ ] Do not deploy security_browser_fixture.py as a running service or expose test
  credentials. Never run a production frontend through Vite's development server.
- [ ] Keep signed webhooks reachable; verify rejection of invalid signatures and
  successful processing of legitimate callbacks. Do not disable verification to
  fix a delivery problem.

## 10. Jobs, monitoring and backups

- [ ] Schedule cleanup-security daily and cleanup-photos under the chosen retention
  policy. Adapt the cleanup service's /srv paths and venv name before installation.
- [ ] Keep one retry-emails worker scheduled (for example, every minute), prevent
  overlapping runs, and alert on delivery failures/backlog. This command sends
  queued customer messages; it is not a read-only health check.
- [ ] Keep sync-delhivery scheduled (existing template: every 15 minutes), using
  the same environment and service account. Track failed or stale refreshes.
- [ ] Do not re-enable optional Shopify return/inventory sync for this rollout.
  Order access, webhook processing and gift cards still need their working access.
- [ ] Monitor website availability, 5xx/429 spikes, repeated auth failures, job
  failures, CPU/RAM/disk, PostgreSQL growth, TLS expiry and backup completion.
  Needs attention handles workflow problems; it does not replace server monitoring.
- [ ] Keep nginx logs free of query strings and avoid recording OTPs, secrets,
  authorization/cookie headers, full request bodies or gift-card codes. Protect
  and rotate logs. Retain staff action records and unresolved financial evidence.
- [ ] Make automated encrypted off-Droplet backups of PostgreSQL and required
  photos, restrict access, define retention and recovery targets, and periodically
  restore both to a separate environment. Keep credentials in a separate restricted
  recovery process; do not ship them in ordinary deployment archives.

## 11. Final acceptance and emergency response

- [ ] Verify HTTPS, security headers, actual Secure/HttpOnly/SameSite cookies,
  customer/admin sign-in, logout and logout-all on the deployed site.
- [ ] Verify missing/wrong CSRF tokens cannot mutate data, customer sessions cannot
  access staff APIs/photos, and one customer cannot read another customer's orders.
- [ ] Test OTP throttling, expiration and one-time use in staging. Exercise
  concurrent requests against PostgreSQL with multiple workers; SQLite tests do
  not prove the live locking behavior.
- [ ] Verify private ports/files are unreachable, valid uploads work and oversized
  images show a useful error. Check CSP failures and mobile behavior.
- [ ] Test duplicate webhook, gift-card uncertainty and shipment uncertainty
  scenarios in staging. Never retry an uncertain financial/carrier action by
  deleting its durable record. Reconcile against the provider first.
- [ ] Keep an incident procedure: restrict access if needed, preserve logs, revoke
  sessions with `flask --app run.py revoke-sessions` in the service environment,
  rotate affected provider/server credentials, patch the cause and restore only
  from verified backups. Revocation does not undo an already-running action.

Remaining improvements beyond this VPS rollout: individual phishing-resistant
staff authentication, independently retained audit logs, and measured load and
PostgreSQL concurrency testing. Local refund validation and durable shipping
controls now exist; the old round-two document's descriptions of those as entirely
unimplemented are outdated. None of these changes constitutes a penetration test
or certification of the live server.
