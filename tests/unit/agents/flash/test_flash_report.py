"""Flash leaves its written report in notes/output.md, like Pro's Outputter."""

from types import SimpleNamespace

from artemis.agents.flash.report import render_flash_report, save_flash_report
from artemis.utils.notes import get_note_file_path, read_note_content, save_note_content


def _ctx(tmp_path):
    return SimpleNamespace(data_engine=SimpleNamespace(base_dir=str(tmp_path)))


def test_explanation_is_saved_as_the_output_note(tmp_path):
    result = {"status": "completed", "explanation": "## Kết quả\n\n| Quán | Giá |\n|---|---|\n| A | 57.000đ |"}
    assert save_flash_report(_ctx(tmp_path), result) is True
    saved = read_note_content(str(tmp_path), "output")
    assert saved.startswith("**Status:** completed")
    assert "| A | 57.000đ |" in saved


def test_outputter_report_wins_over_the_explanation(tmp_path):
    result = {"status": "completed", "explanation": "short"}
    save_flash_report(_ctx(tmp_path), result, synthesized="# Full report\n\nrebuilt from history")
    assert "rebuilt from history" in read_note_content(str(tmp_path), "output")


def test_a_report_the_outputter_already_saved_is_kept(tmp_path):
    save_note_content(str(tmp_path), "output", "outputter wrote this")
    save_flash_report(_ctx(tmp_path), {"status": "completed", "explanation": "other"})
    assert read_note_content(str(tmp_path), "output") == "outputter wrote this"


def test_structured_output_is_rendered_as_json(tmp_path):
    text = render_flash_report({"status": "completed"}, synthesized={"total": 73000})
    assert '"total": 73000' in text and text.count("```") == 2


def test_no_data_engine_means_nothing_is_written(tmp_path):
    assert save_flash_report(SimpleNamespace(data_engine=None), {"status": "failed"}) is False
    assert not get_note_file_path(str(tmp_path), "output").exists()
