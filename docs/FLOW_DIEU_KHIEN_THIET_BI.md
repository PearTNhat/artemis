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
UnifiedMobileController.get_screen_data()           third_party/mobile_use/controllers/unified_controller.py:163
  └─ AdbDriver.get_screen_data(skip_settling=False) artemis/drivers/android/adb_driver.py:127
       ├─ sleep 0.3s  (settle mặc định của driver)
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

### 2.2. Flash: `observe()` — `artemis/mcp/observation.py:70`

Gọi qua MCP tool nội bộ `observe_screen` (`ActionSession.observe`). Giống trên nhưng:
- `settle_ms` do executor quyết định (`action_executor.py:199`): `manage_app` 1500ms, `press_key ENTER/SEARCH` 1000ms, còn lại 400ms.
- Tính thêm `scrollable` (`has_large_scrollable`: có container cuộn ≥25% màn hình) → hiển thị `[Scroll: ...]` cho AI.
- Gọi luôn `format_minimal_list_with_elements` và lưu ảnh **đồng bộ** vào DataEngine.

### 2.3. Biến XML thành danh sách AI đọc được — `artemis/utils/visualization.py:363`

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
- `_inject_mutual_occlusion_warnings` chèn cảnh báo khi các phần tử đè lên nhau.
- `elements[i]["center"]` (pixel thật) được lưu vào `state.indexed_elements` để dịch `target=index` sau này.

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

Flash (`FlashRunner._build_tail`, `flash/runner.py:409`) có tail tương tự nhưng gọn hơn: header + `[Scroll: ...]` + ảnh + list UI + notices + guidance + cảnh báo turn cuối.

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

Mỗi turn: commit turn trước vào ledger → đọc injected instruction → `_build_tail` → `ledger.render([tail])` → `_invoke_model` → `_process_tool_calls` (thực thi ngay từng tool_call, quan sát sau mỗi action, ảnh mới thành tail turn sau). Không gọi tool 5 turn liên tiếp → dừng `failed`. Kết thúc khi AI gọi `report_task_status`.

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
                    nhập text → xoá bằng helper `clear_text` + dán qua clipboard helper (adb_driver.py:453)
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
| Driver settle | **+0.3s** (cộng dồn) | `adb_driver.py:129` |
| `/snapshot` helper | ~100–500ms; fallback UIAutomator2 chậm hơn nhiều | `accessibility_client.py:302` |
| OCR (nếu bật) | 1 HTTP call ngoài | `perception.py` |
| **LLM Operator** | phần lớn thời gian (vài s – chục s); có thể lặp nhiều lần nếu gọi tool phụ | `operator.py:474` |
| Safety Net XML | +1 lần dump hierarchy live | `precondition_xml.py:114` |
| Safety Net Pixel | **+1 lần gọi LLM vision** (khi target là toạ độ/OCR hoặc XML fail) | `precondition_pixel.py` |
| `adb shell input tap` | ~100–300ms (spawn shell) | `adb_driver.py:239` |
| Checker/Planner | chỉ khi có checkpoint / sửa milestone | `graph.py` |

Đo thực tế: `self._turn_timing` (Flash: `prep_s`, `llm_s`, `observe_s`, `action_s`) và `artemis/utils/timing_report.py`.

### 8.2. Cơ hội tối ưu đã quan sát được trong code (chưa sửa)

| # | Vấn đề | Vị trí | Hướng tối ưu |
|---|---|---|---|
| O1 | **Settle cộng dồn:** perception/observe sleep 0.4s rồi `AdbDriver.get_screen_data()` sleep thêm 0.3s (controller không truyền `skip_settling`). Ngay cả nhánh "skip settling" vẫn mất 0.3s. | `unified_controller.py:165`, `adb_driver.py:129` | Truyền `skip_settling=True` từ controller vì tầng trên đã tự settle → tiết kiệm 0.3s/lần chụp. |
| O2 | **Gọi đồng bộ trong async:** `self._ui_adb_client.get_screen_data()` (HTTP timeout 8s) chạy trực tiếp trong coroutine → chặn event loop (ảnh hưởng video recording, background task, MCP owner loop). | `adb_driver.py:141` | Bọc `asyncio.to_thread(...)`. |
| O3 | **Icon không nhãn bị loại khỏi list** → AI phải dùng toạ độ → Pro luôn đi Pixel Safety Net (thêm 1 LLM call). | `visualization.py:363` | Đưa thêm phần tử `clickable=true` không text với nhãn tổng quát (`[n] Icon: <class/resource-id> | Bounds`) để AI chọn bằng index. |
| O4 | **Safety Net XML dump hierarchy lần nữa** ngay sau perception (thường chỉ cách vài giây). | `precondition_xml.py:114` | Có thể dùng hash nhanh/hoặc bỏ qua khi thời gian từ perception < ngưỡng và action không có hiệu ứng ngoài. Cần đo trước. |
| O5 | **Tap qua `adb shell input`** (spawn process mỗi lần) trong khi helper đã có RPC `tap`/`swipe`. | `adb_driver.py:239`, `accessibility_client.py:413` | Ưu tiên RPC helper (đã có kết nối HTTP), fallback adb shell. |
| O6 | **Flash lưu ảnh đồng bộ** (`get_or_create_image`: sha256 + ghi đĩa) trong `observe()`, Pro thì chạy nền. | `observation.py:135` | Offload `asyncio.to_thread` như Pro. |
| O7 | **Pro Operator không có `wait_for_text/screen_change/until_gone`** trong `OPERATOR_SHELL_ORDER`, trái với nguyên tắc 2.1 của AGENTS.md → Pro phải dùng `wait_for_delay` cố định. | `action_specs.py:815` | Cân nhắc thêm các tool chờ có điều kiện vào shell Operator (cần cập nhật `_translate_and_validate_tool_inner` + `to_canonical_call`). |
| O8 | Ảnh gửi nguyên kích thước mỗi turn; token ảnh chiếm phần lớn prompt. | `ObservationPromptComponent` | Downscale/nén JPEG theo `max_side` cấu hình; scrub ảnh cũ sớm hơn khi UI list đủ thông tin. |

> Khi xử lý một mục ở trên: chuyển nó sang Nhật ký (Mục 10), ghi số liệu trước/sau vào `KE_HOACH_TOI_UU_ARTEMIS.md`.

---

## 9. Bản đồ file (cần cập nhật docs khi sửa)

| Tầng | File chính |
|---|---|
| Điểm vào | `third_party/mobile_use/sdk/agent.py` |
| Capture | `artemis/clients/accessibility_client.py`, `artemis/clients/screen_client_factory.py`, `artemis/drivers/android/adb_driver.py`, `third_party/mobile_use/controllers/unified_controller.py`, `artemis/runtime/helper_manager.py` |
| Perception | `artemis/graph/perception.py`, `artemis/mcp/observation.py`, `artemis/utils/ocr_xml_fusion.py`, `artemis/utils/visualization.py` (`format_minimal_list_with_elements`) |
| Prompt | `artemis/agents/operator/prompts.py`, `artemis/agents/operator/operator.json`, `artemis/memory/transcript.py`, `artemis/memory/chunking.py`, `artemis/agents/flash/runner.py` (`_build_tail`), `artemis/agents/flash/flash_runner.md` |
| LLM | `artemis/services/llm.py`, `artemis/config/model_routing.py` |
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
