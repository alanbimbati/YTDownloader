import json

import anthropic

from . import config


def _obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}
CATEGORIES = {"22": "People & Blogs", "24": "Entertainment", "25": "News & Politics",
              "26": "Howto & Style", "27": "Education", "28": "Science & Technology"}
SCHEMA = _obj(
    youtube=_obj(title=_STR, description=_STR, tags={"type": "array", "items": _STR},
                 category={"type": "string", "enum": list(CATEGORIES)}),
    shorts=_obj(title=_STR, description=_STR),
    linkedin=_obj(text=_STR),
    nostr=_obj(title=_STR, text=_STR, hashtags={"type": "array", "items": _STR}),
)

SYSTEM = f"""You write the posts that accompany one video published on several platforms by its author.
Write in the language of the transcript, in the author's voice (first person), never inventing facts
that the transcript and the author's notes do not support. The goal is reach: being found in search
and turning viewers into subscribers/followers.

- youtube (the horizontal video):
  - title: at most 70 characters, the main search keyword in the first words, curiosity without
    clickbait or ALL CAPS.
  - description: the first two lines (about 150 characters) repeat the main keywords and say why to
    watch, since that is what search results and the "more" fold show. Then 2-3 short paragraphs.
    If the video lasts 3 minutes or more, add chapters: one per line as "0:00 Title", the first at
    0:00, at least 3, at least 10 seconds apart, on the [m:ss] marks of the transcript. Then a line
    inviting a comment with a concrete question. Then sources/credits if the author's notes ask for
    them. End with 3 hashtags on their own line (the first three are shown above the title). No "<" or ">".
  - tags: 10-15 search phrases people would type, from specific to broad, including common
    misspellings or English variants of the main keyword.
  - category: the YouTube category id that fits best: {CATEGORIES}.
- shorts (the vertical video): title at most 70 characters with the main keyword and #Shorts at the
  end; description 1-2 lines, then exactly the line "▶️ Video completo: {{video_lungo}}" (the
  placeholder is replaced with the link of the horizontal video), then the credits if the notes ask for them.
- linkedin: a professional post, at most 1500 characters, a strong first line, short paragraphs,
  3-5 hashtags at the end.
- nostr: title at most 70 characters; text a short note (at most 600 characters) for a Bitcoin/free-tech
  minded audience; hashtags without the # sign, 2-5 of them, lowercase."""


def write_posts(transcript: str, language: str, notes: str, durations: dict[str, float]) -> dict:
    client = anthropic.Anthropic()
    lengths = ", ".join(f"{k} video {v:.0f} seconds" for k, v in durations.items())
    user = f"Transcript language: {language}. {lengths}.\n\n<transcript>\n{transcript}\n</transcript>"
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
