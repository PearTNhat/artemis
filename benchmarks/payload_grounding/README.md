# Payload grounding benchmark

Câu hỏi: **gửi gì cho model thì chọn đúng phần tử để bấm nhanh nhất?**
(ảnh thật, ảnh phác thảo, mô tả 2 tầng, hay chỉ danh sách bounding box dạng chữ)

Bộ test **tự chứa**: chỉ đọc `cases.jsonl` + `data/`, không cần `traces/`. Xoá trace,
đổi máy, đổi điện thoại đều không ảnh hưởng.

## Cấu trúc

```
benchmarks/payload_grounding/
├── cases.jsonl          # 18 bài test — SỬA TRỰC TIẾP được
├── data/case_XX/
│   ├── screen.jpg       # ảnh màn hình 1280×2772 (đã che dữ liệu cá nhân)
│   └── ui_tree.json     # cây UI (XML accessibility) cùng thời điểm
├── bench.py             # chạy test + tổng hợp
├── build_dataset.py     # dựng lại data/ + cases.jsonl từ traces (khi cần thêm bài)
└── results/*.jsonl      # kết quả các lần chạy (mỗi dòng = 1 lần gọi model)
```

Mỗi bài là **một lần bấm thật** lấy từ trace ARTEMIS (Clock, Shopee, Grab, YouTube,
Play Store, Chrome…). Một dòng trong `cases.jsonl`:

```json
{"id": "case_00", "instruction": "We have navigated to the Alarm tab ... tap the add button ...",
 "accept": ["Add alarm"], "width": 1280, "height": 2772, "app": "...", "source": "<trace step>"}
```

- `instruction`: lý do của agent lúc đó (đã bỏ số index / toạ độ để không lộ đáp án). Sửa thoải mái.
- `accept`: nhãn phần tử được tính là đúng. Thêm nhãn khác nếu có nhiều đáp án hợp lệ.
- Xoá dòng để bỏ bài; thêm bài bằng `build_dataset.py --count N` (dựng lại toàn bộ).

Chấm điểm: model trả `{"index": N}` → lấy nhãn phần tử N → đúng nếu khớp một nhãn trong `accept`.

## Các cách gửi (`--modes`)

| Mode | Gửi gì |
|---|---|
| `shot768` | ảnh thật 768px + list gọn của Flash (**cách Flash đang làm**) |
| `sketch` | ảnh wireframe trắng đen có đánh số + list gọn |
| `2stage` | model mô tả ảnh (≤60 từ) → lần gọi thứ 2 chỉ text: mô tả + list gọn |
| `list_full` | list đầy đủ (`compact=False`), không ảnh |
| `list_compact` | list gọn (`[9] "Add alarm" 206,305,356,375`), không ảnh |
| `list_center` | nhãn + tâm (`[9] "Add alarm" @281,340`), không ảnh |
| `list_label` | chỉ nhãn (`[9] "Add alarm"`), không ảnh |

List lấy từ `artemis/utils/visualization.py::format_minimal_list_with_elements` — đúng
hàm Flash dùng, nên đổi định dạng list trong Flash thì benchmark đo theo luôn.

## Chạy

```bash
# Ollama local — chạy nhẹ: từng đợt 5 bài, nghỉ 40s, tự dừng nếu RAM khả dụng < 3 GB,
# num_ctx 4096, model tự nhả sau 1 phút
uv run python benchmarks/payload_grounding/bench.py --backend ollama \
    --model qwen3-vl:4b-16k --modes shot768,list_center \
    --out benchmarks/payload_grounding/results/qwen3-vl-4b.jsonl

# Ollama khác (vd. Colab qua tunnel cổng 11435)
uv run python benchmarks/payload_grounding/bench.py --backend ollama \
    --ollama-url http://127.0.0.1:11435 --model qwen-16k --out benchmarks/payload_grounding/results/colab.jsonl

# Gemini API (key trong .env: GEMINI_API_KEY) — 18 bài × 7 mode ≈ 144 request
set -a; . ./.env; set +a
uv run python benchmarks/payload_grounding/bench.py --backend gemini --model gemini-3.5-flash-lite \
    --out benchmarks/payload_grounding/results/gemini-3.5-flash-lite.jsonl

# Bảng tổng hợp (mỗi model một bảng, chỉ tính các bài có đủ mọi mode)
uv run python benchmarks/payload_grounding/bench.py --summary benchmarks/payload_grounding/results/*.jsonl
```

Chạy lại cùng `--out` sẽ **chạy tiếp** chỗ còn thiếu, không chạy lại bài đã xong.
Mỗi lần gọi có một nonce ngẫu nhiên ở đầu prompt để Ollama không dùng lại cache tiền tố
(mọi mode đều trả đủ prefill). Kết quả Ollama ghi kèm digest model để biết chạy trên bản nào.

## Thêm / dựng lại dữ liệu

```bash
uv run python benchmarks/payload_grounding/build_dataset.py --count 18 --redact-file <file-local>
```

`build_dataset.py` **ghi đè** `data/` và `cases.jsonl` (mất các chỉnh sửa tay — sao lưu trước).
Dữ liệu cá nhân được che trước khi ghi:

- nội dung thông báo `"<App> notification: ..."` → `"notification"`;
- email, số điện thoại dạng VN → `[redacted]`;
- mọi regex trong `--redact-file` (vd. địa chỉ nhà/công ty) bị thay trong cây UI và lý do,
  và **tô đen trên ảnh** (theo bounds trong cây UI + theo OCR, vì có chữ chỉ có trên ảnh);
- dòng `box:<tiền tố source>:x1,y1,x2,y2` tô đen một vùng cố định (vd. ảnh bản đồ).

**Không commit file redact** (chính nó chứa thông tin cần che). Repo này public: luôn
tự xem lại ảnh trong `data/` trước khi push.

## Giới hạn

- Chỉ gồm bài có nhãn chữ (`accept`) → thiên về cách gửi chỉ-text. Bấm icon không nhãn,
  màu sắc, game/canvas chưa có trong bộ này.
- Đo **chọn 1 phần tử khi đã biết ý định**; ý định do agent cũ (đã thấy ảnh) viết ra.
  Không đo việc tự nghĩ ra bước tiếp theo khi không có ảnh → cần chạy task thật để kết luận.
- Trên Ollama, ảnh nào cũng ~1058 token (Ollama tự đưa về kích thước cố định);
  llama.cpp server tính token ảnh theo số pixel → khoảng cách ảnh/chữ ở đó nhỏ hơn.
