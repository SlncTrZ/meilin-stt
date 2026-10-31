"""lexicon CLI — mine / promote / demote / render / validate / audit / learn.

Wing: code | Topic: stt-lexicon-cli | Updated: 2026-10-31 23:07

Đây là "bàn lái" của vòng học. Logic thật nằm ở `lexicon/pipeline.py` để nightly
job và CLI dùng chung một đường — không có hai bản cài đặt lệch nhau.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from lexicon import correct, pipeline, render, store
from lexicon.store import (
    FEEDBACK,
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    STATUS_DEMOTED,
    UTTERANCES,
    load_lexicon,
    now_ts,
    read_jsonl,
    save_lexicon,
)

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models"


def cmd_mine(args: argparse.Namespace) -> int:
    summary = pipeline.run_mine(min_support=args.min_support)
    if not summary["pairs"]:
        print("Không có cặp feedback nào. Dùng `meilin-stt-lexicon learn` hoặc POST /v1/feedback.")
        return 0
    print(
        f"{summary['pairs']} cặp feedback → {summary['candidates']} ứng viên "
        f"({summary['new']} mới)."
    )
    if summary.get("terms"):
        print("Top: " + ", ".join(summary["terms"]))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    promoted = pipeline.run_promote(min_hits=args.min_hits)
    print(f"Promote {promoted} entry lên active (ngưỡng hits >= {args.min_hits}).")
    return 0


def cmd_demote(args: argparse.Namespace) -> int:
    entries = load_lexicon()
    target = args.term.lower()
    for entry in entries:
        if entry.term.lower() == target:
            entry.status = STATUS_DEMOTED
            entry.updated = now_ts()
            entry.note = args.reason or "demoted by CLI"
            save_lexicon(entries)
            print(f"Đã hạ '{entry.term}' → demoted ({entry.note}).")
            return 0
    print(f"Không tìm thấy '{args.term}' trong lexicon.")
    return 1


def cmd_render(args: argparse.Namespace) -> int:
    stamp = pipeline.stamp_now()
    out_dir = args.out_dir or (pipeline.ARTIFACTS_DIR / stamp)
    summary = pipeline.render_artifacts(
        model_dir=args.model_dir,
        out_dir=out_dir,
        include_candidates=args.include_candidates,
        min_support_rule=args.min_support_rule,
        validate=not args.skip_validate,
    )
    if summary["invalid"]:
        print(f"⚠️  {len(summary['invalid'])} hotword không emit được (đã bỏ):")
        for term, reason in summary["invalid"][:20]:
            print(f"   - {term}: {reason}")
    print(f"hotwords : {summary['hotwords_path']} ({summary['hotwords_terms']} term)")
    print(f"rules    : {summary['rules_path']} ({summary['rules']} rule)")
    if args.publish:
        pointer = pipeline.publish_artifacts(stamp)
        print(f"Đã publish: {pointer} -> {stamp}")
    else:
        print("(chưa publish — thêm --publish để trỏ `current` sang artifact này)")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    entries = [e for e in load_lexicon() if e.status == STATUS_ACTIVE]
    if not entries:
        print("Lexicon chưa có entry active.")
        return 0
    _, bad = render.validate_entries(entries, args.model_dir)
    if not bad:
        print(f"✅ {len(entries)} hotword active đều tokenize được bằng {args.model_dir}.")
        return 0
    print(f"❌ {len(bad)}/{len(entries)} hotword KHÔNG emit được:")
    for term, reason in bad:
        print(f"   - {term}: {reason}")
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    entries = load_lexicon()
    if args.status:
        entries = [e for e in entries if e.status == args.status]
    if args.tier:
        entries = [e for e in entries if e.tier == args.tier]
    for entry in sorted(entries, key=lambda e: (e.tier, -e.hits, e.term.lower())):
        print(
            f"[{entry.tier}] {entry.term:<40} score={entry.score:<5g} "
            f"status={entry.status:<9} hits={entry.hits}"
        )
    print(f"-- {len(entries)} entry")
    return 0


def _count_hotwords(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def cmd_audit(args: argparse.Namespace) -> int:
    entries = load_lexicon()
    by_status = Counter(e.status for e in entries)
    hotwords = pipeline.current_artifact("hotwords.txt") or (MODELS_DIR / "hotwords.txt")
    report = {
        "lexicon_total": len(entries),
        "by_status": dict(by_status),
        "active_by_tier": dict(Counter(e.tier for e in entries if e.status == STATUS_ACTIVE)),
        "utterances": len(read_jsonl(UTTERANCES)),
        "feedback": len(read_jsonl(FEEDBACK)),
        "current_artifact": pipeline.current_stamp(),
        "hotwords_file": str(hotwords),
        "hotwords_terms": _count_hotwords(hotwords),
        "replacement_rules": len(correct.load_rules()),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    """Ghi 1 cặp sửa tay rồi mine + render ngay (dùng khi chưa bật Pi hook)."""
    raw = (args.raw or "").strip()
    corrected = args.corrected.strip()
    if not corrected:
        print('Cần text đã sửa: meilin-stt-lexicon learn "câu đúng" [--raw "câu máy nghe"]')
        return 2
    uid = store.new_id()
    store.append_jsonl(
        UTTERANCES,
        store.Utterance(
            id=uid,
            ts=now_ts(),
            raw_text=raw,
            final_text=raw,
            model=args.model,
            lane="cli",
            duration_sec=0.0,
        ).to_json(),
    )
    store.append_jsonl(
        FEEDBACK,
        store.Feedback(
            utterance_id=uid,
            ts=now_ts(),
            corrected_text=corrected,
            source="cli",
        ).to_json(),
    )
    print(f"Đã ghi feedback {uid}.")
    pipeline.run_mine(min_support=1)
    return cmd_render(
        argparse.Namespace(
            model_dir=args.model_dir,
            out_dir=None,
            include_candidates=args.include_candidates,
            min_support_rule=1,
            skip_validate=False,
            publish=args.publish,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meilin-stt-lexicon",
        description="Vòng học lexicon cho meilin-stt (hotwords + correction rules).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_mine = sub.add_parser("mine", help="trích ứng viên từ feedback")
    p_mine.add_argument("--min-support", type=int, default=2)
    p_mine.set_defaults(func=cmd_mine)

    p_promote = sub.add_parser("promote", help="candidate -> active")
    p_promote.add_argument("--min-hits", type=int, default=2)
    p_promote.set_defaults(func=cmd_promote)

    p_demote = sub.add_parser("demote", help="hạ 1 term (negative knowledge)")
    p_demote.add_argument("term")
    p_demote.add_argument("--reason", default="")
    p_demote.set_defaults(func=cmd_demote)

    p_render = sub.add_parser("render", help="sinh hotwords.txt + replacements.json")
    p_render.add_argument("--model-dir", type=Path, required=True)
    p_render.add_argument("--out-dir", type=Path, default=None)
    p_render.add_argument("--include-candidates", action="store_true")
    p_render.add_argument("--min-support-rule", type=int, default=1)
    p_render.add_argument("--skip-validate", action="store_true")
    p_render.add_argument("--publish", action="store_true")
    p_render.set_defaults(func=cmd_render)

    p_validate = sub.add_parser("validate", help="kiểm tra hotword có tokenize được")
    p_validate.add_argument("--model-dir", type=Path, required=True)
    p_validate.set_defaults(func=cmd_validate)

    p_list = sub.add_parser("list", help="liệt kê lexicon")
    p_list.add_argument("--status", choices=[STATUS_CANDIDATE, STATUS_ACTIVE, STATUS_DEMOTED])
    p_list.add_argument("--tier", choices=["A", "B", "C"])
    p_list.set_defaults(func=cmd_list)

    p_audit = sub.add_parser("audit", help="báo cáo JSON tình trạng vòng học")
    p_audit.set_defaults(func=cmd_audit)

    p_learn = sub.add_parser("learn", help="ghi 1 cặp sửa tay rồi mine+render")
    p_learn.add_argument("corrected", help="text đã sửa (đúng)")
    p_learn.add_argument("--raw", default="", help="text máy nghe được")
    p_learn.add_argument("--model", default="68m")
    p_learn.add_argument("--model-dir", type=Path, required=True)
    p_learn.add_argument("--include-candidates", action="store_true")
    p_learn.add_argument("--publish", action="store_true")
    p_learn.set_defaults(func=cmd_learn)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
