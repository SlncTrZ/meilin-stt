"""Render artifacts — hotwords.txt cho sherpa-onnx + kiểm tra BPE hợp lệ.

Wing: code | Topic: stt-lexicon-render | Updated: 2026-10-31 23:07

Định dạng hotwords của sherpa-onnx: mỗi dòng `TỪ KHOÁ :score`.
Term phải tokenize được bằng `bpe.model` của model đang dùng; nếu không thì
hotword đó là "hotword chết" (model không thể emit ra) -> cảnh báo và bỏ.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from lexicon.store import (
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    TIER_SCORE,
    LexiconEntry,
)

log = logging.getLogger("meilin-stt.render")

BASE_DIR = Path(__file__).resolve().parent.parent
HOTWORDS_OUT = BASE_DIR / "models" / "hotwords.txt"


def _upper(term: str) -> str:
    return " ".join(term.upper().split())


def render_hotwords(
    entries: Iterable[LexiconEntry],
    out_path: Path = HOTWORDS_OUT,
    include_candidates: bool = False,
    header: str = "",
) -> tuple[Path, int]:
    """Ghi file hotwords từ lexicon. Trả (path, số term đã ghi)."""
    allowed = {STATUS_ACTIVE} | ({STATUS_CANDIDATE} if include_candidates else set())
    seen: set[str] = set()
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}" for line in header.splitlines())
    count = 0
    for tier in ("A", "B", "C"):
        bucket = [
            e for e in entries if e.status in allowed and e.tier == tier
        ]
        if not bucket:
            continue
        lines.append(f"# --- Tier {tier} (score {TIER_SCORE.get(tier, 1.5)}) ---")
        for entry in sorted(bucket, key=lambda e: e.term.lower()):
            term = _upper(entry.term)
            if not term or term in seen:
                continue
            score = entry.score or TIER_SCORE.get(tier, 1.5)
            lines.append(f"{term} :{score:g}")
            seen.add(term)
            count += 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path, count


def _tokens_set(tokens_path: Path) -> set[str]:
    if not tokens_path.exists():
        return set()
    with tokens_path.open("r", encoding="utf-8") as fh:
        return {line.split(" ")[0] for line in fh if line.strip()}


def tokenize_terms(terms: Iterable[str], model_dir: Path) -> dict[str, list[str] | None]:
    """Tokenize từng term bằng BPE của model. None = model không emit được."""
    tokens_path = model_dir / "tokens.txt"
    bpe_vocab = model_dir / "bpe.model"
    valid_tokens = _tokens_set(tokens_path)
    result: dict[str, list[str] | None] = {}

    text2token = None
    if tokens_path.exists() and bpe_vocab.exists():
        try:
            from sherpa_onnx import text2token as _t2t  # type: ignore[attr-defined]

            text2token = _t2t
        except Exception:  # noqa: BLE001 - version cũ không có text2token
            text2token = None

    for term in terms:
        pieces: list[str] | None = None
        if text2token is not None:
            try:
                pieces = text2token(
                    term.upper(),
                    tokens=str(tokens_path),
                    bpe_vocab=str(bpe_vocab),
                    modeling_unit="bpe",
                )
            except Exception:  # noqa: BLE001 - API khác nhau theo version
                pieces = None
        if pieces and valid_tokens:
            missing = [p for p in pieces if p not in valid_tokens]
            result[term] = None if missing else list(pieces)
        else:
            # Không có tokenizer -> coi như không kiểm chứng được, cho qua.
            result[term] = list(pieces) if pieces else []
    return result


def validate_entries(
    entries: Iterable[LexiconEntry], model_dir: Path
) -> tuple[list[LexiconEntry], list[tuple[str, str]]]:
    """Lọc entry không emit được. Trả (hợp lệ, [(term, lý do)])."""
    entries = list(entries)
    tokens = tokenize_terms([e.term for e in entries], model_dir)
    good: list[LexiconEntry] = []
    bad: list[tuple[str, str]] = []
    for entry in entries:
        pieces = tokens.get(entry.term)
        if pieces is None:
            bad.append((entry.term, "BPE không sinh được token (hotword chết)"))
            continue
        good.append(entry)
    return good, bad
