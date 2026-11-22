"""Build a browsable gallery of the benchmark: every case, every image the model receives
(real screenshot, wireframe, set-of-marks, OCR+icon marks), the text lists, and the per-mode
results.

Usage (repo root):
  uv run python benchmarks/payload_grounding/gallery.py \
      --results benchmarks/payload_grounding/results/qwen3-vl-4b-colab-t4.jsonl
Then open benchmarks/payload_grounding/gallery/index.html in a browser.
The output folder is generated (git-ignored); rerun after editing cases or adding results.
"""
import argparse, base64, io, json, os, shutil, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bench as B  # noqa: E402

MODE_INFO = [  # (mode, short chip, label, group)
    ("compact_somU", "Gọn+SốKN", "List gọn + ảnh thật chỉ đánh số phần tử không nhãn", "Có cây UI, có ảnh"),
    ("shot768", "Ảnh", "Ảnh thật + list gọn (Flash hiện tại)", "Có cây UI, có ảnh"),
    ("center2_shot", "NT2+Ảnh", "Nhãn + tâm (giữ loại/trạng thái) + ảnh thật", "Có cây UI, có ảnh"),
    ("center2_somU", "NT2+SốKN", "Nhãn + tâm (giữ loại) + ảnh chỉ đánh số phần tử không nhãn", "Có cây UI, có ảnh"),
    ("center_som", "NT+Số", "Nhãn + tâm (bản cũ, mất loại) + ảnh vẽ khung & số mọi phần tử", "Có cây UI, có ảnh"),
    ("center_shot", "NT+Ảnh", "Nhãn + tâm (bản cũ, mất loại) + ảnh thật", "Có cây UI, có ảnh"),
    ("wire_near", "WF chữ+gần", "Wireframe trắng đen có chữ + list gọn có gợi ý “gần chữ nào”", "Có cây UI, ảnh vẽ lại (không pixel thật)"),
    ("wire_text", "WF chữ", "Wireframe trắng đen có chữ + list gọn", "Có cây UI, ảnh vẽ lại (không pixel thật)"),
    ("somU_near", "Gọn+SốKN+gần", "List gọn có gợi ý “gần chữ nào” + ảnh đánh số phần tử không nhãn", "Có cây UI, có ảnh"),
    ("2stage", "2 tầng", "2 tầng: mô tả ảnh → quyết định bằng chữ", "Có cây UI, có ảnh"),
    ("list_compact", "Gọn", "List gọn, không ảnh", "Có cây UI, không ảnh"),
    ("center2_near", "NT2+gần", "Nhãn + tâm (giữ loại) + gợi ý “gần chữ nào”, không ảnh", "Có cây UI, không ảnh"),
    ("center2", "NT2", "Nhãn + tâm (giữ loại/trạng thái), không ảnh", "Có cây UI, không ảnh"),
    ("list_center", "NT", "Nhãn + tâm (bản cũ, mất loại), không ảnh", "Có cây UI, không ảnh"),
    ("list_label", "Nhãn", "Chỉ nhãn, không ảnh", "Có cây UI, không ảnh"),
    ("xml_ocr", "X+OCR", "Cây UI + OCR gộp (list gọn)", "Có cây UI, không ảnh"),
    ("center_ocr", "X+OCR·NT", "Cây UI + OCR gộp (nhãn + tâm)", "Có cây UI, không ảnh"),
    ("ocr_only", "OCR", "Chỉ OCR", "Không cây UI (kiểu desktop)"),
    ("ocr_clean", "OCR lọc", "OCR đã lọc nhiễu (bỏ status bar, độ tin cậy thấp, 1 ký tự)", "Không cây UI (kiểu desktop)"),
    ("ocr_shot", "OCR+Ảnh", "OCR + ảnh thật", "Không cây UI (kiểu desktop)"),
    ("ocr_icons", "OCR+Icon", "OCR + dò icon OpenCV", "Không cây UI (kiểu desktop)"),
    ("ocr_icons_som", "OCR+Icon+Số", "OCR + dò icon + ảnh vẽ khung & số", "Không cây UI (kiểu desktop)"),
]
TAG_INFO = {
    "orig": "Bài gốc (đích có chữ)", "hard": "Bài khó viết tay", "icon": "Icon", "unlabeled": "Không nhãn",
    "spatial": "Vị trí", "relation": "Quan hệ", "toggle": "Công tắc", "dup_label": "Chữ trùng", "labeled": "Có nhãn",
}


def save_b64(b64, path):
    open(path, "wb").write(base64.b64decode(b64))


def thumb(path_in, path_out, width=360):
    from PIL import Image
    im = Image.open(path_in).convert("RGB")
    im.thumbnail((width, width * 3), Image.LANCZOS)
    im.save(path_out, "JPEG", quality=72)


