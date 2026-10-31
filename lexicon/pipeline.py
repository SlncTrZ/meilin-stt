"""Pipeline dùng chung cho CLI và nightly job (không duplicate logic).

Wing: code | Topic: stt-lexicon-pipeline | Updated: 2026-10-31 23:07

Luồng: feedback.jsonl -> ứng viên -> lexicon.jsonl -> artifacts
(`hotwords.txt` + `replacements.json`) -> publish bằng con trỏ `current.txt`.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from lexicon import correct, mine, render, store
from lexicon.store import (
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    STATUS_DEMOTED,
    TIER_SCORE,
    LexiconEntry,
)

log = logging.getLogger("meilin-stt.pipeline")

# Re-export: runner/report chỉ cần "bây giờ là khi nào" — một nguồn thời gian duy nhất.
now_ts = store.now_ts

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models"
ARTIFACTS_DIR = BASE_DIR / "data" / "artifacts"
CURRENT_POINTER = ARTIFACTS_DIR / "current.txt"


# --- feedback -> pairs ------------------------------------------------------
def collect_pairs() -> list[tuple[str, str]]:
    """Ghép utterances với feedback đã sửa -> [(raw, corrected)]."""
    utts = {row.get("id"): row for row in store.read_jsonl(store.UTTERANCES)}
    pairs: list[tuple[str, str]] = []
    for fb in store.read_jsonl(store.FEEDBACK):
        corrected = (fb.get("corrected_text") or "").strip()
        if not corrected:
            continue
        record = utts.get(fb.get("utterance_id"))
        raw = ""
        if record is not None:
            raw = (record.get("final_text") or record.get("raw_text") or "").strip()
        elif fb.get("raw_text"):
            # Client gửi kèm raw_text (Pi hook / CLI): dùng luôn, khỏi cần utterances row.
            raw = str(fb.get("raw_text")).strip()
        if raw == corrected:
            continue
        pairs.append((raw, corrected))
    return pairs


# --- lexicon mutation -------------------------------------------------------
def guess_tier(term: str) -> str:
    return "A" if mine.is_technical(term) else "B"


def upsert_entry(
    entries: list[LexiconEntry], term: str, tier: str, hits: int = 0
) -> tuple[LexiconEntry, bool]:
    """Thêm mới hoặc cộng dồn hits (so khớp không phân biệt hoa/thường)."""
    lowered = term.lower()
    for entry in entries:
        if entry.term.lower() == lowered:
            entry.hits += hits
            entry.updated = store.now_ts()
            if tier == "A" and entry.tier != "A":
                entry.tier = "A"
                entry.score = TIER_SCORE["A"]
            return entry, False
    entry = LexiconEntry(
        term=term,
        tier=tier,
        score=TIER_SCORE.get(tier, 1.5),
        status=STATUS_CANDIDATE,
        hits=hits,
        created=store.now_ts(),
        updated=store.now_ts(),
    )
    entries.append(entry)
    return entry, True


def run_mine(min_support: int = 2) -> dict[str, Any]:
    """Trích ứng viên từ feedback, ghi lexicon.jsonl."""
    entries = store.load_lexicon()
    pairs = collect_pairs()
    if not pairs:
        return {"pairs": 0, "candidates": 0, "new": 0}
    candidates = mine.mine_candidates(pairs, min_support=min_support)
    new_count = 0
    for term, hits in candidates.most_common():
        _, created = upsert_entry(entries, term, guess_tier(term), hits=hits)
        new_count += int(created)
    store.save_lexicon(entries)
    return {
        "pairs": len(pairs),
        "candidates": len(candidates),
        "new": new_count,
        "terms": [t for t, _ in candidates.most_common(10)],
    }


def run_promote(min_hits: int = 2) -> int:
    """candidate -> active khi đủ hits."""
    entries = store.load_lexicon()
    promoted = 0
    for entry in entries:
        if entry.status == STATUS_CANDIDATE and entry.hits >= min_hits:
            entry.status = STATUS_ACTIVE
            entry.updated = store.now_ts()
            promoted += 1
    if promoted:
        store.save_lexicon(entries)
    return promoted


def build_rules(min_support: int = 1) -> list[correct.Rule]:
    """Sinh replacement rule từ feedback (dùng cho cả CLI và nightly)."""
    pairs = collect_pairs()
    raw_rules: Counter[tuple[str, str]] = mine.mine_replacements(pairs, min_support=min_support)
    rules: list[correct.Rule] = []
    for (left, right), support in raw_rules.most_common():
        fuzzy = len(left) >= 6 and " " not in left and support >= 2
        rules.append(correct.Rule(wrong=left, right=right, support=support, fuzzy=fuzzy))
    return rules


# --- artifacts --------------------------------------------------------------
def stamp_now() -> str:
    return time.strftime("%Y%m%d-%H%M")


def current_stamp() -> str | None:
    if not CURRENT_POINTER.exists():
        return None
    value = CURRENT_POINTER.read_text(encoding="utf-8").strip()
    return value or None


def current_artifact(name: str) -> Path | None:
    stamp = current_stamp()
    if not stamp:
        return None
    candidate = ARTIFACTS_DIR / stamp / name
    return candidate if candidate.exists() else None


def render_artifacts(
    model_dir: Path,
    out_dir: Path,
    include_candidates: bool = False,
    min_support_rule: int = 1,
    validate: bool = True,
) -> dict[str, Any]:
    """Ghi hotwords.txt + replacements.json vào `out_dir` (chưa publish)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: Iterable[LexiconEntry] = [
        e for e in store.load_lexicon() if e.status != STATUS_DEMOTED
    ]
    entries = list(entries)
    invalid: list[tuple[str, str]] = []
    if validate:
        entries, invalid = render.validate_entries(entries, model_dir)
    header = (
        "Sinh tự động bởi meilin-stt — KHÔNG sửa tay.\n"
        f"model_dir={model_dir}\n"
        "Format: TỪ KHOÁ :score (sherpa-onnx hotwords)"
    )
    hotwords_path, count = render.render_hotwords(
        entries,
        out_path=out_dir / "hotwords.txt",
        include_candidates=include_candidates,
        header=header,
    )
    rules = build_rules(min_support=min_support_rule)
    rules_path = correct.save_rules(
        rules, path=out_dir / "replacements.json", meta={"generated_at": store.now_ts(), "source": "nightly"}
    )
    return {
        "hotwords_path": str(hotwords_path),
        "hotwords_terms": count,
        "rules_path": str(rules_path),
        "rules": len(rules),
        "invalid": invalid,
        "active": sum(1 for e in entries if e.status == STATUS_ACTIVE),
        "candidates": sum(1 for e in entries if e.status == STATUS_CANDIDATE),
    }


