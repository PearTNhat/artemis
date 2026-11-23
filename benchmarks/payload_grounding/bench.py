"""Payload grounding benchmark: which input lets the model pick the right element fastest?

Self-contained: reads ./cases.jsonl and ./data/<case>/{screen.jpg,ui_tree.json} only
(build them with build_dataset.py). Each case = one real recorded tap from an ARTEMIS
run: the screen, its UI tree, an instruction (the agent's own reasoning, indices and
coordinates stripped) and the accepted target label(s). Edit cases.jsonl freely:
change "instruction", add alternative labels to "accept", or delete lines.

Modes (all answer {"index": N} over the Flash element list):
  shot768       screenshot (768px JPEG q75) + Flash compact list
  2stage        model describes screenshot (<=60 words), then text-only decide with compact list
  list_full     Flash list compact=False, no image
  list_compact  Flash compact list (current format), no image
  list_center   [i] "label" @cx,cy (0-1000 normalized center), no image
  list_label    [i] "label", no image
  ocr_only      OCR text boxes only ([i] "text" @cx,cy) - no accessibility tree, no image (desktop-like)
  ocr_shot      OCR text boxes + screenshot 768 (no accessibility tree)
  xml_ocr       Flash compact list after fusing OCR into the tree (Flash ocr.mode="always"), no image
  center_ocr    label + center list built from the tree fused with OCR, no image
  center_som    label + center list + real screenshot with every element's box and index drawn on it
  center_shot   label + center list + plain real screenshot (768px)
  center2       Flash compact list with each box replaced by its center: keeps role/state hints
                ("(toggle) @833,321 checked:false"), no image
  center2_shot  center2 + plain real screenshot
  compact_somU  Flash compact list + screenshot with box+index drawn ONLY on unlabeled elements
  center2_somU  center2 + the same unlabeled-only marked screenshot
  center2_near  center2 + 'near "<closest label>"' after every unlabeled element, no image
  somU_near     compact list with the same near-label hints + unlabeled-only marked screenshot
  wire_near     text wireframe + compact list with near-label hints
  ocr2_near     no tree: OCR text + icon detector v2 (nested icons, switches) + near-text hints, no image
  ocr2_somU     ocr2_near list + screenshot with only the detected icons boxed and numbered
  wire2_near    wireframe v2 (text sized to its box, no overlapping tags, duplicates skipped, picture
                regions found on the screenshot drawn as [image], elements behind a menu/dialog
                drawn faded) + compact list with near hints
  wire_hybrid   wireframe v2 where the complex parts (unlabeled controls, picture regions) are the REAL
                pixels, text redrawn; + compact list with near hints
  ocr_hybrid    no tree: OCR text redrawn + detected icons as REAL pixels, all tagged; + ocr2_near list
  xml_ocr_somU  tree fused with OCR (as Flash does) + near hints + screenshot marking unlabeled elements
  compact_somU2 / gapfill_somU2  same lists, marks v2: one index per icon, small tags placed outside the box
  gapfill_somU  tree list unchanged + ONLY OCR lines / detected icons outside every tree element and
                outside pictures (what the app left out of the tree) + unlabeled-only marked screenshot
  fused_icons_somU  same + detected icons that lie outside every tree element and outside pictures,
                added as (icon); catches buttons the app leaves out of the tree
  ocr2_shot     ocr2_near list + plain real screenshot
  ocr2_crops    ocr2_near list + a sheet of small cut-outs of the detected icons (no full screenshot)
  compact_crops Flash compact list + a sheet of cut-outs of the unlabeled elements (no full screenshot)
  ocr_clean     ocr_only after a generic noise filter (status bar band, score < 0.8, 1-char lines)
  wire_text     black/white wireframe WITH text: each box shows its label, or its role/state for
                unlabeled elements ("(toggle) checked:false"), images drawn as "[image]"; + compact list
  ocr_icons     no tree: OCR text boxes + OpenCV-detected icon boxes ("(icon)"), no image
  ocr_icons_som same list + real screenshot with those boxes and indices drawn on it
Cases may reuse another case's screen ("screen": "case_00") and may define the answer as screen
regions ("accept_box": [[x1,y1,x2,y2], ...], px) - needed for icons that have no label; the chosen
element is then correct when its center lies in one of the boxes. "tags" group cases in --summary.
OCR = Flash's own local OCR (artemis.utils.local_ocr: RapidOCR + PP-OCRv5 Latin), run once per
case on this PC and cached in data/<case>/ocr_*.json together with its time (ocr_ms).

Usage (repo root):
  # local Ollama, light on the laptop (batches of 5, 40s rest, stops if RAM < 3 GB)
  uv run python benchmarks/payload_grounding/bench.py --backend ollama \
      --out benchmarks/payload_grounding/results/qwen3-vl-4b.jsonl --modes shot768,list_center
  # Gemini API (key from GEMINI_API_KEY)
  set -a; . ./.env; set +a
  uv run python benchmarks/payload_grounding/bench.py --backend gemini --model gemini-3.5-flash-lite \
      --out benchmarks/payload_grounding/results/gemini-3.5-flash-lite.jsonl
  # summary table
  uv run python benchmarks/payload_grounding/bench.py --summary benchmarks/payload_grounding/results/*.jsonl
Output is resumable: rerunning the same --out continues where it stopped.
"""
import argparse, base64, glob, io, json, os, random, re, statistics, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO)

ALL_MODES = ["shot768", "2stage", "list_full", "list_compact", "list_center", "list_label",
             "center_som", "center_shot", "center2", "center2_shot", "compact_somU",
             "center2_somU", "wire_text", "center2_near", "somU_near", "wire_near", "wire2_near", "wire_hybrid", "xml_ocr_somU", "fused_icons_somU", "gapfill_somU", "compact_somU2", "gapfill_somU2",
             "compact_crops", "ocr_only", "ocr_clean", "ocr2_near", "ocr2_somU", "ocr2_shot", "ocr2_crops", "ocr_hybrid", "ocr_shot", "xml_ocr", "center_ocr",
             "ocr_icons", "ocr_icons_som"]
# Default set: the ones worth running. The others stay runnable with --modes (and their old
# results stay in results/*.jsonl) but proved useless: wireframe, 2-stage, no-image lists,
# OCR fusion, and the old label+center that dropped role/state hints.
CORE_MODES = ["shot768", "compact_somU", "gapfill_somU2", "center2_shot", "wire_hybrid", "center2_near", "ocr2_somU"]
OCR_MODES = ("gapfill_somU", "gapfill_somU2", "xml_ocr_somU", "fused_icons_somU", "ocr_only", "ocr_clean", "ocr2_near", "ocr2_somU", "ocr2_shot", "ocr2_crops", "ocr_hybrid", "ocr_shot", "xml_ocr", "center_ocr", "ocr_icons", "ocr_icons_som")
NO_TREE_MODES = ("ocr_only", "ocr_clean", "ocr2_near", "ocr2_somU", "ocr2_shot", "ocr2_crops", "ocr_hybrid", "ocr_shot", "ocr_icons", "ocr_icons_som")  # graded against the tree
IMAGE_MODES = {"shot768": "shot", "ocr_shot": "shot",
               "center_som": "som", "ocr_icons_som": "som", "center_shot": "shot", "center2_shot": "shot",
               "compact_somU": "somU", "center2_somU": "somU", "wire_text": "wiretext", "somU_near": "somU",
               "wire_near": "wiretext", "ocr2_somU": "somU", "ocr2_shot": "shot", "ocr2_crops": "crops",
               "compact_crops": "crops", "wire2_near": "wire2", "wire_hybrid": "hybrid", "ocr_hybrid": "ocrhybrid",
               "xml_ocr_somU": "somU", "fused_icons_somU": "somU", "gapfill_somU": "somU",
               "compact_somU2": "somU2", "gapfill_somU2": "somU2"}
