"""Report markdown cho nightly job.

Wing: code | Topic: stt-nightly-report | Updated: 2026-10-31 23:07
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = BASE_DIR / "data" / "reports"


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "—"
    return str(value) if value not in (None, "") else "—"


def write_report(data: dict[str, Any], out_dir: Path = REPORTS_DIR) -> Path:
    """Ghi `nightly-YYYY-MM-DD.md`, trả về đường dẫn."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    path = out_dir / f"nightly-{stamp}.md"
    gate = data.get("gate") or {}
    status_icon = "✅" if data.get("published") else "❌"
    lines = [
        f"# meilin-stt nightly — {stamp} {status_icon}",
        "",
        f"- Thời điểm chạy: `{data.get('started_at', '')}` → `{data.get('finished_at', '')}`",
        f"- Artifact: `{data.get('stamp', '')}` (publish: **{data.get('published')}**)",
        f"- Kết quả gate: **{gate.get('passed')}** — mean CER {_fmt(gate.get('mean_cer'))}"
        f" (baseline {_fmt(gate.get('baseline_mean_cer'))}, tolerance {_fmt(gate.get('tolerance'))})",
        "",
        "## Học",
        "",
        "| Bước | Kết quả |",
        "|---|---|",
        f"| Feedback pairs | {_fmt((data.get('mine') or {}).get('pairs'))} |",
        f"| Ứng viên mới | {_fmt((data.get('mine') or {}).get('new'))} |",
        f"| Promoted | {_fmt(data.get('promoted'))} |",
        f"| Hotword active | {_fmt((data.get('artifacts') or {}).get('active'))} |",
        f"| Hotword sinh ra | {_fmt((data.get('artifacts') or {}).get('hotwords_terms'))} |",
        f"| Replacement rules | {_fmt((data.get('artifacts') or {}).get('rules'))} |",
        f"| Hotword không emit được | {_fmt(len((data.get('artifacts') or {}).get('invalid') or []))} |",
        "",
    ]
    cases = gate.get("cases") or []
    if cases:
        lines.extend(["## Case regression", "", "| Case | CER | Ngưỡng | OK |", "|---|---|---|---|"])
        for case in cases:
            lines.append(
                f"| {case.get('case_id')} | {_fmt(case.get('cer'))} | "
                f"{_fmt(case.get('max_cer'))} | {'✅' if case.get('cer', 0) <= case.get('max_cer', 1) else '❌'} |"
            )
        lines.append("")
    reasons = gate.get("reasons") or []
    if reasons:
        lines.extend(["## Lý do chặn", ""])
        lines.extend(f"- {reason}" for reason in reasons)
        lines.append("")
    if data.get("rollback_to") is not None:
        lines.extend(["## Rollback", "", f"Đã trả con trỏ về: `{data['rollback_to'] or 'models/hotwords.txt'}`", ""])
    lines.extend(
        [
            "## Kết luận",
            "",
            data.get("message", "—"),
            "",
            "---",
            f"<sub>meilin-stt nightly · dữ liệu thô: `data/reports/nightly-{stamp}.json`</sub>",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    (out_dir / f"nightly-{stamp}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
