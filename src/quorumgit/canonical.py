"""Canonical JSON serialization and stable hashing.

Every hashed artifact in QuorumGit (approval operations, handoff records)
goes through this single implementation so hashes are reproducible across
processes and time.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

HASH_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


def canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def stable_hash(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()
