#!/usr/bin/env bash
# Executed over SSH by deploy-backend.yml, using the existing VPS deployment user.
set -Eeuo pipefail
umask 022
phase="preflight"
trap 'printf "Backend deployment failed during %s (line %s).\n" "$phase" "$LINENO" >&2' ERR

fail() {
  echo "Deployment preflight failed: $*" >&2
  exit 1
}

revision=${1:?A Git commit SHA is required}
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid deployment revision" >&2
  exit 1
fi

backend=/var/www/tanoti-returns/backend
archive="$HOME/tanoti-backend-$revision.tar.gz"
python="$backend/.venv/bin/python"
service=tanoti-returns.service

# This pipeline uses the existing backend/.env. Preflight before changing code.
echo "Checking deployment access as $(id -un)"
test -d "$backend" || fail "Backend directory does not exist: $backend"
test -w "$backend" || fail "$(id -un) cannot write to $backend"
test -r "$backend/.env" || fail "$(id -un) cannot read $backend/.env, or the file is missing"
test -x "$python" || fail "Virtual environment Python is missing or inaccessible: $python"
test -w "$backend/.venv" || fail "$(id -un) cannot write to $backend/.venv"
test -r "$archive" || fail "Uploaded archive is missing or unreadable: $archive"
command -v curl > /dev/null || fail "curl is not installed"
sudo -n -l /usr/bin/systemctl restart "$service" > /dev/null || \
  fail "$(id -un) needs passwordless sudo for: /usr/bin/systemctl restart $service"

cd "$backend"
# Overlay code only: never delete .env, .venv, uploads, or VPS-only migrations.
phase="extracting backend code"
echo "$phase"
# ACL write access does not allow changing another user's directory metadata.
# Keep existing directories intact while updating packaged file contents.
tar --no-same-owner --no-same-permissions --no-overwrite-dir --touch \
  -xzf "$archive" -C "$backend"
# Only change modes on files/directories we own; existing directory ACLs remain.
find app migrations deploy run.py requirements.lock requirements.txt \
  -user "$(id -un)" -exec chmod a+rX {} +
phase="installing dependencies"
echo "$phase"
"$python" -m pip install --disable-pip-version-check -r requirements.lock

# Load production settings without shell-sourcing .env or printing credentials.
phase="database migration"
echo "$phase"
"$python" -m dotenv -f "$backend/.env" run -- \
  "$python" -m flask --app run db upgrade

phase="service restart"
echo "$phase"
sudo -n /usr/bin/systemctl restart "$service"

# No cookie means 401 is the healthy response; allow time for Gunicorn startup.
phase="startup check"
echo "$phase"
healthy=false
for attempt in $(seq 1 15); do
  status=$(curl --silent --max-time 5 --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:8010/api/auth/session) || status=000
  if [[ "$status" == 401 ]]; then
    healthy=true
    break
  fi
  sleep 2
done
if [[ "$healthy" != true ]]; then
  echo "Backend failed its startup check. Inspect tanoti-returns.service logs on the VPS." >&2
  exit 1
fi

printf '%s\n' "$revision" > .deployed-revision
rm -f -- "$archive"
echo "Backend deployed successfully: $revision"

