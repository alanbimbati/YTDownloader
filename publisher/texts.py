import json

import anthropic

from . import config


def _obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}
SCHEMA = _obj(
    youtube=_obj(title=_STR, description=_STR, tags={"type": "array", "items": _STR}),
    shorts=_obj(title=_STR, description=_STR),
    linkedin=_obj(text=_STR),
    telegram=_obj(text=_STR),
    nostr=_obj(title=_STR, text=_STR, hashtags={"type": "array", "items": _STR}),
)

SYSTEM = """You write the posts that accompany one video published on several platforms by its author.
Write in the language of the transcript, in the author's voice (first person), never inventing facts
that the transcript and the author's notes do not support.

- youtube: the horizontal video. title at most 90 characters, no clickbait in capitals. description
  2-4 short paragraphs plus a few relevant hashtags at the end, no "<" or ">". tags: 5-15 keywords.
- shorts: the vertical cut of the same video. title at most 90 characters; description one or two lines
  ending with #Shorts.
- linkedin: a professional post, at most 1500 characters, a strong first line, short paragraphs,
  3-5 hashtags at the end.
- telegram: a channel caption, at most 900 characters, conversational, may use a couple of emoji.
- nostr: title at most 90 characters; text a short note (at most 600 characters) for a Bitcoin/free-tech
  minded audience; hashtags without the # sign, 2-5 of them, lowercase."""


def write_posts(transcript: str, language: str, notes: str) -> dict:
    client = anthropic.Anthropic()
    user = f"Transcript language: {language}\n\n<transcript>\n{transcript}\n</transcript>"
    if notes:
        user += f"\n\n<author_notes>\n{notes}\n</author_notes>\nFollow the author's notes (links, tone, call to action) over your own choices."
    response = client.beta.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined to write the posts: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens)")
    return json.loads(next(b.text for b in response.content if b.type == "text"))
