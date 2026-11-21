"""Benchmark qwen3-vl:4b-instruct through Ollama /v1 endpoint with real ARTEMIS prompt + 16 tools."""
import base64, json, sqlite3, time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import httpx
from PIL import Image
from artemis.agents.flash.runner import FlashRunner
from artemis.utils.visualization import format_minimal_list_with_elements

T = Path("traces")
db = sqlite3.connect(T / "data_engine.db")
screens, seen = [], set()
for name, ui in db.execute("select image_name, ui_tree from images where ui_tree is not null order by rowid desc limit 300"):
    p = T / "images" / f"{name}.jpg"
    if not p.exists():
        continue
    tree = json.loads(ui)
    if not isinstance(tree, list) or len(tree) < 15:
        continue
    img = Image.open(p)
    W, H = img.size
    txt, els, _ = format_minimal_list_with_elements([dict(n) for n in tree], W, H, compact=True)
    if txt[:300] in seen:
        continue
    seen.add(txt[:300])
    screens.append((name[:8], img, W, H, txt, els))
    if len(screens) >= 8:
        break

print(f"Loaded {len(screens)} screens from {T / 'data_engine.db'}")

GOAL = "Open Grab Food, search 'phở bò' and open the restaurant with the most reviews (do not order)."
r = FlashRunner.__new__(FlashRunner)
r.ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir="/tmp"), execution_setup=None, actuator=None)
r.goal = GOAL
r.compact_list = True
tools = r._get_tools()
SYS = r._render_system_prompt(tools)
TOOLS = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]

print("Starting benchmark with qwen3-vl:4b-instruct...\n" + "="*80)

for idx, (sid, img, W, H, txt, els) in enumerate(screens, 1):
    im = img.copy()
    im.thumbnail((768, 768))
    b = BytesIO()
    im.convert("RGB").save(b, "JPEG", quality=75)
    
    body = {
        "model": "qwen3-vl:4b-16k",
        "temperature": 0,
        "options": {"num_ctx": 16384},
        "tools": TOOLS,
        "messages": [
            {"role": "system", "content": SYS},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()}},
                    {"type": "text", "text": "# CURRENT OBSERVATION [T+00:40]\n--- Visible UI Elements ---\n" + txt}
                ]
            }
        ]
    }
    
    t0 = time.perf_counter()
    try:
        res = httpx.post("http://127.0.0.1:11434/v1/chat/completions", json=body, timeout=120)
        d = res.json()
    except Exception as e:
        print(f"Screen {idx} ({sid}): HTTP/Timeout Error: {e}")
        continue
    dt = time.perf_counter() - t0
    
    if "error" in d:
        print(f"Screen {idx} ({sid}): ERR {str(d['error'])[:150]}")
        continue
        
    choice = d.get("choices", [{}])[0]
    m = choice.get("message", {})
    calls = m.get("tool_calls") or []
    tokens = d.get("usage", {}).get("prompt_tokens", 0)
    
    out = []
    for c in calls:
        func = c.get("function", {})
        fname = func.get("name", "unknown")
        try:
            args = json.loads(func.get("arguments") or "{}")
        except Exception:
            args = func.get("arguments")
        
        tgt = args.get("target") if isinstance(args, dict) else None
        what = ""
        if isinstance(tgt, int):
            e = next((e for e in els if e.get("index") == tgt), None)
            what = f"-> [index {tgt}] {e['text'][:30]!r}" if e else f"-> [index {tgt}] NOT IN LIST"
        elif isinstance(tgt, list) and len(tgt) == 2:
            x, y = tgt[0] * W / 1000, tgt[1] * H / 1000
            hit = sorted([e for e in els if e.get('bounds') and e['bounds'][0] <= x <= e['bounds'][2] and e['bounds'][1] <= y <= e['bounds'][3]], 
                         key=lambda e: (e['bounds'][2] - e['bounds'][0]) * (e['bounds'][3] - e['bounds'][1]))
            what = f"-> coords [{tgt[0]},{tgt[1]}] hit [{hit[0]['index']}] {hit[0]['text'][:30]!r}" if hit else f"-> coords [{tgt[0]},{tgt[1]}] hit NOTHING"
        elif isinstance(args, dict) and "point" in args:
            pt = args["point"]
            if isinstance(pt, list) and len(pt) == 2:
                x, y = pt[0] * W / 1000, pt[1] * H / 1000
                hit = sorted([e for e in els if e.get('bounds') and e['bounds'][0] <= x <= e['bounds'][2] and e['bounds'][1] <= y <= e['bounds'][3]], 
                             key=lambda e: (e['bounds'][2] - e['bounds'][0]) * (e['bounds'][3] - e['bounds'][1]))
                what = f"-> point [{pt[0]},{pt[1]}] hit [{hit[0]['index']}] {hit[0]['text'][:30]!r}" if hit else f"-> point [{pt[0]},{pt[1]}] hit NOTHING"
                
        out.append(f"{fname}({json.dumps(args, ensure_ascii=False)[:70]}) {what}")
        
    thought = (m.get("content") or "")[:100].replace("\n", " ")
    call_str = " | ".join(out) if out else f"NO TOOL CALL: Text='{thought}'"
    print(f"Screen {idx} ({sid}) [{dt:4.1f}s | in={tokens}]: {call_str}")