DESCRIBE = ("Describe this Android screen in at most 60 words: app/screen name, main sections, "
            "and any icon-only buttons with their position and look.")
norm = lambda t: " ".join((t or "").lower().split())


# ------------------------------------------------------------------ dataset
def load_cases(limit=None):
    cases = []
    for line in open(os.path.join(HERE, "cases.jsonl"), encoding="utf-8"):
        if not line.strip():
            continue
        c = json.loads(line)
        screen = c.get("screen", c["id"])
        d = os.path.join(HERE, "data", screen)
        cases.append(dict(id=c["id"], screen=screen, dir=d, instr=c["instruction"], accept=c.get("accept", []),
                          accept_box=c.get("accept_box"), near_y=c.get("near_y"), tags=c.get("tags", ["orig"]),
                          w=c["width"], h=c["height"],
                          img=os.path.join(d, "screen.jpg"), ui=json.load(open(os.path.join(d, "ui_tree.json")))))
    return cases[:limit] if limit else cases


# ------------------------------------------------------------------ payloads
def b64_jpeg(im, max_side, q=75):
    from PIL import Image
    im = im.copy()
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=q)
    return base64.b64encode(buf.getvalue()).decode()


def ocr_lines(case, with_xml):
    """Flash's local OCR on the case screenshot; cached per case (the time is measured once).

    with_xml=False: no tree at all (desktop-like), the whole frame is read.
    with_xml=True: the tree is passed, so the status bar is cropped like in Flash."""
    path = os.path.join(case["dir"], "ocr_xml.json" if with_xml else "ocr_full.json")
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    from artemis.utils import local_ocr
    cfg = local_ocr.local_ocr_settings()
    if local_ocr._get_engine(cfg) is None:  # build the engine outside the timed call
        raise RuntimeError("local OCR engine unavailable")
    local_ocr._cache.clear()  # the module caches by image hash; force a real run
    data = open(case["img"], "rb").read()
    t = time.perf_counter()
    lines = local_ocr.recognize_screenshot(data, case["ui"] if with_xml else [], cfg)
    out = dict(ms=round((time.perf_counter() - t) * 1000), lines=lines)
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return out


def ocr_elements(case, clean=False):
    els = []
    for i, ln in enumerate(ocr_clean_lines(case) if clean else ocr_lines(case, False)["lines"], 1):
        xs = [p["x"] for p in ln["position"]]
        ys = [p["y"] for p in ln["position"]]
        b = [min(xs), min(ys), max(xs), max(ys)]
        els.append(dict(index=i, text=ln["text"], bounds=b, center=[(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]))
    return els


def detect_icons(case):
    """Generic OpenCV icon/graphic detector (no app knowledge): edge blobs of icon-like size and
    shape that do not overlap OCR text. Cached in data/<screen>/icons.json with its time."""
    path = os.path.join(case["dir"], "icons.json")
    if os.path.exists(path):
        return json.load(open(path))
    import cv2
    import numpy as np
    t = time.perf_counter()
    img = cv2.imread(case["img"])
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 120)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    lo, hi = 0.03 * W, 0.16 * W
    text_boxes = [e["bounds"] for e in ocr_elements(case)]
    boxes = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if not (lo <= w <= hi and lo <= h <= hi and 0.6 <= w / h <= 1.67):
            continue
        cx, cy = x + w / 2, y + h / 2
        if any(b[0] - 4 <= cx <= b[2] + 4 and b[1] - 4 <= cy <= b[3] + 4 for b in text_boxes):
            continue
        boxes.append([x, y, x + w, y + h])
    boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    kept = []
    for b in boxes:  # drop boxes mostly inside an already kept (bigger) one
        if not any(min(b[2], k[2]) - max(b[0], k[0]) > 0.6 * (b[2] - b[0])
                   and min(b[3], k[3]) - max(b[1], k[1]) > 0.6 * (b[3] - b[1]) for k in kept):
            kept.append(b)
    kept = sorted(kept, key=lambda b: (b[1], b[0]))[:60]
    out = dict(ms=round((time.perf_counter() - t) * 1000), boxes=kept)
    json.dump(out, open(path, "w"))
    return out


def detect_icons_v2(case):
    """Icon detector v2 (generic OpenCV): like detect_icons, but also keeps icons nested inside a
    bigger graphic (a "+" drawn on a photo), allows wide shapes (switches), and only merges boxes
    of similar size. Cached in data/<screen>/icons_v2.json with its time."""
    path = os.path.join(case["dir"], "icons_v2.json")
    if os.path.exists(path):
        return json.load(open(path))
    import cv2
    import numpy as np
    t = time.perf_counter()
    img = cv2.imread(case["img"])
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.dilate(cv2.Canny(gray, 40, 120), np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    lo, hi = 0.025 * W, 0.16 * W
    text_boxes = [e["bounds"] for e in ocr_elements(case)]
    boxes = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if not (lo <= w <= hi and 0.6 * lo <= h <= hi and 0.4 <= w / h <= 2.5):
            continue
        cx, cy = x + w / 2, y + h / 2
        if any(b[0] - 4 <= cx <= b[2] + 4 and b[1] - 4 <= cy <= b[3] + 4 for b in text_boxes):
            continue
        if y + h < 0.04 * H:  # status bar band
            continue
        boxes.append([x, y, x + w, y + h])
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])
    boxes.sort(key=area, reverse=True)
    kept = []
    for b in boxes:  # merge only near-duplicates (same place, similar size)
        dup = False
        for k in kept:
            iw = min(b[2], k[2]) - max(b[0], k[0])
            ih = min(b[3], k[3]) - max(b[1], k[1])
            if iw > 0 and ih > 0 and iw * ih > 0.6 * area(b) and area(b) > 0.45 * area(k):
                dup = True
                break
        if not dup:
            kept.append(b)
    kept = sorted(kept, key=lambda b: (b[1], b[0]))[:80]
    out = dict(ms=round((time.perf_counter() - t) * 1000), boxes=kept)
    json.dump(out, open(path, "w"))
    return out


def center_text(els, w, h, icon_label="(icon)"):
    return "\n".join(f'[{e["index"]}] ' + (f'"{e["text"]}"' if e.get("text") else icon_label)
                     + f' @{e["center"][0] * 1000 // w},{e["center"][1] * 1000 // h}' for e in els)


