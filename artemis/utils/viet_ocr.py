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

"""VietOCR text recognizer (vgg_seq2seq, ONNX Runtime) for the local OCR.

With ``ocr.recognizer="vietocr"`` the local OCR only *locates* text lines
(RapidOCR detector) and this module reads the lines a consumer actually needs:
the ones the XML-first merge keeps, the ones under a suspected overlay, the
band around a tap the commitment block checks. Reading is ~30 ms per line on a
laptop CPU, so reading ~8 lines instead of the ~26 on a screen is what keeps
the cost down (detector ~0.4 s + reads ~0.25 s per screen).

VietOCR reads Vietnamese tone marks the Latin PaddleOCR model drops, but it is
weaker on digits, prices and logos; the Flash prompt tells the model that
``ocr "..."`` text may be misspelled while its bounds are right.

The model is the Apache-2.0 VietOCR ``vgg_seq2seq`` checkpoint exported to two
ONNX graphs (``encoder.onnx``, ``decoder.onnx``, ``vocab.json``) by
``scripts/export_vietocr_onnx.py``. Missing model -> one warning, and the local
OCR falls back to the PaddleOCR recognizer.
"""

from __future__ import annotations

from io import BytesIO
import json
import math
from pathlib import Path
import threading
import time
from typing import Any

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["available", "default_model_dir", "make_reader", "read_lines"]

_IMAGE_HEIGHT = 32
_MIN_WIDTH = 32
_MAX_WIDTH = 512
_MAX_CHARS = 64
_SOS, _EOS, _FIRST_CHAR = 1, 2, 4

_lock = threading.Lock()
_sessions: dict[str, Any] = {}
_failed: set[str] = set()


def default_model_dir() -> Path:
    from artemis.config.paths import get_app_dir

    return get_app_dir() / "models" / "ocr" / "vietocr-vgg_seq2seq"


def _model_dir(cfg: Any) -> Path:
    custom = getattr(cfg, "vi_model_dir", None)
    return Path(custom) if custom else default_model_dir()


def _load(model_dir: Path, threads: int) -> Any:
    key = f"{model_dir}:{threads}"
    with _lock:
        if key in _sessions:
            return _sessions[key]
        if key in _failed:
            return None
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = threads
            providers = ["CPUExecutionProvider"]
            enc = ort.InferenceSession(str(model_dir / "encoder.onnx"), opts, providers=providers)
            dec = ort.InferenceSession(str(model_dir / "decoder.onnx"), opts, providers=providers)
            chars = json.loads((model_dir / "vocab.json").read_text(encoding="utf-8"))["chars"]
            _sessions[key] = (enc, dec, chars)
            logger.info(f"VietOCR recognizer ready ({model_dir.name}).")
            return _sessions[key]
        except Exception as e:  # pylint: disable=broad-exception-caught
            _failed.add(key)
            logger.warning(
                f"VietOCR model unavailable in {model_dir} ({e.__class__.__name__}: {e}); "
                "local OCR uses the PaddleOCR recognizer. Export it with scripts/export_vietocr_onnx.py."
            )
            return None


def available(cfg: Any) -> bool:
    """True when VietOCR is the configured recognizer and its model loads."""
    if getattr(cfg, "recognizer", "paddle") != "vietocr":
        return False
    return _load(_model_dir(cfg), int(getattr(cfg, "threads", 4))) is not None


def _prepare(img: Any) -> Any:
    import numpy as np
    from PIL import Image

    w, h = img.size
    new_w = math.ceil(int(_IMAGE_HEIGHT * w / max(1, h)) / 10) * 10
    new_w = min(max(new_w, _MIN_WIDTH), _MAX_WIDTH)
    arr = np.asarray(img.convert("RGB").resize((new_w, _IMAGE_HEIGHT), Image.LANCZOS), dtype=np.float32)
    return (arr.transpose(2, 0, 1) / 255.0)[None]


def _decode(model: Any, img: Any) -> str:
    import numpy as np

    enc, dec, chars = model
    hidden, memory = enc.run(None, {"img": _prepare(img)})
    token = np.array([_SOS], dtype=np.int64)
    out: list[str] = []
    for _ in range(_MAX_CHARS):
        logp, hidden = dec.run(None, {"tgt": token, "hidden": hidden, "enc": memory})
        i = int(logp[0].argmax())
        if i == _EOS:
            break
        if i >= _FIRST_CHAR and i - _FIRST_CHAR < len(chars):
            out.append(chars[i - _FIRST_CHAR])
        token = np.array([i], dtype=np.int64)
    return "".join(out).strip()


def read_lines(image_bytes: bytes, boxes: list[tuple[int, int, int, int]], cfg: Any) -> list[str] | None:
    """VietOCR reading of each screen-pixel box; None when the model is unavailable."""
    model = _load(_model_dir(cfg), int(getattr(cfg, "threads", 4)))
    if model is None:
        return None
    if not boxes:
        return []
    from PIL import Image

    started = time.perf_counter()
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    out: list[str] = []
    for left, top, right, bottom in boxes:
        pad = max(2, int((bottom - top) * 0.15))
        crop = img.crop((max(0, left - pad), max(0, top - pad), min(img.width, right + pad), min(img.height, bottom + pad)))
        try:
            out.append(_decode(model, crop) if crop.width > 1 and crop.height > 1 else "")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug(f"VietOCR failed on one line: {e}")
            out.append("")
    logger.info(f"VietOCR: read {len(boxes)} lines in {(time.perf_counter() - started) * 1000:.0f} ms.")
    return out


def make_reader(image_bytes: bytes, cfg: Any) -> Any:
    """A ``read(lines) -> texts`` hook for :func:`merge_ocr_into_list` (``{"text", "box"}`` lines)."""

    def read(lines: list[dict[str, Any]]) -> list[str]:
        texts = read_lines(image_bytes, [tuple(l["box"]) for l in lines], cfg)
        return texts if texts is not None else [l.get("text") or "" for l in lines]

    return read
