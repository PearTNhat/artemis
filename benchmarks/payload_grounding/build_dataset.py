"""Build the self-contained benchmark dataset from recorded ARTEMIS traces.

Copies the selected tap/click steps out of traces/replay/data into ./data/<case_id>/
(screen.jpg + ui_tree.json) and writes ./cases.jsonl. After this the benchmark no
longer needs traces/, so deleting traces does not break it.

Selection is deterministic (seed 7, at most 2 steps per session, only steps whose
recorded target has a text label). Privacy: before anything is written,
  * "<App> notification: ..." content-descs are reduced to "notification";
  * e-mail addresses and phone-number-like strings are redacted;
  * every pattern listed in --redact-file (one regex per line, kept OUT of git) is
    redacted in ui_tree text, in the instruction, and painted black on the image
    (via ui_tree bounds and via OCR, so text missing from the tree is covered too).
Review the images yourself before publishing.

Usage (repo root):
  uv run python benchmarks/payload_grounding/build_dataset.py --count 18 \
      --redact-file scratch/bench_payload/redact.txt
"""
import argparse, glob, json, os, random, re, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
REDACTED = "[redacted]"
BUILTIN = [r"[\w.+-]+@[\w-]+\.[\w.]{2,}",                                # e-mail
           r"(?<![\d.,])(?:\+84|0)\d{2,3}[ .-]?\d{3}[ .-]?\d{3,4}(?![\d.,])"]  # VN-style phone number
norm = lambda t: " ".join((t or "").lower().split())


def smallest_node_at(nodes, x, y):
    best, stack = None, list(nodes)
    while stack:
        n = stack.pop()
        if not isinstance(n, dict):
            continue
        stack.extend(n.get("children") or [])
        b = n.get("parsed_bounds")
        if not b or not (b["left"] <= x <= b["right"] and b["top"] <= y <= b["bottom"]):
            continue
        if not (n.get("text") or n.get("content-desc") or n.get("clickable") == "true"):
            continue
        area = (b["right"] - b["left"]) * (b["bottom"] - b["top"])
        if area > 0 and (best is None or area < best[0]):
            best = (area, [b["left"], b["top"], b["right"], b["bottom"]])
    return best[1] if best else None