def answer_boxes(case):
    if case.get("accept_box"):
        return case["accept_box"]
    els = B.listing("list_compact", case)[1]
    t = [e for e in els if B.label_match(e.get("text"), case["accept"])]
    if case.get("near_y") is not None and t:
        best = min(abs(e["center"][1] - case["near_y"]) for e in t)
        t = [e for e in t if abs(e["center"][1] - case["near_y"]) == best]
    return [e["bounds"] for e in t]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="+", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "gallery"))
    ap.add_argument("--all-modes", action="store_true", help="also show retired modes")
    args = ap.parse_args()
    out = args.out
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(os.path.join(out, "img"))

    cases = B.load_cases()
    by_id = {c["id"]: c for c in cases}
    rows = [json.loads(l) for p in args.results for l in open(p)]
    model = rows[0]["model"] if rows else "?"
    res = {}
    for r in rows:
        c = by_id.get(r["step"])
        if not c:
            continue
        ok, idx, lab = B.evaluate(r["reply"], r["mode"], c)
        pels = B.listing(r["mode"], c)[1]
        pe = next((e for e in pels if e["index"] == idx), None)
        pb = [pe["bounds"][0] / c["w"], pe["bounds"][1] / c["h"], pe["bounds"][2] / c["w"], pe["bounds"][3] / c["h"]] if pe else None
        res[(r["step"], r["mode"])] = dict(ok=ok, idx=idx, picked=lab, pb=pb, wall=r["wall"],
                                           tok=r["in_tok"] + r.get("s1_in", 0), ocr=r.get("ocr_ms", 0) / 1000)
    keep = None if args.all_modes else set(B.CORE_MODES) | {"wire_text", "center2", "somU_near", "ocr_clean"}
    modes = [m for m in MODE_INFO if (keep is None or m[0] in keep) and any((c["id"], m[0]) in res for c in cases)]

    # images per screen
    screens = {}
    for c in cases:
        if c["screen"] in screens:
            continue
        s = c["screen"]
        thumb(c["img"], os.path.join(out, "img", f"{s}_real.jpg"))
        _, els_c = B.listing("list_compact", c)
        save_b64(B.wire_text_image(c, 768), os.path.join(out, "img", f"{s}_wiretext.jpg"))
        save_b64(B.som_image(c["img"], [e for e in els_c if not e.get("text")], 540),
                 os.path.join(out, "img", f"{s}_somU.jpg"))
        _, els_o = B.listing("ocr_icons_som", c)
        save_b64(B.som_image(c["img"], els_o, 540), os.path.join(out, "img", f"{s}_ocr_icons.jpg"))
        screens[s] = dict(id=s, w=c["w"], h=c["h"],
                          list_compact=B.listing("list_compact", c)[0],
                          list_center=B.listing("center2_near", c)[0],
                          ocr_icons=B.listing("ocr_icons", c)[0],
                          n_elements=len(els_c), n_unlabeled=sum(1 for e in els_c if not e.get("text")))

    # summary per mode and per tag
    summary = []
    tags = ["orig", "hard", "icon", "unlabeled", "spatial", "relation"]
    for m, short, label, group in modes:
        X = [res[(c["id"], m)] for c in cases if (c["id"], m) in res]
        row = dict(mode=m, short=short, label=label, group=group, n=len(X), ok=sum(x["ok"] for x in X),
                   wall=round(statistics.median(x["wall"] for x in X), 2),
                   ocr=round(statistics.median(x["ocr"] for x in X), 2),
                   tok=round(statistics.median(x["tok"] for x in X)), by_tag={})
        for t in tags:
            T = [res[(c["id"], m)] for c in cases if t in c["tags"] and (c["id"], m) in res]
            row["by_tag"][t] = [sum(x["ok"] for x in T), len(T)]
        summary.append(row)

    data = dict(
        model=model, n_cases=len(cases), tags={t: TAG_INFO.get(t, t) for t in tags},
        modes=[dict(mode=m, short=s, label=l, group=g) for m, s, l, g in modes],
        summary=summary, screens=screens,
        cases=[dict(id=c["id"], screen=c["screen"], tags=c["tags"], instruction=c["instr"],
                    answer=(", ".join(f'"{a}"' for a in c["accept"]) if c["accept"] and not c.get("accept_box")
                            else "vùng khung đỏ trên ảnh"),
                    boxes=[[b[0] / c["w"], b[1] / c["h"], b[2] / c["w"], b[3] / c["h"]] for b in answer_boxes(c)],
                    results={m: res.get((c["id"], m)) for m, *_ in modes}) for c in cases],
    )
    json.dump(data, open(os.path.join(out, "data.json"), "w", encoding="utf-8"), ensure_ascii=False)
    shutil.copy(os.path.join(HERE, "gallery_template.html"), os.path.join(out, "index.html"))
    print(f"wrote {out}/index.html ({len(cases)} cases, {len(screens)} screens, {len(modes)} modes)")


if __name__ == "__main__":
    main()
