#!/usr/bin/env bash
#
# Provisions PostgreSQL and Grafana on the target host, then imports the
# dashboard.
#
# Every host-specific value arrives through the environment, so this script
# contains no install path, database name, user or password. The deploy
# workflow exports them from GitHub Actions secrets before invoking it.
#
# Required environment:
#   DEPLOY_PATH             absolute install directory
#   DB_NAME                 database name
#   DB_USER                 database role
#   DB_PASSWORD             database role password
#   GRAFANA_ADMIN_PASSWORD  Grafana admin password
#
# Idempotent by design — it runs on every deploy, not just the first.

set -euo pipefail

: "${DEPLOY_PATH:?DEPLOY_PATH is required}"
: "${DB_NAME:?DB_NAME is required}"
: "${DB_USER:?DB_USER is required}"
: "${DB_PASSWORD:?DB_PASSWORD is required}"
: "${GRAFANA_ADMIN_PASSWORD:?GRAFANA_ADMIN_PASSWORD is required}"

PROV_BASE="/etc/grafana/provisioning"
REPO_DIR="${DEPLOY_PATH}/grafana"

# ── 1. PostgreSQL ─────────────────────────────────────────────────────────────
if ! rpm -q postgresql-server &>/dev/null; then
  dnf install -y postgresql-server postgresql-contrib
  postgresql-setup --initdb
  systemctl enable postgresql
  systemctl start postgresql
fi
systemctl is-active --quiet postgresql || systemctl start postgresql

# ── 2. Password auth for local connections ────────────────────────────────────
PG_HBA=$(cd /tmp && sudo -u postgres psql -t -P format=unaligned -c "SHOW hba_file;")
if grep -qE "^host\s+all\s+all\s+127\.0\.0\.1" "${PG_HBA}"; then
  sed -i 's/^host\s\+all\s\+all\s\+127\.0\.0\.1.*$/host    all             all             127.0.0.1\/32            md5/' "${PG_HBA}"
else
  echo "host    all             all             127.0.0.1/32            md5" >> "${PG_HBA}"
fi
if grep -qE "^host\s+all\s+all\s+::1" "${PG_HBA}"; then
  sed -i 's/^host\s\+all\s\+all\s\+::1.*$/host    all             all             ::1\/128                 md5/' "${PG_HBA}"
else
  echo "host    all             all             ::1/128                 md5" >> "${PG_HBA}"
fi
systemctl reload postgresql

# ── 3. Role and database (idempotent) ─────────────────────────────────────────
# Values are passed as psql parameters rather than interpolated into the SQL
# string, so a password containing a quote cannot break or alter the statement.
cd /tmp
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname = '${DB_USER}'" | grep -q 1; then
  sudo -u postgres psql -v ON_ERROR_STOP=1 \
    -v role="${DB_USER}" -v pw="${DB_PASSWORD}" \
    -c "CREATE ROLE :\"role\" WITH LOGIN PASSWORD :'pw';"
else
  # Keep the role's password in step with the secret, so rotating the secret
  # is sufficient — no manual step on the host.
  sudo -u postgres psql -v ON_ERROR_STOP=1 \
    -v role="${DB_USER}" -v pw="${DB_PASSWORD}" \
    -c "ALTER ROLE :\"role\" WITH LOGIN PASSWORD :'pw';"
fi

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1; then
  sudo -u postgres psql -v ON_ERROR_STOP=1 \
    -v db="${DB_NAME}" -v owner="${DB_USER}" \
    -c "CREATE DATABASE :\"db\" OWNER :\"owner\";"
fi

# ── 4. Grafana ────────────────────────────────────────────────────────────────
if ! rpm -q grafana &>/dev/null; then
  cat > /etc/yum.repos.d/grafana.repo << 'REPO'
[grafana]
name=grafana
baseurl=https://rpm.grafana.com
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://rpm.grafana.com/gpg.key
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
REPO
  dnf install -y grafana
fi

# ── 5. Datasource provisioning ────────────────────────────────────────────────
# grafana/provisioning/datasources/postgres.yaml reads ${GF_TRADER_DB_*} from
# the environment rather than carrying a committed password. Grafana expands
# ${VAR} in provisioning files at load time, so the values are supplied through
# a systemd drop-in that is written here and never committed.
mkdir -p "${PROV_BASE}/datasources"
cp "${REPO_DIR}/provisioning/datasources/postgres.yaml" "${PROV_BASE}/datasources/postgres.yaml"

# Leftovers from earlier provisioning approaches.
rm -f "${PROV_BASE}/datasources/sqlite.yaml"
rm -f "${PROV_BASE}/dashboards/dashboards.yaml"
rm -f "${PROV_BASE}/dashboards/json/momentum_trader.json"

chown -R grafana:grafana "${PROV_BASE}"

mkdir -p /etc/systemd/system/grafana-server.service.d
cat > /etc/systemd/system/grafana-server.service.d/datasource.conf << DROPIN
[Service]
Environment="GF_TRADER_DB_NAME=${DB_NAME}"
Environment="GF_TRADER_DB_USER=${DB_USER}"
Environment="GF_TRADER_DB_PASSWORD=${DB_PASSWORD}"
DROPIN
chmod 600 /etc/systemd/system/grafana-server.service.d/datasource.conf

# ── 6. Start Grafana ──────────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable grafana-server
systemctl restart grafana-server
systemctl is-active --quiet grafana-server && echo "Grafana is running on :3000"

# ── 7. Import the dashboard through the API ───────────────────────────────────
echo "Waiting for Grafana to be ready..."
for _ in $(seq 1 20); do
  if curl -sf http://localhost:3000/api/health > /dev/null 2>&1; then
    echo "Grafana is ready."
    break
  fi
  sleep 2
done

# The password reaches Python through the environment, never argv — a command
# line is readable by any local user via /proc.
DASHBOARD_PATH="${REPO_DIR}/dashboards/momentum_trader.json" \
python3 - << 'PYEOF'
import base64
import json
import os
import urllib.error
import urllib.request

password = os.environ["GRAFANA_ADMIN_PASSWORD"]
credentials = base64.b64encode(f"admin:{password}".encode()).decode()

with open(os.environ["DASHBOARD_PATH"]) as handle:
    dashboard = json.load(handle)

dashboard["id"] = None  # let Grafana assign the id

payload = json.dumps({
    "dashboard": dashboard,
    "overwrite": True,
    "folderId": 0,
}).encode()

request = urllib.request.Request(
    "http://localhost:3000/api/dashboards/db",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Basic {credentials}",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(request) as response:
        result = json.loads(response.read())
        print(f"Dashboard imported: {result.get('status')}")
except urllib.error.HTTPError as exc:
    # Deliberately does not echo the response body — it can quote the request,
    # and these logs are public once the repository is.
    print(f"Dashboard import failed: HTTP {exc.status}")
    raise
PYEOF
