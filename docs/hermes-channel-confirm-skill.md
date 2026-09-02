# Hermes skill: confirm the channel before importing

Paste-ready definition for a Hermes skill that makes Telegram ask **"which channel is this?"** with tappable options, instead of taking whatever name you typed literally.

## Why this exists

PPE Engine keys everything off the channel. Channel → style profile → and that profile carries **two separate labels**:

| Label | Example | Means |
|---|---|---|
| Style code | `A.3` | *How* that creator writes |
| Subject | `Psychology` | *What* they write about |

So one wrong channel makes **both** wrong at once. That is exactly how a `philedwardsinc` reel ended up filed under `kylascan` (a finance channel), inheriting the wrong style profile and the wrong subject.

The old import path made this easy to do: channel names were matched on an **exact string**, and anything that didn't match was **silently created as a brand-new channel**. `kylascan`, `Kylascan` and `kyla scan` became three different channels, three profiles, three fingerprints — with no warning.

---

## The two endpoints

Both live on `https://ppe-style-engine.onrender.com` and need the header:

```
X-Ingest-Key: <PPE_INGEST_API_KEY>
```

### 1. Ask what channel this might be — *before* importing

```
GET /api/channels/suggest?name=<typed name>&limit=5
```

Response:

```json
{
  "ok": true,
  "name": "kylscan",
  "exact": false,
  "recommended_action": "confirm_existing",
  "candidates": [
    { "channel_name": "kylascan", "profile_code": "A.10", "subject": null,
      "n_videos": 10, "n_analysed": 10, "platform": "Instagram", "confidence": 0.933 }
  ]
}
```

`recommended_action` is the field to branch on:

| Value | Meaning | What Hermes should do |
|---|---|---|
| `proceed` | Exact match to a known channel | Import immediately, no question asked |
| `confirm_existing` | Close match — probably a typo | Show the candidates as buttons + a "No, it's new" button |
| `confirm_new` | Matches nothing | Ask to confirm it's a genuinely new creator |

### 2. Import, once the channel is settled

```
POST /api/ingest/video
{ "title": "...", "script": "...", "channel": "<confirmed name>",
  "url": "...", "confirm_channel": true }
```

`confirm_channel: true` tells the engine the name is deliberate, so it skips its own hold. **Only send it after the user has actually confirmed** — that flag is the whole safety mechanism.

If you omit it and the channel isn't an exact match, the engine holds the video unclassified and returns `"status": "held for channel confirmation"` with a `suggestion`. That's the backstop, not the intended path.

> **Don't send `platform`** unless you genuinely know it. The engine now infers it from the URL. Sending a wrong value routes a 60-second reel into the long-form analysis pipeline — wrong analysis shape, several times the cost.

---

## Skill definition

```yaml
name: ppe-import-with-channel-confirm
description: >
  Import a video into PPE Engine, confirming which channel it belongs to first.
  Use whenever the user sends a video link to add to the library. Prevents a
  mistyped channel name from silently creating a duplicate channel and filing
  the video under the wrong style profile and subject.

inputs:
  - name: url
    description: The video link the user sent.
  - name: channel_name
    description: The channel/creator name the user typed. May be a typo or a guess.

steps:
  - id: suggest
    description: Ask PPE Engine which existing channels this name might be.
    http:
      method: GET
      url: https://ppe-style-engine.onrender.com/api/channels/suggest
      query:
        name: "{{ channel_name }}"
        limit: 5
      headers:
        X-Ingest-Key: "{{ env.PPE_INGEST_API_KEY }}"

  - id: branch
    description: |
      If suggest.recommended_action == "proceed":
          go straight to the import step with channel = channel_name.

      If "confirm_existing":
          Send a Telegram message with an inline keyboard. One button per
          candidate, labelled:
              "{channel_name} · {profile_code} · {subject or 'no subject'}"
          Plus a final button: "None of these — new channel".
          Message text, e.g.:
              I don't have a channel called "kylscan".
              Did you mean one of these?
          Wait for the tap. The tapped channel_name becomes the confirmed name.

      If "confirm_new":
          Send a Telegram message with two buttons:
              "Yes, new creator"  /  "Let me retype the name"
          Message text, e.g.:
              "philedwardsinc" doesn't match any channel I know.
              Add it as a new creator?
          If they retype, restart from the suggest step.

  - id: import
    description: Import with the confirmed channel name.
    http:
      method: POST
      url: https://ppe-style-engine.onrender.com/api/ingest/video
      headers:
        X-Ingest-Key: "{{ env.PPE_INGEST_API_KEY }}"
        Content-Type: application/json
      body:
        title: "{{ title }}"
        script: "{{ transcript }}"
        channel: "{{ confirmed_channel_name }}"
        url: "{{ url }}"
        confirm_channel: true

  - id: report
    description: |
      Tell the user what happened, using the import response's "status":
        "ingested, classifying in the background" -> "Added to {channel}, analysing now."
        "already classified, skipped re-processing" -> "Already in the library."
        "held for review" -> report the "reason" verbatim; it explains itself.
```

---

## Telegram inline keyboard shape

If the gateway needs the raw Bot API structure:

```json
{
  "chat_id": "<chat id>",
  "text": "I don't have a channel called \"kylscan\". Did you mean one of these?",
  "reply_markup": {
    "inline_keyboard": [
      [{ "text": "kylascan · A.10 · no subject", "callback_data": "ch:kylascan" }],
      [{ "text": "None of these — new channel",  "callback_data": "ch:__new__" }]
    ]
  }
}
```

Keep `callback_data` under Telegram's 64-byte limit. If a channel name could be long, send a short index (`ch:0`, `ch:1`) and keep the mapping in the skill's own state rather than truncating the name.

---

## Worth knowing

- **Showing the subject on each button matters.** Two channels can share a subject while having completely different style codes, and vice versa. Displaying `A.10 · Psychology` lets you spot a wrong pick before it's made.
- **`n_analysed` vs `n_videos`** — a channel with `n_videos: 10, n_analysed: 0` has been imported but never analysed. Worth surfacing, since its profile is effectively empty.
- **Candidates below 0.5 confidence are already filtered out**, so an empty `candidates` list genuinely means "nothing close".
- **The suggest endpoint is read-only.** Safe to call as often as you like — no cost, no writes.
- **A brand-new channel gets no profile until its first video is classified.** That's deliberate: a profile built from zero analysed videos reads as "confirmed" while knowing nothing, which corrupts every style-match ranking.
