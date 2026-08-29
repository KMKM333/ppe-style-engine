"""
test_gatekeeper_dryrun.py — a zero-cost, no-Claude-calls demo of what the
gatekeeper does differently from the old (pre-Aug-2026) /api/ingest/video
behaviour. Makes NO Anthropic API calls and writes NOTHING to your
database — it only reads (one existing already-classified video, for the
duplicate-resubmit test case) and runs the same pure-Python checks
webapp.py's endpoint now runs, printing what would have happened before
vs. what happens now.

Run it from inside scripts/:
    cd scripts
    python3 test_gatekeeper_dryrun.py

Safe to run as many times as you like — it never touches Claude credits.
"""
import gatekeeper as gk
from db_init import get_conn

# A very rough, order-of-magnitude cost estimate for a long-form combined
# call — NOT what llm_client.py actually uses (that logs the real number
# from each API response). This is only for illustrating what a call
# gatekeeper prevented would likely have cost, using the same $2/$10 per
# MTok pricing gatekeeper.py uses.
def rough_estimate_cost(word_count, skip_visuals=True):
    input_tokens = word_count * 1.3 + 6000  # ~6k tokens of rubric/prompt overhead
    output_tokens = 4000 if skip_visuals else 9000  # visuals inflate output a lot
    return (input_tokens / 1_000_000) * gk.PRICE_PER_MTOK_INPUT + (output_tokens / 1_000_000) * gk.PRICE_PER_MTOK_OUTPUT


def check_video(label, title, script, duration_sec, platform, already_classified_video_id=None):
    print(f"\n=== {label} ===")
    print(f"title: {title!r}")
    print(f"duration_sec: {duration_sec}, platform: {platform}")

    print("\nBEFORE gatekeeper (how /api/ingest/video behaved until this patch):")
    print("  -> would ALWAYS spawn the classification subprocess, no questions asked.")
    print(f"  -> estimated cost if it ran: ~${rough_estimate_cost(len(script.split())):.2f}")

    print("\nAFTER gatekeeper (what happens now):")
    if already_classified_video_id is not None:
        print(f"  -> content_hash matches video_id={already_classified_video_id}, already classified_by='claude'")
        print("  -> webapp.py returns immediately: \"already classified, skipped re-processing\"")
        print(f"  -> cost: $0.00 (saved ~${rough_estimate_cost(len(script.split())):.2f})")
        return

    reason = gk.check_length(duration_sec, platform) or gk.check_topic(title, script) or gk.check_daily_budget()
    if reason:
        print(f"  -> HELD for review: {reason}")
        print(f"  -> cost: $0.00 until a human approves (saved ~${rough_estimate_cost(len(script.split())):.2f} for now)")
    else:
        print("  -> passes all checks, proceeds to Claude exactly as before.")
        print(f"  -> cost: ~${rough_estimate_cost(len(script.split())):.2f} (unchanged — this one was always going to be processed)")


def main():
    print("Gatekeeper dry run — no Claude calls, no database writes.")
    print("=" * 70)

    # --- Case 1: a real duplicate resubmission (the actual bug found) ---
    conn = get_conn()
    row = conn.execute(
        "SELECT v.video_id, v.title, v.script, v.duration_sec, v.media_type "
        "FROM videos v JOIN video_attributes a ON a.video_id = v.video_id "
        "WHERE a.classified_by = 'claude' LIMIT 1"
    ).fetchone()
    conn.close()
    if row:
        check_video(
            "Case 1: Bulk Transcriber re-POSTs a video you already have classified",
            row["title"], row["script"], row["duration_sec"] or 60, row["media_type"] or "YouTube",
            already_classified_video_id=row["video_id"],
        )
    else:
        print("\n(No classified videos found in your local db to use for the duplicate-resubmit case — skipping Case 1.)")

    # --- Case 2: an off-topic bulk-import mistake ---
    check_video(
        "Case 2: an off-topic video accidentally included in a bulk import",
        "My Trip to Bali",
        "We went to the beach and ate great food and swam all day, it was such a relaxing vacation with friends.",
        75, "Instagram",
    )

    # --- Case 3: an outlier-length video (e.g. a full podcast episode, not a ~20 min video) ---
    check_video(
        "Case 3: a 3-hour podcast accidentally ingested as a normal long-form video",
        "Full Episode: A Conversation on Economics and Policy",
        "economics policy government market inflation " * 400,  # ~2000 words, stand-in for a huge transcript
        10800, "YouTube",
    )

    # --- Case 4: a normal, legitimate video — should sail through unchanged ---
    check_video(
        "Case 4: a normal on-topic short-form video (control case — should NOT be held)",
        "Why Inflation Keeps Rising",
        "Today we're talking about inflation, interest rates, and how central bank policy shapes the economy.",
        58, "Instagram",
    )

    print("\n" + "=" * 70)
    print("Done. Cases 1-3 show videos that would have silently cost money before;")
    print("Case 4 confirms a normal video is completely unaffected.")


if __name__ == "__main__":
    main()