def som_image(path, els, max_side=768):
    """Real screenshot with every listed element's box and index drawn on it (set-of-marks)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(path).convert("RGB")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", max(14, im.width // 40))
    except OSError:
        font = ImageFont.load_default()
    for e in els:
        d.rectangle(e["bounds"], outline=(255, 0, 0), width=max(2, im.width // 400))
    for e in els:
        x1, y1 = e["bounds"][:2]
        lab = str(e["index"])
        tw = d.textlength(lab, font=font)
        d.rectangle([x1, y1, x1 + tw + 6, y1 + font.size + 4], fill=(255, 0, 0))
        d.text((x1 + 3, y1 + 1), lab, fill=(255, 255, 255), font=font)
    return b64_jpeg(im, max_side, 80)


def wire_text_image(case, max_side=768):
    """Black/white rendering of the element list: box + [index] + its label (or role/state hint
    from the Flash compact list for unlabeled elements; image views as "[image]")."""
    from PIL import Image, ImageDraw, ImageFont
    text, els = listing("list_compact", case)
    hint = {}
    for line in text.splitlines():
        m = re.match(r"\[(\d+)\]\s+(.*?)\s+-?\d+,-?\d+,-?\d+,-?\d+(.*)$", line)
        if m:
            hint[int(m[1])] = (m[2] + m[3]).strip().strip('"')
    w, h = case["w"], case["h"]
    sc = max_side / max(w, h)
    im = Image.new("L", (int(w * sc), int(h * sc)), 255)
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
        bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 10)
    except OSError:
        font = bold = ImageFont.load_default()
    # non-interactive pictures from the raw tree (not in the element list): gray X + "[image]", no index
    stack, seen = list(case["ui"]), {tuple(e["bounds"]) for e in els}
    while stack:
        n = stack.pop()
        if not isinstance(n, dict):
            continue
        stack.extend(n.get("children") or [])
        b = n.get("parsed_bounds")
        if not b or "image" not in (n.get("class") or "").lower():
            continue
        bb = (b["left"], b["top"], min(b["right"], w), min(b["bottom"], h))
        if bb in seen or (bb[2] - bb[0]) < 0.06 * w or (bb[3] - bb[1]) < 0.03 * h or (bb[2] - bb[0]) * (bb[3] - bb[1]) > 0.5 * w * h:
            continue
        seen.add(bb)
        x1, y1, x2, y2 = (int(v * sc) for v in bb)
        d.rectangle([x1, y1, x2 - 1, y2 - 1], outline=150, width=1)
        d.line([x1, y1, x2 - 1, y2 - 1], fill=200)
        d.line([x1, y2 - 1, x2 - 1, y1], fill=200)
        d.text((x1 + 3, y2 - 13), "[image]", fill=110, font=font)
    for e in els:
        x1, y1, x2, y2 = (int(v * sc) for v in e["bounds"])
        x2, y2 = max(x1 + 2, x2 - 1), max(y1 + 2, y2 - 1)
        is_img = "image" in (e.get("class") or "").lower() and not e.get("text")
        d.rectangle([x1, y1, x2, y2], outline=0, width=1)
        if is_img:
            d.line([x1, y1, x2, y2], fill=170)
            d.line([x1, y2, x2, y1], fill=170)
        label = "[image]" if is_img else (e.get("text") or hint.get(e["index"], ""))
        label = " ".join(label.split())  # one line
        tag = str(e["index"])
        tw = d.textlength(tag, font=bold)
        d.rectangle([x1, y1, x1 + tw + 3, y1 + 11], fill=0)
        d.text((x1 + 2, y1), tag, fill=255, font=bold)
        avail = x2 - x1 - 4
        if label and avail > 8:
            while label and d.textlength(label, font=font) > avail:
                label = label[:-1]
            ty = y1 + 12 if y2 - y1 > 24 else y1 + 1
            tx = x1 + 2 if y2 - y1 > 24 else x1 + tw + 6
            d.text((tx, ty), label, fill=0, font=font)
    return b64_jpeg(im.convert("RGB"), max_side, 85)


def add_near_hints(text, els, screen_h):
    """Append 'near "<label>"' to each unlabeled element line. Generic layout rule, no app
    knowledge: (1) a label on the same row (label left / control right), else (2) the first one or
    two labels directly below in the same column (picture or card with its caption below),
    else (3) the closest label."""
    labeled = [e for e in els if e.get("text")]
    hints = {}
    for e in els:
        if e.get("text") or not labeled:
            continue
        x1, y1, x2, y2 = e["bounds"]
        cx, cy = e["center"]
        h = y2 - y1
        row = [t for t in labeled
               if min(y2, t["bounds"][3]) - max(y1, t["bounds"][1]) > 0.5 * min(h, t["bounds"][3] - t["bounds"][1])]
        if row:
            hints[e["index"]] = min(row, key=lambda t: abs(t["center"][0] - cx))["text"][:40]
            continue
        reach = 1.2 * (x2 - x1)
        below = [t for t in labeled if t["bounds"][1] >= y2 - 5 and t["bounds"][1] - y2 < 0.12 * screen_h
                 and t["bounds"][0] - reach <= cx <= t["bounds"][2] + reach]
        if below:
            overlaps = lambda t: min(x2, t["bounds"][2]) > max(x1, t["bounds"][0])
            below.sort(key=lambda t: (not overlaps(t), t["bounds"][1], abs(t["center"][0] - cx)))
            first = below[0]
            col = [t for t in below if abs(t["center"][0] - first["center"][0]) < (x2 - x1)]
            col.sort(key=lambda t: t["bounds"][1])
            hints[e["index"]] = " / ".join(t["text"][:30] for t in col[:2])
            continue
        hints[e["index"]] = min(labeled, key=lambda t: (t["center"][0] - cx) ** 2 + (t["center"][1] - cy) ** 2)["text"][:40]
    out = []
    for line in text.splitlines():
        m = re.match(r"\[(\d+)\]", line)
        if m and int(m[1]) in hints:
            line += f' near "{hints[int(m[1])]}"'
        out.append(line)
    return "\n".join(out)

def ocr_clean_lines(case):
    """Generic OCR noise filter: drop the status-bar band, low-confidence and 1-character lines."""
    top = 0.04 * case["h"]
    keep = []
    for ln in ocr_lines(case, False)["lines"]:
        if max(p["y"] for p in ln["position"]) < top or ln.get("score", 1) < 0.8 or len(ln["text"].strip()) <= 1:
            continue
        keep.append(ln)
    return keep


def crop_sheet(path, els, max_side=768, cell_h=110, limit=48):
    """Only small cut-outs of the given elements (each padded 15%, scaled to the same height),
    tagged with their [index], packed into one sheet image. No full screenshot is sent."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(path).convert("RGB")
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    tiles = []
    for e in els[:limit]:
        x1, y1, x2, y2 = e["bounds"]
        px, py = int(0.15 * (x2 - x1)), int(0.15 * (y2 - y1))
        c = im.crop((max(0, x1 - px), max(0, y1 - py), min(im.width, x2 + px), min(im.height, y2 + py)))
        c = c.resize((max(1, int(c.width * cell_h / max(1, c.height))), cell_h))
        if c.width > max_side // 2:
            c = c.resize((max_side // 2, max(1, int(cell_h * (max_side // 2) / c.width))))
        t = Image.new("RGB", (max(c.width, 40), c.height + 20), "white")
        t.paste(c, (0, 20))
        d = ImageDraw.Draw(t)
        d.rectangle([0, 0, d.textlength(f"[{e['index']}]", font=font) + 6, 19], fill=(220, 30, 30))
        d.text((3, 1), f"[{e['index']}]", fill="white", font=font)
        tiles.append(t)
    if not tiles:
        tiles = [Image.new("RGB", (40, 40), "white")]
    rows, row, x = [], [], 0
    for t in tiles:  # simple row packing
        if row and x + t.width + 6 > max_side:
            rows.append(row)
            row, x = [], 0
        row.append(t)
        x += t.width + 6
    rows.append(row)
    W = max(sum(t.width + 6 for t in r) for r in rows)
    H = sum(max(t.height for t in r) + 6 for r in rows)
    sheet = Image.new("RGB", (W, H), (235, 235, 235))
    y = 0
    for r in rows:
        x = 0
        for t in r:
            sheet.paste(t, (x, y))
            x += t.width + 6
        y += max(t.height for t in r) + 6
    return b64_jpeg(sheet, max_side, 85)


def picture_regions(case, els):
    """Generic picture finder on the screenshot (runs locally, only the drawing is sent):
    32px blocks with high colour variation that hold no UI text, merged into rectangles."""
    import numpy as np
    from PIL import Image
    path = os.path.join(case["dir"], "pictures.json")
    if os.path.exists(path):
        return json.load(open(path))
    im = Image.open(case["img"]).convert("RGB")
    W, H = im.size
    B = 32
    a = np.asarray(im.resize((W // B * B, H // B * B))).astype(np.float32)
    gh, gw = a.shape[0] // B, a.shape[1] // B
    blocks = a.reshape(gh, B, gw, B, 3)
    std = blocks.std(axis=(1, 3)).mean(axis=-1)
    sat = (blocks.max(axis=-1) - blocks.min(axis=-1)).mean(axis=(1, 3))
    mask = (std > 30) & (sat > 25)
    # only real text lines punch holes (not big labelled containers such as a video card)
    text = [e["bounds"] for e in els if e.get("text") and (e["bounds"][3] - e["bounds"][1]) < 0.06 * H] + \
           [e["bounds"] for e in ocr_elements(case)]
    for x1, y1, x2, y2 in text:
        mask[max(0, y1 // B):min(gh, (y2 + B - 1) // B), max(0, x1 // B):min(gw, (x2 + B - 1) // B)] = False
    mask[: int(0.04 * H) // B + 1, :] = False  # status bar band
    seen = np.zeros_like(mask)
    rects = []
    for i in range(gh):
        for j in range(gw):
            if not mask[i, j] or seen[i, j]:
                continue
            stack, cells = [(i, j)], []
            seen[i, j] = True
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < gh and 0 <= nx < gw and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            ys, xs = [c[0] for c in cells], [c[1] for c in cells]
            r = [min(xs) * B, min(ys) * B, (max(xs) + 1) * B, (max(ys) + 1) * B]
            if (r[2] - r[0]) >= 0.08 * W and (r[3] - r[1]) >= 0.04 * H and len(cells) >= 0.4 * ((r[2] - r[0]) // B) * ((r[3] - r[1]) // B):
                rects.append(r)
    json.dump(rects, open(path, "w"))
    return rects


def wire_text_image_v2(case, max_side=768, paste_real=False):
    """Wireframe v2: text sized to its box with an ellipsis, small index tags that do not cover the
    text, nested duplicates (same label) skipped, and picture regions found on the screenshot drawn
    as hatched "[image]" boxes."""
    from PIL import Image, ImageDraw, ImageFont
    text, els = listing("list_compact", case)
    hint = {}
    for line in text.splitlines():
        m = re.match(r"\[(\d+)\]\s+(.*?)\s+-?\d+,-?\d+,-?\d+,-?\d+(.*)$", line)
        if m:
            hint[int(m[1])] = (m[2] + m[3]).strip().strip('"')
    w, h = case["w"], case["h"]
    sc = max_side / max(w, h)
    im = Image.new("RGB" if paste_real else "L", (int(w * sc), int(h * sc)), "white")
    d = ImageDraw.Draw(im)
    real = Image.open(case["img"]).convert("RGB").resize(im.size) if paste_real else None
    fp = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    fonts = {}

    def font(sz):
        if sz not in fonts:
            try:
                fonts[sz] = ImageFont.truetype(fp, sz)
            except OSError:
                fonts[sz] = ImageFont.load_default()
        return fonts[sz]
    tagf = font(9)
    # layering: an element that overlaps a LATER one in the list (drawn on top: menu, dialog,
    # bottom sheet) is behind it -> drawn faded; front elements get a white fill that hides it
    behind = set()
    for line in text.splitlines():
        m = re.match(r"\[(\d+)\].*\(overlaps ([^)]*)", line)
        if m and any(int(k) > int(m[1]) for k in re.findall(r"\[(\d+)\]", m[2])):
            behind.add(int(m[1]))
    G = (lambda v: (v, v, v)) if paste_real else (lambda v: v)
    for r in picture_regions(case, els):
        x1, y1, x2, y2 = (int(v * sc) for v in r)
        if paste_real:  # complex part: real pixels instead of a placeholder
            im.paste(real.crop((x1, y1, x2, y2)), (x1, y1))
            d.rectangle([x1, y1, x2 - 1, y2 - 1], outline=G(140))
            continue
        d.rectangle([x1, y1, x2 - 1, y2 - 1], outline=140, fill=235)
        for k in range(x1 - (y2 - y1), x2, 10):  # hatch
            d.line([max(x1, k), y1 + max(0, x1 - k), min(x2 - 1, k + (y2 - y1)), y1 + min(y2 - y1, x2 - 1 - k)], fill=205)
        d.text((x1 + 3, y1 + 2), "[image]", fill=90, font=font(10))
    # a parent that repeats the label of an element inside it (tab + its text) is not drawn
    skip = set()
    for e in els:
        lab = " ".join((e.get("text") or "").split())
        if not lab:
            continue
        b = e["bounds"]
        for k in els:
            kb = k["bounds"]
            if k is not e and " ".join((k.get("text") or "").split()) == lab and b[0] <= kb[0] and b[1] <= kb[1] \
                    and b[2] >= kb[2] and b[3] >= kb[3] and (kb[2] - kb[0]) * (kb[3] - kb[1]) < (b[2] - b[0]) * (b[3] - b[1]):
                skip.add(e["index"])
                break
    drawn = []
    order = sorted(els, key=lambda e: (e["index"] not in behind, -(e["bounds"][2] - e["bounds"][0]) * (e["bounds"][3] - e["bounds"][1])))
    for e in order:  # behind first, then front (big before small so small labels stay on top)
        label = " ".join((e.get("text") or hint.get(e["index"], "")).split())
        b = e["bounds"]
        if e["index"] in skip:
            continue
        drawn.append((label, b))
        x1, y1, x2, y2 = (int(v * sc) for v in b)
        x2, y2 = max(x1 + 3, x2 - 1), max(y1 + 3, y2 - 1)
        faded = e["index"] in behind
        ink = G(175 if faded else 0)
        unl = not e.get("text")
        if paste_real and unl and not faded:  # unlabeled control: show its real pixels
            im.paste(real.crop((x1, y1, x2 + 1, y2 + 1)), (x1, y1))
            d.rectangle([x1, y1, x2, y2], outline=(220, 30, 30), width=1)
        else:
            d.rectangle([x1, y1, x2, y2], outline=ink, width=1, fill=None if faded else G(255))
        tag = str(e["index"])
        tw = d.textlength(tag, font=tagf)
        d.rectangle([x1, y1, x1 + tw + 2, y1 + 9], fill=(220, 30, 30) if paste_real and unl and not faded else ink)
        d.text((x1 + 1, y1 - 1), tag, fill=G(255), font=tagf)
        if paste_real and unl and not faded:
            continue  # the pixels say what it is; its role/state is in the list
        if not label:
            continue
        bh = y2 - y1
        sz = max(7, min(16, int(bh * 0.55)))
        avail = x2 - x1 - 4 - (tw + 3 if bh < 2 * sz + 4 else 0)
        if avail < 10 and len(label) <= 3:  # short label in a narrow box (rating chip "≥4"): draw below the tag
            d.text((x1 + 2, y1 + 9), label, fill=ink, font=font(8))
            continue
        if avail < 10:
            continue
        while sz > 7 and d.textlength(label, font=font(sz)) > avail:  # shrink before cutting
            sz -= 1
        f = font(sz)
        if d.textlength(label, font=f) > avail:
            while label and d.textlength(label + "…", font=f) > avail:
                label = label[:-1]
            label += "…"
        tx = x1 + 2 + (tw + 3 if bh < 2 * sz + 4 else 0)
        ty = y1 + max(0, (bh - sz) // 2) if bh < 2 * sz + 4 else y1 + 10
        d.text((tx, ty), label, fill=ink, font=f)
    return b64_jpeg(im.convert("RGB"), max_side, 85)


def ocr_hybrid_image(case, els, max_side=768):
    """No accessibility tree: OCR text redrawn in its boxes (black on white), detected icons shown
    with their REAL pixels; every element tagged with its [index]."""
    from PIL import Image, ImageDraw, ImageFont
    w, h = case["w"], case["h"]
    sc = max_side / max(w, h)
    im = Image.new("RGB", (int(w * sc), int(h * sc)), "white")
    real = Image.open(case["img"]).convert("RGB").resize(im.size)
    d = ImageDraw.Draw(im)
    fp = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    tagf = ImageFont.truetype(fp, 9)
    for e in els:
        x1, y1, x2, y2 = (int(v * sc) for v in e["bounds"])
        x2, y2 = max(x1 + 3, x2), max(y1 + 3, y2)
        if e.get("text"):
            sz = max(7, min(16, int((y2 - y1) * 0.8)))
            f = ImageFont.truetype(fp, sz)
            while sz > 7 and d.textlength(e["text"], font=f) > (x2 - x1) + 6:
                sz -= 1
                f = ImageFont.truetype(fp, sz)
            d.text((x1, y1), e["text"], fill=(0, 0, 0), font=f)
            tagc = (90, 90, 90)
        else:
            im.paste(real.crop((x1, y1, x2, y2)), (x1, y1))
            d.rectangle([x1, y1, x2 - 1, y2 - 1], outline=(220, 30, 30), width=1)
            tagc = (220, 30, 30)
        tag = str(e["index"])
        tw = d.textlength(tag, font=tagf)
        d.rectangle([x1 - 1, y1 - 9, x1 + tw + 1, y1], fill=tagc)
        d.text((x1, y1 - 10), tag, fill=(255, 255, 255), font=tagf)
    return b64_jpeg(im, max_side, 85)


def compact_to_center(text):
    """Replace the first 'x1,y1,x2,y2' (0-1000 normalized) of each compact line by '@cx,cy'."""
    def one(line):
        return re.sub(r"(-?\d+),(-?\d+),(-?\d+),(-?\d+)",
                      lambda m: f"@{(int(m[1]) + int(m[3])) // 2},{(int(m[2]) + int(m[4])) // 2}", line, count=1)
    return "\n".join(one(l) for l in text.splitlines())


def som_image_v2(path, els, max_side=768):
    """Set-of-marks v2: one index per icon (boxes overlapping > 60% of the smaller one are marked
    once), small tags (~1.4% of the screen width) placed OUTSIDE the box (above, left, below, right),
    never on top of another tag; only when no outside spot is free does the tag go inside."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(path).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im)
    fs = max(12, W // 55)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", fs)
    except OSError:
        font = ImageFont.load_default()
    area = lambda b: max(1, (b[2] - b[0]) * (b[3] - b[1]))
    marks = []
    for e in sorted(els, key=lambda e: area(e["bounds"])):  # small first: the icon itself wins
        b = e["bounds"]
        if any((min(b[2], m[2]) - max(b[0], m[0])) > 0 and (min(b[3], m[3]) - max(b[1], m[1])) > 0 and
               (min(b[2], m[2]) - max(b[0], m[0])) * (min(b[3], m[3]) - max(b[1], m[1])) > 0.6 * min(area(b), area(m))
               for m in [x["bounds"] for x in marks]):
            continue
        marks.append(e)
    lw = max(2, W // 500)
    for e in marks:
        d.rectangle(e["bounds"], outline=(230, 20, 20), width=lw)
    placed = []

    def free(r):
        return r[0] >= 0 and r[1] >= 0 and r[2] <= W and r[3] <= H and not any(
            r[0] < q[2] and q[0] < r[2] and r[1] < q[3] and q[1] < r[3] for q in placed)
    for e in marks:
        x1, y1, x2, y2 = e["bounds"]
        lab = str(e["index"])
        tw, th = int(d.textlength(lab, font=font)) + 6, fs + 4
        spots = [(x1, y1 - th), (x1 - tw, y1), (x1, y2), (x2, y1), (x1, y1)]  # above, left, below, right, inside
        r = next(((sx, sy, sx + tw, sy + th) for sx, sy in spots if free((sx, sy, sx + tw, sy + th))),
                 (x1, y1, x1 + tw, y1 + th))
        placed.append(r)
        d.rectangle(r, fill=(230, 20, 20), outline=(255, 255, 255), width=1)
        d.text((r[0] + 3, r[1] + 1), lab, fill=(255, 255, 255), font=font)
    return b64_jpeg(im, max_side, 80)


def listing(mode, s):
    import copy
    from artemis.utils.visualization import format_minimal_list_with_elements
    if mode in ("ocr_only", "ocr_shot", "ocr_clean"):
        els = ocr_elements(s, clean=mode == "ocr_clean")
        text = "\n".join(f'[{e["index"]}] "{e["text"]}" @{e["center"][0] * 1000 // s["w"]},'
                         f'{e["center"][1] * 1000 // s["h"]}' for e in els)
        return text, els
    if mode in ("ocr2_near", "ocr2_somU", "ocr2_shot", "ocr2_crops", "ocr_hybrid"):
        els = ocr_elements(s)
        for b in detect_icons_v2(s)["boxes"]:
            els.append(dict(index=len(els) + 1, text="", bounds=b, center=[(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]))
        return add_near_hints(center_text(els, s["w"], s["h"]), els, s["h"]), els
    if mode in ("ocr_icons", "ocr_icons_som"):
        els = ocr_elements(s)
        for b in detect_icons(s)["boxes"]:
            els.append(dict(index=len(els) + 1, text="", bounds=b, center=[(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]))
        return center_text(els, s["w"], s["h"]), els
    if mode in ("compact_somU2", "gapfill_somU2"):
        return listing({"compact_somU2": "compact_somU", "gapfill_somU2": "gapfill_somU"}[mode], s)
    if mode == "gapfill_somU":
        # keep the tree list as is; add ONLY what the tree lacks: OCR lines and detected icons that lie
        # outside every tree element, outside picture regions and below the status bar
        text, els, _ = format_minimal_list_with_elements(s["ui"], s["w"], s["h"], compact=True)
        pics = picture_regions(s, els)

        def inside(p, b, m=0):
            return b[0] - m <= p[0] <= b[2] + m and b[1] - m <= p[1] <= b[3] + m
        top = 0.04 * s["h"]
        extra = []
        cands = [(o["text"], o["bounds"]) for o in ocr_elements(s)] + [("", bx) for bx in detect_icons_v2(s)["boxes"]]
        for label, bx in cands:
            c = ((bx[0] + bx[2]) // 2, (bx[1] + bx[3]) // 2)
            if c[1] < top or any(inside(c, e["bounds"], 4) for e in els) or any(inside(c, r) for r in pics):
                continue
            if any(inside(c, x["bounds"]) for x in extra):
                continue
            extra.append(dict(index=len(els) + len(extra) + 1, text=label, bounds=bx, center=list(c)))
        for e in extra:
            b = e["bounds"]
            tag = f'ocr "{e["text"]}"' if e["text"] else "(icon)"
            text += (f"\n[{e['index']}] {tag} {b[0] * 1000 // s['w']},{b[1] * 1000 // s['h']},"
                     f"{b[2] * 1000 // s['w']},{b[3] * 1000 // s['h']}")
        els = els + extra
        return add_near_hints(text, els, s["h"]), els
    if mode in ("xml_ocr_somU", "fused_icons_somU"):
        from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
        fused = fuse_ocr_with_xml(copy.deepcopy(s["ui"]), ocr_lines(s, True)["lines"], xml_first=True)
        text, els, _ = format_minimal_list_with_elements(fused, s["w"], s["h"], compact=True)
        if mode == "fused_icons_somU":  # icons the tree does not have (outside every element, not in a picture)
            pics = picture_regions(s, els)
            def inside(p, b, m=0):
                return b[0] - m <= p[0] <= b[2] + m and b[1] - m <= p[1] <= b[3] + m
            extra = []
            for bx in detect_icons_v2(s)["boxes"]:
                c = ((bx[0] + bx[2]) // 2, (bx[1] + bx[3]) // 2)
                if any(inside(c, e["bounds"], 4) for e in els) or any(inside(c, r) for r in pics):
                    continue
                n = len(els) + len(extra) + 1
                extra.append(dict(index=n, text="", bounds=bx, center=list(c)))
            for e in extra:
                b = e["bounds"]
                text += (f"\n[{e['index']}] (icon) {b[0] * 1000 // s['w']},{b[1] * 1000 // s['h']},"
                         f"{b[2] * 1000 // s['w']},{b[3] * 1000 // s['h']}")
            els = els + extra
        return add_near_hints(text, els, s["h"]), els
    if mode in ("xml_ocr", "center_ocr"):
        from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
        fused = fuse_ocr_with_xml(copy.deepcopy(s["ui"]), ocr_lines(s, True)["lines"], xml_first=True)
        text, els, _ = format_minimal_list_with_elements(fused, s["w"], s["h"], compact=True)
        if mode == "center_ocr":
            text = center_text(els, s["w"], s["h"], icon_label='""')
        return text, els
    text, els, _ = format_minimal_list_with_elements(s["ui"], s["w"], s["h"], compact=mode != "list_full")
    if mode in ("center2_near", "somU_near", "wire_near", "wire2_near", "wire_hybrid"):
        text = add_near_hints(text, els, s["h"])
    if mode in ("center2", "center2_shot", "center2_somU", "center2_near"):
        text = compact_to_center(text)
    if mode in ("list_center", "center_som", "center_shot"):
        text = "\n".join(f'[{e["index"]}] "{e.get("text", "")}" @{e["center"][0] * 1000 // s["w"]},'
                         f'{e["center"][1] * 1000 // s["h"]}' for e in els)
    elif mode == "list_label":
        text = "\n".join(f'[{e["index"]}] "{e.get("text", "")}"' for e in els)
    return text, els


def decide_prompt(text, instr, desc=None, ocr=False, mode=""):
    head = f"Screen description: {desc}\n\n" if desc else ""
    if mode in ("ocr_icons", "ocr_icons_som", "ocr2_near", "ocr2_somU", "ocr2_shot", "ocr2_crops", "ocr_hybrid"):
        what = ("Elements found on the screen by OCR and icon detection ([index] text @x,y center in 0-1000 "
                "normalized coords; (icon) = a graphic without text)")
    elif ocr:
        what = ("Text found on the screen by OCR ([index] text @x,y center in 0-1000 normalized coords; "
                "icons without text are not listed)")
    else:
        what = "UI elements on the Android screen"
    if mode == "wire_hybrid":
        what += (". The image is a black/white wireframe of the screen (text redrawn); graphics, pictures and "
                 "elements without a text label show the REAL screen pixels, boxed in red with their [index]")
    elif mode == "ocr_hybrid":
        what += (". The image redraws the OCR text in black on white; each detected icon shows the REAL screen "
                 "pixels, boxed in red with its [index]")
    elif mode in ("compact_crops", "ocr2_crops"):
        what += (". The image is NOT the full screen: it is a sheet of small cut-outs of the elements that have "
                 "no text label, each tagged with its [index]")
    elif mode == "ocr2_somU":
        what += ". The image shows the screen; each detected icon (graphic without text) has its box and [index] drawn in red"
    elif mode in ("center_som", "ocr_icons_som"):
        what += ". The image shows the screen with each element's box and [index] drawn in red"
    elif mode in ("compact_somU", "center2_somU", "compact_somU2", "gapfill_somU2"):
        what += ". The image shows the screen; elements without a text label have their box and [index] drawn in red"
    elif mode in ("wire_text", "wire_near", "wire2_near"):
        what += (". The image is a black/white wireframe of the screen: each element's box with its [index] "
                 "and label (pictures shown as [image])")
    return (f"{head}{what}:\n{text}\n\nYou are operating this phone. "
            f"Your intent for this step:\n{instr}\n\nWhich element should be tapped? "
            "Reply ONLY JSON: {\"index\": N}")


def label_match(label, accept):
    lab = norm(label)
    return any(lab == tt or (len(tt) > 3 and len(lab) > 3 and (tt in lab or lab in tt)) for tt in map(norm, accept))


def grade(reply, els, case):
    """Correct if the chosen element carries an accepted label, or is an unlabeled control
    (radio / checkbox / toggle) on the same row as an accepted label. When the accepted label
    occurs more than once, case["near_y"] (screen px) picks the intended row."""
    m = re.search(r'"?index"?\s*:\s*\[?\s*(\d+)', reply or "")
    if not m:
        return False, None, ""
    idx = int(m.group(1))
    e = next((e for e in els if e["index"] == idx), None)
    if not e:
        return False, idx, ""
    targets = [t for t in els if label_match(t.get("text"), case["accept"])]
    if case.get("near_y") is not None and targets:
        best = min(abs(t["center"][1] - case["near_y"]) for t in targets)
        targets = [t for t in targets if abs(t["center"][1] - case["near_y"]) == best]
    lab = norm(e.get("text"))
    if any(t["index"] == idx for t in targets):
        return True, idx, lab
    same_row = not lab and any(t["bounds"][1] <= e["center"][1] <= t["bounds"][3] for t in targets)
    return same_row, idx, lab


def evaluate(reply, mode, case, els=None):
    """Grade one stored answer for any mode. OCR-only lists are graded against the accessibility
    tree: the chosen OCR box is correct if its center falls inside an accepted element (or, for a
    matching text, on that element's row)."""
    if els is None:
        els = listing(mode, case)[1]
    if case.get("accept_box"):
        m = re.search(r'"?index"?\s*:\s*\[?\s*(\d+)', reply or "")
        if not m:
            return False, None, ""
        idx = int(m.group(1))
        e = next((e for e in els if e["index"] == idx), None)
        if not e:
            return False, idx, ""
        cx, cy = e["center"]
        ok = any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in case["accept_box"])
        return ok, idx, norm(e.get("text"))
    if mode not in NO_TREE_MODES:
        return grade(reply, els, case)
    m = re.search(r'"?index"?\s*:\s*\[?\s*(\d+)', reply or "")
    if not m:
        return False, None, ""
    idx = int(m.group(1))
    e = next((e for e in els if e["index"] == idx), None)
    if not e:
        return False, idx, ""
    xml_els = listing("list_compact", case)[1]
    targets = [t for t in xml_els if label_match(t.get("text"), case["accept"])]
    if case.get("near_y") is not None and targets:
        best = min(abs(t["center"][1] - case["near_y"]) for t in targets)
        targets = [t for t in targets if abs(t["center"][1] - case["near_y"]) == best]
    cx, cy = e["center"]
    inside = any(t["bounds"][0] <= cx <= t["bounds"][2] and t["bounds"][1] <= cy <= t["bounds"][3] for t in targets)
    row = label_match(e["text"], case["accept"]) and any(t["bounds"][1] <= cy <= t["bounds"][3] for t in targets)
    return inside or row, idx, norm(e["text"])


# ------------------------------------------------------------------ backends
def post(url, body, headers, retries=6):
    for attempt in range(retries):
        t = time.time()
        try:
            req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
            return json.load(urllib.request.urlopen(req, timeout=300)), time.time() - t
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503):
                print(f"  HTTP {e.code}, backoff {15 * (attempt + 1)}s", flush=True)
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")
        except (urllib.error.URLError, ConnectionError) as e:
            print(f"  connection error {e!r}, retry in 20s", flush=True)
            time.sleep(20)
    raise RuntimeError("gave up after retries")


def call_ollama(args, prompt, image=None, max_out=20):
    msg = {"role": "user", "content": prompt, **({"images": [image]} if image else {})}
    nonce = {"role": "system", "content": f"run {random.getrandbits(48):012x}"}  # defeat prefix cache
    r, wall = post(f"{args.ollama_url}/api/chat",
                   {"model": args.model, "stream": False, "messages": [nonce, msg], "keep_alive": "1m",
                    **({"think": False} if args.no_think else {}),
                    "options": {"temperature": 0, "num_ctx": args.num_ctx, "num_predict": max_out}}, {})
    out = dict(reply=r["message"]["content"], wall=wall, in_tok=r.get("prompt_eval_count", 0),
               out_tok=r.get("eval_count", 0), prefill=r.get("prompt_eval_duration", 0) / 1e9,
               gen=r.get("eval_duration", 0) / 1e9, load=r.get("load_duration", 0) / 1e9,
               server=r.get("total_duration", 0) / 1e9)
    if out["in_tok"] >= args.num_ctx - 32:
        out["truncated"] = True
        print(f"  WARNING: prompt filled num_ctx={args.num_ctx} - raise --num-ctx", flush=True)
    return out


_gemini_keys_cache = []
_gemini_disabled_keys = set()
_gemini_key_idx = 0


def get_gemini_keys():
    global _gemini_keys_cache
    if _gemini_keys_cache:
        return _gemini_keys_cache
    raw = os.environ.get("GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", ""))
    if not raw:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        for p in [os.path.join(repo_root, ".env"), os.path.join(repo_root, "artemis", ".env")]:
            if os.path.exists(p):
                for line in open(p):
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEYS="):
                        raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
                    elif line.startswith("GEMINI_API_KEY=") and not raw:
                        raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                if raw:
                    break
    _gemini_keys_cache = [k.strip() for k in raw.split(",") if k.strip()]
    if not _gemini_keys_cache:
        raise RuntimeError("No GEMINI_API_KEY or GEMINI_API_KEYS found in environment or .env file.")
    return _gemini_keys_cache


def get_active_gemini_keys():
    all_keys = get_gemini_keys()
    active = [k for k in all_keys if k not in _gemini_disabled_keys]
    if not active:
        print(f"  ⚠️ Tất cả {len(all_keys)} Gemini API keys đều đã bị limit! Đang đợi 60s để reset hạn mức...", flush=True)
        time.sleep(60)
        _gemini_disabled_keys.clear()
        active = all_keys
    return active


def get_next_gemini_key():
    global _gemini_key_idx
    active = get_active_gemini_keys()
    key = active[_gemini_key_idx % len(active)]
    _gemini_key_idx += 1
    return key


def disable_gemini_key(key):
    _gemini_disabled_keys.add(key)
    remaining = [k for k in get_gemini_keys() if k not in _gemini_disabled_keys]
    key_mask = f"...{key[-6:]}" if len(key) >= 6 else key
    print(f"  🚫 [Gemini key {key_mask}] Bị Limit 429! Đã ghi nhớ và LOẠI BỎ KHỎI DANH SÁCH (còn lại {len(remaining)} keys hoạt động).", flush=True)


def call_gemini(args, prompt, image=None, max_out=40):
    parts = [{"text": prompt}]
    if image:
        parts.insert(0, {"inline_data": {"mime_type": "image/jpeg", "data": image}})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": max_out,
                             "thinkingConfig": {"thinkingLevel": "minimal"}}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{args.model}:generateContent"
    keys = get_gemini_keys()
    retries = max(6, len(keys) * 2)
    last_err = None

    for attempt in range(retries):
        key = get_next_gemini_key()
        headers = {"x-goog-api-key": key}
        key_mask = f"...{key[-6:]}" if len(key) >= 6 else key
        t0 = time.time()
        try:
            req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
            r = json.load(urllib.request.urlopen(req, timeout=300))
            wall = time.time() - t0
            u = r.get("usageMetadata", {})
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"].get("parts", []))
            return dict(reply=txt, wall=wall, in_tok=u.get("promptTokenCount", 0),
                        out_tok=u.get("candidatesTokenCount", 0), think_tok=u.get("thoughtsTokenCount", 0))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                disable_gemini_key(key)
                time.sleep(0.5)
                continue
            elif e.code in (500, 503):
                print(f"  [Gemini key {key_mask}] HTTP {e.code}, backoff {15 * (attempt + 1)}s", flush=True)
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")
        except (urllib.error.URLError, ConnectionError) as e:
            last_err = e
            print(f"  [Gemini key {key_mask}] Connection error {e!r}, retry in 15s", flush=True)
            time.sleep(15)
    raise RuntimeError(f"Gave up after {retries} retries: {last_err}")


def mem_available_gb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) / 1024 / 1024
    return 99.0


def model_digest(args):
    if args.backend != "ollama":
        return args.model
    try:
        tags = json.load(urllib.request.urlopen(f"{args.ollama_url}/api/tags", timeout=10))
        return next((f"{m['name']}@{m['digest'][:12]}" for m in tags["models"] if m["name"] == args.model), args.model)
    except Exception:
        return args.model


# ------------------------------------------------------------------ run / summary
def run(args):
    from PIL import Image
    call = call_ollama if args.backend == "ollama" else call_gemini
    # RAM guard / rests only matter when the model runs on this laptop
    local_model = args.backend == "ollama" and re.search(r"(127\.0\.0\.1|localhost):11434", args.ollama_url)
    pause = args.pause if args.pause is not None else (2.5 if args.backend == "ollama" else 4.5)
    modes = args.modes.split(",")
    steps = load_cases(args.steps)
    model_id = model_digest(args)
    done = {(r["step"], r["mode"]) for r in map(json.loads, open(args.out))} if os.path.exists(args.out) else set()
    for extra_file in args.also_done:  # results kept in another file (e.g. the Colab run) count as done
        done |= {(r["step"], r["mode"]) for r in map(json.loads, open(extra_file))}
    todo = [s for s in steps if any((s["id"], m) not in done for m in modes)]
    print(f"{len(steps)} steps selected, {len(todo)} with work left; model {model_id}", flush=True)
    with open(args.out, "a") as f:
        for bi in range(0, len(todo), args.batch):
            if bi and local_model:
                print(f"-- rest {args.rest}s (RAM available {mem_available_gb():.1f} GB)", flush=True)
                time.sleep(args.rest)
            for s in todo[bi: bi + args.batch]:
                instr = s["instr"]
                shot = Image.open(s["img"]).convert("RGB")
                for mode in modes:
                    if (s["id"], mode) in done:
                        continue
                    if local_model and mem_available_gb() < 3:
                        print(f"STOP: only {mem_available_gb():.1f} GB RAM available", flush=True)
                        return
                    text, els = listing(mode, s)
                    extra = {}
                    if mode in OCR_MODES:
                        extra["ocr_ms"] = ocr_lines(s, mode in ("xml_ocr", "center_ocr", "xml_ocr_somU", "fused_icons_somU"))["ms"]
                    if mode in ("fused_icons_somU", "gapfill_somU", "gapfill_somU2"):
                        extra["ocr_ms"] += detect_icons_v2(s)["ms"]
                    if mode in ("ocr_icons", "ocr_icons_som"):
                        extra["ocr_ms"] += detect_icons(s)["ms"]
                    if mode in ("ocr2_near", "ocr2_somU", "ocr2_shot", "ocr2_crops", "ocr_hybrid"):
                        extra["ocr_ms"] += detect_icons_v2(s)["ms"]
                    if mode == "2stage":
                        d = call(args, DESCRIBE, b64_jpeg(shot, 768), 150)
                        time.sleep(pause)
                        r = call(args, decide_prompt(text, instr, d["reply"].strip()))
                        extra = dict(s1_wall=d["wall"], s1_in=d["in_tok"], s1_out=d["out_tok"])
                        r["wall"] += d["wall"]
                    else:
                        kind = IMAGE_MODES.get(mode)
                        img = (b64_jpeg(shot, 768) if kind == "shot"
                               else som_image(s["img"], els) if kind == "som"
                               else som_image(s["img"], [e for e in els if not e.get("text")]) if kind == "somU"
                               else wire_text_image(s) if kind == "wiretext"
                               else crop_sheet(s["img"], [e for e in els if not e.get("text")]) if kind == "crops"
                               else som_image_v2(s["img"], [e for e in els if not e.get("text")]) if kind == "somU2"
                               else wire_text_image_v2(s) if kind == "wire2"
                               else wire_text_image_v2(s, paste_real=True) if kind == "hybrid"
                               else ocr_hybrid_image(s, els) if kind == "ocrhybrid" else None)
                        r = call(args, decide_prompt(text, instr, ocr=mode in ("ocr_only", "ocr_shot", "ocr_clean"), mode=mode), img)
                    ok, idx, lab = evaluate(r["reply"], mode, s, els)
                    f.write(json.dumps(dict(step=s["id"], mode=mode, model=model_id, ok=ok, idx=idx, picked=lab,
                                            accept=s["accept"], n_els=len(els), **r, **extra),
                                       ensure_ascii=False) + "\n")
                    f.flush()
                    print(f"  {mode:13s} {'OK ' if ok else 'BAD'} {r['wall']:.2f}s in={r['in_tok']} "
                          f"target={(s['accept'] or [s['id']])[0][:22]!r} picked={lab[:22]!r}", flush=True)
                    time.sleep(pause)


FAILURES = False
SHOW_MODES = None


def summary(paths):
    """Re-grades every stored answer against the CURRENT cases.jsonl (edit answers, no model calls)."""
    rows = [json.loads(l) for p in paths for l in open(p)]
    cases = {c["id"]: c for c in load_cases()}
    lists = {}
    for r in rows:
        c = cases.get(r["step"])
        if c is None:
            r["ok"] = False
            continue
        lm = r["mode"] if r["mode"] in ("list_full",) + OCR_MODES else "list_compact"
        if (r["step"], lm) not in lists:
            lists[(r["step"], lm)] = listing(lm, c)[1]
        r["ok"] = evaluate(r["reply"], r["mode"], c, lists[(r["step"], lm)])[0]
    rows = [r for r in rows if r["step"] in cases]
    for model in sorted({r["model"] for r in rows}):
        R = [r for r in rows if r["model"] == model]
        modes = [m for m in (SHOW_MODES or ALL_MODES) if any(r["mode"] == m for r in R)]
        by = {}
        for r in R:
            by.setdefault(r["step"], {})[r["mode"]] = r
        common = [v for v in by.values() if all(m in v for m in modes)]
        print(f"\n== {model}: {len(common)} steps with all of {modes}")
        print(f"{'mode':13s} {'ok':>6s} {'wall_med':>8s} {'p90':>6s} {'prefill':>7s} {'gen':>5s} "
              f"{'ocr':>5s} {'total':>6s} {'in_tok':>7s}")
        med = lambda xs: statistics.median(xs) if xs else 0.0
        for m in modes:
            X = [v[m] for v in common]
            if not X:
                continue
            W = sorted(x["wall"] for x in X)
            tok = statistics.median(x["in_tok"] + x.get("s1_in", 0) for x in X)
            ocr = med([x.get("ocr_ms", 0) / 1000 for x in X])
            total = med([x["wall"] + x.get("ocr_ms", 0) / 1000 for x in X])
            print(f"{m:13s} {sum(x['ok'] for x in X):2d}/{len(X):<3d} {statistics.median(W):8.2f} "
                  f"{W[int(0.9 * (len(W) - 1))]:6.2f} {med([x.get('prefill', 0) for x in X]):7.2f} "
                  f"{med([x.get('gen', 0) for x in X]):5.2f} {ocr:5.2f} {total:6.2f} {tok:7.0f}")
        # accuracy per tag
        tags = sorted({t for v in common for t in cases[next(iter(v.values()))["step"]]["tags"]})
        print(f"\naccuracy by case tag  ({', '.join(f'{t}={sum(t in cases[next(iter(v.values()))['step']]['tags'] for v in common)}' for t in tags)})")
        print(f"{'mode':13s} " + " ".join(f"{t[:9]:>9s}" for t in tags))
        for m in modes:
            cells = []
            for t in tags:
                X = [v[m] for v in common if t in cases[v[m]["step"]]["tags"]]
                cells.append(f"{sum(x['ok'] for x in X):>4d}/{len(X):<4d}")
            print(f"{m:13s} " + " ".join(f"{c:>9s}" for c in cells))
        if FAILURES:
            print("\nper case (✓ correct / ✗ wrong):")
            print(f"{'case':22s} " + " ".join(f"{m[:6]:>6s}" for m in modes))
            for v in sorted(common, key=lambda v: next(iter(v.values()))["step"]):
                sid = next(iter(v.values()))["step"]
                print(f"{sid:22s} " + " ".join(f"{'✓' if v[m]['ok'] else '✗':>6s}" for m in modes))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["ollama", "gemini"], default="ollama")
    ap.add_argument("--model", default=None)
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--out")
    ap.add_argument("--steps", type=int, default=None, help="only the first N cases")
    ap.add_argument("--modes", default=",".join(CORE_MODES),
                    help="comma list; default = CORE_MODES. 'all' = every mode (incl. retired ones)")
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--rest", type=int, default=40)
    ap.add_argument("--pause", type=float, default=None)
    ap.add_argument("--no-think", action="store_true",
                    help="Ollama: disable the thinking phase (needed for thinking variants such as qwen3-vl:4b)")
    ap.add_argument("--also-done", nargs="*", default=[], help="other result files whose (case, mode) pairs are not re-run")
    ap.add_argument("--num-ctx", type=int, default=4096, help="Ollama context; raise if a WARNING says truncated")
    ap.add_argument("--summary", nargs="*")
    ap.add_argument("--failures", action="store_true", help="with --summary: print the per-case matrix")
    ap.add_argument("--all", action="store_true", help="with --summary: include retired modes")
    args = ap.parse_args()
    if args.summary:
        global FAILURES, SHOW_MODES
        FAILURES = args.failures
        SHOW_MODES = ALL_MODES if args.all else CORE_MODES
        return summary(args.summary)
    if args.modes == "all":
        args.modes = ",".join(ALL_MODES)
    args.model = args.model or ("qwen3-vl:4b-16k" if args.backend == "ollama" else "gemini-3.5-flash-lite")
    if args.backend == "gemini":
        args.rest = 0
    run(args)


if __name__ == "__main__":
    main()
