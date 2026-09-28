"""Distinguish indexed duplicate evidence from stored SHA-256 evidence."""

from donedatahoarder.db.models import File


def stored_sha256_match(candidate: File, keeper: File | None) -> bool | None:
    if keeper is None or not candidate.hash_sha256 or not keeper.hash_sha256:
        return None
    return candidate.hash_sha256 == keeper.hash_sha256


def indexed_md5_match(candidate: File, keeper: File | None) -> bool | None:
    if keeper is None or not candidate.hash_md5 or not keeper.hash_md5:
        return None
    return candidate.hash_md5 == keeper.hash_md5
