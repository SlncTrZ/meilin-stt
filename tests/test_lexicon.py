"""Test vòng học lexicon: mine -> promote -> render -> publish/rollback.

Wing: code | Topic: stt-tests-lexicon | Updated: 2026-10-31 23:07

Mỗi test khoá một BẤT BIẾN của vòng học (why), không chỉ khoá output (what):
  - không có feedback thì không sinh ứng viên rác
  - term đã active không bị hạ cấp ngoài ý muốn
  - render luôn phân tier đúng score
  - gate fail thì KHÔNG publish (nguyên tắc "train không phá")
"""

from __future__ import annotations

from pathlib import Path

from lexicon import correct, mine, pipeline, render
from lexicon.store import STATUS_ACTIVE, STATUS_CANDIDATE, TIER_SCORE, LexiconEntry


def test_diff_words_extracts_replace_span() -> None:
    ops = mine.diff_words("mở quảng chạy ra", "mở Qdrant ra")
    assert ops, "phải phát hiện được span bị sửa"
    assert any("quảng" in op.raw_span for op in ops)
    assert any("Qdrant" in op.corrected_span for op in ops)


def test_mine_candidates_needs_support() -> None:
    pairs = [("mở quảng chạy ra", "mở Qdrant ra")]
    assert mine.mine_candidates(pairs, min_support=2) == {}, "1 lần xuất hiện chưa đủ ngưỡng"
    pairs.append(("tắt quảng chạy đi", "tắt Qdrant đi"))
    candidates = mine.mine_candidates(pairs, min_support=2)
    assert any("Qdrant" in term for term in candidates)


def test_mine_ignores_long_rewrites() -> None:
    """Người dùng viết lại cả câu không phải thuật ngữ — không được thành hotword."""
    pairs = [("câu này hoàn toàn khác", "một câu khác hẳn nghĩa luôn"), ("câu này hoàn toàn khác", "một câu khác hẳn nghĩa luôn")]
    candidates = mine.mine_candidates(pairs, min_support=1, min_term_len=3)
    assert not candidates, f"span dài phải bị loại, nhận được {candidates}"


def test_run_mine_and_promote(sample_pairs: dict[str, Path]) -> None:
    summary = pipeline.run_mine(min_support=1)
    assert summary["pairs"] == 3
    assert summary["new"] >= 1
    entries = pipeline.store.load_lexicon()
    assert all(e.status == STATUS_CANDIDATE for e in entries)
    promoted = pipeline.run_promote(min_hits=1)
    assert promoted >= 1
    entries = pipeline.store.load_lexicon()
    assert any(e.status == STATUS_ACTIVE for e in entries)


def test_render_hotwords_tier_order_and_scores(tmp_path: Path) -> None:
    entries = [
        LexiconEntry(term="meilin", tier="A", score=TIER_SCORE["A"], status=STATUS_ACTIVE),
        LexiconEntry(term="zipformer", tier="B", score=TIER_SCORE["B"], status=STATUS_ACTIVE),
        LexiconEntry(term="báo cáo", tier="C", score=TIER_SCORE["C"], status=STATUS_ACTIVE),
        LexiconEntry(term="rác", tier="B", status="demoted"),
    ]
    out, count = render.render_hotwords(entries, out_path=tmp_path / "hotwords.txt")
    text = out.read_text(encoding="utf-8")
    assert count == 3, "entry demoted không được render"
    assert "MEILIN :2.5" in text
    assert "ZIPFORMER :1.5" in text
    assert "BÁO CÁO :1.1" in text
    assert text.index("Tier A") < text.index("Tier B") < text.index("Tier C")


def test_render_dedupes_case_insensitively(tmp_path: Path) -> None:
    entries = [
        LexiconEntry(term="Qdrant", tier="A", status=STATUS_ACTIVE),
        LexiconEntry(term="qdrant", tier="A", status=STATUS_ACTIVE),
    ]
    out, count = render.render_hotwords(entries, out_path=tmp_path / "hotwords.txt")
    assert count == 1
    assert out.read_text(encoding="utf-8").count("QDRANT") == 1


