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

"""Downscale/re-encode a screenshot for the model only.

The model addresses the screen in normalized 0-1000 coordinates, so a smaller
image changes no coordinate. Exact text comes from the accessibility tree and
local OCR (both read from the full-resolution capture), so the image only has to
show layout, icons and colours. Everything else (OCR, the safety checks, trace
storage, screen-change detection) keeps the original bytes.

Never blurs: downscaling uses Lanczos resampling, which keeps text edges sharp.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
import threading

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["shrink_for_model"]

_lock = threading.Lock()
_last: tuple[str, int, int, bytes] | None = None


def shrink_for_model(image_bytes: bytes, max_side: int, jpeg_quality: int = 80) -> bytes:
    """JPEG bytes whose longer side is at most ``max_side`` (0 = unchanged).

    An image already within the limit is returned as-is (no re-encoding loss).
    Any decoding error returns the original bytes: shrinking is an optimisation,
    never a reason to lose the observation.
    """
    global _last
    if not image_bytes or not max_side or max_side <= 0:
        return image_bytes
    digest = hashlib.sha256(image_bytes).hexdigest()
    with _lock:
        if _last and _last[0] == digest and _last[1] == max_side and _last[2] == jpeg_quality:
            return _last[3]
    try:
        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        width, height = img.size
        longest = max(width, height)
        if longest <= max_side:
            return image_bytes
        scale = max_side / float(longest)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        small = img.convert("RGB").resize(size, Image.Resampling.LANCZOS)
        buf = BytesIO()
        small.save(buf, format="JPEG", quality=int(jpeg_quality), optimize=True)
        out = buf.getvalue()
    except Exception as e:
        logger.warning(f"Could not shrink the screenshot for the model; sending it unchanged: {e}")
        return image_bytes
    with _lock:
        _last = (digest, max_side, jpeg_quality, out)
    return out
