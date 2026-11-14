"""Known Bilibili CC corrections, bound to the original platform segment."""

_CORRECTIONS = {
    ("BV1Lv4a6XE7z", "43363598647", "43363598647:2145470070871501568:0", "晶晶"): "鲸鲸",
}
CORRECTED_VIDEOS = frozenset(key[0] for key in _CORRECTIONS)


def corrected_caption(bvid, cid, segment):
    original = segment["text"]
    text = _CORRECTIONS.get((bvid, str(cid), segment["id"], original))
    return segment | {"text": text, "original_text": original} if text is not None else segment
