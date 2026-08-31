"""
llm_client.py — thin wrapper around the Anthropic Messages API so the
webapp's Rewrite/Transform screen can generate the styled rewrite directly,
instead of you copying transform.TRANSFORM_PROMPT into a separate Claude
chat and pasting the reply back in.

Requires an ANTHROPIC_API_KEY environment variable — a separate, pay-per-use
API key from console.anthropic.com, NOT your Claude.ai/Claude Code login.
Get one, then before starting webapp.py:

    export ANTHROPIC_API_KEY=sk-ant-...

Every call below also logs its actual usage (from the API response's own
`usage` field, not an estimate) via gatekeeper.record_usage(), so
gatekeeper.check_daily_budget() has real numbers to work from. Logging is
best-effort and wrapped in try/except everywhere — a logging hiccup must
never break the underlying Claude call.
"""
import base64
import json
import os
import re

import gatekeeper

DEFAULT_MODEL = "claude-sonnet-5"


class LLMConfigError(Exception):
    """Raised when ANTHROPIC_API_KEY isn't set or the client can't be built."""


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "ANTHROPIC_API_KEY is not set. Get a key at console.anthropic.com, then "
            "run: export ANTHROPIC_API_KEY=sk-ant-... and restart webapp.py."
        )
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def _log_usage(model, usage, call_site):
    if not usage:
        return
    try:
        gatekeeper.record_usage(model, usage.input_tokens, usage.output_tokens, call_site)
    except Exception as e:
        print(f"[llm_client] usage logging failed (non-fatal): {e}")


def generate_transform(prompt_text: str, model: str = DEFAULT_MODEL) -> dict:
    """Sends a transform.TRANSFORM_PROMPT-shaped prompt to Claude and parses
    the TITLE:/SCRIPT: reply into {"title": ..., "script": ..., "raw_reply": ...}.
    Streams like generate_json() does — a large prompt (e.g. a full book's
    text as the source) makes the non-streaming call take long enough to
    hit the SDK's own timeout, which previously showed up as a multi-minute
    hang tying up a production worker thread instead of a clean error."""
    client = _client()
    reply_parts = []
    with client.messages.stream(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt_text}],
    ) as stream:
        for chunk in stream.text_stream:
            reply_parts.append(chunk)
        final_message = stream.get_final_message()
    _log_usage(model, getattr(final_message, "usage", None), "generate_transform")
    reply = "".join(reply_parts)
    return _parse_reply(reply)


def generate_json(prompt_text: str, model: str = DEFAULT_MODEL, max_tokens: int = 8192):
    """Sends a prompt that asks Claude to return a raw JSON array/object and parses it.
    Tolerates a markdown code fence around the JSON — anywhere in the reply, not just
    at the very start, since models sometimes preface it with commentary ("# Analysis:
    ...") despite being told to return raw JSON. Uses streaming since large max_tokens
    values can otherwise exceed the SDK's non-streaming timeout."""
    client = _client()
    reply_parts = []
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt_text}],
    ) as stream:
        for chunk in stream.text_stream:
            reply_parts.append(chunk)
        final_message = stream.get_final_message()
    _log_usage(model, getattr(final_message, "usage", None), "generate_json")
    reply = "".join(reply_parts)
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model didn't return valid JSON: {e}\n\nReply was:\n{reply}")


def generate_json_with_images(prompt_text: str, images: list, model: str = DEFAULT_MODEL, max_tokens: int = 8192):
    """Vision counterpart to generate_json() — used by
    classify_production_spec_shots.py to classify each shot's content
    category from its extracted frame. `images` is a list of
    (media_type, raw_bytes) tuples, e.g. [("image/png", b"...")]. Builds an
    Anthropic content list of image blocks (same base64 shape
    instagram_transcriber/app.py's generate_smart_title() already builds for
    a single thumbnail) followed by a trailing text block, then parses the
    JSON reply the same tolerant way generate_json() does. Non-streaming —
    unlike generate_json(), a shot-classification batch is capped at a
    modest number of images per call (see classify_production_spec_shots.py),
    so this doesn't need generate_json()'s streaming workaround for very
    large max_tokens responses."""
    client = _client()
    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(raw_bytes).decode("ascii")},
        }
        for media_type, raw_bytes in images
    ]
    content.append({"type": "text", "text": prompt_text})
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
    )
    _log_usage(model, message.usage, "generate_json_with_images")
    reply = "".join(block.text for block in message.content if block.type == "text")
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model didn't return valid JSON: {e}\n\nReply was:\n{reply}")


def _parse_reply(reply: str) -> dict:
    m = re.search(r"TITLE:\s*(.+?)\s*\nSCRIPT:\s*\n?(.*)", reply, re.DOTALL)
    if not m:
        raise ValueError(f"Couldn't find a TITLE:/SCRIPT: reply in the model's response:\n\n{reply}")
    title, script = m.group(1).strip(), m.group(2).strip()
    if not title or not script:
        raise ValueError(f"Model reply was missing a title or script:\n\n{reply}")
    return {"title": title, "script": script, "raw_reply": reply}