def clean_instruction(t):
    """The agent's reasoning without element indices / coordinates (they would leak the answer)."""
    t = re.sub(r"\(?element\s*\[\d+\][^)]*\)?", "", t, flags=re.I)
    t = re.sub(r"\[\d+\]", "", t)
    t = re.sub(r"[Bb]ounds?\s*\[[^\]]*\](\[[^\]]*\])?", "", t)
    t = re.sub(r"\(\s*\d+\s*,\s*\d+\s*\)|\[\s*\d+\s*,\s*\d+\s*(,\s*\d+\s*,\s*\d+\s*)?\]", "", t)
    t = re.sub(r"\b(index|idx)\s*\d+", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()[:600]


def select_steps(count):
    out = []
    for sj in sorted(glob.glob(f"{REPO}/traces/replay/data/*/step_*/step.json")):
        d = os.path.dirname(sj)
        try:
            st = json.load(open(sj))
            meta = json.load(open(f"{d}/pre_image_meta.json"))
        except Exception:
            continue
        acts = st.get("action_taken") or []
        if isinstance(acts, dict):
            acts = [acts]
        if len(acts) != 1 or acts[0].get("action") not in ("tap", "click"):
            continue
        tb = acts[0].get("target_bounds")
        if not tb and acts[0].get("coordinate_space") == "pixel" and acts[0].get("coordinates"):
            tb = smallest_node_at(meta.get("ui_tree") or [], *acts[0]["coordinates"])
        think = st.get("operator_raw_thinking") or ""
        if not tb or len(think) < 40 or "ONE JSON object" in think or not meta.get("ui_tree"):
            continue
        if not os.path.exists(f"{d}/pre.jpg"):
            continue
        w = st.get("extra_metadata", {}).get("width") or 1280
        h = st.get("extra_metadata", {}).get("height") or 2772
        if (tb[2] - tb[0]) * (tb[3] - tb[1]) > 0.4 * w * h:
            continue
        out.append(dict(dir=d, source=d.split("/data/")[1], session=st["session_id"], think=think,
                        target_text=acts[0].get("target_text", ""), w=w, h=h, ui=meta["ui_tree"],
                        app=st.get("extra_metadata", {}).get("foreground_app", "")))
    random.Random(7).shuffle(out)
    picked, per = [], {}
    for s in out:
        if per.get(s["session"], 0) < 2:
            per[s["session"]] = per.get(s["session"], 0) + 1
            picked.append(s)
    return [s for s in picked if norm(s["target_text"])][:count]


def redact_tree(nodes, regexes, boxes):
    for n in nodes:
        if not isinstance(n, dict):
            continue
        for k in ("text", "content-desc", "accessibilityText", "hint"):
            v = n.get(k)
            if not v:
                continue
            m = re.match(r"^(.*?) notification:", v)
            if m:
                n[k] = "notification"
                continue
            new = v
            for rx in regexes:
                new = rx.sub(REDACTED, new)
            if new != v:
                n[k] = new
                b = n.get("parsed_bounds")
                if b:
                    boxes.append((b["left"], b["top"], b["right"], b["bottom"]))
        redact_tree(n.get("children") or [], regexes, boxes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=18)
    ap.add_argument("--redact-file", default=None, help="regex per line; keep this file out of git")
    args = ap.parse_args()

    import numpy as np
    from PIL import Image, ImageDraw

    extra = []
    region_rules = []  # "box:<source-prefix>:x1,y1,x2,y2" -> paint that rectangle (e.g. a map thumbnail)
    if args.redact_file:
        for line in open(args.redact_file, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("box:"):
                _, prefix, coords = line.split(":", 2)
                region_rules.append((prefix, tuple(int(v) for v in coords.split(","))))
            else:
                extra.append(line)
    regexes = [re.compile(p, re.I) for p in BUILTIN + extra]
    ocr = None
    if extra:
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR()

    data_dir = os.path.join(HERE, "data")
    shutil.rmtree(data_dir, ignore_errors=True)
    os.makedirs(data_dir)
    cases = []
    for i, s in enumerate(select_steps(args.count)):
        cid = f"case_{i:02d}"
        boxes = []
        redact_tree(s["ui"], regexes, boxes)
        im = Image.open(f"{s['dir']}/pre.jpg").convert("RGB")
        if ocr:
            res, _ = ocr(np.asarray(im)[:, :, ::-1])
            for box, text, _score in res or []:
                if any(rx.search(text) for rx in regexes):
                    xs, ys = [p[0] for p in box], [p[1] for p in box]
                    boxes.append((min(xs), min(ys), max(xs), max(ys)))
        boxes += [box for prefix, box in region_rules if s["source"].startswith(prefix)]
        d = ImageDraw.Draw(im)
        for x1, y1, x2, y2 in boxes:
            d.rectangle([x1 - 4, y1 - 4, x2 + 4, y2 + 4], fill="black")
        os.makedirs(os.path.join(data_dir, cid))
        im.save(os.path.join(data_dir, cid, "screen.jpg"), "JPEG", quality=85)
        json.dump(s["ui"], open(os.path.join(data_dir, cid, "ui_tree.json"), "w"), ensure_ascii=False)
        instr = clean_instruction(s["think"])
        for rx in regexes:
            instr = rx.sub(REDACTED, instr)
        cases.append(dict(id=cid, instruction=instr, accept=[s["target_text"]], width=im.width, height=im.height,
                          app=s["app"], source=s["source"], redacted_regions=len(boxes)))
        print(f"{cid}  {s['app'][:28]:28s} target={s['target_text'][:30]!r} redacted={len(boxes)}")
    with open(os.path.join(HERE, "cases.jsonl"), "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"wrote {len(cases)} cases")


if __name__ == "__main__":
    main()
