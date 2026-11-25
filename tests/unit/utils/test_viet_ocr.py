"""VietOCR recognizer mode: locate every line, read only the lines a consumer needs."""

import io

from PIL import Image

from artemis.config.agent import FlashOcrConfig
from artemis.utils import local_ocr, viet_ocr
from artemis.utils.local_ocr import read_unread, recognize_screenshot
from artemis.utils.viet_ocr import make_reader, read_lines
from artemis.utils.visualization import _merge_ocr_words, merge_ocr_into_list


def _line(text, l, t, r, b, score=0.95, unread=False):
    res = {"text": text, "score": score,
           "position": [{"x": l, "y": t}, {"x": r, "y": t}, {"x": r, "y": b}, {"x": l, "y": b}]}
    if unread:
        res["unread"] = True
    return res


def _png(w=100, h=200):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_missing_model_reads_nothing_and_reader_keeps_texts(tmp_path):
    cfg = FlashOcrConfig(recognizer="vietocr", vi_model_dir=str(tmp_path))
    viet_ocr._failed.clear()
    assert read_lines(b"x", [(0, 0, 10, 10)], cfg) is None
    assert viet_ocr.available(cfg) is False
    assert viet_ocr.available(FlashOcrConfig()) is False  # paddle recognizer configured
    assert make_reader(b"x", cfg)([{"text": "", "box": (0, 0, 10, 10)}]) == [""]


def test_detect_only_mode_returns_unread_lines(monkeypatch):
    class FakeEngine:
        def __call__(self, img, use_rec=True):
            assert use_rec is False
            return [[[10, 20], [60, 20], [60, 40], [10, 40]]], None

    monkeypatch.setattr(local_ocr, "_get_engine", lambda cfg: FakeEngine())
    monkeypatch.setattr(local_ocr, "_vietocr_on", lambda cfg: True)
    local_ocr._cache.clear()
    out = recognize_screenshot(_png(), [], FlashOcrConfig(recognizer="vietocr"))
    assert out == [{"text": "", "score": 1.0, "unread": True,
                    "position": [{"x": 10, "y": 20}, {"x": 60, "y": 20}, {"x": 60, "y": 40}, {"x": 10, "y": 40}]}]


def test_read_unread_fills_only_lines_within(monkeypatch):
    calls = []

    def fake_read(image_bytes, boxes, cfg):
        calls.append(boxes)
        return ["Phở 24" for _ in boxes]

    monkeypatch.setattr(viet_ocr, "read_lines", fake_read)
    lines = [_line("", 0, 0, 50, 20, unread=True), _line("", 0, 500, 50, 520, unread=True), _line("Done", 0, 900, 50, 920)]
    read_unread(b"img", lines, FlashOcrConfig(recognizer="vietocr"), within=[(0, 480, 100, 540)])
    assert calls == [[(0, 500, 50, 520)]]
    assert lines[1]["text"] == "Phở 24" and "unread" not in lines[1]
    assert lines[0]["unread"] and lines[2]["text"] == "Done"


def test_word_merge_keeps_unread_flag():
    merged = _merge_ocr_words([_line("", 0, 0, 40, 20, unread=True), _line("", 45, 0, 90, 20, unread=True)])
    assert len(merged) == 1 and merged[0]["unread"] is True


def test_merge_reads_only_lines_the_xml_does_not_cover():
    elements = [{"index": 1, "text": "Checkout", "bounds": [0, 400, 500, 460], "center": [250, 430]}]
    seen = []

    def reader(lines):
        seen.extend(l["box"] for l in lines)
        return ["Ưu đãi", "*"][: len(lines)]

    ocr = [_line("", 10, 410, 300, 450, unread=True),   # on "Checkout": never read
           _line("", 100, 800, 400, 850, unread=True),  # gap text
           _line("", 100, 1200, 130, 1230, unread=True)]  # reads as a lone glyph: dropped
    text, stats = merge_ocr_into_list('[1] "Checkout" 0,200,500,230', elements, ocr, 1000, 2000, reader)
    assert seen == [(100, 800, 400, 850), (100, 1200, 130, 1230)]
    assert 'ocr "Ưu đãi"' in text and stats["read"] == 2 and stats["added"] == 1 and stats["duplicate"] == 1


def test_unread_lines_without_reader_are_dropped():
    text, stats = merge_ocr_into_list("", [], [_line("", 100, 1800, 900, 1900, unread=True)], 1000, 2000)
    assert text == "" and stats["added"] == 0


def test_reader_failure_drops_unread_lines():
    def reader(lines):
        raise RuntimeError("boom")

    text, stats = merge_ocr_into_list("", [], [_line("", 100, 1800, 900, 1900, unread=True)], 1000, 2000, reader)
    assert text == "" and stats["read"] == 0
