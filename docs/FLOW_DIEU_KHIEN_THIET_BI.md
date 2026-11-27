# LUỒNG ĐIỀU KHIỂN THIẾT BỊ: ĐIỆN THOẠI → AI → ĐIỆN THOẠI

> **Mục đích:** Mô tả chi tiết vòng lặp *Quan sát → Đóng gói prompt → AI quyết định → Dịch lệnh → Thực thi → Quan sát lại* của ARTEMIS, để theo dõi và tối ưu.
>
> **Quy tắc bảo trì:** Mỗi khi thay đổi bất kỳ file nào trong mục [Bản đồ file](#9-bản-đồ-file-cần-cập-nhật-docs-khi-sửa) thì **bắt buộc** cập nhật tài liệu này và ghi vào [Nhật ký thay đổi](#10-nhật-ký-thay-đổi-luồng) ở cuối.
>
> Cập nhật lần cuối: 2026-11-19 (commit gốc `9f6866c`).
>
> Tham chiếu `file:line` có thể lệch vài dòng sau khi sửa code — tìm theo tên hàm.

---

## 0. Tổng quan một màn hình

```
┌──────────────┐  /snapshot (HTTP qua adb forward)   ┌─────────────────────────┐
│  ĐIỆN THOẠI  │ ──────────────────────────────────▶ │ AccessibilityClient     │
│  Accessibility│   XML + screenshot cùng 1 thời điểm│ (fallback: UIAutomator2)│
│  Helper APK  │                                     └───────────┬─────────────┘
└──────▲───────┘                                                 │ ScreenData
       │                                                         ▼
       │ adb shell input tap/swipe            ┌───────────────────────────────────┐
       │ (hoặc RPC helper: type/clear/global) │ Perception / observe()            │
       │                                      │  • (OCR tuỳ chọn) + fuse vào XML   │
┌──────┴───────┐                              │  • lưu ảnh vào DataEngine          │
│ AdbActuator  │◀── MCP in-process ──┐        └───────────────┬───────────────────┘
│ (FastMCP)    │   ActionSession     │                        ▼
└──────────────┘                     │        ┌───────────────────────────────────┐
                                     │        │ format_minimal_list_with_elements │
                       ┌─────────────┴─────┐  │  → "[1] Text: 'OK' | Bounds:..."   │
                       │ Validator (Pro) / │  └───────────────┬───────────────────┘
                       │ McpActionExecutor │                  ▼
                       │ (Flash)           │  ┌───────────────────────────────────┐
                       │  • Safety Net     │  │ Prompt = System(S) + History(F+A) │
                       │  • Guards         │  │   + Tail: ảnh JPEG b64 + list UI   │
                       └─────────▲─────────┘  │   + plan + incident + notices      │
                                 │            └───────────────┬───────────────────┘
                                 │ action dict                ▼
                       ┌─────────┴─────────┐  ┌───────────────────────────────────┐
                       │ Dịch tool_call →  │◀─│ LLM (bind_tools) → tool_calls      │
                       │ index→pixel,      │  │ click(target=3) / click([500,820]) │
                       │ 0-1000 → pixel    │  └───────────────────────────────────┘
                       └───────────────────┘
```

Có **2 chế độ chạy** dùng chung tầng thiết bị (Mục 1, 2, 6) nhưng khác tầng điều phối:

| | **Flash** | **Pro** |
|---|---|---|
| Điểm vào | `third_party/mobile_use/sdk/agent.py:562` → `FlashRunner.run()` | `agent.py:~620` → `get_graph(ctx).astream()` |
| Điều phối | Vòng lặp while đơn, 1 LLM | LangGraph: Planner → Perception → Operator → Validator → Summarizer → Convergence |
| Quan sát | Sau **mỗi action** (`observe()` trong executor) | Đầu **mỗi turn** (`perception_node`) |
| Thực thi | `McpActionExecutor` (`artemis/mcp/action_executor.py`) | `ValidatorNode` (`artemis/agents/validator/`) |
| Kiểm tra trước khi bấm | Guards (Anchor/Commitment/Ineffective-tap) | Safety Net XML → Pixel(LLM) + Guards ở actuator |
| Kết thúc | AI gọi `report_task_status` | `convergence_gate` đọc `task_plan` (tất cả `[x]`) → Checker |

---

## 1. Tầng 1 — Lấy dữ liệu từ điện thoại (Capture)

### 1.1. Chuỗi gọi

```
UnifiedMobileController.get_screen_data(skip_settling)   third_party/mobile_use/controllers/unified_controller.py:163
  └─ AdbDriver.get_screen_data(skip_settling)        artemis/drivers/android/adb_driver.py:127
       ├─ sleep 0.3s chỉ khi skip_settling=False (observe() của MCP đã tự settle → truyền True, observation.py `_capture`)
       └─ _ui_adb_client.get_screen_data()          = FallbackScreenClient
            └─ _call("get_screen_data")             artemis/clients/screen_client_factory.py:242
                 ├─ helper khả dụng? → AccessibilityClient.get_screen_data()   accessibility_client.py:346
                 │     └─ HTTP GET /snapshot?fields=xml (timeout 8s, header X-Artemis-Token)
                 │          • Android 11+: XML + screenshot_base64 chụp CÙNG thời điểm
                 │          • Android ≤10: chỉ XML, rồi bổ sung `adb exec-out screencap -p`
                 └─ lỗi → _degrade(): chuyển sang UIAutomator2 trong 30s rồi thử lại helper
```

### 1.2. Dữ liệu trả về (`UIAutomatorScreenData` → `ScreenDataResponse`)

| Trường | Nội dung |
|---|---|
| `base64` | Screenshot JPEG (helper) hoặc PNG (fallback `device.screenshot`) |
| `elements` | List dict phẳng do `parse_hierarchy_xml_to_elements` sinh: `text`, `content-desc`, `resource-id`, `class`, `bounds="[l,t][r,b]"`, `clickable`, `checked`, `enabled`, `focused`, `hint`, `error`, `package`... |
| `width`, `height` | Kích thước bitmap thật → ghi vào `ctx.device.device_width/height` |

Backend chọn bằng `ARTEMIS_HIERARCHY_BACKEND` = `auto` (helper → UIAutomator2) | `helper` | `uiautomator`.
Nếu điện thoại offline thì **không** fallback mà ném `DeviceOfflineError` ngay.

---

## 2. Tầng 2 — Xử lý quan sát (Perception / Observe)

### 2.1. Pro: `perception_node` — `artemis/graph/perception.py:118`

1. Cấp `step_id` mới nếu chưa có.
2. Đọc file `injected_instruction.json` (lệnh người dùng chèn giữa chừng) → nếu có, mở khoá các dòng check của plan.
3. **Settle:** nếu action trước là `wait_for_delay` hoặc là turn đầu → chụp ngay; ngược lại `sleep 0.4s` rồi chụp.
4. **OCR (tuỳ chọn, chỉ khi cấu hình key):** cắt status bar → `perform_ocr()` → map toạ độ về.
5. `fuse_ocr_with_xml(xml, ocr)` → gắn `ocr_elements` vào node XML (không có OCR thì giữ nguyên).
6. Lưu ảnh + XML + OCR vào DataEngine **ở background thread** (tên file = sha256 ảnh).
7. Trả về state: `latest_screenshot` (path), `latest_ui_hierarchy` (fused), `operator_raw_data` {screenshot_b64, xml, ocr, w, h}.

### 2.2. Flash: `observe()` — `artemis/mcp/observation.py:84`

Gọi qua MCP tool nội bộ `observe_screen` (`ActionSession.observe`). Giống trên nhưng:
- `settle_ms` do executor quyết định (`action_executor.py:199`): `manage_app` 1500ms, `press_key ENTER/SEARCH` 1000ms, còn lại 400ms.
- Tính thêm `scrollable` (`has_large_scrollable`: có container cuộn ≥25% màn hình) → hiển thị `[Scroll: ...]` cho AI.
- **OCR cục bộ ONNX** (`_local_ocr` → `artemis/utils/local_ocr.py`), cấu hình `agent.flash.ocr` trong `config/artemis.jsonc`:
  - `mode`: `off` (mặc định trong code) | `auto` (chỉ khi số node app có nhãn < `min_xml_labels`=5: game, canvas, WebView, XML trống) | `always` (mọi lần quan sát, đọc cả chữ trong banner/ảnh). Config dự án đang bật `auto`.
  - Pipeline: cắt status bar (node `com.android.systemui` gần đỉnh) → RapidOCR (detector PP-OCRv4) + recognizer **PaddleOCR `latin_PP-OCRv5_mobile_rec`** (Apache-2.0, ghim commit `89d3a50`, kiểm SHA-256, tải 1 lần vào `<app_dir>/models/ocr/`) → `use_cls=False`, 4 luồng → chạy trong `asyncio.to_thread`.
  - ~0.7–1 s/màn hình trên CPU laptop; cache 4 frame theo hash ảnh → guard look `persist=False` cùng frame tốn ~0 ms.
  - Ghép bằng `fuse_ocr_with_xml(..., xml_first=True)` trên **bản sao** XML (XML gốc lưu trace không đổi): node đã có `text` thì **XML thắng** (OCR không ghi đè); node chỉ có `content-desc` (vd banner `FEED_ADS_EXTERNAL`) nhận chữ OCR; dòng OCR **không nằm trong node nào** thành **node ảo** `ocr_virtual` (trước đây bị bỏ) → hiện `[n] OCR Text: '...'`.
  - Lỗi thiếu thư viện/model/ONNX crash → bỏ qua OCR, quan sát vẫn thành công.
  - Tiếng Việt: model Latin thiếu dấu hỏi/nặng/dấu chồng (xem `KE_HOACH_TOI_UU_ARTEMIS.md` 1.6); đổi `rec_model_path`/`rec_keys_path` để dùng model tiếng Việt.
- **Tấm đè (bottom sheet / dialog), chỉ list gọn, khi chưa chạy OCR ở trên** (`observation.py`, `find_overlay_hidden` → `verified_overlay_hidden` → `gap_fill_ocr` trong `visualization.py`): node bấm được phủ ≥30% màn hình, xuất hiện sau ≥2 node có nhãn **cùng package** nằm trong nó (bỏ node bao trùm = cha) → ứng viên. Chạy OCR (trừ khi `ocr.mode=off`); chỉ ẩn khi OCR **không đọc thấy** nhãn của ≥70% ứng viên tại đúng chỗ (cây UI không phải lúc nào cũng đúng thứ tự vẽ) — khi đó ẩn cả ứng viên không kiểm được (icon, nhãn 1 ký tự), list ghi 1 dòng `(a layer on top covers the screen below: N elements under it are not listed)`, rồi **lấp chỗ trống**: dòng OCR nằm ngoài mọi phần tử đã liệt kê → `[n] ocr "..."` (nút ghim đáy không có trong cây, vd "Confirm address"). Quét 1064 màn trace: 125 ứng viên, 94 xác nhận; ~0.7 s OCR chỉ trên các màn này.
- **Flash (list gọn) — gộp OCR kiểu "XML trước"** (`observation.py` `_flash_ocr_list` → `visualization.py` `merge_ocr_into_list`), từ 2026-11-25 thay cho `fuse_ocr_with_xml` ở Flash (Pro giữ đường cũ): list dựng từ cây XML; OCR toàn màn khi `ocr.mode=always` (mặc định Flash) hoặc `auto` + cây nghèo, bỏ qua với lượt nhìn tạm (`persist=False`). Mỗi dòng OCR (các từ cùng dòng được ghép trước): (1) chồng lên phần tử có nhãn (không phải panel > 25% màn) → bỏ, giữ chữ XML; (2) cùng chữ với phần tử có nhãn ở gần (≤ 3% chiều cao) → bỏ; (3) tâm nằm trong control không nhãn → thành nhãn của nó `[n] (button) ocr "..."` (gắn `text`, `ocr_label`); (4) còn lại → `[n] ocr "..."` (tối đa 25). Bỏ dòng < 2 ký tự chữ/số (sao đánh giá), score < 0.5, status bar. Kiểm tra tấm đè dùng lại cùng kết quả OCR. Đo offline 40 màn Grab: OCR ~0.78 s/màn; thêm ~6.3 dòng/màn (trước: 17, 42% trùng), gắn nhãn 9 control, list +25% ký tự; bắt được tiêu đề quán, "Order summary", tên món, "Place Order" vốn không có trong cây. Log: `stdout.log` dòng `OCR merge (...)`, `turns.jsonl` `obs[].ocr_added/ocr_labeled/ocr_dropped_dup/ocr_band_*`. Khi `ocr.recognizer="vietocr"` (mặc định trong config): `local_ocr.recognize_screenshot` chỉ tìm vùng chữ (dòng `unread`), VietOCR (`utils/viet_ocr.py`, ONNX) đọc dòng không bị loại ở bước (1) qua hook `refine` của `merge_ocr_into_list` (chạy trong `asyncio.to_thread`), dòng trên node bị nghi che (`local_ocr.read_unread(within=...)` trước `verified_overlay_hidden`) và dòng tại điểm bấm của chốt chặn cam kết; ~0.86 s/màn; prompt báo chữ `ocr` có thể sai chính tả/số nhưng toạ độ đúng; log `VietOCR: read N lines`, `obs[].ocr_read_s/ocr_read_lines`.
- **Dải đáy không có node, chỉ list gọn, khi không có tấm đè** (`observation.py:224`, `find_unlisted_bottom_band` → `gap_fill_ocr` trong `visualization.py:625`): từ đáy phần tử thấp nhất đã liệt kê đến 97% chiều cao (chừa thanh điều hướng), cao ≥ 4% màn hình và không phải nền trơn (độ lệch chuẩn xám ≥ 6) → OCR **chỉ dải đó** (`recognize_screenshot(region=...)`), thêm `[n] ocr "..."` (nút ghim đáy như "Place Order"/"Confirm address"). `gap_fill_ocr` gộp các từ cùng dòng bị detector tách ("Place"+"Order"). 40 màn trace Grab: kích hoạt 13, 0.3–0.8 s/màn.
- Gọi luôn `format_minimal_list_with_elements` và lưu ảnh **đồng bộ** vào DataEngine.
- **Cây UI gốc cho app workflow** (`_publish_raw_observation`, `observation.py`): mỗi lần chụp, cây gốc chưa định dạng (container, resource-id, cờ) được gắn lên `ctx.last_raw_observation = {elements, width, height, seq, ts}`; `seq` tăng mỗi lần → recorder / autopilot biết cây nào mới (2026-11-26).

### 2.3. Biến XML thành danh sách AI đọc được — `artemis/utils/visualization.py:368`

`format_minimal_list_with_elements(fused_xml, w, h)` trả `(text_list, elements, labels)`:

```
[1] Text: 'Wi-Fi' | Bounds: [42,180][958,230] | checked:true enabled:true
[2] Hint: 'Search settings' | Bounds: [60,90][940,130]
[3] OCR Text: 'Play' | Bounds: [400,800][600,850]
[4] Text: 'Password' | Bounds: [...] | Error: 'Too short'
```

Quy tắc lọc (quan trọng khi tối ưu):
- Chỉ đưa vào node **có text / content-desc / hint** và bounds hợp lệ trong màn hình. **Icon không có nhãn bị loại** → AI phải dùng toạ độ `[x,y]` nhìn từ ảnh.
- Bounds được **chuẩn hoá 0–1000** (độc lập độ phân giải).
- Bỏ trùng: cùng text và tâm cách nhau < 8px.
- Sau phần tử thứ 80 chỉ giữ phần tử tương tác; sau 100 bỏ hẳn phần tử không tương tác.
- Hiện trạng thái `checked/enabled` cho control checkable; `error` của accessibility.
- `_inject_mutual_occlusion_warnings` chèn cảnh báo khi các phần tử đè lên nhau (và đặt `occluded=True` trên element). **Bỏ qua cặp có node OCR ảo** (`ocr_virtual`): chữ đọc từ pixel luôn nằm trên phần tử vẽ ra nó, đó không phải bị che.
- `elements[i]["center"]` (pixel thật) được lưu vào `state.indexed_elements` để dịch `target=index` sau này.

**Dạng gọn (`compact=True`) — Flash khi `agent.flash.element_list = "compact"` (mặc định trong config dự án, 2026-11-20).** `FlashRunner.__init__` đặt `ctx.flash_compact_list = True`; `observe()` truyền `compact` vào formatter (Pro không đặt cờ → giữ dạng cũ). Cùng phần tử/chỉ số, khác:

```
[1] "Search" 100,100,600,130
[2] hint "Type a message" 100,900,700,940
[3] "Wi-Fi" 40,350,300,380 checked:true
[4] ocr "Play" 400,400,600,425
[5] "Ad" 372,395,406,410 (overlaps [4], may be occluded)
[6] (icon) 10,95,90,135          ← control không nhãn, chọn bằng index
```
- Bỏ node `com.android.systemui` nằm trọn trong dải status bar (đáy ≤ 6% chiều cao).
- Chỉ ghi `checked:` cho control checkable (hoặc đang checked) và `enabled:false`; bỏ `checked:false enabled:true` mặc định.
- **Control không nhãn** (`clickable`/`long-clickable`/`EditText`, không text/desc/hint) thêm cuối list dạng `(icon|button|input|toggle)`; bỏ container (> 5% màn hình — `EditText` > 15% —, hoặc chứa tâm phần tử đã liệt kê), mẩu < 12px, wrapper trùng tâm; control bọc control không nhãn khác → chỉ giữ control con; tối đa 40. Thêm **sau** bước cảnh báo che khuất của phần tử có nhãn (không tạo cờ mới cho chúng); riêng icon bị node vẽ sau trong cây (clickable hoặc có chữ, không phải con của nó) che ≥ 50% → ` (may be occluded)` + `occluded=True` (`_covered_by_later_layer`). Element có `is_icon=True`, `text=""`.
- Đo trên 3 màn `traces/`: 3201→1174, 5490→2033, 3481→1267 ký tự (~−63%, đã gồm dòng icon). Prompt `flash_runner.md` mô tả dạng tương ứng (biến `compact_list`).

---

## 3. Tầng 3 — Đóng gói thông điệp gửi AI (Prompt)

### 3.1. Cấu trúc prompt (transcript ledger, cả Flash và Pro)

Prompt = **S + F + A + tail** (`artemis/memory/transcript.py`, `OperatorNode._build_prompt_transcript` `operator.py:348`):

| Vùng | Nội dung | Tính chất |
|---|---|---|
| **S** (static) | System prompt: vai trò, luật, mô tả tool, grammar plan | Render **1 lần/phiên**, byte-stable → hưởng prompt cache |
| **F** (frozen) | Lịch sử khôi phục khi process restart; các "era" lịch sử đã nén (chunking L2/L3) | Bất biến |
| **A** (active) | Các turn gần đây (append-only): tail cũ + tool_calls + ToolMessage + kết quả Validator | Ảnh cũ bị **scrub** sau `image_scrub_depth` (mặc định 3) turn, list XML sau `xml_scrub_depth` (1) turn |
| **tail** | Quan sát hiện tại | Mới mỗi turn |

Nếu transcript tắt (`agent.memory.transcript.enabled=false`) → Pro dùng cách cũ 2-message (System chứa plan+history, Human chứa quan sát).

### 3.2. Nội dung tail mỗi turn (Pro) — thứ tự component

```
# CURRENT OBSERVATION [T+03:12]
--- Task plan ---              ← PlanRecitationPromptComponent (đọc notes/task_plan.md)
--- Execution Incident (OPEN) ← ExecutionIncidentPromptComponent (nếu action trước lỗi)
--- Current Screenshot ---     ← ObservationPromptComponent  (prompts.py:378)
<image_url data:image/jpeg;base64,...>
--- Visible UI Elements ---
[1] Text: ... (danh sách mục 2.3)
(ScreenshotSimilarity)         ← cảnh báo nếu màn hình gần như không đổi so với turn trước
(HistoricalStateHint)          ← "bạn đã từng ở trạng thái này ở step N"
(InjectedInstruction)          ← lệnh người dùng chèn giữa chừng
(BackgroundTasks)              ← tác vụ adb nền đang chạy/vừa xong
(Verification Findings)        ← phán quyết Checker/Planner
(ReasoningReminder)            ← nhắc khi turn trước không viết suy luận
(ToolLimitWarning)
```

Các block per-turn được đánh dấu **ephemeral** → bị xoá khỏi lịch sử khi rời cửa sổ gần đây.

Flash (`FlashRunner._build_tail`, `flash/runner.py:418`) có tail tương tự nhưng gọn hơn: header + `[Scroll: ...]` + ảnh + list UI + notices + guidance + cảnh báo turn cuối.

**Ảnh gửi AI ở Flash được thu nhỏ** (`shrink_for_model`, `artemis/utils/model_image.py`), cấu hình `agent.flash.model_image` (`max_side`, `jpeg_quality`; mặc định code `max_side=0` = giữ nguyên, config dự án `1280` / q75): cạnh dài ≤ `max_side`, resize Lanczos (không làm mờ), JPEG lại; ảnh đã nhỏ hơn giới hạn thì giữ nguyên byte. **Chỉ bản gửi AI** bị thu nhỏ — OCR, guard, so sánh màn hình, trace vẫn dùng ảnh gốc. Toạ độ 0–1000 nên không đổi gì phía dịch lệnh. Ảnh này cũng là ảnh nằm lại trong vùng A của transcript (tới khi bị scrub).

### 3.3. Tool khai báo cho AI

- **Pro Operator** (`operator.py:859`): device actions theo `OPERATOR_SHELL_ORDER` (`artemis/mcp/action_specs.py:815`) = `click, input_text, swipe, press_key, manage_app, wait_for_delay, long_press` (chỉ những action actuator hỗ trợ) + note tools (`save_note`, `update_note`, ...) + `ask_explorer`, `ask_diagnoser`, `run_adb_command`, `manage_task`, history tools, `analyze_task_output`, (`video_analyzer`).
- **Flash**: toàn bộ action trong manifest (`artemis/mcp/action_manifest.py:68`, gồm cả `click_sequence`, `wait_for_text`, `wait_for_screen_change`, `wait_until_gone`, ...) + `ask_explorer`, history tools, (`video_analyzer`), `report_task_status` — không có note tools. Danh sách ở `FlashRunner._get_tools` (`flash/runner.py:262`).
- Mô tả tool lấy từ 1 nguồn duy nhất: `artemis/mcp/action_specs.py` (manifest) → server MCP và prompt không lệch nhau.

### 3.4. Hình dạng tham số target mà AI trả về

- `target: 3` → index trong danh sách UI (an toàn nhất, có metadata để Safety Net kiểm tra).
- `target: [500, 820]` → toạ độ **chuẩn hoá 0–1000**, **bắt buộc** kèm `target_description`.
- `target_text: "..."` (Flash) → tìm phần tử theo text trên màn hình live (Anchor Guard).

---

## 4. Tầng 4 — Gọi AI và nhận lệnh

### 4.1. Pro: `OperatorNode._invoke_llm_loop` — `operator.py:474`

Vòng lặp tối đa `OPERATOR_MAX_TOOL_ITERATIONS` lần trong **một turn**:

1. `llm.bind_tools(tools)` → `acomplete()` (`artemis/services/llm.py:802`, có retry/pause/circuit-breaker).
2. Ghi token usage vào ledger (để ngưỡng nén context hoạt động).
3. Tách `tool_calls` thành:
   - **other_calls** (note, explorer, adb, history...) → chạy ngay, trả `ToolMessage`, **LLM được gọi lại** trong cùng turn.
   - **action_calls** (device action) → *turn-ending*.
4. Luật trộn:
   - Tool phụ lỗi → toàn bộ action bị huỷ, AI sửa lại.
   - Có tool "deferring" (cần đọc kết quả trước khi quyết) + action → action bị hoãn.
   - **Plan ledger gate:** action đơn lẻ mà plan không được cập nhật quá lâu → bị bật lại 1 lần.
   - Số action > `burst_cap` → từ chối cả loạt.
5. Mỗi action → `_translate_and_validate_tool` (Mục 5). OK → `ToolMessage("Action Recorded")`, lỗi → `ToolMessage(error)` và lặp lại.
6. Bất biến: **mỗi tool_call có đúng 1 ToolMessage** (AGENTS.md 2.6).

Kết quả: `state.structured_decisions` = JSON list action đã dịch.

### 4.2. Flash: `FlashRunner.run` — `flash/runner.py:1487`

Mỗi turn: commit turn trước vào ledger → đọc injected instruction → `_build_tail` (ảnh bắt buộc ở turn đầu và turn ngay sau khi người dùng trả lời `_force_image`) → `ledger.render([tail])` → `_invoke_model` → `_process_tool_calls` (thực thi ngay từng tool_call, quan sát sau mỗi action, ảnh mới thành tail turn sau). Khi `ask_user_choice` được gọi, runner ghi `pending_choice.json`, cập nhật `awaiting_user=True` (giữ `status="running"`), gửi SSE + thông báo MCP `needs_input` và chờ không giới hạn thời gian `choice_answer.json` trước khi ghi `ToolMessage` + `injected_instruction` và bước vào turn mới (lượt hỏi không tiêu tốn `max_turns` khi trả lời, `turns.jsonl` ghi riêng `user_wait_s`). Không gọi tool 5 turn liên tiếp → dừng `failed`. Kết thúc khi AI gọi `report_task_status`.

**App workflow (2026-11-26, cờ `agent.flash.workflow.mode` = off | record | replay, env `ARTEMIS_WORKFLOW_MODE`; mặc định off)** — `agents/flash/workflow_hook.py`:
- record: `_execute_and_record_action` lưu mỗi thao tác máy kèm cây gốc trước/sau (`ctx.last_raw_observation`; thao tác trong burst không có cây trước → bỏ). `run()` xong với `status="completed"` → `workflow/recorder.record_run` phân loại tất định (nav / slot / judgement / commitment_stop) và ghi `<app_dir>/workflows/<package>/*.json` (`workflow/store.py`, ghi nguyên tử + `.bak`).
- replay: đầu mỗi turn, **trước** `_build_tail`/`_invoke_model`, `maybe_autopilot` khớp màn hiện tại (vân tay bố cục `fingerprint.app_fingerprint`: chỉ node của app foreground, có/không thay vì số lượng, id không kèm vị trí) → chọn bước duy nhất chạy được → giải target trên cây sống (duy nhất, không bị che kể cả bởi cửa sổ khác như bàn phím, không chữ cam kết, mục danh sách chỉ khi danh sách có đúng 1 mục) → thực thi qua `_execute_and_record_action` (cùng executor + guard) → kiểm màn đích (node đích là khớp tốt nhất ≥ τ + neo resource-id/nhãn, gõ chữ kiểm bằng giá trị trong ô) poll 5 lần qua `observe(persist=False)`. Dừng → quan sát lại, `_force_image=True`, khối `[Autopilot] … Stopped: <lý do>` vào notices của tail (không tạo AIMessage/ToolMessage giả). Chỉ đi tiến trong 1 lần chạy (không làm lại bước đã chạy); quay về màn đã qua → nhắc 1 lần "đã làm rồi, màn cuộn được thì cuộn kiểm tra".
- Chọn workflow: `workflow/selector.py`, 1 lần gọi LLM chỉ-chữ / package; giá trị slot phải có nguyên văn trong goal.

---

## 5. Tầng 5 — Dịch lệnh AI thành lệnh thiết bị

### 5.1. Pro: `_translate_and_validate_tool_inner` — `operator.py:1087`

| Tool AI | Action nội bộ | Ghi chú |
|---|---|---|
| `click(target, times, delay_ms)` | `tap` | `coordinates` (pixel) + `normalized_coordinates` (0–1000) + metadata `target_text/bounds/resource_id/class` |
| `long_press` | `long_press_on` | `duration` |
| `input_text(text, target, clear_exist)` | `focus_and_input_text` | |
| `swipe`, `press_key`, `manage_app`, `wait_for_delay` | tương ứng | |

Index → `indexed_elements[i-1].center`; toạ độ chuẩn hoá → `x = nx * width / 1000`.

### 5.2. Validator → MCP — `artemis/agents/validator/action_execution.py:67`

`to_canonical_call(action_item)` (`artemis/mcp/action_names.py:74`) đổi verb nội bộ sang tên tool chuẩn (`tap` → `click`, ...) với toạ độ chuẩn hoá → `ActionSession.call(name, args)`.

### 5.3. Flash: `McpActionExecutor._execute_device_action` — `action_executor.py:269`

Dịch trực tiếp tool_call của AI (`_translate`, `_resolve_target`, `_resolve_index`, `_resolve_target_text`) → `session.call()`.

---

## 6. Tầng 6 — Thực thi trên điện thoại

```
ActionSession.call(name, args)              artemis/mcp/action_session.py:168
  └─ hàng đợi → 1 owner task duy nhất (tuần tự hoá mọi lệnh thiết bị, không bao giờ cancel giữa chừng)
       └─ FastMCP in-memory server          artemis/mcp/action_server.py:120
            ├─ ScreenEffectTracker ghi nhận "đã có action" (cho wait_for_screen_change after=my_action)
            └─ AdbActuator.<action>()        artemis/mcp/actuators/adb.py
                 ├─ _to_px: 0–1000 → pixel theo ctx.device
                 └─ controller.tap_at → AdbDriver.tap → `adb shell input tap x y`   adb_driver.py:239
                    swipe → `input swipe x1 y1 x2 y2 dur`; long press = swipe tại chỗ ≥500ms
                    nhập text → focus: đọc cây UI (không chụp ảnh) rồi chờ ô nhập được focus (poll 0.15s, tối đa 1.0s, adb.py `_wait_for_focused_input`);
                    driver xoá bằng helper `clear_text` (key event chỉ khi helper hỏng) + dán qua clipboard, kiểm tra ngay rồi tối đa 0.35s (adb_driver.py `input_text`)
                    back/home/recents → RPC helper `global`
                    phím khác → `adb shell input keyevent KEYCODE_*`
```

Kết quả: `ActionResult{ok, message, ...}` có cấu trúc — **không** parse chuỗi.

---

## 7. Tầng 7 — Kiểm soát an toàn & phản hồi

### 7.1. Pro: Validator — `artemis/agents/validator/execution_loop.py:362`

Với mỗi action trong `structured_decisions`:

1. **1 action (vetted)** → **Safety Net** `_run_precondition_gate` (`execution_loop.py:76`):
   - Có metadata index → **XML check** (`precondition_xml.py`): lấy hierarchy **live** (`session.ui_hierarchy`) → chấm điểm text/bounds/id → phần tử dịch chuyển thì sửa toạ độ; bị che/biến mất thì chặn.
   - Không có metadata (toạ độ thuần, OCR, Explorer) hoặc XML bypass → **Pixel check** (`precondition_pixel.py`) = **1 lần gọi LLM vision** so ảnh lúc quyết định vs ảnh live.
2. **Nhiều action (burst)** → bỏ Safety Net, bắn liên tục, mỗi action 1 lần (cho UI thoáng qua).
3. Thực thi, retry tối đa 2 lần (vetted; `launch_app` 1 lần). "Dispatched" chỉ nghĩa là thiết bị nhận lệnh — **không** kiểm hiệu ứng.
4. Lỗi → chụp ảnh lỗi → mở **Execution Incident** (lưu trong state, hiện ở tail turn sau tới khi có action thành công) → bỏ qua các action còn lại.
5. Ghi report vào DataEngine (`update_step_execution_result`).

Sau Validator → **Summarizer** (tóm tắt ảnh step chạy nền) → **Convergence gate** → lại **Perception** (Mục 2.1).

### 7.2. Flash: Guards trong executor — `action_executor.py:269+`

- **Commitment Guard:** chặn `target_text` trỏ vào nút cam kết (thanh toán, xác nhận...) trên màn hình chưa thấy.
- **Anchor Guard:** `target_text` → `observe(settle_ms=0, persist=False)` → không thấy / thấy nhiều (Ambiguity) / bị che (Occlusion) thì chặn.
- **Ineffective-tap memory** (`_ineffective_taps`): bấm cùng vùng trên cùng signature màn hình mà không đổi gì → chặn lặp.
- **Morphed-commitment guard** trong burst.
- Sau action OK → `observe(settle_ms)`; nếu màn hình chưa đổi thì thử lại với 800–1000ms → báo `(no visible change)` hoặc `(no visible change; element error: '...')`.
- Burst safety (AGENTS.md 2.4): sau bước chờ hoặc `target_text`, các action dùng index/toạ độ cũ bị dừng (`_has_stale_screen_target`, `runner.py:102`).
- **Hỏi người dùng chọn (`artemis/agents/flash/user_choice.py`):** công tắc `ask_on_choice` / `ask_when_stuck` đọc nóng từ `user_choice_mode.json`; khi `stuck + auto` bật chế độ thử có kiểm soát (`_try_budget = 2 × len(options)`); `_no_progress_turns` đếm cả thao tác quay lại (`back`) bất kể màn hình đổi hay không và nhắc AI gọi `ask_user_choice(kind="stuck")` sau 2 lượt không tiến triển (bỏ qua lượt wait/helper).

### 7.3. Pro: điều kiện kết thúc — `convergence_gate` (`graph.py:898`)

- `assert_halt` → settlement.
- Plan rỗng hoặc mọi milestone `[x]` (trừ `[Loop:continuous]` chưa có lệnh dừng) → `exit_settlement` (Checker review) hoặc `END`.
- Ngược lại → `continue` → Perception.

---

## 8. Ngân sách thời gian 1 turn & điểm tối ưu

### 8.1. Phân rã thời gian (Pro, 1 action đơn)

| Bước | Chi phí ước lượng | Nguồn |
|---|---|---|
| Perception settle | 0.4s (cố định) | `perception.py` |
| Driver settle | 0.3s — **đã bỏ ở đường observe của MCP (Flash)** (O1); Pro `perception.py` vẫn cộng dồn | `adb_driver.py:129` |
| `/snapshot` helper | ~100–500ms; fallback UIAutomator2 chậm hơn nhiều | `accessibility_client.py:302` |
| OCR (nếu bật) | 1 HTTP call ngoài | `perception.py` |
| **LLM Operator** | phần lớn thời gian (vài s – chục s); có thể lặp nhiều lần nếu gọi tool phụ | `operator.py:474` |
| Safety Net XML | +1 lần dump hierarchy live | `precondition_xml.py:114` |
| Safety Net Pixel | **+1 lần gọi LLM vision** (khi target là toạ độ/OCR hoặc XML fail) | `precondition_pixel.py` |
| `adb shell input tap` | ~100–300ms (spawn shell) | `adb_driver.py:239` |
| Checker/Planner | chỉ khi có checkpoint / sửa milestone | `graph.py` |

Đo thực tế: `self._turn_timing` (Flash: `prep_s`, `llm_s`, `observe_s`, `action_s`) và `artemis/utils/timing_report.py`.

### 8.2. Cơ hội tối ưu đã quan sát được trong code (chưa sửa)

> [!IMPORTANT]
> **Hướng đi (2026-11-19): chỉ tối ưu Flash, sau đó gộp Pro vào Flash** — xem `KE_HOACH_TOI_UU_ARTEMIS.md` Mục 0.1.
> - **Làm (Flash + tầng thiết bị dùng chung):** O1, O2, O3, O5, O6, O8.
> - **Không làm (chỉ thuộc Pro):** O4, O7 — Pro chỉ sửa lỗi, không tối ưu thêm.

| # | Vấn đề | Vị trí | Hướng tối ưu |
|---|---|---|---|
| O1 | ✅ Xong (đường observe MCP/Flash, 2026-11-20; Pro `perception.py` chưa đổi). **Settle cộng dồn:** perception/observe sleep 0.4s rồi `AdbDriver.get_screen_data()` sleep thêm 0.3s (controller không truyền `skip_settling`). Ngay cả nhánh "skip settling" vẫn mất 0.3s. | `unified_controller.py:165`, `adb_driver.py:129` | Truyền `skip_settling=True` từ controller vì tầng trên đã tự settle → tiết kiệm 0.3s/lần chụp. |
| O2 | **Gọi đồng bộ trong async:** `self._ui_adb_client.get_screen_data()` (HTTP timeout 8s) chạy trực tiếp trong coroutine → chặn event loop (ảnh hưởng video recording, background task, MCP owner loop). | `adb_driver.py:141` | Bọc `asyncio.to_thread(...)`. |
| ~~O3~~ | ✅ **Flash xong 2026-11-20** (dạng gọn, mục 2.3: `[n] (icon) l,t,r,b`). Pro không làm. Cũ: **Icon không nhãn bị loại khỏi list** → AI phải dùng toạ độ → Pro luôn đi Pixel Safety Net (thêm 1 LLM call). | `visualization.py:363` | Đưa thêm phần tử `clickable=true` không text với nhãn tổng quát (`[n] Icon: <class/resource-id> | Bounds`) để AI chọn bằng index. |
| O4 | **Safety Net XML dump hierarchy lần nữa** ngay sau perception (thường chỉ cách vài giây). | `precondition_xml.py:114` | Có thể dùng hash nhanh/hoặc bỏ qua khi thời gian từ perception < ngưỡng và action không có hiệu ứng ngoài. Cần đo trước. |
| O5 | **Tap qua `adb shell input`** (spawn process mỗi lần) trong khi helper đã có RPC `tap`/`swipe`. | `adb_driver.py:239`, `accessibility_client.py:413` | Ưu tiên RPC helper (đã có kết nối HTTP), fallback adb shell. |
| O6 | **Flash lưu ảnh đồng bộ** (`get_or_create_image`: sha256 + ghi đĩa) trong `observe()`, Pro thì chạy nền. | `observation.py:135` | Offload `asyncio.to_thread` như Pro. |
| O7 | **Pro Operator không có `wait_for_text/screen_change/until_gone`** trong `OPERATOR_SHELL_ORDER`, trái với nguyên tắc 2.1 của AGENTS.md → Pro phải dùng `wait_for_delay` cố định. | `action_specs.py:815` | Cân nhắc thêm các tool chờ có điều kiện vào shell Operator (cần cập nhật `_translate_and_validate_tool_inner` + `to_canonical_call`). |
| ~~O8~~ | ✅ **Flash xong 2026-11-20** (`agent.flash.model_image`, mục 3.2). Còn lại: đo `prompt_tokens` trước/sau trên task thật; scrub ảnh cũ sớm hơn. Pro không làm. | Flash: `FlashRunner._build_tail` (`flash/runner.py:418`) | — |

> Khi xử lý một mục ở trên: chuyển nó sang Nhật ký (Mục 10), ghi số liệu trước/sau vào `KE_HOACH_TOI_UU_ARTEMIS.md`.

---

## 9. Bản đồ file (cần cập nhật docs khi sửa)

| Tầng | File chính |
|---|---|
| Điểm vào | `third_party/mobile_use/sdk/agent.py` |
| Capture | `artemis/clients/accessibility_client.py`, `artemis/clients/screen_client_factory.py`, `artemis/drivers/android/adb_driver.py`, `third_party/mobile_use/controllers/unified_controller.py`, `artemis/runtime/helper_manager.py` |
| Perception | `artemis/graph/perception.py`, `artemis/mcp/observation.py`, `artemis/utils/ocr_xml_fusion.py`, `artemis/utils/local_ocr.py`, `artemis/utils/visualization.py` (`format_minimal_list_with_elements`) |
| Prompt | `artemis/agents/operator/prompts.py`, `artemis/agents/operator/operator.json`, `artemis/memory/transcript.py`, `artemis/memory/chunking.py`, `artemis/agents/flash/runner.py` (`_build_tail`), `artemis/agents/flash/flash_runner.md` |
| Chốt vòng lặp (Flash) | `artemis/agents/flash/loop_guard.py` |
| App workflow (Flash) | `artemis/workflow/{fingerprint,matcher,targets,store,recorder,autopilot,selector,playbook}.py`, `artemis/agents/flash/workflow_hook.py`, `artemis/mcp/observation.py` (`_publish_raw_observation`) |
| Hỏi người dùng (Flash) | `artemis/agents/flash/user_choice.py`, `apps/admin_console/routers/tasks.py`, `mcp_server/tools/task_manager.py`, `apps/showcase_ui/src/app/components/user-choice-card/user-choice-card.component.ts` |
| LLM | `artemis/services/llm.py`, `artemis/config/model_routing.py`, `artemis/llm/router.py` (`media_resolution`, `max_tokens`) |
| Đo đạc | `artemis/utils/turn_log.py` (`turns.jsonl`, tự động bật theo `ARTEMIS_DEBUG_AI_IO`), `artemis/utils/ai_io_logger.py` |
| Dịch lệnh | `artemis/agents/operator/operator.py`, `artemis/mcp/action_names.py`, `artemis/mcp/action_specs.py`, `artemis/mcp/action_manifest.py`, `artemis/mcp/action_executor.py` |
| Thực thi | `artemis/mcp/action_session.py`, `artemis/mcp/action_server.py`, `artemis/mcp/actuators/adb.py`, `artemis/mcp/screen_effect.py` |
| An toàn | `artemis/agents/validator/*`, guards trong `artemis/mcp/action_executor.py` |
| Đồ thị Pro | `artemis/graph/graph.py`, `artemis/graph/checkpoints.py` |

---

## 10. Nhật ký thay đổi luồng

| Ngày | Thay đổi | File | Ảnh hưởng tới luồng |
|---|---|---|---|
| 2026-11-19 | Tạo tài liệu từ code tại commit `9f6866c` | — | Baseline |
| 2026-11-19 | Áp dụng PR upstream #193, #217, #219, #221 | `visualization.py`, `runner.py`, `flash_runner.md`, `prompts.py`, `checker.py`, `agent.py` | Bổ sung trạng thái `[checkable]`/`[checked]`/`[disabled]` vào UI list; ngắt vòng lặp silent turns ở Flash runner; chuyển prompt reasoning sang progress notes tránh từ chối trên Claude 5.x; cách ly final reviews trước đó trong Checker. |
| 2026-11-20 | Thêm cơ chế toggle debug AI I/O (`ARTEMIS_DEBUG_AI_IO`) | `ai_io_logger.py`, `runner.py`, `operator.py`, `constants.py`, `.env` | Cho phép bật/tắt in log trực quan 3 chặng: (1) Output Android (UI list, screen size) -> (2) Input AI (Prompt/messages đã rút gọn base64) -> (3) Output AI (Thinking & Tool calls). |
| 2026-11-20 | Thu nhỏ ảnh gửi AI ở Flash (O8): `agent.flash.model_image` (`max_side` 1280, q75 trong config dự án) | `model_image.py` (mới), `flash/runner.py`, `config/agent.py`, `config/artemis.jsonc` | Tầng 3 (Flash): ảnh trong tail ~76 KB thay ~203 KB (−63%); OCR/guard/trace vẫn dùng ảnh gốc; toạ độ không đổi. |
| 2026-11-20 | OCR cục bộ ONNX cho Flash (RapidOCR + PaddleOCR Latin v5), `agent.flash.ocr.mode` = off/auto/always; ghép `xml_first`; node OCR ảo; bỏ cảnh báo che khuất cho OCR ảo. Sửa thiếu `import time` trong `operator.py` (Pro crash từ commit `0f7693b`). | `local_ocr.py` (mới), `observation.py`, `ocr_xml_fusion.py`, `visualization.py`, `config/agent.py`, `config/artemis.jsonc`, `pyproject.toml`, `operator.py` | Tầng 2 (Flash): màn hình XML nghèo nay có `[n] OCR Text` kèm toạ độ → AI chọn bằng index thay vì đoán toạ độ; `target_text` tìm được chữ OCR. Pro không đổi hành vi (vẫn Google Vision khi có key). |
| 2026-11-20 | Thêm cơ chế toggle quay video màn hình (`ARTEMIS_VIDEO_RECORDING_ENABLED`) trên UI | `system.py`, `system.service.ts`, `home.component.html`, `.env` | Thêm nút Record ON/OFF trên UI cho phép bật/tắt quay video qua scrcpy, giúp giảm tải CPU/mạng và tăng tốc phản hồi khi không cần video. |
| 2026-11-20 | List phần tử dạng gọn cho Flash (`agent.flash.element_list="compact"`) + liệt kê control không nhãn (O3); prompt Flash: ghi chú 1–2 câu (~40 từ), milestone 1 dòng; `media_resolution`/`max_tokens` cấu hình được theo node (Gemini); preset Qwen ảnh 768 | `visualization.py`, `observation.py`, `flash/runner.py`, `flash_runner.md`, `config/agent.py`, `llm/router.py`, `services/llm.py`, `third_party/mobile_use/config/llm.py`, `config/artemis*.jsonc` | Tầng 2–3 (Flash): token list −36% (Gemini 1597→1024), nút "+"/back/icon có index thay vì đoán toạ độ; Qwen: prompt 2253→1297 token, prefill 14→7 s, vẫn 13/13. Pro không đổi (không đặt cờ). |
| 2026-11-20 | `media_resolution` của Gemini gửi **theo từng ảnh** (block `{"type":"image","base64",…,"media_resolution"}`) vì langchain-google-genai 4.3.4 bỏ qua tham số cấp model với Gemini 3; Flash đọc từ node `operator` (chỉ provider `google`). Thêm `turns.jsonl` (1 dòng JSON/turn: `llm_s`, token vào/cache/ra, cỡ list, từng tool call với điểm bấm 0–1000, `no_effect`, `action_s`, `observe_s`), tự động bật khi bật `ARTEMIS_DEBUG_AI_IO`; executor thêm `no_effect` vào metadata | `flash/runner.py` (`_image_block`, `_start_turn_record`, `_write_turn_record`), `utils/turn_log.py` (mới), `utils/ai_io_logger.py`, `mcp/action_executor.py` | Tầng 3 (Flash, Gemini): token ảnh 1081→267 khi `low`; provider khác giữ `image_url`. Đo đạc: phân tích điểm nghẽn từ `traces/<session>/turns.jsonl` thay vì lọc log. |
| 2026-11-20 | Sửa O3: ngưỡng icon 15%→5% (`EditText` 15%), wrapper → giữ control con, icon bị lớp vẽ sau che → `(may be occluded)`; bỏ gợi ý `media_resolution: low` khỏi preset Gemini | `utils/visualization.py` (`_unlabeled_controls`, `_covered_by_later_layer`), `config/artemis.gemini.jsonc` | Tầng 2 (Flash compact): banner/thẻ lớn không còn hiện là `(icon)`; không còn 2 index cho cùng một nút; AI thấy icon nằm dưới dialog. 36 màn hình thật: 0 cờ sai. |
| 2026-11-20 | Cắt chờ thừa phía thiết bị: (1) observe MCP không còn settle 0.3s lần hai trong driver (O1); (2) `input_text` kiểm focus bằng cây UI (không chụp ảnh), chờ focus bằng poll thay sleep 1.0s, để driver xoá bằng helper `clear_text` (1 RPC) thay chuỗi 4 lệnh `input keyevent`, kiểm chữ đã dán ngay thay sleep 0.35s; (3) Flash xả tóm tắt bước + capsule chunk tối đa `agent.flash.final_summary_wait_s` (3s) khi task xong thay 30s + 30s; (4) `ObserveResult.timings` + `observe_detail` → `turns.jsonl` (`obs`: settle/capture/ocr/list/persist) | `mcp/observation.py`, `mcp/action_executor.py`, `mcp/action_types.py`, `mcp/actuators/adb.py`, `drivers/android/adb_driver.py`, `third_party/.../unified_controller.py`, `agents/flash/runner.py`, `config/agent.py`, fixture `action_server_manifest.json` | Tầng 4–6: chụp sau thao tác trung vị 0.72→0.41–0.61s; `input_text` 2.6–3.2→1.8–2.5s; lượt cuối 18.7–30.3s→3.4–4.1s, không còn chờ 30s sau "Session ended". Không đổi prompt/AI. |
| 2026-11-20 | Phân chia log AI I/O theo từng session/đoạn chat: ghi riêng vào `traces/<session_id>/ai_io.log`, thêm banner phân cách phiên vào file tổng `logs/ai_io.log`, hỗ trợ API `/ai-io-log?session_id=...` | `ai_io_logger.py`, `runner.py`, `operator.py`, `system.py`, `test_ai_io_logger.py` | Dễ dàng debug riêng từng lượt chat/task độc lập; không bị lẫn lộn giữa các phiên. |
| 2026-11-23 | List gọn biết tấm đè + lấp chỗ trống OCR; control không nhãn dẹt (< 2.5% chiều cao, rộng ≥ 2× cao) ghi `(cut off: … scroll to see it whole)`; prompt Flash: không bịa dữ liệu cá nhân (vị trí → GPS / địa chỉ đã lưu, cho phép quyền vị trí) | `visualization.py` (`find_overlay_hidden`, `verified_overlay_hidden`, `gap_fill_ocr`, `_is_cut_off_strip`, tham số `hidden`), `observation.py` (`_gap_ocr`), `flash_runner.md` | Tầng 2 (Flash): phần tử bị tấm đè che không còn trong list (AI/Explorer không chọn được nút "Place Order" bên dưới), nút ghim đáy thiếu trong cây có index OCR. Grab đặt món (Gemini 3.5 Flash-Lite): bước Confirm address 15–20 → 8 lượt, cả task 38/43 → 29 lượt, 272/293 → 201 s; không còn bấm nhầm nút đổi thanh toán. |
| 2026-11-25 | OCR dải đáy chưa có node (nút ghim đáy) vào list gọn; gộp từ OCR cùng dòng; OCR theo vùng; bật `agent.flash.ocr.mode=auto` trong `config/artemis.jsonc` + `artemis.qwen.jsonc`; prompt Flash: `{{ goal }}` chuyển xuống cuối (mục 7. OBJECTIVE) để prefix cache dùng chung giữa các tác vụ, bỏ gợi ý `wait_for_delay` cho spinner (mâu thuẫn Rule 7) | `mcp/observation.py`, `utils/visualization.py` (`find_unlisted_bottom_band`, `_merge_ocr_words`), `utils/local_ocr.py` (`region`), `agents/flash/flash_runner.md`, `config/artemis*.jsonc` | Tầng 2 (Flash): nút đáy không có trong cây có index thay vì AI đoán toạ độ; +0.3–0.8 s chỉ trên màn có dải đáy trống. Tầng 3: prompt hệ thống giống nhau đến hết mục 6 giữa các tác vụ. |
| 2026-11-25 | Flash gộp OCR "XML trước" cho mọi màn (`merge_ocr_into_list`, `ocr.mode=always` trong config): chữ XML luôn thắng, OCR đặt nhãn cho control không nhãn và thêm chữ không phần tử nào phủ; lượt nhìn tạm bỏ OCR. Chốt chặn cam kết cho chạy thử (`ARTEMIS_BLOCK_COMMITMENT_TAPS`): từ chối mọi cú bấm (index / toạ độ / `click_sequence`) rơi vào nút đặt hàng / thanh toán, kiểm cây đã thấy + cây sống + OCR quanh điểm bấm, lỗi đọc màn → từ chối | `mcp/observation.py` (`_flash_ocr_list`), `utils/visualization.py` (`merge_ocr_into_list`), `mcp/action_executor.py` (`_guard_commitment_block`, `is_commitment_text`), `config/artemis*.jsonc`, `tests/conftest.py` | Tầng 2 (Flash): +~0.8 s/lần chụp, list +25%, ít phải đoán toạ độ. Tầng 7: bấm nút cam kết bị chặn bằng code khi bật cờ (chỉ dùng khi test trên tài khoản thật). |
| 2026-11-25 | OCR tiếng Việt cho Flash: VietOCR là bộ đọc chữ duy nhất (`ocr.recognizer="vietocr"` trong `config/artemis*.jsonc`); RapidOCR chỉ tìm vùng chữ, VietOCR chỉ đọc dòng XML không phủ / dòng dưới tấm đè nghi ngờ / dòng tại điểm bấm của chốt chặn; thiếu model → recognizer Paddle; prompt Flash báo chữ `ocr` có thể sai chính tả/số, toạ độ đúng, chữ XML chuẩn; model xuất bằng `scripts/export_vietocr_onnx.py` | `utils/viet_ocr.py` (mới), `utils/local_ocr.py` (`read_unread`, chế độ chỉ tìm vùng), `utils/visualization.py` (`merge_ocr_into_list(refine=)`, `_merge_ocr_words` giữ cờ `unread`), `mcp/observation.py`, `mcp/action_executor.py` (`_guard_commitment_block`), `config/agent.py` (`FlashOcrConfig.recognizer/vi_model_dir`), `agents/flash/flash_runner.md`, `scripts/export_vietocr_onnx.py` | Tầng 2 (Flash): chữ OCR bổ sung đúng dấu; 0.86 s/lần chụp (Paddle 0.80); số trong dòng `ocr` kém tin cậy hơn. Tầng 3: thêm 1 câu trong mô tả list. |
| 2026-11-25 | Hỏi người dùng chọn khi gặp phân vân hoặc bí đường cho Flash (`ask_user_choice`, `pick_item`/`stuck`): IPC file nguyên tử (`pending_choice.json`, `choice_answer.json`, `user_choice_mode.json`), SSE + endpoint `/api/tasks/choice*`, MCP `mobile_run_task`/`mobile_manage_task` (`answer_choice`, `set_choice_mode`, `needs_input`), Angular `UserChoiceCardComponent`, chế độ thử có kiểm soát `2N` cho `stuck + auto`, đếm `back` vào `_no_progress_turns` | `agents/flash/user_choice.py` (mới), `agents/flash/runner.py`, `agents/flash/flash_runner.md`, `config/agent.py`, `mcp/action_manifest.py`, `data_engine/engine.py`, `apps/admin_console/*`, `apps/showcase_ui/*`, `mcp_server/*`, `runtime/daemon_client.py` | Tầng 3–4 & tầng điều phối (Flash): dừng chờ không giới hạn trên tool `ask_user_choice`, giữ `status="running"` và lock PID, buộc gửi ảnh turn sau trả lời, lưu `injected_instruction` + `user_choice` vào transcript bền vững, hỗ trợ bật/tắt nóng qua UI/API/MCP/CLI. |
| 2026-11-26 | App workflow cho Flash: ghi luồng từ phiên `completed`, autopilot phát lại bước đã ghi có kiểm màn đích trước khi gọi AI (cờ `agent.flash.workflow.mode`, mặc định off); observe công bố cây UI gốc lên `ctx.last_raw_observation` | `workflow/*` (mới: targets, store, recorder, autopilot, selector; `fingerprint.app_fingerprint`, `matcher.is_matchable`), `agents/flash/workflow_hook.py` (mới), `agents/flash/runner.py`, `mcp/observation.py`, `config/agent.py` (`FlashWorkflowConfig`) | Tầng 4 (Flash): khi bật replay, đầu turn có thể chạy 0–15 thao tác không gọi LLM rồi bàn giao kèm notice + ảnh; tắt → không đổi luồng. Emulator Đồng hồ (Qwen3-VL 4B): thêm thành phố 9 lượt/98 s (off) → 3 lượt/46 s (replay). |
| 2026-11-26 | (1) Flash chờ UI đứng yên thay chờ cố định 400 ms: sau thao tác chụp nhanh liên tục (dHash), pha 1 chờ màn bắt đầu đổi so với trước thao tác (≤ 1 s, chỉ thao tác hay chuyển màn), pha 2 chờ 2 khung liền nhau lệch ≤ 2 bit (≤ 3 s), rồi observe(settle_ms=0); bỏ lần chụp lại 800 ms khi đã chờ đổi mà không đổi. (2) Chốt vòng lặp Flash: chữ ký = nội dung + trạng thái màn, thao tác, thao tác trước; lặp lần 2 → cảnh báo, lần 3 → dừng `failed`. (3) Workflow nhớ màn kết thúc (`end_nodes`); autopilot tới đó → báo AI việc đã xong, gọi `report_task_status`, không làm lại. (4) Prompt Flash: không thấy mục tiêu + màn cuộn được → cuộn tối đa 3 lần, màn không đổi = hết danh sách; autopilot tự cuộn tìm target tối đa 3 lần | `mcp/action_executor.py` (`_wait_screen_settled`), `agents/flash/loop_guard.py` (mới), `agents/flash/runner.py`, `agents/flash/flash_runner.md`, `workflow/{autopilot,recorder,store}.py`, `agents/flash/workflow_hook.py` | Tầng 6 (Flash): ảnh sau thao tác không còn chụp giữa chuyển cảnh; ~0.7–1.8 s/thao tác trên emulator (helper chụp ~0.4 s/khung). Tầng 4: phiên không còn lặp vô hạn — chạy lại offline trên 72 phiên trong DB: bắt 2/2 vòng lặp thật (dừng ở bước 13–14), 1/57 phiên thành công bị dừng (Lichess, bấm cùng nút đầu hàng 5 lần). Pro không đổi. |
| 2026-11-26 | LLM: lỗi server không parse được tool call của model (Ollama 500 "invalid character …") → `FailureCategory.MALFORMED_OUTPUT`, không thử lại / không Pause; Flash hỏi lại với notice `[System] Your previous reply could not be parsed…`, 3 lần liên tiếp → `failed`. Workflow: chặn chạy lại theo tập bước đã chạy (không theo thứ tự file) | `llm/reliability.py`, `agents/flash/runner.py`, `workflow/autopilot.py` | Tầng 4 (Flash): không còn treo ở "Paused" khi model 4B sinh JSON hỏng; prompt hỏi lại khác prompt cũ. |
| 2026-11-27 | App workflow v6 (W0–W4) + sửa L-021..L-026: đo offline giả lập `gt.post` khi bấm lệch (`scripts/workflow_replay_eval.py`); `nav` cần ≥ 2 lần chạy độc lập trong workflow có cam kết/lựa chọn (`provisional` → `active`, `stats.seen`); playbook từ Turn 1 (`W1`); chọn workflow khung tĩnh + `index.json` (`W2`); lọc tên app từ `manage_app`/icon Launcher/`package_cache` + `RecyclerView` kể cả `scrollable=False` (`W3`); hỏi HOME launcher qua `cmd package resolve-activity` cache mỗi thiết bị (`W4`); `safe_swipe_in_norm` tránh FAB/bàn phím cho cả Flash `swipe` lẫn autopilot; gắn nhãn UI Web cho phiên & từng bước autopilot không gọi AI | `workflow/*`, `agents/flash/{workflow_hook,loop_guard,runner}.py`, `utils/{coordinates,timing_report}.py`, `mcp/action_executor.py`, `scripts/workflow_replay_eval.py`, `apps/showcase_ui/*` | Tầng 4, Tầng 6 & UI (Flash): chặn bấm nhầm bước phụ thuộc trạng thái 1 lần (`Make another`); `swipe` hướng tránh nút nổi/bàn phím; 0 LLM calls chọn workflow & launch từ Launcher; W0 trên 35 phiên `Undet=0`; UI đánh dấu rõ bước/phiên chạy workflow không gọi AI; mặc định vẫn `off`. |
| 2026-11-27 | Độ bền & chính xác workflow (L-043..L-047): (1) L-043 tự ẩn bàn phím bằng `BACK` 1 lần + tìm lại `EditText` theo `resource_id` duy nhất / `hint`, đặt `has_waited=True` cho chuỗi burst và ghi `wf_pre` sau `BACK`; (2) L-044 bỏ qua `EditText` khi tự kiểm L-039 và không kết thúc L-042 khi form chưa lưu; (3) L-045 so khớp đúng giá trị sau chuẩn hoá (`- ( ) .` + khoảng trắng) trong Autopilot và cảnh báo lệch trong Executor (`field now holds ...`); (4) L-046 lưu `app_version` (`dumpsys package`) hạ `nav` về `provisional` khi đổi phiên bản và dọn `Node` khi `miss_count > 10` (tính cả bước `autopilot`, giữ `start_nodes`/`end_nodes`); (5) L-047 hỗ trợ slot ghép `slot1+slot2` theo thứ tự yêu cầu ở cả recorder, autopilot và playbook | `utils/visualization.py`, `mcp/action_executor.py`, `agents/flash/{runner,workflow_hook}.py`, `workflow/{targets,store,recorder,autopilot,playbook}.py`, `tests/unit/workflow/test_l1_l4_fixes.py` | Tầng 4 & Tầng 6–7 (Flash): tránh sót ô dưới bàn phím và ghi đè tiền tố cũ (`+1...`); tự làm mới cạnh `nav` khi app cập nhật mà vẫn giữ bước `slot` và dọn đồ thị cũ; ô gộp họ tên không còn tụt về `judgement`. |
| 2026-11-27 | Can thiệp workflow theo prompt & ghi đè theo từng task (L-048) và siết L-049: khi prompt mới khác khung tĩnh sau khi che slot (`goal_deviation_summary`), `maybe_autopilot` dừng gửi danh sách bước + yêu cầu mới cho AI chọn `workflow_autopilot('proceed'|'manual')` hoặc tự bấm trực tiếp; hỗ trợ tham số `workflow` (`off|record|replay`) trên `mobile_run_task`, `--workflow` CLI và `/api/run`; L-049 giữ nguyên dấu tiếng Việt (NFC) khi so giá trị ô nhập và kiểm ô `password=true` theo độ dài chuỗi che | `workflow/{selector,fingerprint,targets,autopilot}.py`, `agents/flash/{workflow_hook,runner}.py`, `mcp/action_executor.py`, `mcp_server/*/task_runner.py`, `runtime/daemon_client.py`, `interfaces/cli/commands/run.py`, `apps/admin_console/*` | Tầng 4 & 7 (Flash): chặn autopilot chạy theo khuôn mẫu cũ khi prompt có lưu ý mới; tắt/đổi workflow riêng từng task; không báo lỗi giả ở ô mật khẩu và bắt được lỗi gõ rơi dấu tiếng Việt. |
| 2026-11-27 | Chống gõ nhầm vào nút & chặn bỏ qua giá trị lệch (L-050, L-051): `visualization.py` giữ `EditText` không nhãn dù chứa chữ nhãn tĩnh và hiển thị `(input field: "<nhãn gần nhất>")` từ XML/OCR; `action_executor.py` từ chối `input_text(target=i)` vào nút không phải ô nhập và chuyển kết quả gõ bị lệch giá trị thành `failed` (`status="error"`, vẫn kèm ảnh mới); `flash/runner.py` nhắc lại đúng 1 lần khi AI gọi `report_task_status(completed)` mà chưa sửa ô lệch | `utils/visualization.py`, `mcp/action_executor.py`, `agents/flash/{runner.py,flash_runner.md}`, `tests/unit/mcp/test_compact_element_list.py`, `tests/unit/workflow/test_l1_l4_fixes.py` | Tầng 2 & 7 (Flash): AI phân biệt được ô nhập không nhãn với nút bấm và không còn vô tình bấm nhầm hay lưu số dính tiền tố cũ (`+1...`) dù đã bị cảnh báo. |
| 2026-11-27 | Siết đúng phạm vi lỗi L-051 (L-055), gộp phím IME (L-053) & Set-of-Mark có chọn lọc (L-054): `targets.normalize_field_value` bỏ `,` phân cách nghìn với số thuần; `action_executor.py` chỉ trả `status="error"` khi dính nội dung có sẵn không do lần chạy gõ / rơi dấu / mật khẩu, chấp nhận gõ nối có chủ ý (`clear_exist=False`) và hạ xuống ghi chú mềm nếu `clear_exist=True` vẫn ra định dạng app (`+84...`, `100.000 đ`); `visualization.py` & `observation.py` in phần tử app trước, gộp phím IME chỉ giữ phím hành động có index và bỏ qua vùng phím khi gộp OCR; `draw_som_marks` (`som_image_v2`) vẽ `[index]` chỉ lên control không nhãn, ô nhập và dòng `ocr` (trừ bàn phím và nhãn XML) qua `flash.model_image.marks="unlabeled"` | `utils/visualization.py`, `workflow/targets.py`, `mcp/{observation,action_executor}.py`, `agents/flash/{runner.py,flash_runner.md}`, `config/{agent.py,artemis.jsonc}`, `tests/unit/workflow/test_l1_l4_fixes.py` | Tầng 2, 3 & 7 (Flash): tránh vòng lặp thất bại giả ở ô tiền/số điện thoại/nối chữ; danh sách UI gọn gàng ưu tiên phần tử app; ảnh gửi AI đánh số chính xác control mập mờ dưới `50 ms/màn`. |
| 2026-11-27 | Chặn ghi workflow sai (L-057), chặn báo xong khi thiếu dữ liệu (L-058) & bảng theo dõi giá trị đã gõ (L-059): `targets.extract_requested_literals` rút số ≥3 chữ số, chuỗi ngoặc kép, email; `recorder._unverified_run_outcome` bỏ qua ghi workflow khi thiếu giá trị yêu cầu hoặc gõ vào sai nhãn đối chiếu theo prompt; `FlashRunner._consume_missing_requested_literals` nhắc 1 lần trước `report_task_status(completed)` nếu literal yêu cầu chưa gõ và chưa thấy trên màn cuối; `_typed_progress_line` kèm trạng thái `Typed so far` / `Requested values` vào observation header khi có dữ liệu nhập | `workflow/{targets,recorder}.py`, `agents/flash/runner.py`, `tests/unit/workflow/test_l1_l4_fixes.py` | Tầng 4 & 7 (Flash): không cho workflow học lại quy trình sai (`Toan / Company=Le`, thiếu số); buộc model nhẹ hoàn tất hoặc giải trình phần thiếu; theo dõi trực tiếp chữ đã vào ô nào. |
| 2026-11-27 | Diff trước/sau khi vuốt & gợi ý mép cuộn (L-060): `action_executor.py` (`_apply_swipe_outcome_feedback`, `scroll_edge_state`) giải nghĩa chiều ngón tay ↔ chiều trôi trang kèm danh sách phần tử `New:` / `Hidden:` sau `swipe`; khi `swipe(direction=...)` không làm đổi màn thì ghi nhận mép `'top'`/`'bottom'` và báo đổi chiều ở cả kết quả lệnh lẫn dòng `[Scroll: ...]` trong `FlashRunner._build_tail` | `mcp/action_executor.py`, `agents/flash/runner.py`, `tests/unit/workflow/test_l1_l4_fixes.py` | Tầng 2 & 7 (Flash): model nhẹ không bị lộn chiều `up`/`down` và nhận biết ngay vừa chạm mép trên/dưới. |
