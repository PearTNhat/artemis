# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compact UI element list (Flash): fewer tokens, same targets, plus unlabeled controls."""

from unittest.mock import MagicMock

from artemis.llm.router import ModelEndpoint, _gemini_media_resolution
from artemis.utils.visualization import format_minimal_list_with_elements

W, H = 1000, 2000


def _node(text="", bounds="[0,0][10,10]", **kw):
    n = {"text": text, "bounds": bounds, "class": kw.pop("cls", "android.widget.TextView"),
         "package": kw.pop("package", "com.example.app"), "checked": "false", "enabled": "true"}
    n.update(kw)
    return n


SCREEN = [
    # containers come first: a flattened tree lists a parent before its children
    _node("", "[0,0][1000,2000]", cls="android.widget.FrameLayout", clickable="true"),  # backdrop
    _node("", "[0,480][1000,600]", cls="android.view.ViewGroup", clickable="true"),  # row holding 59.000
    _node("10:38", "[60,20][150,90]", package="com.android.systemui"),
    _node("Battery 48 percent.", "[800,20][900,90]", package="com.android.systemui"),
    _node("Search", "[100,200][600,260]"),
    _node("59.000", "[40,500][200,540]"),
    _node(hint="Type a message", bounds="[100,1800][700,1880]", cls="android.widget.EditText"),
    _node("Wi-Fi", "[40,700][300,760]", checkable="true", checked="true"),
    _node("Pay", "[40,900][300,960]", enabled="false"),
    # unlabeled controls
    _node("", "[10,190][90,270]", cls="android.widget.ImageButton", clickable="true"),  # back arrow
    _node("", "[230,450][300,520]", cls="android.view.View", clickable="true"),  # "+" over a card
    _node("", "[231,451][299,519]", cls="android.widget.ImageView", clickable="true"),  # same "+" wrapper
    _node("", "[500,20][540,60]", cls="android.widget.ImageView", clickable="true",
          package="com.android.systemui"),  # status-bar icon
]


def test_full_format_is_unchanged_by_default():
    text, elements, _ = format_minimal_list_with_elements([dict(n) for n in SCREEN], W, H)
    assert "[1] Text: '10:38' | Bounds: [60,10][150,45] | checked:false enabled:true" in text
    assert not any(el.get("is_icon") for el in elements)
    assert "(icon)" not in text and "(button)" not in text


def test_compact_drops_status_bar_and_default_states():
    text, elements, _ = format_minimal_list_with_elements([dict(n) for n in SCREEN], W, H, compact=True)
    assert "10:38" not in text and "Battery" not in text
    lines = text.splitlines()
    assert lines[0] == '[1] "Search" 100,100,600,130'
    assert lines[1] == '[2] "59.000" 40,250,200,270'
    assert lines[2] == '[3] hint "Type a message" 100,900,700,940'
    assert lines[3] == '[4] "Wi-Fi" 40,350,300,380 checked:true'
    assert lines[4] == '[5] "Pay" 40,450,300,480 enabled:false'
    assert [el["index"] for el in elements] == list(range(1, len(elements) + 1))


def test_compact_lists_unlabeled_controls_but_not_containers():
    text, elements, labels = format_minimal_list_with_elements([dict(n) for n in SCREEN], W, H, compact=True)
    icons = [el for el in elements if el.get("is_icon")]
    assert [tuple(el["bounds"]) for el in icons] == [(10, 190, 90, 270), (230, 450, 300, 520)]
    assert '[6] (icon) 10,95,90,135' in text  # ImageButton
    assert '[7] (button) 230,225,300,260' in text
    assert icons[0]["text"] == "" and icons[0]["center"] == [50, 230]
    assert labels[-2:] == ["6", "7"]


def test_compact_icons_do_not_create_occlusion_flags():
    _, elements, _ = format_minimal_list_with_elements([dict(n) for n in SCREEN], W, H, compact=True)
    assert not any(el.get("occluded") for el in elements)


def test_compact_icon_under_a_later_dialog_is_marked_occluded():
    nodes = [
        _node("Menu", "[100,100][400,160]"),
        _node("", "[880,100][960,180]", cls="android.widget.ImageButton", clickable="true"),  # icon below
        _node("", "[0,0][1000,2000]", cls="android.widget.FrameLayout", clickable="true"),  # dialog scrim on top
        _node("Allow", "[300,1000][700,1080]", clickable="true"),
    ]
    text, elements, _ = format_minimal_list_with_elements([dict(n) for n in nodes], W, H, compact=True)
    icon = next(el for el in elements if el.get("is_icon"))
    assert icon["occluded"] is True
    assert text.splitlines()[-1].endswith("(may be occluded)")
    assert not any(el.get("occluded") for el in elements if not el.get("is_icon"))


def test_compact_keeps_the_inner_control_not_its_wrapper():
    nodes = [
        _node("", "[100,300][500,400]", cls="android.widget.LinearLayout", clickable="true"),  # wrapper
        _node("", "[420,320][480,380]", cls="android.widget.ImageView", clickable="true"),  # "+" inside, off-center
    ]
    _, elements, _ = format_minimal_list_with_elements([dict(n) for n in nodes], W, H, compact=True)
    assert [tuple(el["bounds"]) for el in elements if el.get("is_icon")] == [(420, 320, 480, 380)]


