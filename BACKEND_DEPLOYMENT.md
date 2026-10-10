# GitHub Actions backend deployment

The backend repository's `.github/workflows/deploy-backend.yml` runs the full
test suite on pull requests and pushes to `master`. Successful pushes to `master`
deploy to production. It can also be run manually from Actions on `master`.
Pull requests never receive deployment credentials or deploy.

The workflow uses GitHub's [checkout](https://github.com/actions/checkout) and
[setup-python](https://github.com/actions/setup-python) actions, Python 3.12 and
the pinned `requirements.lock` dependencies. Production deployments are serialized.

## One-time GitHub configuration

In **kashhhh/tanoti-returns-backend**, create an environment named `production`
under Settings → Environments, and add these environment secrets. Frontend
repository secrets are not automatically available to the backend repository.

| Secret | Value |
| --- | --- |
| `VPS_HOST` | Existing VPS hostname or IP |
| `VPS_USER` | SSH deployment user with write access to the backend and its virtual environment |
| `VPS_SSH_KEY` | Private deployment SSH key authorized for that user |
| `VPS_HOST_KEY` | Verified OpenSSH known_hosts entry for that VPS, including hostname/IP |

Use the existing frontend values if its deployment user also has backend access.
Do not put Shopify, WhatsApp, email or database credentials in GitHub; they stay
in the existing production `backend/.env`. SSH host key checking remains enabled.

## One-time VPS configuration

This targets the live layout documented in `VPS_SECURITY_CHECKLIST.md`:

| Setting | Value |
| --- | --- |
| Backend directory | `/var/www/tanoti-returns/backend` |
| Python virtual environment | `/var/www/tanoti-returns/backend/.venv` |
| Production configuration | `/var/www/tanoti-returns/backend/.env` |
| Existing systemd service | `tanoti-returns.service` |
| Local API | `127.0.0.1:8010` |

The deployment user needs write access to the backend code and `.venv` and read
access to `.env`. Keep `.env` restricted to the required user/group; do not make
it publicly readable. The service must already be configured for these paths
and the same production settings. If the service uses an external EnvironmentFile
instead, adapt the preflight and dotenv invocation in `deploy/deploy-backend.sh`
to that file and grant only the necessary read access before enabling deployment.

Allow only the service restart without a password. Run `sudo visudo -f
/etc/sudoers.d/tanoti-returns-deploy` and insert the following, replacing
`DEPLOY_USER` with the actual `VPS_USER` (not necessarily your interactive user):

```sudoers
DEPLOY_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart tanoti-returns.service
```

From a shell as that deployment user, check:

```bash
test -w /var/www/tanoti-returns/backend
test -w /var/www/tanoti-returns/backend/.venv
test -r /var/www/tanoti-returns/backend/.env
sudo -n -l /usr/bin/systemctl restart tanoti-returns.service
```

Do not copy the older `/srv/tanoti` service template over the live service.

## What deployment changes

The workflow packages only application code, migrations, deployment scripts and
dependency manifests. It overlays these onto the existing backend, installs pinned
dependencies, upgrades the database using the production settings, then restarts
the existing service. `.env`, uploads, `.venv` and VPS-only migrations are retained.
The Git checkout itself is not advanced by this archive deployment; the last
successful deployed commit is recorded in `.deployed-revision` instead.

The startup check expects an unauthenticated `/api/auth/session` request to return
401 on port 8010. A separate public check verifies nginx can reach the API. These
checks do not send OTPs or certify live Shopify/WhatsApp delivery.

Keep database backups and use additive migrations. This is an in-place deployment
with a brief service restart, not an atomic release or automatic rollback. A failed
installation or migration can leave updated code on disk; the workflow stops and
reports failure. Investigate the Actions log and the VPS service logs before rerunning.
The backend pipeline does not deploy or alter the frontend.

