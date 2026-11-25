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

"""Exports VietOCR vgg_seq2seq (Apache-2.0) to the ONNX files artemis/utils/viet_ocr.py loads.

Torch is only needed for this one-off export, never at ARTEMIS runtime, so run
it in a throwaway environment:

    uv run --no-project --python 3.12 --with vietocr --with torch --with torchvision \\
        --with "setuptools<70" --with onnx \\
        python scripts/export_vietocr_onnx.py --out "<ARTEMIS app dir>/models/ocr/vietocr-vgg_seq2seq"

Writes encoder.onnx (CNN + GRU encoder), decoder.onnx (one greedy decoding
step, log-softmax output) and vocab.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import types

WEIGHTS_URL = "https://vocr.vn/data/vietocr/vgg_seq2seq.pth"
WEIGHTS_SHA256 = "0921503a41375a0584268e23ef3d414ea478a8fe8777865c7745d38f2d0bc5db"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="Output folder")
    parser.add_argument("--weights", help="Local vgg_seq2seq.pth (default: download and verify)")
    args = parser.parse_args()

    import torch
    from vietocr.tool.config import Cfg
    from vietocr.tool.translate import build_model
    from vietocr.tool.utils import download_weights

    weights = args.weights or download_weights(WEIGHTS_URL)
    digest = hashlib.sha256(Path(weights).read_bytes()).hexdigest()
    if digest != WEIGHTS_SHA256:
        raise SystemExit(f"{weights}: SHA-256 {digest} does not match the pinned checkpoint")

    cfg = Cfg.load_config_from_name("vgg_seq2seq")
    cfg["device"] = "cpu"
    cfg["cnn"]["pretrained"] = False  # the checkpoint holds every weight
    model, _vocab = build_model(cfg)
    model.load_state_dict(torch.load(weights, map_location="cpu"))
    model.eval()

    def vgg_forward(self, x):  # same maths, positive permute axes (older ONNX Runtime rejects -1)
        conv = self.last_conv_1x1(self.dropout(self.features(x)))
        return conv.transpose(2, 3).flatten(2).permute(2, 0, 1)

    model.cnn.model.forward = types.MethodType(vgg_forward, model.cnn.model)

    class Encoder(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, img):
            hidden, memory = self.m.transformer.forward_encoder(self.m.cnn(img))
            return hidden, memory

    class DecoderStep(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.d = m.transformer.decoder

        def forward(self, tgt, hidden, memory):
            logits, hidden, _ = self.d(tgt, hidden, memory)
            return torch.log_softmax(logits, -1), hidden

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    img = torch.rand(2, 3, 32, 160)
    with torch.no_grad():
        hidden, memory = Encoder(model)(img)
        torch.onnx.export(
            Encoder(model), (img,), str(out / "encoder.onnx"),
            input_names=["img"], output_names=["hidden", "enc"],
            dynamic_axes={"img": {0: "b", 3: "w"}, "hidden": {0: "b"}, "enc": {0: "t", 1: "b"}},
            opset_version=17, dynamo=False,
        )
        torch.onnx.export(
            DecoderStep(model), (torch.ones(2, dtype=torch.long), hidden, memory), str(out / "decoder.onnx"),
            input_names=["tgt", "hidden", "enc"], output_names=["logp", "hidden_out"],
            dynamic_axes={"tgt": {0: "b"}, "hidden": {0: "b"}, "enc": {0: "t", 1: "b"},
                          "logp": {0: "b"}, "hidden_out": {0: "b"}},
            opset_version=17, dynamo=False,
        )
    (out / "vocab.json").write_text(json.dumps({"chars": cfg["vocab"]}, ensure_ascii=False), encoding="utf-8")
    print(f"Exported to {out}")


if __name__ == "__main__":
    main()
