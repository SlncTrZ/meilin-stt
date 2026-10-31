"""Correction layer — áp bảng thay thế đã học lên text cuối.

Wing: code | Topic: stt-correct | Updated: 2026-10-31 23:07

Chỉ áp lên bản `final` (không đụng `partial`), để Anh không thấy chữ nhảy.
Mỗi lần áp đều trả về danh sách `applied` để ghi log/audit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
REPLACEMENTS = BASE_DIR / "models" / "replacements.json"


@dataclass(frozen=True)
class Rule:
    wrong: str
    right: str
    support: int = 1
    fuzzy: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "wrong": self.wrong,
            "right": self.right,
            "support": self.support,
            "fuzzy": self.fuzzy,
        }


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def load_rules(path: Path = REPLACEMENTS) -> list[Rule]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rules: list[Rule] = []
    for item in raw.get("rules", []):
        try:
            rules.append(
                Rule(
                    wrong=str(item["wrong"]),
                    right=str(item["right"]),
                    support=int(item.get("support", 1)),
                    fuzzy=bool(item.get("fuzzy", False)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rules


def save_rules(rules: Iterable[Rule], path: Path = REPLACEMENTS, meta: dict[str, Any] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "generated_at": (meta or {}).get("generated_at", ""),
        "source": (meta or {}).get("source", "nightly"),
        "rules": [r.to_json() for r in rules],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _pattern(wrong: str) -> re.Pattern[str]:
    """Khớp theo biên từ, không phân biệt hoa/thường, coi khoảng trắng là linh hoạt."""
    parts = [re.escape(p) for p in wrong.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.IGNORECASE | re.UNICODE)


def apply_rules(text: str, rules: Iterable[Rule] | None = None) -> tuple[str, list[dict[str, str]]]:
    """Áp rule trong MỘT lượt quét, ưu tiên match dài nhất.

    Không dùng vòng lặp `str.replace` tuần tự: nếu rule ngắn chạy sau rule dài,
    nó sẽ sửa cả phần vừa được sửa (vd "sherpa" ăn vào "sherpa-onnx" vừa tạo ra).
    """
    if not text:
        return text, []
    rules = [r for r in (rules or []) if r.wrong.strip()]
    if not rules:
        return text, []

    # Dài nhất trước để alternation khớp đúng rule chuyên biệt hơn.
    ordered = sorted(rules, key=lambda r: len(r.wrong.split()), reverse=True)
    by_pattern = {r.wrong.lower(): r for r in ordered}
    alternation = "|".join(_pattern(r.wrong).pattern for r in ordered)
    combined = re.compile(alternation, re.IGNORECASE | re.UNICODE)

    applied: list[dict[str, str]] = []

    def _replace(match: re.Match[str]) -> str:
        source = match.group(0)
        key = " ".join(source.split()).lower()
        rule = by_pattern.get(key)
        if rule is None:  # khớp nhờ biên từ khác -> tra theo pattern đã dùng
            for candidate in ordered:
                if _pattern(candidate.wrong).fullmatch(source):
                    rule = candidate
                    break
        if rule is None:
            return source
        applied.append({"wrong": rule.wrong, "right": rule.right, "mode": "exact"})
        return rule.right

    return combined.sub(_replace, text), applied


def fuzzy_apply(text: str, terms: Iterable[str], max_distance: int = 1) -> tuple[str, list[dict[str, str]]]:
    """Sửa token gần giống term đã biết (chỉ token >= 6 ký tự)."""
    vocabulary = [t for t in terms if len(t) >= 6]
    if not vocabulary or not text:
        return text, []
    out_tokens: list[str] = []
    applied: list[dict[str, str]] = []
    for token in text.split(" "):
        core = token.strip(".,!?;:()[]\"'")
        replacement = None
        if len(core) >= 6:
            for term in vocabulary:
                if core.lower() == term.lower():
                    break
                if levenshtein(core.lower(), term.lower()) <= max_distance:
                    replacement = term
                    applied.append({"wrong": core, "right": term, "mode": "fuzzy"})
                    break
        out_tokens.append(token.replace(core, replacement) if replacement else token)
    return " ".join(out_tokens), applied
