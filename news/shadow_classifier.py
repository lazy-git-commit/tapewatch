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
   provider slowdown into a memory leak and a thread explosion. Each drop is
   still RECORDED (`error_type='dropped_backlog'`), because a drop means the
   provider is degraded and the liveness figures are computed over the rows
   that exist: leaving a gap would have excluded Qwen's worst periods from its
   own success-rate denominator.
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
# Drops are counted on the news-cycle thread and persisted on the background
# thread (see _flush_drops). This caps that hand-off buffer.
_MAX_DROPS_BUFFERED = 64

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_pending = 0
_dropped: list[tuple[int, int]] = []
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


def _flush_drops() -> None:
    """
    Write one `classifier_calls` row per batch dropped since the last flush.

    Called from the BACKGROUND thread only. The drop itself is detected on the
    news-cycle thread, where a DB write is not allowed: `get_conn()` retries
    three times with backoff, so a database blip would block the news cycle for
    seconds — exactly the coupling safety property 1 exists to prevent. So the
    drop is counted in memory and persisted here, on the next job to run.

    A drop only ever happens while a job is in flight, and that job flushes on
    its way out, so the counter is bounded by the drops of one job's duration.
    """
    from storage.database import record_classifier_call
    global _dropped
    with _pending_lock:
        pending, _dropped = _dropped, []
    for batch_size, backlog in pending:
        try:
            record_classifier_call(
                PROVIDER, cfg.qwen_model, batch_size, None, ok=False,
                error_type="dropped_backlog",
                error_detail=f"{backlog} job(s) already pending",
            )
        except Exception as exc:
            logger.debug("Could not record shadow drop: %s", exc)


