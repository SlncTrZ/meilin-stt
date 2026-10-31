"""Word-level diff + candidate mining từ feedback.

Wing: code | Topic: stt-lexicon-mine | Updated: 2026-10-31 23:07

Ý tưởng: mỗi lần người dùng sửa text trả về, phần "sửa" chính là nhãn dữ liệu
miễn phí. Từ các cặp (raw -> corrected) ta trích ra:
  - thuật ngữ mới nên đưa vào lexicon (hotword candidate)
  - cặp thay thế cố định (replacement rule) cho tầng post-ASR
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

# Từ chức năng tiếng Việt — không bao giờ là hotword.
STOPWORDS = {
    "và", "là", "của", "có", "cho", "thì", "mà", "nên", "nhưng", "được", "không",
    "một", "hai", "ba", "các", "những", "cái", "này", "đó", "khi", "nếu", "vì",
    "để", "với", "từ", "đến", "trong", "ngoài", "trên", "dưới", "ra", "vào",
    "rồi", "đã", "đang", "sẽ", "vẫn", "cũng", "rất", "quá", "lắm", "ạ", "à",
    "ừ", "vâng", "dạ", "em", "anh", "chị", "nó", "mình", "tôi", "bạn", "họ",
    "làm", "nói", "biết", "thấy", "muốn", "cần", "phải", "nha", "nhé", "nhỉ",
    "thế", "vậy", "sao", "gì", "nào", "ai", "đâu", "bao", "nhiêu", "lúc",
}

WORD_RE = re.compile(r"[\w\-\.]+", re.UNICODE)


def normalize_punct(text: str) -> str:
    """Chuẩn hoá Unicode NFC — tránh lệch dạng tổ hợp khi so sánh."""
    return unicodedata.normalize("NFC", text)


def words(text: str) -> list[str]:
    return WORD_RE.findall(normalize_punct(text or ""))


@dataclass(frozen=True)
class DiffOp:
    """Một thao tác sửa: raw_span được thay bằng corrected_span."""

    raw_span: str
    corrected_span: str

    @property
    def is_insert(self) -> bool:
        return not self.raw_span

    @property
    def is_replace(self) -> bool:
        return bool(self.raw_span) and bool(self.corrected_span)


def diff_words(raw: str, corrected: str, max_span_words: int = 4) -> list[DiffOp]:
    """So khớp theo từ, trả về các span bị sửa (giới hạn độ dài span)."""
    a = words(raw)
    b = words(corrected)
    if a == b:
        return []
    ops: list[DiffOp] = []
    matcher = SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if (i2 - i1) > max_span_words or (j2 - j1) > max_span_words:
            # Span quá dài = người dùng viết lại cả câu, không phải thuật ngữ.
            continue
        ops.append(DiffOp(raw_span=" ".join(a[i1:i2]), corrected_span=" ".join(b[j1:j2])))
    return ops


def is_ascii_term(term: str, min_len: int = 4) -> bool:
    """Term ASCII không dấu, đủ dài -> gần như chắc chắn là brand/thuật ngữ ngoại lai.

    Tiếng Việt chuẩn luôn có dấu; người dùng sửa "quảng chạy" -> "Qdrant" là tín hiệu
    thật, nên nhánh này phải chấp nhận dù độ giống ký tự thấp.
    """
    core = term.strip()
    if len(core.replace(" ", "")) < min_len or " " in core:
        return False
    if core.lower() in STOPWORDS:
        return False
    return core.isascii() and any(ch.isalpha() for ch in core)


def looks_like_misrecognition(raw_span: str, corrected_span: str, min_ratio: float = 0.55) -> bool:
    """Phân biệt "nghe sai" với "viết lại cả câu".

    Nghe sai  -> hai span gần giống nhau về ký tự ("quảng chạy" vs "Qdrant" thì KHÔNG,
                 nhưng "sherpa ông nx" vs "sherpa-onnx" thì CÓ), hoặc là thuật ngữ.
    Viết lại -> hai span khác hẳn nhau, không phải dữ liệu học hữu ích cho hotword.
    """
    if is_technical(corrected_span) or is_ascii_term(corrected_span):
        return True
    a = normalize_punct(raw_span or "").lower().replace(" ", "")
    b = normalize_punct(corrected_span or "").lower().replace(" ", "")
    if not a or not b:
        return False
    return SequenceMatcher(a=a, b=b).ratio() >= min_ratio


def mine_candidates(
    pairs: Iterable[tuple[str, str]],
    min_support: int = 2,
    min_term_len: int = 3,
) -> Counter[str]:
    """Đếm ứng viên hotword từ các cặp (raw, corrected)."""
    counter: Counter[str] = Counter()
    for raw, corrected in pairs:
        for op in diff_words(raw, corrected):
            term = op.corrected_span.strip()
            if not term:
                continue
            if len(term) < min_term_len:
                continue
            if term.lower() in STOPWORDS:
                continue
            if term.isdigit():
                continue
            if not looks_like_misrecognition(op.raw_span, term):
                continue
            counter[term] += 1
    return Counter({t: c for t, c in counter.items() if c >= min_support})


def mine_replacements(
    pairs: Iterable[tuple[str, str]],
    min_support: int = 1,
    max_span_words: int = 4,
) -> Counter[tuple[str, str]]:
    """Đếm cặp (sai -> đúng) đủ điều kiện thành rule thay thế."""
    counter: Counter[tuple[str, str]] = Counter()
    for raw, corrected in pairs:
        for op in diff_words(raw, corrected, max_span_words=max_span_words):
            if not op.corrected_span:
                continue
            left = op.raw_span.strip()
            right = op.corrected_span.strip()
            if not left or left.lower() == right.lower():
                continue
            counter[(left, right)] += 1
    return Counter({k: v for k, v in counter.items() if v >= min_support})


def is_technical(term: str) -> bool:
    """Heuristic: có chữ số / gạch nối / chữ hoa giữa từ -> thuật ngữ kỹ thuật."""
    if any(ch.isdigit() for ch in term):
        return True
    if "-" in term or "." in term:
        return True
    core = term.split()
    return any(w.isupper() and len(w) > 1 for w in core)
