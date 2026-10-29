"""Local rules for a short's public copy: the post caption (description) and its hashtags.

The discovery model writes them; these rules only clean and bound what it wrote. Hashtags must be plain topic
tags a viewer could follow or search: no spam tags, no numbers the clip never says, at most MAX_HASHTAGS.
`#Shorts` is never chosen here because the YouTube publisher always adds it.
"""
from __future__ import annotations

import re

from .hooks import _numbers

MAX_HASHTAGS = 5
TAG_MIN, TAG_MAX = 2, 30
CAPTION_MAX = 300
# Tags that say nothing about the clip. They are dropped rather than failing the clip.
SPAM = frozenset('shorts short youtubeshorts ytshorts fyp foryou foryoupage viral trending trend explore explorepage '
                 'subscribe like follow reels tiktok video clip clips new'.split())


def clean_tag(value):
    """Lowercase letters and digits only; '' when nothing usable is left."""
    return re.sub(r'[^0-9a-z]', '', str(value).casefold().lstrip('#'))


def clean_hashtags(tags, speech=''):
    """Bounded, unique, grounded tags in the model's order. Unusable entries are dropped, never invented."""
    if not isinstance(tags, list):
        return []
    spoken = set(_numbers(speech))
    result = []
    for raw in tags:
        if not isinstance(raw, str):
            continue
        tag = clean_tag(raw)
        if not TAG_MIN <= len(tag) <= TAG_MAX or tag in SPAM or tag.isdigit() or tag in result:
            continue
        if any(number not in spoken for number in re.findall(r'\d+', tag)):
            continue  # "#pajero2026" when the clip never says 2026
        result.append(tag)
        if len(result) == MAX_HASHTAGS:
            break
    return result


def clean_caption(text):
    """(caption, tags found inline): hashtags written into the caption move to the tag list."""
    if not isinstance(text, str):
        return '', []
    inline = [clean_tag(t) for t in re.findall(r'#[\w]+', text)]
    caption = ' '.join(re.sub(r'(^|\s)#[\w]+', ' ', text).split())
    return caption[:CAPTION_MAX], [t for t in inline if t]


def public_copy(candidate, speech=''):
    """`{post_title, post_description, hashtags}` for a candidate, with the editor title as the title fallback."""
    title = candidate.get('post_title')
    title = title.strip() if isinstance(title, str) and title.strip() else str(candidate.get('title') or '').strip()
    caption, inline = clean_caption(candidate.get('post_description'))
    given = candidate.get('hashtags')
    tags = clean_hashtags((given if isinstance(given, list) else []) + inline, speech)
    return {'post_title': ' '.join(title.split())[:100], 'post_description': caption, 'hashtags': tags}
