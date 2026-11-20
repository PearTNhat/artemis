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

"""The screenshot sent to the model is shrunk; nothing else changes."""

import base64
from io import BytesIO
from unittest.mock import MagicMock

from PIL import Image

from artemis.agents.flash.runner import FlashRunner
from artemis.config.agent import FlashModelImageConfig
from artemis.utils import model_image
from artemis.utils.model_image import shrink_for_model


def _jpeg(width, height) -> bytes:
    img = Image.new("RGB", (width, height), "white")
    for x in range(0, width, 7):  # some detail so JPEG sizes differ
        img.putpixel((x, x % height), (0, 0, 0))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    return Image.open(BytesIO(data)).size


def setup_function():
    model_image._last = None


def test_shrink_keeps_aspect_ratio_and_limits_longest_side():
    out = shrink_for_model(_jpeg(1080, 2400), 1280, 75)
    assert _size(out) == (576, 1280)
    assert Image.open(BytesIO(out)).format == "JPEG"


def test_shrink_landscape_limits_width():
    assert _size(shrink_for_model(_jpeg(2400, 1080), 1280, 75)) == (1280, 576)


def test_shrink_disabled_or_already_small_returns_same_bytes():
    big = _jpeg(1080, 2400)
    assert shrink_for_model(big, 0, 75) is big
    small = _jpeg(500, 1000)
    assert shrink_for_model(small, 1280, 75) is small


def test_shrink_bad_bytes_returns_original():
    assert shrink_for_model(b"not an image", 1280, 75) == b"not an image"


def test_shrink_reuses_last_result_for_the_same_frame(monkeypatch):
    frame = _jpeg(1080, 2400)
    first = shrink_for_model(frame, 1280, 75)
    opened = MagicMock(side_effect=AssertionError("should be cached"))
    monkeypatch.setattr(Image, "open", opened)
    assert shrink_for_model(frame, 1280, 75) is first


def _tail_image(runner: FlashRunner, img: bytes) -> bytes:
    ledger = MagicMock()
    ledger.elapsed_label.return_value = "T+00:01"
    tail = runner._build_tail(ledger, 1, img, "[1] Text: 'OK' | Bounds: [0,0][10,10]")
    url = next(b["image_url"]["url"] for b in tail.content if b.get("type") == "image_url")
    return base64.b64decode(url.split(",", 1)[1])


def test_build_tail_sends_the_shrunk_image():
    runner = FlashRunner.__new__(FlashRunner)
    runner.model_image_cfg = FlashModelImageConfig(max_side=1280, jpeg_quality=75)
    assert _size(_tail_image(runner, _jpeg(1080, 2400))) == (576, 1280)


def test_build_tail_sends_the_original_when_disabled():
    runner = FlashRunner.__new__(FlashRunner)
    runner.model_image_cfg = FlashModelImageConfig()
    frame = _jpeg(1080, 2400)
    assert _tail_image(runner, frame) == frame
