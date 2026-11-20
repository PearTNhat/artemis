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

"""Local on-host OCR (RapidOCR detector + PaddleOCR Latin v5 recognizer, ONNX Runtime).

Runs on the screenshot the device already sends for the model, so it adds no
device transfer, only host CPU time. Results use the same shape as the cloud
OCR (``{"text", "position": [{"x", "y"}, ...]}``) so ``fuse_ocr_with_xml`` and
the indexed element list consume them unchanged.

Measured on 30 trace screenshots (KE_HOACH_TOI_UU_ARTEMIS.md section 1.6): ~0.5 s
per screen on a laptop CPU with ``use_cls=False`` and 4 intra-op threads.
Vietnamese tone marks outside the Latin dictionary (hook above, dot below) are
not recognised by this model; swap ``rec_model_path`` for a Vietnamese model.

Everything degrades to "no OCR" (an empty list, one warning) when the optional
runtime or the model is unavailable; OCR never fails an observation.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import re
import threading
import time
from typing import Any
import urllib.request

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "count_app_labels",
    "local_ocr_settings",
    "recognize_screenshot",
    "should_run_ocr",
]

# Official PaddleOCR Latin v5 recognizer (Apache-2.0), pinned to one revision and
# verified by content hash so a moved or tampered file is never loaded.
_MODEL_REPO = "PaddlePaddle/latin_PP-OCRv5_mobile_rec_onnx"
_MODEL_REVISION = "89d3a50e2c27e2e7cceeab0e944c25c807d5db4f"
_MODEL_FILES = {
    "inference.onnx": "7888113072263cb471b93f66dd5e2ad70548dc526fa1ace760d0d973dd121498",
    "inference.yml": "0bbe984570f597af3638e50bdf2e8276f3ab26a61966096538b3b0d1849f5c84",
}
_SYSTEM_UI_PACKAGE = "com.android.systemui"

_engine: Any = None
_engine_key: tuple | None = None
_engine_failed: str | None = None
_engine_lock = threading.Lock()
_cache_lock = threading.Lock()
_cache: dict[str, list[dict[str, Any]]] = {}
_CACHE_SIZE = 4


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def local_ocr_settings(ctx: Any = None) -> Any:
    """The Flash OCR config, read once per context (default: OCR off)."""
    cached = getattr(ctx, "_flash_ocr_cfg", None) if ctx is not None else None
    if cached is not None:
        return cached
    from artemis.config.agent import FlashOcrConfig

    cfg = FlashOcrConfig()
    try:
        from artemis.config import load_agent_config

        cfg = load_agent_config().flash.ocr
    except Exception as e:
        logger.debug(f"Flash OCR config unavailable, OCR stays off: {e}")
    if ctx is not None:
        try:
            ctx._flash_ocr_cfg = cfg
        except (AttributeError, TypeError, ValueError):
            pass
    return cfg


# --------------------------------------------------------------------------- #
# Trigger (app-agnostic: only the accessibility tree's own richness counts)
# --------------------------------------------------------------------------- #


def _iter_nodes(nodes: Any):
    stack = list(nodes) if isinstance(nodes, list) else [nodes]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        yield node
        children = node.get("children") or node.get("nodes")
        if isinstance(children, (list, dict)):
            stack.extend(children if isinstance(children, list) else [children])


def count_app_labels(nodes: Any) -> int:
    """Visible app (non-system-UI) nodes that carry a text, description or hint."""
    count = 0
    for node in _iter_nodes(nodes or []):
        if (node.get("package") or node.get("packageName")) == _SYSTEM_UI_PACKAGE:
            continue
        if str(node.get("visible-to-user", "true")).lower() == "false":
            continue
        label = node.get("text") or node.get("content-desc") or node.get("hint")
        if str(label or "").strip() and node.get("bounds"):
            count += 1
    return count


def should_run_ocr(cfg: Any, nodes: Any) -> bool:
    mode = getattr(cfg, "mode", "off")
    if mode == "always":
        return True
    if mode == "auto":
        return count_app_labels(nodes) < int(getattr(cfg, "min_xml_labels", 5))
    return False


# --------------------------------------------------------------------------- #
# Model + engine
# --------------------------------------------------------------------------- #


def _model_dir() -> Path:
    from artemis.config.paths import get_app_dir

    return get_app_dir() / "models" / "ocr" / f"latin_PP-OCRv5_mobile_rec-{_MODEL_REVISION[:8]}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_default_model() -> tuple[str, str]:
    """Downloads (once) and verifies the default recognizer; returns (model, keys)."""
    target = _model_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name, digest in _MODEL_FILES.items():
        path = target / name
        if path.exists() and _sha256(path) == digest:
            continue
        url = f"https://huggingface.co/{_MODEL_REPO}/resolve/{_MODEL_REVISION}/{name}"
        logger.info(f"Downloading local OCR model file {name} from {_MODEL_REPO}...")
        tmp = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        if _sha256(tmp) != digest:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"OCR model file {name} failed its SHA-256 check")
        tmp.replace(path)

    keys = target / "dict.txt"
    if not keys.exists():
        import yaml

        spec = yaml.safe_load((target / "inference.yml").read_text(encoding="utf-8"))
        chars = spec["PostProcess"]["character_dict"]
        keys.write_text("\n".join(str(c) for c in chars) + "\n", encoding="utf-8")
    return str(target / "inference.onnx"), str(keys)


def _get_engine(cfg: Any) -> Any:
    """The shared RapidOCR engine, built on first use; None when unavailable."""
    global _engine, _engine_key, _engine_failed
    key = (getattr(cfg, "rec_model_path", None), getattr(cfg, "rec_keys_path", None),
           int(getattr(cfg, "threads", 4)))
    with _engine_lock:
        if _engine is not None and _engine_key == key:
            return _engine
        if _engine_failed is not None and _engine_key == key:
            return None
        _engine_key = key
        try:
            from rapidocr_onnxruntime import RapidOCR

            model, keys = key[0], key[1]
            if not model:
                model, keys = _ensure_default_model()
            kwargs: dict[str, Any] = {
                "use_cls": False,  # screen text is upright; the classifier only costs time
                "intra_op_num_threads": key[2],
                "rec_batch_num": 3,
                "rec_model_path": model,
            }
            if keys:
                kwargs["rec_keys_path"] = keys
            _engine = RapidOCR(**kwargs)
            _engine_failed = None
            logger.info(f"Local OCR engine ready (rec model: {Path(model).name}).")
        except Exception as e:
            _engine = None
            _engine_failed = f"{e.__class__.__name__}: {e}"
            logger.warning(f"Local OCR unavailable, observations continue without it: {e}")
        return _engine


# --------------------------------------------------------------------------- #
# Recognition
# --------------------------------------------------------------------------- #


def _status_bar_height(nodes: Any, height: int) -> int:
    limit = int(height * 0.12)
    best = 0
    for node in _iter_nodes(nodes or []):
        if (node.get("package") or node.get("packageName")) != _SYSTEM_UI_PACKAGE:
            continue
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", str(node.get("bounds") or ""))
        if m:
            top, bottom = int(m.group(2)), int(m.group(4))
            # Status-bar items (clock, icons) sit near the top, not always at y=0.
            if top < height * 0.06 and bottom <= limit:
                best = max(best, bottom)
    return best


def recognize_screenshot(image_bytes: bytes, nodes: Any, cfg: Any) -> list[dict[str, Any]]:
    """OCR of one screenshot (status bar excluded), in screen pixel coordinates.

    Blocking (CPU-bound): call it through ``asyncio.to_thread``. The last few
    results are cached by image hash, so the transient guard looks that follow
    an observation of the same frame cost nothing.
    """
    if not image_bytes:
        return []
    digest = hashlib.sha256(image_bytes).hexdigest()
    with _cache_lock:
        if digest in _cache:
            return _cache[digest]

    engine = _get_engine(cfg)
    if engine is None:
        return []

    import numpy as np
    from PIL import Image

    started = time.perf_counter()
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        offset = _status_bar_height(nodes, img.height)
        if 0 < offset < img.height:
            img = img.crop((0, offset, img.width, img.height))
        result, _ = engine(np.asarray(img)[:, :, ::-1])  # RapidOCR expects BGR
    except Exception as e:
        logger.warning(f"Local OCR failed on this frame: {e}")
        return []

    out: list[dict[str, Any]] = []
    for box, text, score in result or []:
        text = str(text or "").strip()
        if not text:
            continue
        out.append(
            {
                "text": text,
                "score": round(float(score), 3),
                "position": [{"x": int(p[0]), "y": int(p[1]) + offset} for p in box],
            }
        )
    logger.info(f"Local OCR: {len(out)} text lines in {(time.perf_counter() - started) * 1000:.0f} ms.")
    with _cache_lock:
        _cache[digest] = out
        while len(_cache) > _CACHE_SIZE:
            _cache.pop(next(iter(_cache)))
    return out
