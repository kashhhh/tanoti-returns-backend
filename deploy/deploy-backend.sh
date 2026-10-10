#!/usr/bin/env bash
# Executed over SSH by deploy-backend.yml, using the existing VPS deployment user.
set -euo pipefail
umask 022

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
test -d "$backend"
test -w "$backend"
test -r "$backend/.env"
test -x "$python"
test -r "$archive"
command -v curl > /dev/null
sudo -n -l /usr/bin/systemctl restart "$service" > /dev/null

cd "$backend"
# Overlay code only: never delete .env, .venv, uploads, or VPS-only migrations.
tar --no-same-owner --no-same-permissions -xzf "$archive" -C "$backend"
chmod -R a+rX app migrations deploy
chmod a+r run.py requirements.lock requirements.txt
"$python" -m pip install --disable-pip-version-check -r requirements.lock

# Load production settings without shell-sourcing .env or printing credentials.
"$python" -m dotenv -f "$backend/.env" run -- \
  "$python" -m flask --app run db upgrade

sudo -n /usr/bin/systemctl restart "$service"

# No cookie means 401 is the healthy response; allow time for Gunicorn startup.
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

