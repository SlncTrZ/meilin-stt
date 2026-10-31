"""Kiểm tra format hotwords trước khi publish (dùng được trong CI).

Wing: code | Topic: stt-scripts | Updated: 2026-10-31 23:35

Format hợp lệ: mỗi dòng `TỪ KHOÁ :score`, score là số thực; dòng `#` là comment.
Lỗi format khiến sherpa-onnx ném lỗi khó hiểu ở tầng C++ — nên chặn từ đây.
"""

from __future__ import annotations

import sys

# Console Windows mặc định cp1252 -> ép UTF-8 để log tiếng Việt không crash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT = BASE_DIR / "models" / "hotwords.txt"


def check(path: Path) -> tuple[int, list[str]]:
    if not path.exists():
        return 0, [f"không thấy file {path}"]
    terms = 0
    problems: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " :" not in line:
            problems.append(f"{path.name}:{lineno} thiếu ' :score' -> {line[:60]}")
            continue
        term, _, score = line.rpartition(" :")
        if not term.strip():
            problems.append(f"{path.name}:{lineno} term rỗng")
            continue
        try:
            float(score)
        except ValueError:
            problems.append(f"{path.name}:{lineno} score không phải số -> {score!r}")
            continue
        terms += 1
    return terms, problems


def main(argv: list[str] | None = None) -> int:
    target = Path(argv[0]) if argv else DEFAULT
    terms, problems = check(target)
    if problems:
        print(f"❌ {len(problems)} dòng sai format:", file=sys.stderr)
        for problem in problems[:20]:
            print(f"   - {problem}", file=sys.stderr)
        return 1
    print(f"✅ hotwords OK: {terms} term ({target})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
