"""Atomic JSON writer for publish payloads.

Writes ``model.model_dump(mode='json')`` to disk via a sibling tempfile +
``os.replace`` rename so partial writes never leak out (a half-written
``latest.json`` that the frontend fetches would be worse than the
previous version sticking around). The destination directory is created
on demand.

A separate writer (not :mod:`dota_deals.notifier.json_file`) because the
notifier is tied to the BuyScore-with-data_quality shape; this one takes
any Pydantic wire model from :mod:`dota_deals.publish.models`.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def write_atomic(payload: BaseModel, dest: Path) -> None:
    """Serialize ``payload`` to ``dest`` atomically.

    The parent directory is created on demand. JSON is written with a
    trailing newline (so editors don't complain) and stable key ordering
    (so the git diff between published reports is reviewable).

    Uses ``model_dump(mode='python')`` so datetimes survive as ``datetime``
    objects and reach ``json.dumps`` via ``default=_json_default``, which
    emits the ``Z``-suffixed form. ``mode='json'`` would pre-stringify
    datetimes to ``+00:00`` and bypass the formatting hook.

    :raises OSError: if the destination cannot be opened or replaced.
    """
    data = payload.model_dump(mode="python")
    serialized = json.dumps(data, indent=2, sort_keys=True, default=_json_default) + "\n"

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
        os.replace(tmp_path, dest)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _json_default(value: Any) -> str:
    """Fallback for ``json.dumps``: turn ``datetime`` into the ``Z`` form.

    Pydantic's ``model_dump(mode='json')`` already serializes datetimes to
    ``"+00:00"``; we convert here so the wire stays consistent with the
    documented ``Z`` convention.
    """
    if isinstance(value, datetime):
        s = value.isoformat()
        return s.replace("+00:00", "Z") if s.endswith("+00:00") else s
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unserializable type: {type(value).__name__}")