def _run(articles: list[dict], user_message: str) -> None:
    """Body of one shadow scoring job. Runs on the background thread."""
    # Imported here rather than at module scope to avoid a circular import
    # (fetcher imports this module).
    from news.fetcher import (CATALYST_TYPES, _CLASSIFY_TOOL, _SYSTEM_PROMPT,
                              _output_budget)
    from storage.database import record_classifier_call, save_qwen_scores

    client = _get_client()
    if client is None:
        # Recorded, not silently dropped: a permanently dead client (typo'd
        # QWEN_BASE_URL, `openai` not installed) would otherwise be
        # indistinguishable from "shadow was never enabled", because the
        # warning inside _get_client latches once per process.
        record_classifier_call(
            PROVIDER, cfg.qwen_model, len(articles), None, ok=False,
            error_type="client_unavailable",
        )
        return

    by_id = {str(a.get("id")): a for a in articles}
    started = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=cfg.qwen_model,
            temperature=0,
            # Same budget as the live Claude call, computed by the SHARED
            # helper rather than a copied formula — a shadow scored under a
            # different budget is not a like-for-like comparison.
            #
            # v21.14.1 hard-coded Claude's old `n * 60 + 64`, which was BELOW
            # the 68-72 tokens/article both models actually need. Ten Qwen
            # batches truncated in three days, every one with tokens_out
            # exactly equal to the cap, all recorded as Qwen's own `truncated`
            # failure — our sizing blamed on the provider. v21.15 shared the
            # two constants but still duplicated the arithmetic, so a changed
            # overhead term would have re-diverged silently; v21.15.1 shares
            # the function. The caller chunks to _MAX_ARTICLES_PER_BATCH, so
            # this stays bounded well under any provider's per-request output
            # ceiling (an over-cap request 400s and would be recorded as the
            # provider failing, which is the same misattribution again).
            max_tokens=_output_budget(len(articles)),
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

    # finish_reason is read BEFORE any early return. It is the single signal
    # that separates "our output budget cut the answer off" from "the provider
    # answered badly", and every exit below has to be able to tell those apart
    # — filing our sizing as the provider's unreliability is the exact
    # misdiagnosis that cost v21.7, v21.12 and v21.13 on the Claude side.
    finish = getattr(resp.choices[0], "finish_reason", None) if resp.choices else None
    truncated = finish == "length"

    calls = (resp.choices[0].message.tool_calls or []) if resp.choices else []
    if not calls:
        # Truncation before the tool call is even emitted: forced tool_choice on
        # an OpenAI-compatible endpoint is best-effort, not enforced the way
        # Anthropic's is, so a model that writes a preamble first can run out of
        # budget with tool_calls still empty. That is our cap, not the provider
        # refusing to use the tool.
        record_classifier_call(
            PROVIDER, cfg.qwen_model, len(articles), latency_ms, ok=False,
            error_type="truncated" if truncated else "no_tool_call",
            error_detail=f"finish_reason={finish}", **usage,
        )
        return
    try:
        payload = json.loads(calls[0].function.arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        record_classifier_call(
            PROVIDER, cfg.qwen_model, len(articles), latency_ms, ok=False,
            error_type="truncated" if truncated else "bad_json",
            error_detail=f"finish_reason={finish}: {exc}", **usage,
        )
        return

    # Shape guard, mirroring the live path's `bad_shape` check. Two real
    # failures are possible here and neither may be allowed to escape:
    #   * `payload` is not a dict at all (arguments were "[]" or a scalar), so
    #     .get() raises AttributeError. _job() swallows that at DEBUG, so NO
    #     classifier_calls row is written and the failure is deleted from
    #     Qwen's own denominator — the defect v21.14.1 fixed for dropped
    #     batches, reappearing by a different route.
    #   * `classifications` is a JSON *string* rather than a list (a common
    #     weak-model behaviour for nested array params). Iterating it yields
    #     characters, every record is discarded, yet ok=True and
    #     scored_count=len(string) would report ~1,500 articles scored for a
    #     25-article batch and feed that straight into the comparison's
    #     articles_scored total.
    if not isinstance(payload, dict) or \
            not isinstance(payload.get("classifications"), list):
        record_classifier_call(
            PROVIDER, cfg.qwen_model, len(articles), latency_ms, ok=False,
            error_type="truncated" if truncated else "bad_shape",
            error_detail=f"finish_reason={finish}, "
                         f"payload={type(payload).__name__}", **usage,
        )
        return
    # ok/scored_count are computed on the RAW list, exactly as the Claude path
    # does (news/fetcher.py records before its own per-record validation loop).
    # Computing them post-validation instead made the two providers' liveness
    # columns incomparable: a batch of well-formed answers that all missed the
    # taxonomy would have been filed under the same `empty_batch` error_type as
    # a genuine outage, while Claude emitting the identical answers records
    # ok=true.
    parsed = payload["classifications"]

    rows = []
    for rec in parsed:
        if not isinstance(rec, dict):
            continue
        art = by_id.get(str(rec.get("id")))
        if art is None:
            continue
        # Validate exactly as the live path does — same coercions, same
        # accept/reject boundaries. Divergence here is not a small mismatch:
        # anything rejected on this side but kept on Claude's disappears from
        # the INNER JOIN in classifier_compare.prediction(), which would
        # remove Qwen's WORST answers from the paired sample and flatter the
        # challenger with pure survivorship bias.
        #
        # Out-of-range values are rejected, never clamped — a confidence of 7
        # is more likely a mis-scaled 0-10 answer than a genuine 100%, and
        # guessing the scale is worse than not scoring the article.
        sentiment = str(rec.get("sentiment", "neutral")).lower()
        if sentiment not in ("positive", "neutral", "negative"):
            continue
        catalyst = str(rec.get("catalyst_type", "other"))
        if catalyst not in CATALYST_TYPES:
            continue
        try:
            # Same defaults as fetcher.py: a missing field is a default, not a
            # dropped record, or a model that simply omits `confidence` would
            # be scored as having failed rather than as having answered.
            conf = float(rec.get("confidence", 0.5))
            # int(3.0) == 3: JSON has no integer type, so a magnitude that
            # round-trips as a float is still a valid magnitude.
            mag = int(rec.get("catalyst_magnitude", 1))
        except (TypeError, ValueError):
            continue
        if not (0.0 <= conf <= 1.0) or not (1 <= mag <= 5):
            continue
        rows.append({
            "article_id": str(rec.get("id")),
            "ticker": art.get("ticker") or "",
            "headline": art.get("headline"),
            "sentiment": sentiment,
            "confidence": conf,
            "catalyst_type": catalyst,
            "already_moved": bool(rec.get("already_moved", False)),
            "catalyst_magnitude": mag,
        })

    # A completion can hit the cap and STILL leave parseable arguments — the
    # gateway closes the JSON after record N of M. Claude's path discards the
    # whole batch as `truncated` in that situation; recording it here as a
    # success with partial data would hand Qwen a liveness and coverage
    # advantage on exactly the largest batches, where the comparison is most
    # load-bearing. Rows already saved are harmless (UNIQUE + ON CONFLICT DO
    # NOTHING), but the CALL is recorded for what it was.
    saved = save_qwen_scores(rows, cfg.qwen_model)
    record_classifier_call(
        PROVIDER, cfg.qwen_model, len(articles), latency_ms,
        ok=bool(parsed) and not truncated, scored_count=len(parsed),
        error_type=("truncated" if truncated
                    else None if parsed else "empty_batch"),
        error_detail=f"finish_reason={finish}" if truncated else None,
        **usage,
    )
    logger.info("Shadow [qwen]: %d returned, %d valid of %d articles in %dms "
                "(%d new rows)",
                len(parsed), len(rows), len(articles), latency_ms, saved)


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
            dropped = _pending
        else:
            _pending += 1
            dropped = None
    if dropped is not None:
        # Dropping is still the correct behaviour: shadow data is a
        # nice-to-have, an unbounded queue is a memory leak, and a backlog
        # means the provider is degraded.
        #
        # But the drop is RECORDED rather than left as a gap. The original
        # reasoning — "the gap in the data will itself show it" — was wrong as
        # implemented: latency_and_liveness() computes success_rate over the
        # rows that EXIST, so a provider slow enough to saturate the single
        # worker for an hour would report a high success rate and a
        # worst_failure_streak of 0. The periods when Qwen is degraded were
        # precisely the periods excluded from its own denominator.
        logger.debug("Shadow [qwen]: %d job(s) pending — dropping batch", dropped)
        with _pending_lock:
            # Bounded: capped well above the drops one job's duration can
            # produce, so a pathological stall can never grow this without end.
            if len(_dropped) < _MAX_DROPS_BUFFERED:
                _dropped.append((len(articles), dropped))
        return

    def _job():
        global _pending
        try:
            _run(articles, user_message)
        except Exception as exc:                      # belt and braces
            logger.debug("Shadow [qwen] job failed: %s", exc)
        finally:
            try:
                _flush_drops()
            except Exception as exc:
                logger.debug("Shadow [qwen] drop flush failed: %s", exc)
            with _pending_lock:
                _pending -= 1

    try:
        _get_pool().submit(_job)
    except Exception as exc:
        with _pending_lock:
            _pending -= 1
        logger.debug("Could not submit shadow job: %s", exc)
