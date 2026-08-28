# Security Policy

## Reporting a vulnerability

**Please do not report security issues through public GitHub issues.**

Email **info@parallaxtech.co.uk** with:

- A description of the issue and why you believe it is a security problem
- Steps to reproduce, or a proof of concept
- The version or commit you tested against
- Any suggested mitigation

You will get an acknowledgement within **3 working days**, and an assessment
with a proposed timeline within **10 working days**. We will keep you informed
while a fix is prepared, and — unless you prefer otherwise — credit you when it
ships.

Please give us a reasonable opportunity to fix an issue before disclosing it
publicly.

## What is in scope

This is software that holds credentials and can place trades, so we take the
following seriously:

- Anything that could expose API keys, broker credentials or database
  credentials
- Anything that could cause an unintended order to be placed, or a protective
  order (stop-loss) to be omitted or cancelled
- Anything allowing code execution through untrusted input — news headlines,
  API responses, or LLM output are all untrusted by design
- SQL injection, credential leakage into logs, or unsafe deserialisation
- Dependency vulnerabilities that are actually reachable from this codebase

## What is out of scope

- **Losing money.** A strategy that performs badly is not a security
  vulnerability. See [`docs/RESULTS.md`](docs/RESULTS.md) — the bundled
  reference strategy is measured as unprofitable, and that is documented rather
  than hidden.
- Vulnerabilities in third-party services (Trading 212, Benzinga, Finnhub,
  Twelve Data, Anthropic) — please report those to the vendor directly.
- Issues requiring an attacker to already have access to your `.env` file, your
  server, or your GitHub account.
- Missing hardening that has no demonstrated exploit path.

## Practices in this repository

- **No credentials are committed.** `.env` is git-ignored and has never been
  committed; the full history has been scanned to confirm this.
- **Secret scanning and push protection** are enabled on this repository.
- **Dependabot** monitors dependencies for known vulnerabilities.
- All configuration is supplied through environment variables, never hard-coded.
- The system **fails closed** on data unavailability: if it cannot verify a
  price, it does not trade.

## A note for anyone running this

The largest security risk in operating this software is not in the code — it is
that a broker API key grants full power over a real account. Treat that
credential accordingly:

- Use a **demo/paper account** until you have a specific reason not to.
- Never commit `.env`, and never paste keys into an issue, a pull request or a
  chat log.
- Restrict file permissions on `.env` (`chmod 600`).
- Rotate keys if you suspect exposure, and after any machine you used them on is
  decommissioned.
