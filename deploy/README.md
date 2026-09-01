# Deployment

This directory holds a deployment path for a **single private host**: a
systemd unit template, a provisioning script, and the workflow in
[`../.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) that
drives them.

It is here as a worked reference. It is not required to run the system —
[`../docs/GETTING-STARTED.md`](../docs/GETTING-STARTED.md) covers running it
directly, and most people should start there.

---

## Nothing here identifies a host

Every hostname, username, install path, unit name, network tag, database name
and credential comes from **GitHub Actions secrets** at deploy time. That is
why this directory contains a `service.template` rather than a `.service` file,
and why `grafana.sh` refuses to start unless its environment is populated.

Two separate reasons, and the second is the one people miss:

1. **The workflow file is world-readable.** Anything written into it is
   published.
2. **Actions logs are world-readable too.** GitHub automatically redacts
   registered secret values wherever they appear in log output — including
   inside `rsync` output, `journalctl` excerpts, and error messages. Making the
   install path and unit name secrets is not paranoia about the YAML; it is what
   keeps them out of the logs when a health check dumps 50 lines of journal on
   failure.

**Values *derived* from secrets are not masked.** If you add a step that
computes something from a secret, register it with
`echo "::add-mask::$value"` before anything can print it. The workflow does this
for the assembled database URL.

---

## Required secrets

| Secret | What it is |
|---|---|
| `VM_HOST` | Host address the runner connects to |
| `VM_USER` | SSH user |
| `VM_SSH_KEY` | Private key for that user |
| `TAILSCALE_OAUTH_CLIENT_ID` | OAuth client id for the private network |
| `TAILSCALE_OAUTH_CLIENT_SECRET` | OAuth client secret |
| `TAILSCALE_TAG` | Network tag the runner joins as, e.g. `tag:something` |
| `DEPLOY_PATH` | Absolute install directory on the host |
| `SERVICE_NAME` | systemd unit name |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Database name, role and password |
| `DB_HOST` / `DB_PORT` | *Optional* — default to `localhost` / `5432` |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin password |
| Provider API keys | See [`../.env.example`](../.env.example) |

The workflow checks all of these are non-empty before it touches the host. A
missing secret expands to an empty string, which turns `user@host` into `@host`
and an install path into `/` — worth failing loudly for.

---

## The database password is assembled, not stored

There is deliberately **no `DB_URL` secret.** The connection string is built
from `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_HOST` and `DB_PORT` at deploy
time.

Storing both a URL and a password invites them to drift. Rotating the role
password without also editing the URL leaves the service unable to connect, and
that failure appears as a crash-loop on the host rather than as a failed deploy
— the slowest possible way to find out.

**To rotate the database password:** change the `DB_PASSWORD` secret and
redeploy. `grafana.sh` issues `ALTER ROLE … PASSWORD` on every run, so one
change updates the role, the Grafana datasource and the application's
connection string together. No manual step on the host.

The password is percent-encoded when assembled, so `@`, `:`, `/` and `#` are
all safe to use. Generate one with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Order of operations

The workflow is arranged so that a failure stops *before* it can damage a
running service:

1. **Test gate** — the suite must pass. Nothing reaches the host otherwise.
   Added after a deploy with a missing environment variable crash-looped the
   service for 18 hours unnoticed.
2. **Secret validation** — fail on an empty secret before connecting.
3. **Join the network, upload code.**
4. **Write `.env`** to a temporary file, `chmod 600`, then move it into place —
   so an interrupted transfer cannot leave a half-written file for the service
   to load.
5. **Render and install the unit**, install dependencies, then
   **`cfg.validate()` before restarting**. A missing required variable fails
   the deploy here rather than crash-looping the service.
6. **Restart, then health-check** — wait, confirm the unit is active, and check
   the journal for tracebacks. A service can start cleanly and die on its first
   real work.

`concurrency` queues deploys rather than cancelling them. A cancelled deploy can
leave new code beside an old running service.

---

## Triggers

`push` to `main` and manual dispatch, nothing else.

Never add `pull_request_target`, or any trigger a fork can influence. Those run
with repository secrets in scope, and a pull request can modify the very
workflow that consumes them.

Set an **environment protection rule** on `production` in repository settings to
require manual approval before any deploy reaches the host. The workflow already
declares `environment: production`; the rule itself is a repository setting.