def test_render_artifacts_and_publish(sample_pairs: dict[str, Path], isolated_data: dict[str, Path], fake_model_dir: Path) -> None:
    pipeline.run_mine(min_support=1)
    pipeline.run_promote(min_hits=1)
    stamp = "20261031-0300"
    out_dir = isolated_data["artifacts"] / stamp
    summary = pipeline.render_artifacts(model_dir=fake_model_dir, out_dir=out_dir)
    assert Path(summary["hotwords_path"]).exists()
    assert Path(summary["rules_path"]).exists()
    assert summary["hotwords_terms"] >= 1

    pipeline.publish_artifacts(stamp)
    assert pipeline.current_stamp() == stamp
    assert pipeline.current_artifact("hotwords.txt") is not None
    # mirror sang models/ để có fallback
    assert (isolated_data["models"] / "hotwords.txt").exists()


def test_rollback_restores_previous_pointer(isolated_data: dict[str, Path]) -> None:
    pipeline.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    for stamp in ("20261030-0300", "20261031-0300"):
        (pipeline.ARTIFACTS_DIR / stamp).mkdir()
    pipeline.publish_artifacts("20261031-0300")
    pipeline.rollback_to("20261030-0300")
    assert pipeline.current_stamp() == "20261030-0300"
    pipeline.rollback_to(None)
    assert pipeline.current_stamp() is None


def test_retention_keeps_current_and_latest(isolated_data: dict[str, Path]) -> None:
    pipeline.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    for day in range(1, 6):
        (pipeline.ARTIFACTS_DIR / f"2026100{day}-0300").mkdir()
    pipeline.publish_artifacts("20261001-0300")  # bản current là bản CŨ nhất
    removed = pipeline.retention(keep=2)
    remaining = sorted(p.name for p in pipeline.ARTIFACTS_DIR.iterdir() if p.is_dir())
    assert "20261001-0300" in remaining, "không được xoá artifact đang current"
    assert len(remaining) >= 2
    assert removed and set(removed).isdisjoint(remaining)


def test_build_rules_marks_fuzzy_only_for_short_tokens(sample_pairs: dict[str, Path]) -> None:
    rules = pipeline.build_rules(min_support=1)
    assert rules, "phải sinh được rule từ feedback"
    for rule in rules:
        if rule.fuzzy:
            assert " " not in rule.wrong and len(rule.wrong) >= 6


def test_collect_pairs_falls_back_to_feedback_raw_text(isolated_data: dict[str, Path]) -> None:
    """Feedback gửi kèm raw_text (Pi hook/CLI) phải dùng được dù không có utterances row."""
    from lexicon import store

    store.append_jsonl(
        isolated_data["feedback"],
        store.Feedback(
            utterance_id="khong-ton-tai",
            ts="2026-10-31T23:20:00+0700",
            corrected_text="mở Qdrant ra",
            source="test",
            raw_text="mở quảng chạy ra",
        ).to_json(),
    )
    pairs = pipeline.collect_pairs()
    assert pairs == [("mở quảng chạy ra", "mở Qdrant ra")]


def test_apply_rules_respects_word_boundary() -> None:
    rules = [correct.Rule(wrong="quảng chạy", right="Qdrant")]
    text, applied = correct.apply_rules("mở quảng chạy ra, đừng quảng chạy lung tung", rules)
    assert applied
    assert text.count("Qdrant") == 2
    text2, applied2 = correct.apply_rules("quảngchạy liền nhau", rules)
    assert not applied2, "không được thay khi không có biên từ"


def test_apply_rules_prefers_longer_rule() -> None:
    rules = [
        correct.Rule(wrong="sherpa ông nx", right="sherpa-onnx"),
        correct.Rule(wrong="sherpa", right="SHERPA"),
    ]
    text, _ = correct.apply_rules("dùng sherpa ông nx đi", rules)
    assert text == "dùng sherpa-onnx đi"


def test_fuzzy_apply_only_long_tokens() -> None:
    text, applied = correct.fuzzy_apply("chạy zipformr đi", ["zipformer"])
    assert applied and "zipformer" in text
    text2, applied2 = correct.fuzzy_apply("abc xyz", ["abc"])
    assert not applied2
