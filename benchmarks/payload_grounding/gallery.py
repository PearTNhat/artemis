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
    ("gapfill_somU2", "XML+lấp trống+Số2", "Cây UI + chỉ thêm OCR/icon ở chỗ cây thiếu + ảnh đánh số v2 (1 icon 1 số, số nhỏ đặt ngoài khung)", "Có cây UI, có ảnh"),
    ("compact_somU2", "Gọn+Số2", "List gọn + ảnh đánh số v2 (1 icon 1 số, số nhỏ đặt ngoài khung)", "Có cây UI, có ảnh"),
    ("gapfill_somU", "XML+lấp trống", "Cây UI giữ nguyên + chỉ thêm OCR/icon ở chỗ cây thiếu (ngoài mọi phần tử, ngoài vùng ảnh) + ảnh đánh số phần tử không nhãn", "Có cây UI, có ảnh"),
    ("fused_icons_somU", "XML+OCR+Icon", "Cây UI + OCR gộp + icon ngoài cây (dò được) + ảnh đánh số phần tử không nhãn", "Có cây UI, có ảnh"),
    ("xml_ocr_somU", "XML+OCR", "Cây UI + OCR gộp (như Flash) + ảnh đánh số phần tử không nhãn", "Có cây UI, có ảnh"),
    ("compact_somU", "Gọn+SốKN", "List gọn + ảnh thật chỉ đánh số phần tử không nhãn", "Có cây UI, có ảnh"),
    ("shot768", "Ảnh", "Ảnh thật + list gọn (Flash hiện tại)", "Có cây UI, có ảnh"),
    ("center2_shot", "NT2+Ảnh", "Nhãn + tâm (giữ loại/trạng thái) + ảnh thật", "Có cây UI, có ảnh"),
    ("compact_crops", "Gọn+Cắt", "List gọn + chỉ gửi ảnh cắt nhỏ các phần tử không nhãn", "Có cây UI, có ảnh"),
    ("center2_somU", "NT2+SốKN", "Nhãn + tâm (giữ loại) + ảnh chỉ đánh số phần tử không nhãn", "Có cây UI, có ảnh"),
    ("center_som", "NT+Số", "Nhãn + tâm (bản cũ, mất loại) + ảnh vẽ khung & số mọi phần tử", "Có cây UI, có ảnh"),
    ("center_shot", "NT+Ảnh", "Nhãn + tâm (bản cũ, mất loại) + ảnh thật", "Có cây UI, có ảnh"),
    ("wire_hybrid", "WF lai", "Wireframe lai: chữ vẽ lại, phần phức tạp (icon, công tắc, ảnh) dán pixel thật + list gọn có gợi ý gần chữ", "Có cây UI, có ảnh"),
    ("wire2_near", "WF2", "Wireframe v2 (chữ vừa khung, vùng ảnh [image]) + list gọn có gợi ý gần chữ", "Có cây UI, ảnh vẽ lại (không pixel thật)"),
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
    ("ocr_icons_som", "OCR+Icon+Số", "OCR + dò icon v1 + ảnh vẽ khung & số", "Không cây UI (kiểu desktop)"),
    ("ocr2_somU", "OCR+Icon2+Số", "OCR + dò icon v2 + gợi ý gần chữ + ảnh chỉ đánh số icon", "Không cây UI (kiểu desktop)"),
    ("ocr2_near", "OCR+Icon2", "OCR + dò icon v2 + gợi ý gần chữ, không ảnh", "Không cây UI (kiểu desktop)"),
    ("ocr2_shot", "OCR+Icon2+Ảnh", "OCR + dò icon v2 + gợi ý gần chữ + ảnh thật (không vẽ)", "Không cây UI (kiểu desktop)"),
    ("ocr_hybrid", "OCR lai", "OCR vẽ lại chữ + icon dò được dán pixel thật (1 ảnh) + list OCR/icon", "Không cây UI (kiểu desktop)"),
    ("ocr2_crops", "OCR+Icon2+Cắt", "OCR + dò icon v2 + chỉ gửi ảnh cắt nhỏ các icon", "Không cây UI (kiểu desktop)"),
]
TAG_INFO = {
    "orig": "Bài gốc (đích có chữ)", "hard": "Bài khó viết tay", "icon": "Icon", "unlabeled": "Không nhãn",
    "spatial": "Vị trí", "relation": "Quan hệ", "not_in_tree": "Không có trong cây", "toggle": "Công tắc", "dup_label": "Chữ trùng", "labeled": "Có nhãn",
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


def _strip_marks(t):
    import unicodedata
    t = unicodedata.normalize("NFD", t.replace("đ", "d").replace("Đ", "D"))
    return "".join(ch for ch in t if unicodedata.category(ch) != "Mn")


def compare_ocr(case):
    """Per element of the Flash list: what OCR (+ icon detector v2) delivers for it."""
    norm = lambda t: " ".join((t or "").lower().split())
    _, xml = B.listing("list_compact", case)
    ocr = B.ocr_elements(case)
    icons = B.detect_icons_v2(case)["boxes"]

    def inside(p, e, m=8):
        return e["bounds"][0] - m <= p[0] <= e["bounds"][2] + m and e["bounds"][1] - m <= p[1] <= e["bounds"][3] + m
    rows = []
    for e in xml:
        if e.get("text"):
            hits = [o for o in ocr if inside(o["center"], e)]
            got = " | ".join(o["text"] for o in hits)
            xt, j = norm(e["text"]), norm(got)
            if not hits:
                st = "missing"
            elif xt in j or j in xt:
                st = "ok"
            else:
                a, b2 = _strip_marks(xt).replace(" ", ""), _strip_marks(j).replace(" ", "")
                if a in b2 or b2 in a:
                    st = "accent"
                elif len(j) > len(xt) * 1.6:
                    st = "merged"
                else:
                    st = "wrong"
            rows.append(dict(i=e["index"], xml=e["text"][:60], ocr=got[:60], st=st))
        else:
            found = any(inside(((b[0] + b[2]) // 2, (b[1] + b[3]) // 2), e) or
                        (b[0] <= e["center"][0] <= b[2] and b[1] <= e["center"][1] <= b[3]) for b in icons)
            rows.append(dict(i=e["index"], xml="(không nhãn: icon/công tắc/nút)", ocr="(icon) — không biết là gì" if found else "",
                             st="icon_found" if found else "icon_missed"))
    noise = [o["text"] for o in ocr if not any(inside(o["center"], e) for e in xml)]
    icon_noise = sum(1 for b in icons if not any(b[0] <= e["center"][0] <= b[2] and b[1] <= e["center"][1] <= b[3]
                                                    or inside(((b[0] + b[2]) // 2, (b[1] + b[3]) // 2), e, 0) for e in xml))
    return dict(rows=rows, noise=noise, n_ocr=len(ocr), n_icons=len(icons), icon_noise=icon_noise)


def covered(mode, case):
    """Is a correct answer present at all in what this mode sends?"""
    els = B.listing(mode, case)[1]
    return any(B.evaluate('{"index": %d}' % e["index"], mode, case, els)[0] for e in els)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="+", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "gallery"))
    ap.add_argument("--all-modes", action="store_true", help="also show retired modes")
    ap.add_argument("--compare", nargs="*", default=[], help="result files of other models for the comparison table")
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
    # retired from the page (still runnable): ocr_only, ocr_clean, ocr2_near (no-image OCR lists),
    # xml_ocr_somU, fused_icons_somU (full OCR fusion adds noise; gapfill_somU merges only what the tree lacks)
    keep = None if args.all_modes else (set(B.CORE_MODES) | {"wire_text", "center2", "somU_near",
                                                            "ocr_icons_som", "ocr2_shot", "ocr2_crops",
                                                            "compact_crops", "wire2_near", "wire_near",
                                                            "wire_hybrid", "ocr_hybrid", "xml_ocr_somU",
                                                            "gapfill_somU", "gapfill_somU2", "compact_somU2"}) - {"ocr_only", "ocr_clean", "ocr2_near"}
    modes = [m for m in MODE_INFO if (keep is None or m[0] in keep) and any((c["id"], m[0]) in res for c in cases)]

    # images per screen
    screens = {}
    for c in cases:
        if c["screen"] in screens:
            continue
        s = c["screen"]
        thumb(c["img"], os.path.join(out, "img", f"{s}_real.jpg"))
        _, els_c = B.listing("list_compact", c)
        save_b64(B.wire_text_image_v2(c, 768), os.path.join(out, "img", f"{s}_wiretext.jpg"))
        save_b64(B.crop_sheet(c["img"], [e for e in els_c if not e.get("text")]), os.path.join(out, "img", f"{s}_crops.jpg"))
        save_b64(B.wire_text_image_v2(c, 768, paste_real=True), os.path.join(out, "img", f"{s}_hybrid.jpg"))
        _, els_oh = B.listing("ocr_hybrid", c)
        save_b64(B.ocr_hybrid_image(c, els_oh, 768), os.path.join(out, "img", f"{s}_ocrhybrid.jpg"))
        save_b64(B.som_image(c["img"], [e for e in els_c if not e.get("text")], 540),
                 os.path.join(out, "img", f"{s}_somU.jpg"))
        _, els_g = B.listing("gapfill_somU2", c)
        save_b64(B.som_image_v2(c["img"], [e for e in els_g if not e.get("text")], 768),
                 os.path.join(out, "img", f"{s}_gapfill.jpg"))
        _, els_o = B.listing("ocr2_somU", c)
        save_b64(B.som_image(c["img"], [e for e in els_o if not e.get("text")], 540),
                 os.path.join(out, "img", f"{s}_ocr_icons.jpg"))
        screens[s] = dict(id=s, w=c["w"], h=c["h"],
                          list_compact=B.listing("list_compact", c)[0],
                          list_center=B.listing("center2_near", c)[0],
                          ocr_icons=B.listing("ocr2_somU", c)[0],
                          list_gapfill=B.listing("gapfill_somU2", c)[0], n_compact=len(els_c),
                          n_elements=len(els_c), n_unlabeled=sum(1 for e in els_c if not e.get("text")),
                          ocr_compare=compare_ocr(c))

    # summary per mode and per tag
    summary = []
    tags = ["orig", "hard", "icon", "unlabeled", "spatial", "relation", "not_in_tree"]
    for m, short, label, group in modes:
        X = [res[(c["id"], m)] for c in cases if (c["id"], m) in res]
        cov = sum(1 for c in cases if (c["id"], m) in res and covered(m, c))
        row = dict(mode=m, short=short, label=label, group=group, n=len(X), ok=sum(x["ok"] for x in X), cov=cov,
                   wall=round(statistics.median(x["wall"] for x in X), 2),
                   ocr=round(statistics.median(x["ocr"] for x in X), 2),
                   total=round(statistics.median(x["wall"] + x["ocr"] for x in X), 2),
                   total_mean=round(statistics.mean(x["wall"] + x["ocr"] for x in X), 2),
                   tok=round(statistics.median(x["tok"] for x in X)), by_tag={})
        for t in tags:
            T = [res[(c["id"], m)] for c in cases if t in c["tags"] and (c["id"], m) in res]
            row["by_tag"][t] = [sum(x["ok"] for x in T), len(T)]
        summary.append(row)

    # totals of the XML-vs-OCR comparison over all screens
    from collections import Counter
    st_count = Counter(r["st"] for sc in screens.values() for r in sc["ocr_compare"]["rows"])
    ocr_totals = dict(status=dict(st_count), labeled=sum(1 for sc in screens.values() for r in sc["ocr_compare"]["rows"]
                                                         if not r["st"].startswith("icon")),
                      unlabeled=sum(1 for sc in screens.values() for r in sc["ocr_compare"]["rows"] if r["st"].startswith("icon")),
                      n_ocr=sum(sc["ocr_compare"]["n_ocr"] for sc in screens.values()),
                      noise=sum(len(sc["ocr_compare"]["noise"]) for sc in screens.values()),
                      n_icons=sum(sc["ocr_compare"]["n_icons"] for sc in screens.values()),
                      icon_noise=sum(sc["ocr_compare"]["icon_noise"] for sc in screens.values()),
                      roles=sum(len([l for l in sc["list_compact"].splitlines() if "] (" in l]) for sc in screens.values()))
    # other models (e.g. Gemini): accuracy / time / tokens per mode, next to the main model
    compare = []
    for path in args.compare:
        crows = [json.loads(l) for l in open(path)]
        if not crows:
            continue
        cmodel = crows[0]["model"]
        cres = {}
        for r in crows:
            c = by_id.get(r["step"])
            if c:
                cres[(r["step"], r["mode"])] = (B.evaluate(r["reply"], r["mode"], c)[0], r["wall"], r["in_tok"] + r.get("s1_in", 0))
        for m, short, label, group in MODE_INFO:
            Xo = [cres[(c["id"], m)] for c in cases if (c["id"], m) in cres]
            Xm = [res[(c["id"], m)] for c in cases if (c["id"], m) in res]
            if not Xo or not Xm:
                continue
            compare.append(dict(mode=m, label=label, model=cmodel, n=len(Xo), ok=sum(x[0] for x in Xo),
                                wall=round(statistics.median(x[1] for x in Xo), 2), tok=round(statistics.median(x[2] for x in Xo)),
                                main_n=len(Xm), main_ok=sum(x["ok"] for x in Xm),
                                main_wall=round(statistics.median(x["wall"] for x in Xm), 2),
                                main_tok=round(statistics.median(x["tok"] for x in Xm))))
    data = dict(compare=compare, ocr_totals=ocr_totals,
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