def publish_artifacts(stamp: str) -> Path:
    """Trỏ con trỏ `current` sang artifact vừa qua gate."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_POINTER.write_text(stamp + "\n", encoding="utf-8")
    # Mirror sang models/ để ai chạy mode "không artifact" vẫn có file mới nhất.
    src = ARTIFACTS_DIR / stamp
    for name in ("hotwords.txt", "replacements.json"):
        if (src / name).exists():
            shutil.copyfile(src / name, MODELS_DIR / name)
    log.info("Publish artifact %s", stamp)
    return CURRENT_POINTER


def rollback_to(stamp: str | None) -> None:
    """Trả con trỏ về artifact cũ (hoặc xoá con trỏ nếu không có)."""
    if stamp:
        CURRENT_POINTER.write_text(stamp + "\n", encoding="utf-8")
        log.warning("Rollback về artifact %s", stamp)
    else:
        CURRENT_POINTER.unlink(missing_ok=True)
        log.warning("Rollback: bỏ con trỏ current, dùng models/hotwords.txt")


def retention(keep: int = 14) -> list[str]:
    """Xoá artifact cũ, giữ `keep` bản mới nhất (kể cả bản đang current)."""
    if not ARTIFACTS_DIR.exists():
        return []
    dirs = sorted(p for p in ARTIFACTS_DIR.iterdir() if p.is_dir())
    removed: list[str] = []
    current = current_stamp()
    for path in dirs[: max(0, len(dirs) - keep)]:
        if path.name == current:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return removed
