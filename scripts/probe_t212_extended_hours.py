# Licensed to ParallaxTech Ltd under one or more contributor licence
# agreements. See the NOTICE file distributed with this work for additional
# information regarding copyright ownership.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
scripts/probe_t212_extended_hours.py
─────────────────────────────────────
One-shot probe of the T212 DEMO API's extended-hours (24/5) order support.

WHY: the public API docs list `extendedHours` on market orders; community
reports say the limit/stop endpoints reject it with HTTP 400. The v21 code
feature-detects at runtime (trading/executor._extended_limit_supported), but
this probe gives a definitive answer up front so the answer is knowledge, not
a runtime surprise.

WHAT IT DOES (demo account only, nothing can fill):
  1. POST a LIMIT buy far BELOW market ($100 vs AAPL ~$230) with
     extendedHours=true  → accepted or 400?
  2. Same LIMIT without the flag (control: proves the payload is otherwise
     valid).
  3. POST a STOP buy far ABOVE market ($500) with extendedHours=true.
  Every accepted order is cancelled immediately.

RUN IT ON THE VM (the demo keys live in /opt/tapewatch/.env):
  cd /opt/tapewatch && .venv/bin/python scripts/probe_t212_extended_hours.py

It refuses to run against the live API.
"""

import base64
import json
import sys
import time

import requests
from dotenv import dotenv_values

BASE = "https://demo.trading212.com/api/v0"  # demo ONLY — never live


def main() -> None:
    env = dotenv_values(".env")
    key_id = env.get("TRADING212_DEMO_API_KEY_ID", "")
    key = env.get("TRADING212_DEMO_API_KEY", "")
    if not key_id or not key:
        sys.exit("TRADING212_DEMO_API_KEY(_ID) not found in .env — run from the app directory")

    headers = {
        "Authorization": "Basic "
        + base64.b64encode(f"{key_id}:{key}".encode()).decode(),
        "Content-Type": "application/json",
    }

    def post(path: str, payload: dict) -> tuple[int, str]:
        r = requests.post(BASE + path, headers=headers, json=payload, timeout=10)
        return r.status_code, r.text[:300]

    def cancel(body: str) -> None:
        try:
            oid = json.loads(body).get("id")
        except (ValueError, AttributeError):
            return
        if oid:
            r = requests.delete(f"{BASE}/equity/orders/{oid}", headers=headers, timeout=10)
            print(f"    cancelled order {oid} → HTTP {r.status_code}")

    probes = [
        ("LIMIT + extendedHours", "/equity/orders/limit",
         {"quantity": 0.01, "ticker": "AAPL_US_EQ", "limitPrice": 100.0,
          "timeValidity": "DAY", "extendedHours": True}),
        ("LIMIT control (no flag)", "/equity/orders/limit",
         {"quantity": 0.01, "ticker": "AAPL_US_EQ", "limitPrice": 100.0,
          "timeValidity": "DAY"}),
        ("STOP + extendedHours", "/equity/orders/stop",
         {"quantity": 0.01, "ticker": "AAPL_US_EQ", "stopPrice": 500.0,
          "timeValidity": "DAY", "extendedHours": True}),
    ]

    print("T212 DEMO extended-hours capability probe")
    print("=" * 60)
    for name, path, payload in probes:
        sc, body = post(path, payload)
        verdict = "ACCEPTED" if sc == 200 else f"REJECTED (HTTP {sc})"
        print(f"  {name}: {verdict}")
        print(f"    response: {body}")
        if sc == 200:
            cancel(body)
        time.sleep(1)

    print("=" * 60)
    print(
        "Interpretation: if 'LIMIT + extendedHours' was REJECTED while the\n"
        "control was ACCEPTED, T212 still limits the flag to market orders —\n"
        "which is what the v21 code assumes until its runtime feature-detect\n"
        "sees otherwise. If it was ACCEPTED, bounded-slippage limit exits are\n"
        "available in extended sessions automatically (the executor probes\n"
        "per-process and uses them when accepted)."
    )


if __name__ == "__main__":
    main()