def test_compact_skips_large_unlabeled_banner_but_keeps_wide_input():
    nodes = [
        _node("", "[0,300][1000,700]", cls="android.widget.ImageView", clickable="true"),  # 20% banner
        _node("", "[50,800][950,900]", cls="android.widget.EditText", clickable="true"),  # 4.5% search field
        _node("", "[0,1000][1000,1150]", cls="android.view.View", clickable="true"),  # 7.5% plain row
    ]
    text, elements, _ = format_minimal_list_with_elements([dict(n) for n in nodes], W, H, compact=True)
    assert [tuple(el["bounds"]) for el in elements if el.get("is_icon")] == [(50, 800, 950, 900)]
    assert "(input field)" in text


def test_compact_shortens_overlap_warning():
    nodes = [_node("Ad   Shop", "[360,780][730,830]"), _node("Ad", "[372,790][406,820]")]
    full, _, _ = format_minimal_list_with_elements([dict(n) for n in nodes], W, H)
    compact, els, _ = format_minimal_list_with_elements([dict(n) for n in nodes], W, H, compact=True)
    assert "WARNING: may overlap with [2]" in full
    assert compact.splitlines()[1].endswith("(overlaps [1], may be occluded)")
    assert all(el.get("occluded") for el in els)


def test_compact_quotes_ocr_and_errors():
    nodes = [
        {"text": "", "bounds": "[0,300][1000,400]", "class": "[OCR]", "ocr_virtual": True,
         "ocr_elements": [{"text": 'Say "hi"', "bounds": "[10,310][200,350]"}]},
        _node("Email", "[10,500][600,560]", error="Required"),
    ]
    text, _, _ = format_minimal_list_with_elements(nodes, W, H, compact=True)
    assert "[1] ocr \"Say 'hi'\" 10,155,200,175" in text
    assert '[2] "Email" 10,250,600,280 error:"Required"' in text


def test_observe_uses_compact_only_when_the_flash_flag_is_set(monkeypatch):
    import asyncio

    from artemis.mcp import observation

    seen = []
    real = observation.format_minimal_list_with_elements

    def spy(*a, **kw):
        seen.append(kw.get("compact"))
        return real(*a, **kw)

    monkeypatch.setattr(observation, "format_minimal_list_with_elements", spy)
    monkeypatch.setattr(observation, "_local_ocr", MagicMock(side_effect=lambda *a: asyncio.sleep(0, [])))

    import base64

    data = MagicMock(base64=base64.b64encode(b"img").decode(), elements=[dict(n) for n in SCREEN],
                     width=W, height=H)
    controller = MagicMock()
    controller.get_screen_data = MagicMock(side_effect=lambda: asyncio.sleep(0, data))
    for flag in (None, True):
        ctx = MagicMock(spec=["device", "data_engine"])
        ctx.data_engine = None
        if flag:
            ctx.flash_compact_list = True
        asyncio.run(observation.observe(ctx, controller, settle_ms=0, persist=False))
    assert seen == [False, True]


def test_media_resolution_maps_to_the_sdk_enum():
    assert _gemini_media_resolution("low").name == "MEDIA_RESOLUTION_LOW"
    assert _gemini_media_resolution("Medium").name == "MEDIA_RESOLUTION_MEDIUM"
    assert _gemini_media_resolution(None) is None
    assert _gemini_media_resolution("ultra") is None
    a = ModelEndpoint(model_name="m", media_resolution="low")
    b = ModelEndpoint(model_name="m")
    assert a.cache_key() != b.cache_key()


def test_node_config_media_resolution_and_max_tokens_reach_the_endpoint():
    from artemis.services.llm import _resolve_endpoint

    cfg = MagicMock(provider="google", model="gemini-3.5-flash-lite", temperature=0.0, timeout=60,
                    thinking_budget=None, thinking_level=None, reasoning_effort=None,
                    include_thoughts=None, enable_grounding=None, max_tokens=900,
                    media_resolution="low")
    ctx = MagicMock()
    ctx.llm_config.get_agent.return_value = cfg
    ep = _resolve_endpoint(ctx, "operator")
    assert ep.max_tokens == 900 and ep.media_resolution == "low"


def test_flash_prompt_describes_the_list_format_in_use():
    from pathlib import Path

    from jinja2 import Template

    import artemis.agents.flash.runner as runner_mod

    tpl = (Path(runner_mod.__file__).parent / "flash_runner.md").read_text(encoding="utf-8")
    compact = Template(tpl).render(goal="g", available_tools=[], compact_list=True)
    full = Template(tpl).render(goal="g", available_tools=[], compact_list=False)
    assert '`[3] "Wi-Fi" 40,120,344,143`' in compact and "(icon)" in compact
    assert "`[3] Text: 'Wi-Fi' | Bounds: ...`" in full and "(icon)" not in full
    assert "1-2 sentences" in compact
