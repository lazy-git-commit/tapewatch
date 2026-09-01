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
# Values are passed as psql variables rather than pasted into the SQL text, so a
# password containing a quote cannot terminate the literal and alter the
# statement. :'x' interpolates as a quoted literal, :"x" as a quoted identifier.
#
# Every statement is fed through STDIN, never `psql -c`. The -c form requires a
# string "completely parsable by the server" and deliberately performs no
# psql-side interpolation, so :"role" reaches PostgreSQL verbatim and fails with
# `syntax error at or near ":"`. Interpolation happens only for input read from
# stdin or a file.
cd /tmp

role_exists=$(sudo -u postgres psql -tA -v role="${DB_USER}" << 'SQL'
SELECT 1 FROM pg_roles WHERE rolname = :'role';
SQL
)

if [ -z "${role_exists}" ]; then
  sudo -u postgres psql -v ON_ERROR_STOP=1 -v role="${DB_USER}" -v pw="${DB_PASSWORD}" << 'SQL'
CREATE ROLE :"role" WITH LOGIN PASSWORD :'pw';
SQL
else
  # Keep the role's password in step with the secret, so rotating the secret is
  # sufficient — no manual step on the host.
  sudo -u postgres psql -v ON_ERROR_STOP=1 -v role="${DB_USER}" -v pw="${DB_PASSWORD}" << 'SQL'
ALTER ROLE :"role" WITH LOGIN PASSWORD :'pw';
SQL
fi

db_exists=$(sudo -u postgres psql -tA -v db="${DB_NAME}" << 'SQL'
SELECT 1 FROM pg_database WHERE datname = :'db';
SQL
)

if [ -z "${db_exists}" ]; then
  # CREATE DATABASE cannot run inside a transaction block, so it gets its own
  # invocation rather than being grouped with anything above.
  sudo -u postgres psql -v ON_ERROR_STOP=1 -v db="${DB_NAME}" -v owner="${DB_USER}" << 'SQL'
CREATE DATABASE :"db" OWNER :"owner";
SQL
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

# Make GRAFANA_ADMIN_PASSWORD authoritative.
#
# Grafana keeps the admin password in its OWN database, and the value in the
# config file is applied only when that database is first created. Setting the
# secret therefore does nothing on an existing install, and the API call below
# fails with 401 against a password Grafana has never held. Reset it explicitly
# on every run, so rotating the secret is sufficient — the same property the
# database role has above.
#
# Consequence worth knowing: a password changed in the Grafana UI is reverted by
# the next deploy. The secret is the source of truth.
if echo "${GRAFANA_ADMIN_PASSWORD}" \
     | grafana-cli --homepath /usr/share/grafana \
         admin reset-admin-password --password-from-stdin > /dev/null 2>&1; then
  echo "Grafana admin password set."
elif grafana-cli --homepath /usr/share/grafana \
       admin reset-admin-password "${GRAFANA_ADMIN_PASSWORD}" > /dev/null 2>&1; then
  # Older grafana-cli has no --password-from-stdin. The password is then visible
  # in the process list for the moment the command runs, which is why this is
  # the fallback and not the first choice.
  echo "Grafana admin password set (legacy CLI form)."
else
  echo "::error::Could not set the Grafana admin password."
  exit 1
fi

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
