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
reporting/report.py
────────────────────
Generates a human-readable performance summary from the trade log.
Run directly:  python -m reporting.report
Or call generate_report() programmatically.
"""

import logging
from datetime import datetime, timezone
from config.settings import cfg
from storage.database import get_conn

logger = logging.getLogger(__name__)


def generate_report() -> str:
    """Return a plain-text performance report from all closed trades."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM trades WHERE status = 'closed' AND mode = %s ORDER BY sell_time DESC",
                (cfg.trading_mode,),
            )
            trades = cur.fetchall()
            cur.execute(
                "SELECT * FROM trades WHERE status = 'open' AND mode = %s",
                (cfg.trading_mode,),
            )
            open_trades = cur.fetchall()

    if not trades and not open_trades:
        return "No trades recorded yet."

    lines = []
    lines.append("=" * 60)
    lines.append("  MOMENTUM TRADER — PERFORMANCE REPORT")
    lines.append(f"  Mode:      {cfg.trading_mode.upper()}")
    lines.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)

    # ── Open positions ────────────────────────────────────────────────────────
    if open_trades:
        lines.append(f"\nOPEN POSITIONS ({len(open_trades)})")
        lines.append("-" * 40)
        for t in open_trades:
            lines.append(
                f"  {t['ticker']:<20} bought @ ${t['buy_price']:.4f}  "
                f"qty={t['quantity']:.4f}  opened {t['buy_time'][:16]}"
            )

    if not trades:
        lines.append("\nNo closed trades yet.")
        return "\n".join(lines)

    # ── Closed trade summary ──────────────────────────────────────────────────
    total_pnl = sum(t["profit_loss"] for t in trades if t["profit_loss"] is not None)
    wins = [t for t in trades if (t["profit_loss"] or 0) > 0]
    losses = [t for t in trades if (t["profit_loss"] or 0) <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    lines.append(f"\nCLOSED TRADES ({len(trades)})")
    lines.append("-" * 40)
    lines.append(f"  Win rate:    {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    lines.append(f"  Total P&L:   £{total_pnl:+.2f}")
    if wins:
        avg_win = sum(t["profit_loss"] for t in wins) / len(wins)
        lines.append(f"  Avg win:     £{avg_win:+.2f}")
    if losses:
        avg_loss = sum(t["profit_loss"] for t in losses) / len(losses)
        lines.append(f"  Avg loss:    £{avg_loss:+.2f}")

    # ── Exit reason breakdown ─────────────────────────────────────────────────
    reasons: dict[str, int] = {}
    for t in trades:
        r = t["exit_reason"] or "unknown"
        reasons[r] = reasons.get(r, 0) + 1

    lines.append("\n  Exit reasons:")
    for reason, count in sorted(reasons.items()):
        lines.append(f"    {reason:<20} {count}")

    # ── Last 10 trades ────────────────────────────────────────────────────────
    lines.append("\nLAST 10 TRADES")
    lines.append("-" * 60)
    for t in trades[:10]:
        pnl = t["profit_loss"] or 0
        pnl_pct = t["profit_loss_pct"] or 0
        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"  {t['ticker']:<18} {sign}£{pnl:.2f} ({sign}{pnl_pct:.2f}%)  "
            f"[{t['exit_reason'] or '?'}]  {(t['sell_time'] or '')[:16]}"
        )

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_report())
