"""
Shadow-mode second classifier (Qwen-Flash via Alibaba Model Studio).

WHAT THIS IS
------------
Every batch that goes to Claude is ALSO sent to Qwen. Claude's verdict is the
only one that ever reaches a trading decision — nothing in this module returns a
value to the news pipeline. Qwen's answers are written to `qwen_scores`, and
BOTH providers' call metrics go to `classifier_calls`, so the two can be
compared later on real production traffic rather than a synthetic benchmark.

WHY
---
Claude is the only external dependency with no fallback path, and it has failed
twice in three sessions in a way retries cannot fix: 25 consecutive cycles
returning an empty classification list on 2026-08-04, then 58 cycles across 98
minutes on 2026-08-06 (overlapping the premarket watchlist build). Choosing a
fallback needs evidence on three axes, and this collects all three:

  latency    — `classifier_calls.latency_ms` per provider
  liveness   — `classifier_calls.ok` / `error_type`; the failure rows ARE the data
  prediction — join `qwen_scores` to `sentiment_scores` on article_id, which
               already carries the measured 5m/60m/120m/EOD forward returns

SAFETY PROPERTIES (all deliberate)
----------------------------------
1. Runs on a background thread. The news cycle NEVER waits for Qwen, so a slow
   or hung provider cannot delay a trading decision by even a millisecond.
2. Single worker with a bounded queue. If Qwen is slow enough that work piles
   up, batches are DROPPED rather than queued — an unbounded queue would turn a
   provider slowdown into a memory leak and a thread explosion.
3. Every exception is caught and recorded. A shadow failure is data, never an
   incident.
4. Disabled entirely unless QWEN_API_KEY and QWEN_BASE_URL are both set, so a
   missing secret degrades to "no shadow data", never to a broken news cycle.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config.settings import cfg

logger = logging.getLogger(__name__)

PROVIDER = "qwen"

# One worker: shadow scoring is strictly best-effort background work and must
# never compete with the trading path for resources. _MAX_PENDING bounds the
# backlog — see safety property 2.
_MAX_PENDING = 2
_REQUEST_TIMEOUT = 45

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_pending = 0
_pending_lock = threading.Lock()
_client = None
_client_lock = threading.Lock()
_unavailable_logged = False


def shadow_enabled() -> bool:
    return bool(cfg.qwen_api_key and cfg.qwen_base_url)


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=1,
                                       thread_name_prefix="qwen-shadow")
        return _pool


def _get_client():
    """Lazily build the OpenAI-protocol client. None if unusable."""
    global _client, _unavailable_logged
    with _client_lock:
        if _client is not None:
            return _client
        try:
            from openai import OpenAI
            _client = OpenAI(api_key=cfg.qwen_api_key,
                             base_url=cfg.qwen_base_url,
                             timeout=_REQUEST_TIMEOUT, max_retries=0)
            return _client
        except Exception as exc:
            if not _unavailable_logged:
                logger.warning("Shadow classifier unavailable (%s) — "
                               "continuing with Claude only", exc)
                _unavailable_logged = True
            return None


def _openai_tool(tool: dict) -> dict:
    """
    Translate our Anthropic tool definition to OpenAI function-tool shape.

    Derived from the live _CLASSIFY_TOOL rather than hand-copied: if the schema
    changes, the shadow follows automatically and stays a like-for-like test.
    """
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


def _extract_usage(resp) -> dict:
    out = {"tokens_in": None, "tokens_out": None, "tokens_cached": None}
    usage = getattr(resp, "usage", None)
    if not usage:
        return out
    out["tokens_in"] = getattr(usage, "prompt_tokens", None)
    out["tokens_out"] = getattr(usage, "completion_tokens", None)
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        out["tokens_cached"] = getattr(details, "cached_tokens", None)
    return out


def _run(articles: list[dict], user_message: str) -> None:
    """Body of one shadow scoring job. Runs on the background thread."""
    # Imported here rather than at module scope to avoid a circular import
    # (fetcher imports this module).
    from news.fetcher import CATALYST_TYPES, _CLASSIFY_TOOL, _SYSTEM_PROMPT
    from storage.database import record_classifier_call, save_qwen_scores

    client = _get_client()
    if client is None:
        return

    by_id = {str(a.get("id")): a for a in articles}
    started = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=cfg.qwen_model,
            temperature=0,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            tools=[_openai_tool(_CLASSIFY_TOOL)],
            tool_choice={"type": "function",
                         "function": {"name": _CLASSIFY_TOOL["name"]}},
        )
    except Exception as exc:
        # A shadow failure is DATA, not an incident. Record it and move on —
        # this is exactly the liveness signal the comparison needs.
        record_classifier_call(
            PROVIDER, cfg.qwen_model, len(articles),
            int((time.monotonic() - started) * 1000), ok=False,
            error_type=type(exc).__name__, error_detail=str(exc),
        )
        return

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = _extract_usage(resp)

    calls = (resp.choices[0].message.tool_calls or []) if resp.choices else []
    if not calls:
        # The same failure shape we are hedging against on the Claude side —
        # worth recording precisely so we can compare how often each provider
        # does it.
        record_classifier_call(
            PROVIDER, cfg.qwen_model, len(articles), latency_ms, ok=False,
            error_type="no_tool_call", **usage,
        )
        return
    try:
        payload = json.loads(calls[0].function.arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        record_classifier_call(
            PROVIDER, cfg.qwen_model, len(articles), latency_ms, ok=False,
            error_type="bad_json", error_detail=str(exc), **usage,
        )
        return

    rows = []
    for rec in payload.get("classifications") or []:
        if not isinstance(rec, dict):
            continue
        art = by_id.get(str(rec.get("id")))
        if art is None:
            continue
        # Validate exactly as the live path does: reject out-of-range values,
        # never clamp them. A model that emits nonsense should score as having
        # emitted nonsense, not be quietly corrected into looking competent.
        if rec.get("catalyst_type") not in CATALYST_TYPES:
            continue
        if rec.get("sentiment") not in ("positive", "neutral", "negative"):
            continue
        try:
            conf = float(rec.get("confidence"))
        except (TypeError, ValueError):
            continue
        if not 0.0 <= conf <= 1.0:
            continue
        mag = rec.get("catalyst_magnitude")
        mag = mag if isinstance(mag, int) and 1 <= mag <= 5 else None
        rows.append({
            "article_id": str(rec.get("id")),
            "ticker": art.get("ticker") or "",
            "headline": art.get("headline"),
            "sentiment": rec["sentiment"],
            "confidence": conf,
            "catalyst_type": rec["catalyst_type"],
            "already_moved": bool(rec.get("already_moved", False)),
            "catalyst_magnitude": mag,
        })

    saved = save_qwen_scores(rows, cfg.qwen_model)
    record_classifier_call(
        PROVIDER, cfg.qwen_model, len(articles), latency_ms,
        ok=bool(rows), scored_count=len(rows),
        error_type=None if rows else "empty_batch", **usage,
    )
    logger.info("Shadow [qwen]: %d/%d scored in %dms (%d new rows)",
                len(rows), len(articles), latency_ms, saved)


def shadow_score(articles: list[dict], user_message: str) -> None:
    """
    Fire-and-forget a shadow classification of the same batch Claude just saw.

    Returns immediately. Never raises. Never returns a verdict — by design, this
    module cannot influence a trading decision.
    """
    global _pending
    if not articles or not shadow_enabled():
        return
    with _pending_lock:
        if _pending >= _MAX_PENDING:
            # Dropping is the correct behaviour: shadow data is a nice-to-have,
            # an unbounded queue is a memory leak, and a backlog means the
            # provider is degraded — which the gap in the data will itself show.
            logger.debug("Shadow [qwen]: %d job(s) pending — dropping batch", _pending)
            return
        _pending += 1

    def _job():
        global _pending
        try:
            _run(articles, user_message)
        except Exception as exc:                      # belt and braces
            logger.debug("Shadow [qwen] job failed: %s", exc)
        finally:
            with _pending_lock:
                _pending -= 1

    try:
        _get_pool().submit(_job)
    except Exception as exc:
        with _pending_lock:
            _pending -= 1
        logger.debug("Could not submit shadow job: %s", exc)
