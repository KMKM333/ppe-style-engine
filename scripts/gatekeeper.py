"""
gatekeeper.py — shared, rule-based pre-checks that run before an
ingested video reaches an actual Claude API call (auto_process_video.py /
auto_process_shortform_video.py, via classify_video_combined.py). No LLM
calls happen here — everything below is plain Python, so these checks
cost nothing to run.

Added after a cost investigation (Aug 2026) found that /api/ingest/video
had no filtering at all: every ingested video unconditionally triggered a
full classification subprocess — including duplicate re-submissions of
videos that were already classified (Bulk Transcriber's import_job_to_ppe()
re-POSTs the whole job on retry; content_hash already prevented a second DB
row, but nothing stopped a second classification subprocess from spawning
against the existing row).

Three independent things live here:
  1. check_length()   — flags outlier-duration videos for manual review
                         instead of auto-running the expensive pipeline
                         unsupervised.
  2. check_topic()    — a cheap keyword check against the PPE subject
                         list; flags videos that don't look on-topic
                         before the 32k-token combined call runs.
  3. record_usage() / today_estimated_cost() / check_daily_budget() — a
     local, best-effort running log of Claude spend, read from each API
     response's own `usage` field (not re-derived from word counts), used
     to enforce PPE_DAILY_BUDGET_USD.

Everything here fails OPEN, never silently: a flagged video is marked
'needs_review' — the same status auto_process_video.py already uses when
an API call itself fails — rather than dropped, so nothing here loses your
data; it just holds the expensive step for a human to glance at first.
Every flag reason is human-readable and shows up wherever webapp.py
already surfaces classification_error.

Env vars (all optional — sane defaults below):
    PPE_MAX_DURATION_SEC   default 3600 (60 min) — outlier-length cap
    PPE_MIN_DURATION_SEC   default 5 — reject near-empty stub videos
    PPE_SKIP_TOPIC_CHECK   default "0" — set "1" to disable check_topic()
    PPE_DAILY_BUDGET_USD   default 15.00 — set "0" to disable the cap
    PPE_SKIP_VISUALS       default "1" — read by classify_video_combined.py,
                            not this module, but documented here since it's
                            part of the same cost-control pass.
"""
import json
import os
import time

from db_init import DB_PATH, get_conn

USAGE_LOG_PATH = DB_PATH.parent / "api_usage_log.jsonl"

# claude-sonnet-5 pricing, confirmed permanent as of Aug 2026
# (https://platform.claude.com/docs/en/about-claude/pricing). Update here if
# Anthropic changes it — nowhere else in this codebase hardcodes a price.
PRICE_PER_MTOK_INPUT = 2.00
PRICE_PER_MTOK_OUTPUT = 10.00

DAILY_BUDGET_USD = float(os.environ.get("PPE_DAILY_BUDGET_USD", "15.00"))
MAX_DURATION_SEC = float(os.environ.get("PPE_MAX_DURATION_SEC", "3600"))
MIN_DURATION_SEC = float(os.environ.get("PPE_MIN_DURATION_SEC", "5"))
SKIP_TOPIC_CHECK = os.environ.get("PPE_SKIP_TOPIC_CHECK", "0") == "1"

# Expanded from webapp.py's SUBJECTS list — a handful of related terms per
# subject so a video isn't flagged just for not literally saying
# "economics" in its title. Deliberately generous: this is a cheap sanity
# check for an obviously-wrong bulk import, not a precise classifier —
# a false negative here just means a human glances at one more video
# before it's processed, which costs nothing.
SUBJECT_KEYWORDS = {
    "Economics": ["econom", "gdp", "inflation", "market", "trade", "capitalis", "wealth", "tax", "recession", "currency", "financ", "poverty", "labor", "labour"],
    "Politics": ["politic", "election", "democra", "government", "policy", "geopolit", " war ", "diploma", "vote", "senate", "president", "regime", "propaganda"],
    "Philosophy": ["philosoph", "ethic", "moral", "existential", "stoic", "meaning of life", "metaphysic", "epistemolog", "nihilis", "virtue"],
    "Psychology": ["psycholog", "cognit", "behav", "brain", "bias", "habit", "mental", "emotion", "therapy", "trauma", "personality"],
    "Sustainability": ["climate", "sustainab", "environment", "carbon", "renewable", "biodivers", "pollution", "ecosystem"],
    "Science": ["scien", "physics", "biolog", "chemistr", "quantum", "evolution", "research stud", "experiment", "universe", "genetic"],
    "Technology": ["technolog", "artificial intelligence", " ai ", "software", "internet", "crypto", "blockchain", "robot", "algorithm", "startup", "silicon valley"],
}
_ALL_KEYWORDS = [kw for kws in SUBJECT_KEYWORDS.values() for kw in kws]


