"""The Checker's structured report survives a model naming ``status`` as ``verdict``.

Regression (Pro trace a8445e7d): gemini returned ``{"verdict": "passed", ...}``;
CheckReport validation failed, the LLM call errored and the run paused.
"""

from artemis.agents.checker.checker import CheckReport


def test_verdict_key_is_read_as_status():
    report = CheckReport.model_validate(
        {
            "verdicts": [
                {"item_text": "Cart has 1 item", "kind": "verify", "verdict": "failed", "evidence": "step 18"},
                {"item_text": "No Place Order", "kind": "assert", "status": "passed", "evidence": "step 20"},
            ]
        }
    )
    assert [v.status for v in report.verdicts] == ["failed", "passed"]


def test_schema_still_names_the_field_status():
    props = CheckReport.model_json_schema()["$defs"]["CheckVerdict"]["properties"]
    assert set(props) == {"item_text", "kind", "status", "evidence", "suggestion"}


def test_missing_kind_and_status_do_not_fail_the_report():
    """Regression (same trace, later check): verdicts carried only item_text and
    evidence. The report must still parse; a missing status is inconclusive."""
    report = CheckReport.model_validate(
        {"verdicts": [{"item_text": "DO NOT tap Place Order", "evidence": "timed out"}]}
    )
    v = report.verdicts[0]
    assert v.status == "inconclusive"
    assert v.kind_inferred is True


def test_normalize_takes_the_kind_from_the_quoted_check_item():
    from types import SimpleNamespace

    from artemis.agents.checker.checker import _normalize_report

    report = CheckReport.model_validate(
        {"verdicts": [{"item_text": "DO NOT tap Place Order", "verdict": "passed", "evidence": "step 20"}]}
    )
    items = [SimpleNamespace(text="DO NOT tap Place Order", kind="assert")]
    out = _normalize_report(report, items)
    assert [(v.kind, v.status) for v in out.verdicts] == [("assert", "passed")]
