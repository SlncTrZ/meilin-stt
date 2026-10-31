"""Lexicon store — JSONL append/read cho utterances, feedback, lexicon.

Wing: code | Topic: stt-lexicon-store | Updated: 2026-10-31 23:07

Ghi atomic (tmp + replace) để nightly job và server không đọc phải file dở.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

UTTERANCES = DATA_DIR / "utterances.jsonl"
FEEDBACK = DATA_DIR / "feedback.jsonl"
LEXICON = DATA_DIR / "lexicon.jsonl"

STATUS_CANDIDATE = "candidate"
STATUS_ACTIVE = "active"
STATUS_DEMOTED = "demoted"


def _ensure(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # dòng hỏng thì bỏ, không làm chết job
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> Path:
    """Ghi toàn bộ file atomic."""
    _ensure(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def append_jsonl(path: Path, row: dict[str, Any]) -> dict[str, Any]:
    """Append 1 dòng (O(1), dùng cho server runtime)."""
    _ensure(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --- Records ----------------------------------------------------------------
@dataclass
class Utterance:
    """Một lần nhận dạng (chỉ metadata + text, không lưu audio mặc định)."""

    id: str
    ts: str
    raw_text: str
    final_text: str
    model: str
    lane: str
    duration_sec: float
    corrections: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Feedback:
    """Người dùng sửa lại text -> nguồn học của nightly job."""

    utterance_id: str
    ts: str
    corrected_text: str
    source: str = "cli"
    raw_text: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LexiconEntry:
    """Một thuật ngữ trong lexicon (ứng viên / đang bật / bị hạ)."""

    term: str
    tier: str = "B"
    score: float = 1.5
    status: str = STATUS_CANDIDATE
    hits: int = 0
    corrections_fixed: int = 0
    created: str = ""
    updated: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


TIER_SCORE = {"A": 2.5, "B": 1.5, "C": 1.1}


def load_lexicon(path: Path | None = None) -> list[LexiconEntry]:
    path = path or LEXICON
    entries: list[LexiconEntry] = []
    for row in read_jsonl(path):
        try:
            entries.append(
                LexiconEntry(
                    term=str(row["term"]),
                    tier=str(row.get("tier", "B")),
                    score=float(row.get("score", TIER_SCORE.get(str(row.get("tier", "B")), 1.5))),
                    status=str(row.get("status", STATUS_CANDIDATE)),
                    hits=int(row.get("hits", 0)),
                    corrections_fixed=int(row.get("corrections_fixed", 0)),
                    created=str(row.get("created", "")),
                    updated=str(row.get("updated", "")),
                    note=str(row.get("note", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return entries


def save_lexicon(entries: Iterable[LexiconEntry], path: Path | None = None) -> Path:
    return write_jsonl(path or LEXICON, (e.to_json() for e in entries))
