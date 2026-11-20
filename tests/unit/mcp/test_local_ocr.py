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

"""Local ONNX OCR in Flash observations: trigger, fusion, list rendering, observe()."""

import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from PIL import Image
import pytest

from artemis.config.agent import FlashOcrConfig
from artemis.mcp import observation
from artemis.utils import local_ocr
from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
from artemis.utils.visualization import format_minimal_list_with_elements


def _node(text="", bounds="[0,0][10,10]", desc="", package="com.example.app", **kw):
    return {"text": text, "content-desc": desc, "bounds": bounds, "package": package,
            "class": kw.pop("cls", "android.widget.TextView"), **kw}


def _ocr(text, left, top, right, bottom):
    return {"text": text, "position": [{"x": left, "y": top}, {"x": right, "y": top},
                                       {"x": right, "y": bottom}, {"x": left, "y": bottom}]}


def _jpeg(width=200, height=400, color=(255, 255, 255)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _clear_ocr_cache():
    local_ocr._cache.clear()
    yield
    local_ocr._cache.clear()


# --------------------------------------------------------------------------- #
# Trigger
# --------------------------------------------------------------------------- #


def test_count_app_labels_ignores_system_ui_invisible_and_unlabeled_nodes():
    nodes = [
        _node("11:32", package="com.android.systemui"),
        _node("Hidden", **{"visible-to-user": "false"}),
        _node(""),
        _node(desc="Back"),
        _node("Title"),
        {"text": "", "hint": "Search", "bounds": "[0,0][5,5]", "package": "com.example.app"},
    ]
    assert local_ocr.count_app_labels(nodes) == 3


def test_count_app_labels_reads_nested_children():
    tree = {"package": "com.example.app", "bounds": "[0,0][9,9]",
            "children": [_node("A"), {"children": [_node("B")]}]}
    assert local_ocr.count_app_labels([tree]) == 2


@pytest.mark.parametrize(
    "mode,labels,expected",
    [("off", 0, False), ("always", 50, True), ("auto", 4, True), ("auto", 5, False)],
)
def test_should_run_ocr_modes(mode, labels, expected):
    nodes = [_node(f"t{i}") for i in range(labels)]
    cfg = FlashOcrConfig(mode=mode, min_xml_labels=5)
    assert local_ocr.should_run_ocr(cfg, nodes) is expected


def test_default_config_keeps_ocr_off():
    assert FlashOcrConfig().mode == "off"


# --------------------------------------------------------------------------- #
# Fusion (xml_first is the Flash path; the default path is unchanged)
# --------------------------------------------------------------------------- #


def test_xml_first_never_overwrites_existing_xml_text():
    xml = [_node("225.000", "[100,100][300,150]")]
    fused = fuse_ocr_with_xml([dict(n) for n in xml], [_ocr("225.000250.000", 100, 100, 300, 150)],
                              xml_first=True)
    assert "ocr_elements" not in fused[0]
    assert len(fused) == 1


def test_xml_first_keeps_ocr_for_description_only_nodes():
    # Under 10% of the screen, so fusion matches into it (bigger containers are skipped).
    xml = [_node("", "[0,500][1000,700]", desc="FEED_ADS_EXTERNAL", cls="android.widget.ImageView")]
    fused = fuse_ocr_with_xml([dict(n) for n in xml], [_ocr("Shop Now", 100, 600, 400, 660)],
                              xml_first=True)
    assert fused[0]["ocr_elements"][0]["text"] == "Shop Now"


def test_xml_first_keeps_unmatched_ocr_as_virtual_nodes_and_drops_noise():
    xml = [_node("Header", "[0,0][1080,100]")]
    ocr = [_ocr("Play now", 300, 1200, 700, 1260), _ocr("x", 10, 1500, 30, 1520)]
    fused = fuse_ocr_with_xml([dict(n) for n in xml], ocr, xml_first=True)
    virtual = [n for n in fused if n.get("ocr_virtual")]
    assert [v["ocr_elements"][0]["text"] for v in virtual] == ["Play now"]
    assert virtual[0]["bounds"] == "[300,1200][700,1260]"


def test_default_fusion_still_drops_unmatched_ocr():
    xml = [_node("Header", "[0,0][1080,100]")]
    fused = fuse_ocr_with_xml([dict(n) for n in xml], [_ocr("Play now", 300, 1200, 700, 1260)])
    assert not any(n.get("ocr_virtual") for n in fused)
    assert len(fused) == 1


def test_virtual_ocr_is_listed_as_ocr_text_without_occlusion_warnings():
    # A large non-text container: fusion skips it, so the OCR line becomes a
    # virtual node lying inside the listed card.
    xml = [_node("", "[0,1000][1080,1600]", desc="Card title", clickable="true",
                 cls="android.view.View")]
    fused = fuse_ocr_with_xml([dict(n) for n in xml], [_ocr("Big banner text", 600, 1500, 1000, 1580)],
                              xml_first=True)
    text, elements, _ = format_minimal_list_with_elements(fused, 1080, 2400)
    ocr_el = next(e for e in elements if e["is_ocr"])
    assert ocr_el["ocr_virtual"] is True
    assert ocr_el["text"] == "Big banner text"
    assert "OCR Text: 'Big banner text'" in text
    assert "possible occlusion" not in text
    assert not any(e.get("occluded") for e in elements)


# --------------------------------------------------------------------------- #
# recognize_screenshot
# --------------------------------------------------------------------------- #


class _FakeEngine:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, img):
        self.calls.append(img.shape)
        return self.result, None


