"""Payload grounding benchmark: which input lets the model pick the right element fastest?

Self-contained: reads ./cases.jsonl and ./data/<case>/{screen.jpg,ui_tree.json} only
(build them with build_dataset.py). Each case = one real recorded tap from an ARTEMIS
run: the screen, its UI tree, an instruction (the agent's own reasoning, indices and
coordinates stripped) and the accepted target label(s). Edit cases.jsonl freely:
change "instruction", add alternative labels to "accept", or delete lines.

Modes (all answer {"index": N} over the Flash element list):
  shot768       screenshot (768px JPEG q75) + Flash compact list
  sketch        512px grayscale wireframe with index numbers + compact list
  2stage        model describes screenshot (<=60 words), then text-only decide with compact list
  list_full     Flash list compact=False, no image
  list_compact  Flash compact list (current format), no image
  list_center   [i] "label" @cx,cy (0-1000 normalized center), no image
  list_label    [i] "label", no image
  ocr_only      OCR text boxes only ([i] "text" @cx,cy) - no accessibility tree, no image (desktop-like)
  ocr_shot      OCR text boxes + screenshot 768 (no accessibility tree)
  xml_ocr       Flash compact list after fusing OCR into the tree (Flash ocr.mode="always"), no image
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

ALL_MODES = ["shot768", "sketch", "2stage", "list_full", "list_compact", "list_center", "list_label",
             "ocr_only", "ocr_shot", "xml_ocr"]
OCR_MODES = ("ocr_only", "ocr_shot", "xml_ocr")
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
        d = os.path.join(HERE, "data", c["id"])
        cases.append(dict(id=c["id"], instr=c["instruction"], accept=c["accept"], near_y=c.get("near_y"),
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


def sketch(els, w, h, max_side=512):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    s = max_side / max(w, h)
    im = Image.new("L", (int(w * s), int(h * s)), 255)
    d = ImageDraw.Draw(im)
    for e in els:
        x1, y1, x2, y2 = (int(v * s) for v in e["bounds"])
        d.rectangle([x1, y1, max(x1 + 1, x2 - 1), max(y1 + 1, y2 - 1)], outline=0, width=1)
    for e in els:
        x1, y1 = (int(v * s) for v in e["bounds"][:2])
        lab = str(e["index"])
        d.rectangle([x1, y1, x1 + d.textlength(lab, font=font) + 3, y1 + 14], fill=0)
        d.text((x1 + 2, y1), lab, fill=255, font=font)
    return b64_jpeg(im, max_side, 85)


def ocr_lines(case, with_xml):
    """Flash's local OCR on the case screenshot; cached per case (the time is measured once).

    with_xml=False: no tree at all (desktop-like), the whole frame is read.
    with_xml=True: the tree is passed, so the status bar is cropped like in Flash."""
    path = os.path.join(HERE, "data", case["id"], "ocr_xml.json" if with_xml else "ocr_full.json")
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


def ocr_elements(case):
    els = []
    for i, ln in enumerate(ocr_lines(case, False)["lines"], 1):
        xs = [p["x"] for p in ln["position"]]
        ys = [p["y"] for p in ln["position"]]
        b = [min(xs), min(ys), max(xs), max(ys)]
        els.append(dict(index=i, text=ln["text"], bounds=b, center=[(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]))
    return els


def listing(mode, s):
    import copy
    from artemis.utils.visualization import format_minimal_list_with_elements
    if mode in ("ocr_only", "ocr_shot"):
        els = ocr_elements(s)
        text = "\n".join(f'[{e["index"]}] "{e["text"]}" @{e["center"][0] * 1000 // s["w"]},'
                         f'{e["center"][1] * 1000 // s["h"]}' for e in els)
        return text, els
    if mode == "xml_ocr":
        from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
        fused = fuse_ocr_with_xml(copy.deepcopy(s["ui"]), ocr_lines(s, True)["lines"], xml_first=True)
        text, els, _ = format_minimal_list_with_elements(fused, s["w"], s["h"], compact=True)
        return text, els
    text, els, _ = format_minimal_list_with_elements(s["ui"], s["w"], s["h"], compact=mode != "list_full")
    if mode == "list_center":
        text = "\n".join(f'[{e["index"]}] "{e.get("text", "")}" @{e["center"][0] * 1000 // s["w"]},'
                         f'{e["center"][1] * 1000 // s["h"]}' for e in els)
    elif mode == "list_label":
        text = "\n".join(f'[{e["index"]}] "{e.get("text", "")}"' for e in els)
    return text, els


def decide_prompt(text, instr, desc=None, ocr=False):
    head = f"Screen description: {desc}\n\n" if desc else ""
    what = ("Text found on the screen by OCR ([index] text @x,y center in 0-1000 normalized coords; "
            "icons without text are not listed)" if ocr else "UI elements on the Android screen")
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
    m = re.search(r'"?index"?\s*:\s*(\d+)', reply or "")
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
    if mode not in ("ocr_only", "ocr_shot"):
        return grade(reply, els, case)
    m = re.search(r'"?index"?\s*:\s*(\d+)', reply or "")
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
                    "options": {"temperature": 0, "num_ctx": args.num_ctx, "num_predict": max_out}}, {})
    out = dict(reply=r["message"]["content"], wall=wall, in_tok=r.get("prompt_eval_count", 0),
               out_tok=r.get("eval_count", 0), prefill=r.get("prompt_eval_duration", 0) / 1e9,
               gen=r.get("eval_duration", 0) / 1e9, load=r.get("load_duration", 0) / 1e9,
               server=r.get("total_duration", 0) / 1e9)
    if out["in_tok"] >= args.num_ctx - 32:
        out["truncated"] = True
        print(f"  WARNING: prompt filled num_ctx={args.num_ctx} - raise --num-ctx", flush=True)
    return out


def call_gemini(args, prompt, image=None, max_out=40):
    parts = [{"text": prompt}]
    if image:
        parts.insert(0, {"inline_data": {"mime_type": "image/jpeg", "data": image}})
    r, wall = post(f"https://generativelanguage.googleapis.com/v1beta/models/{args.model}:generateContent",
                   {"contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {"temperature": 0, "maxOutputTokens": max_out,
                                         "thinkingConfig": {"thinkingLevel": "minimal"}}},
                   {"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    u = r.get("usageMetadata", {})
    txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"].get("parts", []))
    return dict(reply=txt, wall=wall, in_tok=u.get("promptTokenCount", 0),
                out_tok=u.get("candidatesTokenCount", 0), think_tok=u.get("thoughtsTokenCount", 0))


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
                        extra["ocr_ms"] = ocr_lines(s, mode == "xml_ocr")["ms"]
                    if mode == "2stage":
                        d = call(args, DESCRIBE, b64_jpeg(shot, 768), 150)
                        time.sleep(pause)
                        r = call(args, decide_prompt(text, instr, d["reply"].strip()))
                        extra = dict(s1_wall=d["wall"], s1_in=d["in_tok"], s1_out=d["out_tok"])
                        r["wall"] += d["wall"]
                    else:
                        img = (b64_jpeg(shot, 768) if mode in ("shot768", "ocr_shot")
                               else sketch(els, s["w"], s["h"]) if mode == "sketch" else None)
                        r = call(args, decide_prompt(text, instr, ocr=mode in ("ocr_only", "ocr_shot")), img)
                    ok, idx, lab = evaluate(r["reply"], mode, s, els)
                    f.write(json.dumps(dict(step=s["id"], mode=mode, model=model_id, ok=ok, idx=idx, picked=lab,
                                            accept=s["accept"], n_els=len(els), **r, **extra),
                                       ensure_ascii=False) + "\n")
                    f.flush()
                    print(f"  {mode:13s} {'OK ' if ok else 'BAD'} {r['wall']:.2f}s in={r['in_tok']} "
                          f"target={s['accept'][0][:22]!r} picked={lab[:22]!r}", flush=True)
                    time.sleep(pause)


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
        lm = r["mode"] if r["mode"] in ("list_full", "ocr_only", "ocr_shot", "xml_ocr") else "list_compact"
        if (r["step"], lm) not in lists:
            lists[(r["step"], lm)] = listing(lm, c)[1]
        r["ok"] = evaluate(r["reply"], r["mode"], c, lists[(r["step"], lm)])[0]
    rows = [r for r in rows if r["step"] in cases]
    for model in sorted({r["model"] for r in rows}):
        R = [r for r in rows if r["model"] == model]
        modes = [m for m in ALL_MODES if any(r["mode"] == m for r in R)]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["ollama", "gemini"], default="ollama")
    ap.add_argument("--model", default=None)
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--out")
    ap.add_argument("--steps", type=int, default=None, help="only the first N cases")
    ap.add_argument("--modes", default=",".join(ALL_MODES))
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--rest", type=int, default=40)
    ap.add_argument("--pause", type=float, default=None)
    ap.add_argument("--num-ctx", type=int, default=4096, help="Ollama context; raise if a WARNING says truncated")
    ap.add_argument("--summary", nargs="*")
    args = ap.parse_args()
    if args.summary:
        return summary(args.summary)
    args.model = args.model or ("qwen3-vl:4b-16k" if args.backend == "ollama" else "gemini-3.5-flash-lite")
    if args.backend == "gemini":
        args.rest = 0
    run(args)


if __name__ == "__main__":
    main()
