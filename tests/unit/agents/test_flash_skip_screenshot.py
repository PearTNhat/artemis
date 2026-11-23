"""Flash ``model_image.skip_unchanged``: leave the screenshot out on same-screen turns."""

from artemis.agents.flash.runner import SCREENSHOT_SKIPPED_NOTE, FlashRunner, _list_signature
from artemis.config.agent import FlashModelImageConfig

LIST_A = '[1] "Wi-Fi" 40,100,900,160\n[2] (toggle) 800,110,900,150 checked:false\n[3] "Bluetooth" 40,200,900,260'
LIST_A_TOGGLED = LIST_A.replace("checked:false", "checked:true")
LIST_B = '[1] "Display" 40,100,900,160\n[2] "Dark theme" 40,200,900,260\n[3] "Font size" 40,300,900,360'


def _runner(**cfg) -> FlashRunner:
    r = FlashRunner.__new__(FlashRunner)
    r.model_image_cfg = FlashModelImageConfig(**cfg)
    return r


def test_signature_ignores_numbers():
    assert _list_signature('[3] "Wi-Fi" 10,20,30,40') == _list_signature('[7] "Wi-Fi" 11,22,33,44')
    assert _list_signature(None) == set()
    assert _list_signature("Not available") == set()


def test_off_by_default_never_skips():
    r = _runner()
    assert not r._skip_screenshot(1, LIST_A, False)
    assert not r._skip_screenshot(2, LIST_A, False)


def test_skips_same_screen_after_an_image_turn():
    r = _runner(skip_unchanged=True, skip_similarity=0.5)
    assert not r._skip_screenshot(1, LIST_A, False)  # first turn always has the image
    assert r._skip_screenshot(2, LIST_A_TOGGLED, False)  # 2 of 4 lines shared -> 0.5


def test_new_screen_and_notices_send_the_image():
    r = _runner(skip_unchanged=True)
    assert not r._skip_screenshot(1, LIST_A, False)
    assert not r._skip_screenshot(2, LIST_B, False)  # new screen
    assert not r._skip_screenshot(3, LIST_B, True)  # a notice (failed / no change) asks to look


def test_consecutive_skip_cap_and_stable_decision_per_turn():
    r = _runner(skip_unchanged=True, max_consecutive_skips=1)
    assert not r._skip_screenshot(1, LIST_A, False)
    assert r._skip_screenshot(2, LIST_A, False)
    assert r._skip_screenshot(2, LIST_A, False)  # re-asked turn keeps its decision
    assert not r._skip_screenshot(3, LIST_A, False)  # cap reached -> image again


def test_tail_carries_note_instead_of_image():
    from types import SimpleNamespace

    r = _runner(skip_unchanged=True)
    r.media_resolution = None
    ledger = SimpleNamespace(elapsed_label=lambda: "T+00:01")
    t1 = r._build_tail(ledger, 1, b"\xff\xd8jpeg", LIST_A)
    t2 = r._build_tail(ledger, 2, b"\xff\xd8jpeg", LIST_A)
    kinds1 = [b.get("type") for b in t1.content]
    texts2 = [b.get("text", "") for b in t2.content]
    assert "image_url" in kinds1
    assert all(b.get("type") != "image_url" for b in t2.content)
    assert SCREENSHOT_SKIPPED_NOTE in texts2