def test_recognize_screenshot_maps_past_status_bar_and_caches(monkeypatch):
    engine = _FakeEngine([[[[10, 5], [90, 5], [90, 25], [10, 25]], "Hello", 0.98]])
    monkeypatch.setattr(local_ocr, "_get_engine", lambda cfg: engine)
    nodes = [_node("9:41", "[20,8][80,40]", package="com.android.systemui")]
    img = _jpeg(200, 400)

    out = local_ocr.recognize_screenshot(img, nodes, FlashOcrConfig(mode="always"))
    assert out == [{"text": "Hello", "score": 0.98,
                    "position": [{"x": 10, "y": 45}, {"x": 90, "y": 45},
                                 {"x": 90, "y": 65}, {"x": 10, "y": 65}]}]
    assert engine.calls == [(360, 200, 3)]  # cropped by the 40 px status bar

    again = local_ocr.recognize_screenshot(img, nodes, FlashOcrConfig(mode="always"))
    assert again == out
    assert len(engine.calls) == 1  # same frame: served from the cache


def test_recognize_screenshot_returns_empty_when_engine_unavailable(monkeypatch):
    monkeypatch.setattr(local_ocr, "_get_engine", lambda cfg: None)
    assert local_ocr.recognize_screenshot(_jpeg(), [], FlashOcrConfig(mode="always")) == []


def test_engine_failure_is_remembered_not_retried(monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("offline")

    monkeypatch.setattr(local_ocr, "_engine", None)
    monkeypatch.setattr(local_ocr, "_engine_key", None)
    monkeypatch.setattr(local_ocr, "_engine_failed", None)
    monkeypatch.setattr(local_ocr, "_ensure_default_model", boom)
    cfg = FlashOcrConfig(mode="always")
    assert local_ocr._get_engine(cfg) is None
    assert local_ocr._get_engine(cfg) is None
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# observe()
# --------------------------------------------------------------------------- #


def _controller(elements, width=1080, height=2400):
    data = SimpleNamespace(base64=base64.b64encode(_jpeg()).decode(), elements=elements,
                           width=width, height=height)
    ctrl = MagicMock()
    ctrl.get_screen_data = AsyncMock(return_value=data)
    return ctrl


def _ctx(mode):
    ctx = SimpleNamespace(data_engine=None, device=None)
    ctx._flash_ocr_cfg = FlashOcrConfig(mode=mode)
    return ctx


@pytest.mark.asyncio
async def test_observe_skips_ocr_when_off(monkeypatch):
    called = MagicMock(return_value=[])
    monkeypatch.setattr(local_ocr, "recognize_screenshot", called)
    obs, _ = await observation.observe(_ctx("off"), _controller([]), settle_ms=0, persist=False)
    assert obs.ok
    called.assert_not_called()


@pytest.mark.asyncio
async def test_observe_auto_adds_ocr_elements_on_poor_hierarchy(monkeypatch):
    monkeypatch.setattr(local_ocr, "recognize_screenshot",
                        lambda img, nodes, cfg: [_ocr("Start game", 300, 1200, 780, 1290)])
    raw = [_node("", "[0,0][1080,2400]", cls="android.view.View")]
    obs, _ = await observation.observe(_ctx("auto"), _controller(raw), settle_ms=0, persist=False)
    assert obs.hierarchy_ok
    assert "OCR Text: 'Start game'" in obs.elements_text
    hit = next(e for e in obs.elements if e["text"] == "Start game")
    assert hit["center"] == [540, 1245]
    assert raw == [_node("", "[0,0][1080,2400]", cls="android.view.View")]  # raw tree untouched


@pytest.mark.asyncio
async def test_observe_auto_skips_ocr_on_rich_hierarchy(monkeypatch):
    called = MagicMock(return_value=[])
    monkeypatch.setattr(local_ocr, "recognize_screenshot", called)
    rich = [_node(f"Item {i}", f"[0,{i * 100}][1080,{i * 100 + 90}]") for i in range(1, 8)]
    obs, _ = await observation.observe(_ctx("auto"), _controller(rich), settle_ms=0, persist=False)
    assert obs.ok
    called.assert_not_called()


@pytest.mark.asyncio
async def test_observe_survives_ocr_errors(monkeypatch):
    def boom(*a):
        raise RuntimeError("onnx crashed")

    monkeypatch.setattr(local_ocr, "recognize_screenshot", boom)
    obs, _ = await observation.observe(_ctx("always"), _controller([_node("A")]), settle_ms=0,
                                       persist=False)
    assert obs.ok and obs.hierarchy_ok
    assert "Text: 'A'" in obs.elements_text
