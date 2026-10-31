import hashlib
from typing import Optional, Union


def make_etag(body_bytes: Union[bytes, str]) -> str:
    """
    Generate a strong ETag from response body bytes:
    '"' + first 32 hex characters of sha256(body_bytes) + '"'
    """
    if isinstance(body_bytes, str):
        body_bytes = body_bytes.encode("utf-8")
    digest = hashlib.sha256(body_bytes).hexdigest()[:32]
    return f'"{digest}"'


def etag_matches(if_none_match_header: Optional[str], etag: str) -> bool:
    """
    Check if an incoming If-None-Match header matches the resource ETag.
    Handles '*', comma-separated lists of ETags, and weak prefixes 'W/' (ignoring W/).
    An empty or malformed header never matches.
    """
    if not if_none_match_header or not isinstance(if_none_match_header, str):
        return False

    raw = if_none_match_header.strip()
    if not raw:
        return False

    if raw == "*":
        return True

    def _strip_weak(tag: str) -> str:
        tag = tag.strip()
        if tag.startswith(("W/", "w/")):
            tag = tag[2:].strip()
        return tag

    target = _strip_weak(etag)
    if not target:
        return False

    for item in raw.split(","):
        cleaned = _strip_weak(item)
        if cleaned == "*":
            return True
        if cleaned and cleaned == target:
            return True

    return False