def mark_needs_review(video_id, reason):
    """Same UPDATE auto_process_video.py's own module-level
    _mark_needs_review() runs — duplicated here (rather than imported)
    because that function is a private helper local to three different
    scripts, not something shared; this is the version webapp.py's
    pre-subprocess check uses."""
    conn = get_conn()
    conn.execute(
        "UPDATE video_attributes SET classified_by = 'needs_review', classification_error = ? WHERE video_id = ?",
        (reason, video_id),
    )
    conn.commit()
    conn.close()
    print(f"[gatekeeper] video_id={video_id} held for review: {reason}")


def check_length(duration_sec, media_type):
    """Returns a reason string if this video's duration is an outlier,
    else None. Doesn't compare against platform-specific expectations
    (e.g. YouTube vs Instagram) — just flags anything unusually long (risk
    of a huge transcript blowing up token cost on the 32k-budget combined
    call) or suspiciously short (near-empty script not worth a
    classification call at all)."""
    if duration_sec is None:
        return None  # no duration reported — nothing to check
    if duration_sec > MAX_DURATION_SEC:
        return (
            f"Duration {duration_sec:.0f}s exceeds the {MAX_DURATION_SEC:.0f}s outlier "
            f"cap for auto-processing — held for manual review."
        )
    if duration_sec < MIN_DURATION_SEC:
        return (
            f"Duration {duration_sec:.0f}s is below the {MIN_DURATION_SEC:.0f}s minimum "
            f"— likely not worth a classification call."
        )
    return None


def check_topic(title, script):
    """Returns a reason string if neither the title nor the opening ~200
    words of the script hits any PPE subject keyword, else None. Cheap
    (no LLM call) — meant to catch an obviously off-topic bulk import, not
    to be a precise classifier; a real PPE video with unusual phrasing can
    still fail this and get held — that's a one-click approve in the
    review UI, not a silent drop."""
    if SKIP_TOPIC_CHECK:
        return None
    haystack = (" " + (title or "") + " " + " ".join((script or "").split()[:200]) + " ").lower()
    if any(kw in haystack for kw in _ALL_KEYWORDS):
        return None
    return (
        "No PPE subject keyword (economics/politics/philosophy/psychology/"
        "sustainability/science/technology) found in the title or opening "
        "text — held for manual review in case this is an off-topic import."
    )


BATCH_DISCOUNT = 0.5  # Anthropic's Message Batches API: half price, in exchange for async turnaround


def record_usage(model, input_tokens, output_tokens, call_site, is_batch=False):
    """Appends one line to db/api_usage_log.jsonl, next to the sqlite DB.
    Best-effort: a logging failure here must never break the actual Claude
    call it's recording, so callers wrap this in try/except (see
    llm_client.py). is_batch applies BATCH_DISCOUNT so a Batch API call's
    logged cost — and therefore check_daily_budget()'s running total —
    doesn't overstate what was actually spent by 2x."""
    cost = (input_tokens / 1_000_000) * PRICE_PER_MTOK_INPUT + (output_tokens / 1_000_000) * PRICE_PER_MTOK_OUTPUT
    if is_batch:
        cost *= BATCH_DISCOUNT
    line = {
        "ts": time.time(),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(cost, 6),
        "call_site": call_site,
        "is_batch": is_batch,
    }
    USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LOG_PATH, "a") as f:
        f.write(json.dumps(line) + "\n")


def today_estimated_cost():
    """Sums estimated_cost_usd for lines logged since the most recent UTC
    midnight. Best-effort and LOCAL to this repo's own llm_client.py calls
    — it can't see spend from anywhere else on the same Anthropic API key
    (e.g. a one-off script that calls the anthropic SDK directly instead
    of going through llm_client.py), so treat this as a floor, not an
    exact total. Cross-check against console.anthropic.com periodically."""
    if not USAGE_LOG_PATH.exists():
        return 0.0
    midnight = time.time() - (time.time() % 86400)
    total = 0.0
    with open(USAGE_LOG_PATH) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", 0) >= midnight:
                total += rec.get("estimated_cost_usd", 0.0)
    return total


def check_daily_budget():
    """Returns a reason string if today's estimated spend has already hit
    PPE_DAILY_BUDGET_USD, else None. Set PPE_DAILY_BUDGET_USD=0 to disable
    this check entirely."""
    if DAILY_BUDGET_USD <= 0:
        return None
    spent = today_estimated_cost()
    if spent >= DAILY_BUDGET_USD:
        return (
            f"Today's estimated Claude spend (${spent:.2f}) has reached the "
            f"${DAILY_BUDGET_USD:.2f} daily cap — held for manual processing. "
            f"Re-run manually (e.g. python3 auto_process_video.py --video_id N) "
            f"or raise PPE_DAILY_BUDGET_USD once you're ready to keep going today."
        )
    return None
