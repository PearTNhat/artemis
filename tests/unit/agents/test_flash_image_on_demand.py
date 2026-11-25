"""Flash ``model_image.send = "on_demand"``: text-first turns, screenshot when needed."""

from io import BytesIO
from types import SimpleNamespace

from PIL import Image, ImageDraw

from artemis.agents.flash.runner import LOOK_AT_SCREEN_TOOL, TEXT_ONLY_NOTE, FlashRunner
from artemis.config.agent import FlashModelImageConfig

LIST_A = '[1] "Delivery instructions" 40,566,507,599 (required)\n[2] "Meet at lobby" 81,722,807,749\n[3] (button) 807,707,928,764'


def _jpeg(red_box: bool = False) -> bytes:
    img = Image.new("RGB", (400, 800), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([20, 300, 380, 420], outline="red" if red_box else "white", width=12)
    buf = BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def _runner(send="on_demand", **cfg) -> FlashRunner:
    r = FlashRunner.__new__(FlashRunner)
    r.model_image_cfg = FlashModelImageConfig(send=send, **cfg)
    r.image_on_demand = send == "on_demand"
    r.media_resolution = None
    return r


def test_always_mode_sends_every_turn():
    r = _runner(send="always")
    assert all(r._send_image(t, _jpeg(), LIST_A, False) for t in (1, 2, 3))


def test_text_after_first_turn_and_image_on_notice_or_request():
    r = _runner()
    img = _jpeg()
    assert r._send_image(1, img, LIST_A, False)  # first turn
    assert not r._send_image(2, img, LIST_A, False)  # same screen, nothing new
    assert r._send_image(3, img, LIST_A, True)  # a notice (failed / no-effect action)
    r._force_image = True  # look_at_screen or a deferred completion
    assert r._send_image(4, img, LIST_A, False)
    assert not r._send_image(5, img, LIST_A, False)


def test_pixel_only_change_sends_the_image():
    r = _runner()
    assert r._send_image(1, _jpeg(), LIST_A, False)
    # Same element list, but a red error border appeared: only pixels show it.
    assert r._send_image(2, _jpeg(red_box=True), LIST_A, False)


def test_cap_on_consecutive_text_turns_and_stable_decision():
    r = _runner(text_max_consecutive=2)
    img = _jpeg()
    assert r._send_image(1, img, LIST_A, False)
    assert not r._send_image(2, img, LIST_A, False)
    assert not r._send_image(2, img, LIST_A, False)  # re-asked turn keeps its decision
    assert not r._send_image(3, img, LIST_A, False)
    assert r._send_image(4, img, LIST_A, False)  # cap reached


def test_tail_has_note_instead_of_image_on_text_turns():
    r = _runner()
    ledger = SimpleNamespace(elapsed_label=lambda: "T+00:01")
    t1 = r._build_tail(ledger, 1, _jpeg(), LIST_A)
    t2 = r._build_tail(ledger, 2, _jpeg(), LIST_A)
    assert any(b.get("type") == "image_url" for b in t1.content)
    assert all(b.get("type") != "image_url" for b in t2.content)
    assert TEXT_ONLY_NOTE in [b.get("text", "") for b in t2.content]


def test_look_at_screen_tool_only_in_on_demand_mode():
    on = _runner()
    on.ctx = SimpleNamespace(actuator=None)
    off = _runner(send="always")
    off.ctx = SimpleNamespace(actuator=None)
    on._video_tools_enabled = off._video_tools_enabled = lambda: False
    assert LOOK_AT_SCREEN_TOOL in on._get_tools()
    assert LOOK_AT_SCREEN_TOOL not in off._get_tools()
