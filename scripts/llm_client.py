"""
llm_client.py — thin wrapper around the Anthropic Messages API so the
webapp's Rewrite/Transform screen can generate the styled rewrite directly,
instead of you copying transform.TRANSFORM_PROMPT into a separate Claude
chat and pasting the reply back in.

Requires an ANTHROPIC_API_KEY environment variable — a separate, pay-per-use
API key from console.anthropic.com, NOT your Claude.ai/Claude Code login.
Get one, then before starting webapp.py:

    export ANTHROPIC_API_KEY=sk-ant-...
"""
import json
import os
import re

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


def generate_transform(prompt_text: str, model: str = DEFAULT_MODEL) -> dict:
    """Sends a transform.TRANSFORM_PROMPT-shaped prompt to Claude and parses
    the TITLE:/SCRIPT: reply into {"title": ..., "script": ..., "raw_reply": ...}."""
    client = _client()
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt_text}],
    )
    reply = "".join(block.text for block in message.content if block.type == "text")
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
    reply = "".join(reply_parts)
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
