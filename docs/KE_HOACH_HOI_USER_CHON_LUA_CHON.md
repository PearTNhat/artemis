# KẾ HOẠCH: AI HỎI NGƯỜI DÙNG CHỌN (Human-in-the-loop Choice)

> Ngày lập: 2026-11-25 · Bản 6 (đã sửa theo 3 vòng phản biện đối chiếu code + chặn bấm bậy khi AI bí, cùng ngày) · Nhánh gốc: `bench/payload-grounding` (commit `aa6be5b`)
> Phạm vi: **chỉ Flash** (chiến lược Flash-first; Pro làm sau).
> Người thực hiện: đọc hết mục 0–4 trước khi code. Mục 5 là danh sách việc theo pha, mỗi việc có tiêu chí nghiệm thu. Mục 6 ghi các điểm phản biện đã xử lý để khỏi tranh luận lại.

---

## 0. Yêu cầu

AI **không đoán, không bấm bậy** khi không chắc chắn mà đưa lựa chọn lên UI để người dùng bấm. Có 2 tình huống:

**(A) Nhiều lựa chọn ngang hàng** — màn hình có nhiều sản phẩm / quán / chuyến xe / gói cước… và đề bài không nói rõ chọn cái nào → AI liệt kê các mục (tên + chi tiết ngắn: giá, đánh giá, khoảng cách…) để người dùng chọn.

**(B) AI bí** — AI không biết làm gì tiếp (không tìm thấy thứ cần tìm, màn hình lạ, nhiều hướng đi đều có thể đúng, bấm mấy lần không có tác dụng) → AI đưa ra các hướng đi nó nghĩ tới để người dùng chọn, **thay vì bấm thử lung tung**.

Cuối danh sách luôn có các lựa chọn do **hệ thống tự thêm** (AI không phải sinh ra):

| Lựa chọn hệ thống | Ý nghĩa |
|---|---|
| 🤖 **Để AI tự chọn** — *chỉ có ở câu hỏi `pick_item`* | AI tự chọn 1 mục trong danh sách theo đề bài và phải nêu lý do trong thought. Chọn sai món thì vẫn an toàn vì chưa có bước cam kết nào. |
| 🤖 **Cho AI thử các hướng trên** — *chỉ có ở câu hỏi `stuck`, thay cho "Để AI tự chọn"* | AI **chỉ** được thử đúng các hướng nó đã liệt kê, theo luật "thử có kiểm soát" ở 0.3. **Không** được tự nghĩ thêm hướng mới và bấm thử. |
| ✏️ **Yêu cầu khác…** | Người dùng gõ prompt tự do (vd "lấy cái rẻ nhất dưới 50k, có freeship"). AI làm tiếp theo prompt đó. |
| ✋ **Tôi tự thao tác trên điện thoại** | Người dùng tự bấm trên máy (đăng nhập, OTP, thứ khó mô tả…), xong bấm **"Tiếp tục"** trên UI. AI quan sát lại và làm tiếp từ trạng thái mới. |

Nhận câu trả lời xong, AI **quan sát lại màn hình (kèm ảnh)** rồi làm tiếp.

### 0.1. Công tắc bật/tắt chế độ hỏi (người dùng điều khiển)

Có **2 công tắc độc lập**, đặt ở ô nhập task trên UI web (và đổi được **giữa chừng** khi task đang chạy):

| Công tắc | Mặc định | BẬT | TẮT |
|---|---|---|---|
| **Hỏi tôi khi có nhiều lựa chọn** | Bật | AI hỏi `pick_item` | AI tự chọn theo đề bài, nêu lý do trong thought |
| **Hỏi tôi khi AI bí** | Bật | AI hỏi `stuck` thay vì bấm thử | AI **vẫn không được bấm bậy**: dừng và `report_task_status(failed)` kèm mô tả màn hình hiện tại + các hướng đã nghĩ tới |

Ghi chú: khi tắt "hỏi khi bí", hành vi an toàn là **dừng có báo cáo**, không phải đoán. Muốn AI tự xoay xở thì người dùng bật "hỏi khi bí" rồi chọn "🤖 Cho AI thử các hướng trên" khi được hỏi — vẫn bị giới hạn bởi 0.3.

### 0.3. Nguyên tắc: AI bí thì KHÔNG BAO GIỜ được bấm bậy

"Bấm bậy" = bấm vào thứ AI không hiểu nó làm gì, hoặc bấm thử lung tung hi vọng trúng. Ở **mọi** nhánh (công tắc bật hay tắt, người dùng trả lời gì), AI bí chỉ có 3 đường đi hợp lệ:

| Đường đi | Khi nào |
|---|---|
| **Hỏi người dùng** (`stuck`) | Công tắc "hỏi khi bí" bật. |
| **Dừng và báo cáo** (`report_task_status(failed)` + mô tả màn hình + các hướng đã nghĩ tới) | Công tắc tắt, hoặc đã thử hết các hướng được phép mà vẫn không tiến triển. |
| **Thử có kiểm soát** | Chỉ khi người dùng chọn "🤖 Cho AI thử các hướng trên". Luật: |

Luật "thử có kiểm soát":
1. Chỉ thử **các hướng đã liệt kê** trong câu hỏi vừa rồi, lần lượt từ hướng an toàn nhất; mỗi hướng **thử 1 lần**.
2. Chỉ được thử thao tác **quay lui được**: mở menu, mở mục, cuộn, back, tìm kiếm. **Cấm**: bước cam kết (thanh toán, đặt hàng, gửi, xoá, xác nhận), gõ/sửa dữ liệu của người dùng, đổi cài đặt, cấp quyền, đăng xuất.
3. Sau mỗi lần thử phải nhìn lại màn hình: tiến gần mục tiêu → làm tiếp bình thường; không đúng → `back` về chỗ cũ rồi thử hướng sau.
4. Hết danh sách mà vẫn bí → **hỏi lại** (`stuck`, kèm những gì đã thử) nếu công tắc bật, ngược lại **dừng và báo cáo**. Không được tự sang hướng ngoài danh sách.

Phía code hỗ trợ (3.3 mục 4 và mục 8): ngân sách lượt thử cố định `2 × số hướng`; hết ngân sách hoặc bộ đếm "không tiến triển" đạt ngưỡng thì notice bắt AI hỏi lại / dừng. Commitment Guard sẵn có vẫn chặn bước cam kết như mọi khi.

**Thế nào là "bấm bậy" và thế nào là thao tác bình thường** (để AI không hỏi quá nhiều ở các bước hiển nhiên):

| Được làm không cần hỏi (tác dụng rõ ràng) | Phải hỏi `stuck` thay vì bấm |
|---|---|
| Bấm phần tử có nhãn/nội dung khớp rõ với bước tiếp theo của đề bài | Bấm icon/nút **không rõ chức năng** "để xem nó làm gì" |
| Cuộn danh sách, gõ vào ô tìm kiếm, mở thanh điều hướng có nhãn | Phân vân giữa nhiều phần tử mà không cái nào khớp rõ |
| `back` để thoát popup / màn hình vừa mở nhầm (1 lần) | Đã `back` khỏi một màn hình mở nhầm mà vẫn chưa thấy đường đi rõ |
| Chờ tải (các tool `wait_*`) | Không có phần tử nào trên màn hình phục vụ rõ đề bài |

Quy tắc ngắn: **chỉ bấm khi đoán trước được kết quả**. Không đoán trước được → hỏi, không thử.

### 0.2. Quyết định đã chốt
1. **Không trả lời thì cứ chờ, không giới hạn thời gian.** Không timeout, không tự chuyển "AI tự chọn". Chờ đến khi có trả lời hoặc có người bấm Stop.
2. **Không giới hạn số lần hỏi.** Chỉ chặn hỏi lại y hệt câu vừa được trả lời (xem 3.3 mục "chống lặp").
3. **Người dùng bật/tắt được** chế độ hỏi theo 0.1. Config có thêm công tắc tổng để tắt hẳn khi chạy benchmark/CI tự động.
4. **Agent gọi qua MCP không được trả lời thay người dùng**, không Stop task chỉ vì chờ lâu, không poll định kỳ trong lúc chờ (xem 3.6b).
5. Thẻ trên UI dùng **1 ảnh màn hình thu nhỏ**.

### Ngoài phạm vi
- Pro graph: không sửa.
- Không thay thế Commitment Guard: người dùng chọn sản phẩm ≠ cho phép đặt hàng / thanh toán.

---

## 1. Ràng buộc bắt buộc (từ `AGENTS.md` và quy ước dự án)

| # | Ràng buộc | Hệ quả cho thiết kế |
|---|-----------|---------------------|
| R1 | **App-agnostic, cấm hardcode** (AGENTS §1, §2.7): code/prompt không chứa tên app ("Shopee", "Grab"…) hay chữ trên nút ("Add to cart", "Đặt hàng"…) | Điều kiện hỏi mô tả theo **dạng giao diện** ("nhiều mục cùng loại, bố cục lặp lại"). Nhãn option lấy từ màn hình lúc chạy. Ví dụ trong prompt/test dùng tên chung chung. |
| R2 | 1-to-1 ToolMessage (AGENTS §2.6) | Tool hỏi trả đúng 1 `ToolMessage`. Các tool_call còn lại cùng lượt nhận `ToolMessage` "skipped". |
| R3 | Burst Safety (AGENTS §2.4) | Tool hỏi là **tool kết thúc lượt** (giống `look_at_screen`). Sau câu trả lời, index/toạ độ cũ là **stale** → bắt buộc quan sát lại. |
| R4 | Không đặt hàng thật | Benchmark dừng trước Place Order / thanh toán. |
| R5 | Chờ vô hạn không được phá phần còn lại | **Không đổi giá trị `status` lưu trữ** (giữ `"running"`), Stop vẫn dừng được, liveness vẫn phát hiện được task chết khi đang chờ (xem 3.2). |
| R6 | Test `tests/unit/mcp/test_action_manifest.py` quét mọi tên trong backtick ở `flash_runner.md`, bắt buộc phải thuộc `ALL_KNOWN_TOOLS` | Phải đăng ký `ask_user_choice` vào `BACKEND_INDEPENDENT_TOOLS` (`artemis/mcp/action_manifest.py:112`). |

---

## 2. Trải nghiệm người dùng

### 2.1. Tình huống A — nhiều lựa chọn
```
User: "Tìm tai nghe bluetooth và thêm vào giỏ"   [✔ Hỏi khi nhiều lựa chọn] [✔ Hỏi khi bí]
  └─ Flash: mở app → tìm → thấy 12 sản phẩm, đề bài không nói chọn cái nào
       └─ Flash gọi ask_user_choice(kind="pick_item", question, options[≤8])   ← kết thúc lượt
            ├─ Runner ghi pending_choice.json + phát SSE "user_choice_requested"
            ├─ UI web hiện thẻ:  ┌───────────────────────────────────────────────┐
            │                    │ AI cần bạn chọn: Bạn muốn sản phẩm nào?       │
            │                    │ [ảnh màn hình thu nhỏ]                        │
            │                    │ ○ 1. Tai nghe A — 199.000đ · 4.8★             │
            │                    │ ○ 2. Tai nghe B — 349.000đ · 4.9★             │
            │                    │ ○ 3. Tai nghe C — 129.000đ · 4.5★             │
            │                    │ ○ 🤖 Để AI tự chọn                             │
            │                    │ ○ ✏️ Yêu cầu khác: [__________________]        │
            │                    │ ○ ✋ Tôi tự thao tác trên điện thoại           │
            │                    │                 [Gửi]   ⏳ đang chờ bạn 2:14  │
            │                    └───────────────────────────────────────────────┘
            ├─ Runner chờ choice_answer.json — KHÔNG timeout (Stop vẫn dừng được)
            └─ Có trả lời → quan sát lại (bắt buộc kèm ảnh) → ToolMessage cho model
       └─ Flash tìm lại sản phẩm đã chọn → thêm vào giỏ → report_task_status
```

### 2.2. Tình huống B — AI bí
```
User: "Đổi ảnh đại diện"
  └─ Flash: vào trang cá nhân, không thấy chỗ đổi ảnh; 2 cú bấm liền không có tác dụng
       └─ Runner thêm notice "no progress for 2 turns" (3.3 mục 8)
       └─ Flash gọi ask_user_choice(kind="stuck",
              question="Mình không tìm thấy chỗ đổi ảnh đại diện. Bạn muốn làm gì?",
              options=[{label:"Mở menu ⋮ góc trên"}, {label:"Vào Cài đặt > Tài khoản"},
                       {label:"Bấm vào ảnh đại diện hiện tại"}])
            └─ Người dùng chọn 1 hướng / gõ chỉ dẫn / tự bấm trên máy rồi "Tiếp tục"
```

---

## 3. Thiết kế kỹ thuật

### 3.1. Tool mới: `ask_user_choice`

Đặt ở file mới `artemis/agents/flash/user_choice.py` (runner.py đã 2000+ dòng). Mẫu khai báo: `REPORT_TASK_STATUS_TOOL` (`artemis/agents/validator/tool_declarations.py:285`), `LOOK_AT_SCREEN_TOOL` (`artemis/agents/flash/runner.py:237`).

```jsonc
{
  "name": "ask_user_choice",
  "description": "[ASK USER] Let the user decide instead of guessing or tapping at random. Ends your turn and waits for the answer. kind='pick_item': the screen offers several comparable items of the same kind and the goal does not say which one (no name, variant, or rule such as cheapest/first/best rated). kind='stuck': you cannot tell how to continue toward the goal (target not found, unfamiliar screen, several plausible paths, taps with no effect); list the next steps you consider. Never for commitment actions (pay, order, delete, send). Never re-ask what the user just answered.",
  "parameters": {
    "type": "object",
    "properties": {
      "kind": { "type": "string", "enum": ["pick_item", "stuck"] },
      "question": { "type": "string", "description": "One short question in the user's language." },
      "options": {
        "type": "array", "minItems": 1, "maxItems": 8,
        "items": {
          "type": "object",
          "properties": {
            "label":  { "type": "string", "description": "pick_item: the item's name as shown on screen. stuck: a short next step." },
            "detail": { "type": "string", "description": "Facts that tell options apart: price, rating, distance, shop, why this step…" },
            "index":  { "type": "integer", "description": "Element index in the current list, if visible." }
          },
          "required": ["label"]
        }
      },
      "reason": { "type": "string", "description": "Why you cannot decide this yourself." }
    },
    "required": ["kind", "question", "options"]
  }
}
```

- `pick_item` cần ≥ 2 option; `stuck` cho phép ≥ 1 option.
- Model **không** tự thêm 3 lựa chọn hệ thống — runner thêm.
- **Đăng ký** `"ask_user_choice"` vào `BACKEND_INDEPENDENT_TOOLS` (`artemis/mcp/action_manifest.py:112-131`) để `filter_declarations` không lọc mất và `test_action_manifest.py` không fail (R6).
- Tool được khai báo **cố định** khi công tắc tổng trong config bật (3.5), **không** thêm/bớt theo công tắc người dùng — để không phá prompt cache. Công tắc người dùng xử lý lúc gọi (3.3 mục 2).

### 3.2. Kênh trao đổi runner ↔ UI và trạng thái task

**Nguồn sự thật = file trong trace dir** (không phải `status.json`, vì task chạy từ UI web qua daemon không phải lúc nào cũng có `status.json`). Mẫu: `injected_instruction.json` (`apps/admin_console/routers/tasks.py:271-320` ghi; `artemis/graph/perception.py:75`, `runner.py:483` đọc). Trace dir = `self.ctx.data_engine.base_dir` (phía runner) = `trace_store.get_trace_dir(session_id)` (phía admin/MCP).

**`<trace_dir>/pending_choice.json`** — runner ghi khi bắt đầu chờ:
```json
{
  "choice_id": "c_7f3a…",
  "session_id": "…",
  "kind": "pick_item",
  "question": "Bạn muốn sản phẩm nào?",
  "options": [
    {"id": "1", "label": "Tai nghe A", "detail": "199.000đ · 4.8★", "index": 14},
    {"id": "2", "label": "Tai nghe B", "detail": "349.000đ · 4.9★", "index": 21},
    {"id": "auto",   "label": "Để AI tự chọn",                    "kind": "auto"},   // kind=stuck: label "Cho AI thử các hướng trên"
    {"id": "custom", "label": "Yêu cầu khác…",                     "kind": "custom"},
    {"id": "manual", "label": "Tôi tự thao tác trên điện thoại",   "kind": "manual"}
  ],
  "reason": "…",
  "screenshot_path": "<ảnh màn hình lúc hỏi>",
  "created_at": 1764050000.0
}
```

**`<trace_dir>/choice_answer.json`** — UI/MCP ghi khi người dùng trả lời:
```json
{"choice_id": "c_7f3a…", "type": "option", "option_id": "2"}
{"choice_id": "c_7f3a…", "type": "auto"}
{"choice_id": "c_7f3a…", "type": "custom", "text": "lấy cái rẻ nhất có freeship"}
{"choice_id": "c_7f3a…", "type": "manual"}
```

**`<trace_dir>/user_choice_mode.json`** — công tắc người dùng (0.1); runner đọc **mỗi lượt** (không xoá). Ai ghi:
- **Lúc tạo task: runner tự ghi** khi session bắt đầu, từ giá trị 2 công tắc được truyền theo task (3.5). Không để admin console ghi lúc enqueue, vì lúc đó `session_id` có thể chưa được cấp (sinh trong `enqueue_tasks`, kể cả batch `goals`) và trace dir có thể chưa tồn tại.
- **Giữa chừng: admin console / MCP ghi đè** khi người dùng gạt công tắc (task đã chạy nên đã có trace dir).
```json
{"ask_on_choice": true, "ask_when_stuck": true, "updated_at": 1764050000.0}
```
Không có file → dùng mặc định từ config.

Quy tắc:
- Mọi file ghi atomic (`*.tmp` + `os.replace`).
- Runner bỏ qua answer có `choice_id` lệch.
- Trong lúc chờ có `injected_instruction.json` mới (người dùng gõ ô chat) → coi như answer `custom` với text đó.
- Xong (trả lời / Stop) → runner xoá `pending_choice.json` và `choice_answer.json` trong `finally`.
- **Dọn khi bị kill cứng:** vì SIGTERM / kill process tree có thể không kịp chạy `finally`, phía Stop (`mcp_server/tools/task_manager.py` action `stop` ~dòng 549, `task_queue_service.stop_tasks`) và lúc khởi động session mới (`data_engine` start, cạnh chỗ dọn `PAUSE_FILE` ở `engine.py:596`) cũng xoá 2 file này và clear cờ chờ.

**Trạng thái task — KHÔNG đổi `status` lưu trữ:**
- `status.json` / DB giữ nguyên `status = "running"`. Chỉ thêm `awaiting_user: true` + `pending_choice` (bản rút gọn) vào `status.json` **nếu file đó tồn tại** (task MCP); xoá khi xong.
- Lý do: nhiều chỗ đang so cứng `status in ("running", "pending")` — `task_manager.py:147,159,174` (liveness), `:487` (stop), `:582` (inject), `task_runner.py:93,100` (watchdog). Đổi giá trị lưu trữ sẽ làm task chết khi đang chờ không bị phát hiện và làm Stop/inject từ chối.
- Chỉ **lớp trình bày** mới map ra ngoài: `mobile_manage_task status` trả `status: "awaiting_user"` khi đọc thấy `pending_choice.json`; DTO cho Angular thêm `awaiting_user` + `pending_choice`.

### 3.3. Thay đổi trong Flash runner (`artemis/agents/flash/runner.py`)

1. **`_get_tools()` (dòng 365-397)**: nếu công tắc tổng bật → thêm `ASK_USER_CHOICE_TOOL` trước `REPORT_TASK_STATUS_TOOL`.
2. **Nhánh trong `_process_tool_calls()`** — cạnh nhánh `look_at_screen` (dòng 1410-1437), cùng khuôn; xử lý theo thứ tự:
   1. **Công tắc người dùng** (đọc `user_choice_mode.json`):
      - `pick_item` mà `ask_on_choice=false` → ToolMessage error: `Asking is turned off by the user. Choose yourself: pick what best fits the goal and state the deciding criterion in your thought.`
      - `stuck` mà `ask_when_stuck=false` → ToolMessage error: `Asking is turned off by the user. Do not tap at random: if you truly cannot continue, call report_task_status with status='failed' and describe the current screen and the options you considered.`
   2. **Chưa nhìn ảnh (chế độ `image_on_demand`, `self._text_only_turn`)** → không chờ; đặt `self._force_image = True`, ToolMessage error: `You have not seen the screenshot this turn. The next observation includes it; check it, then ask again if still needed.` (cùng cách xử lý `defer_completion`, dòng 1404-1426).
   3. **Hợp lệ**: số option đúng theo `kind`, label không rỗng, **chống lặp** (mục 3). Sai → ToolMessage error, không chờ.
   4. **Chờ**: `await wait_for_user_choice(...)` (trong `user_choice.py`): ghi file, phát SSE, poll 0.5 s/lần tới khi có answer hoặc bị huỷ (không nuốt `asyncio.CancelledError`).
   5. **Sau khi có answer**: quan sát lại bằng `_reobserve_after_resume` (dòng 1764) **và** đặt `self._force_image = True` để lượt sau chắc chắn có ảnh mới (bắt buộc với `manual`: người dùng có thể đã đổi hẳn màn hình).
   6. Trả **1** ToolMessage cho tool_call này + ToolMessage `Action skipped: the turn ended to ask the user.` cho mọi tool_call còn lại (R2). Return giống nhánh `look_at_screen`.
3. **Chống lặp (không giới hạn số lần hỏi)** — không dùng signature màn hình (helper `_list_signature` ở runner.py:247 thay mọi chữ số bằng `#` nên 2 danh sách khác nhau dễ trùng; `_ineffective_taps` nằm trong `action_executor.py`, không dùng chung được). Quy tắc đơn giản, không cần so màn hình:
   - Khoá chống lặp = `(kind, tập label chuẩn hoá: thường hoá, bỏ khoảng trắng thừa, bỏ "…"/"...")`.
   - Chặn nếu khoá trùng với **câu hỏi vừa được trả lời gần nhất** và **chưa có thao tác thiết bị nào thành công** kể từ lúc trả lời. Đã có thao tác (cuộn, bấm, back…) thì được hỏi lại bình thường.
   - Bị chặn → ToolMessage error nhắc lại câu trả lời cũ: `You just asked this and the user answered: '…'. Act on it.`
4. **Nội dung ToolMessage khi có answer** (tiếng Anh, trung tính — R1):
   - option (pick_item): `User chose option {id}: '{label}' ({detail}). Indices from before the question are stale: re-locate it on the current screen by matching '{label}', and use its details '{detail}' to tell it apart if several items share that name or the name is truncated. Scroll if needed.` (không ép `target_text='{label}'` vì tên bị cắt "…" hoặc trùng tên sẽ trượt / vướng Ambiguity Guard.)
   - option (stuck): `User chose: '{label}'. Do that next on the current screen.`
   - auto (pick_item): `User let you decide. Pick the option that best fits the goal and state the deciding criterion in your thought.`
   - auto (stuck): `User allowed you to try the steps you listed, and ONLY those: {danh sách label}. Try them one at a time, safest first, each at most once. Only reversible steps (open, scroll, back, search); never a commitment, never editing user data, settings or permissions. After each try check the screen; if it did not help, go back and try the next. You have {budget} turns for this. When the list is used up and you are still stuck, ask again (kind='stuck', say what you tried) or stop with report_task_status(failed). Do not invent new steps to tap.`
     **Cơ chế phía code (deterministic, không đọc thought của AI — R1):**
     - Runner đặt `self._controlled_try = True`, `self._allowed_stuck_steps = [label…]`, `self._try_budget = 2 * len(_allowed_stuck_steps)` (mỗi hướng tối đa 1 lượt thử + 1 lượt `back`).
     - Mỗi lượt có **thao tác thiết bị** trong chế độ này → `_try_budget -= 1` (lượt chỉ helper/wait không trừ).
     - `_try_budget` về 0 → tắt chế độ và thêm notice: `The {n} turns the user allowed for trying the listed steps are used up. If you are not yet clearly on the way to the goal, do not tap anything else: ask the user again (kind='stuck', list what you tried) or stop with report_task_status(failed).` (bản "stop" khi công tắc tắt). Câu chữ có điều kiện "if not yet clearly on the way" để không báo động nhầm khi hướng thử đầu tiên đã đúng và AI đang làm tiếp bình thường.
     - Chế độ cũng tắt khi AI gọi `ask_user_choice` lần nữa hoặc task kết thúc.
     - `_allowed_stuck_steps` dùng để: in lại trong notice, ghi vào `injected_instruction` (mục 5), và đối chiếu trong benchmark P5 bài 7. **Không** dùng để chặn cứng từng cú bấm (so label với phần tử bị bấm sẽ sai với hướng nhiều bước như "Vào Cài đặt > Tài khoản").
   - custom: `User instruction: "{text}". Follow it.`
   - manual: `The user operated the phone themselves and pressed Continue. The screen may be very different now: read the current observation and continue the goal from here.`
5. **Ghi lịch sử bền (sống qua nén transcript)** — hiện `_record_action_step` chỉ chạy khi `name in action_names` (dòng 1114) và `chunking.py` chỉ đọc `extra_metadata["injected_instruction"]` (`artemis/memory/chunking.py:135-169`). Làm như sau, **không sửa chunking**:
   - Ghi 1 step riêng cho `ask_user_choice` qua `data_engine` (action = `ask_user_choice`, args = câu hỏi + options, kết quả = answer), với `extra_metadata["injected_instruction"]` chứa **cả câu hỏi lẫn câu trả lời**, vì `search_history` (`artemis/tools/history/search.py:188,254`) và Checker (`artemis/agents/checker/checker.py:470`, dùng khi gộp Pro sau này) đọc trường này như chỉ đạo của người dùng — chỉ có câu trả lời thì mất ngữ cảnh. Định dạng cố định:
     - `Asked user ({kind}): "{question}" -> User chose option {id}: "{label}" ({detail})`
     - `Asked user (pick_item): "{question}" -> User let the AI decide`
     - `Asked user (stuck): "{question}" -> User allowed trying only the listed steps: {labels}`
     - `Asked user ({kind}): "{question}" -> User instructed: "{text}"`
     - `Asked user ({kind}): "{question}" -> User operated the phone themselves, then pressed Continue`

     và `extra_metadata["user_choice"] = {choice_id, kind, answer…}` (để inspect/UI). Append `recorded_step_id` vào `turn.step_keys`.
   - Nhờ đó `injected_instruction_line()` và bản tóm tắt L2/L3 tự giữ câu trả lời khi `ToolMessage` gốc bị nén.
   - Test: ép nén chunk rồi kiểm tra dòng `Asked user (…)` vẫn có trong transcript render.
6. **Đồng hồ**: `user_wait_s` ghi riêng vào `_turn_timing`. Lượt chờ không tính là lượt "silent".
7. **`max_turns > 0`**: lượt có `ask_user_choice` **không tính** vào `max_turns`, để lượt cuối "chỉ được report" không rơi ngay sau câu trả lời. **Không trừ biến `turns`**: `turns` (`_run_reactive_loop`, runner.py:1849-1861) còn là số thứ tự cho `log_ai_input`, `log_android_output`, `_start_turn_record` và `turns.jsonl`, trừ đi sẽ làm trùng số lượt trong log. Thay vào đó:
   - `turns` giữ tăng đều mỗi lượt, chỉ dùng cho log/telemetry.
   - Thêm biến `counted_turns`: tăng mỗi lượt **trừ** lượt kết thúc bằng `ask_user_choice` đã chờ được câu trả lời (lượt bị từ chối vì công tắc/chống lặp/text-only vẫn tính, để không lách được giới hạn).
   - Điều kiện vòng lặp và `is_final` dùng `counted_turns` thay cho `turns`.
   Mặc định `max_turns=0` (vô hạn) nên chỉ ảnh hưởng ai cấu hình riêng.
8. **Gợi ý "đang bí" phía code (chống bấm bậy)** — pattern-based, không đọc chữ. Bộ đếm `no_progress_turns`:
   - **Chỉ tăng** khi lượt đó có **thao tác tương tác thiết bị** (`click`, `click_sequence`, `long_press`, `swipe`, `input_text`, `press_key`) mà màn hình không đổi (`ExecResult` "no visible change", `_pixels_changed`/`_jaccard` ở runner.py:254-258) hoặc bị `_ineffective_taps` chặn.
   - **Không tăng** với lượt chỉ gọi helper (`ask_explorer`, `search_history`, `replay_steps`, `get_step_screenshot`, `video_analyzer`, `look_at_screen`), `wait_for_delay`, `wait_for_screen_change` (đặc biệt `after="external"` đang chờ người khác — Soft Handover), `wait_for_text`, `wait_until_gone`. Lượt có cả helper lẫn thao tác thì xét theo thao tác.
   - **Thao tác quay lui tính là không tiến triển:** lượt mà thao tác là `back` (nút back / `press_key` BACK) → **tăng** bộ đếm, **không** reset dù màn hình có đổi. Lý do: bấm nhầm sang màn hình khác rồi phải lùi về là dấu hiệu bấm mò; nếu reset vì "màn hình đã đổi" thì AI có thể bấm mò → lùi → bấm mò mãi mà không bao giờ chạm ngưỡng. (Không cần so màn hình để biết "quay lại màn cũ" — signature màn hình không tin cậy, xem mục 3.)
   - **Reset về 0** khi một thao tác **không phải quay lui** làm màn hình đổi, hoặc ngay sau khi người dùng trả lời một câu hỏi.
   - Giới hạn còn lại (chấp nhận): AI bấm sai liên tiếp sang các màn hình mới mà không lùi thì bộ đếm không bắt được — chặn trường hợp này bằng prompt (3.4: "chỉ bấm khi đoán trước được kết quả") và đo bằng benchmark P5 bài 4.
   - Đạt ngưỡng (`stuck_after_no_progress_turns`, mặc định 2) → thêm 1 dòng vào `pending_notices`: `No progress for 2 turns. If you cannot tell what to do next, ask the user (kind='stuck') instead of trying random taps.` (chỉ khi `ask_when_stuck=true`; khi tắt thì notice nói `…stop and report failed instead of trying random taps.`). Ngưỡng 2 đặt trong config.
   - **Đang ở chế độ thử có kiểm soát** (`self._controlled_try`) mà đạt ngưỡng → notice mạnh hơn: `The steps the user allowed did not help. Do not tap anything else: ask the user again (kind='stuck', list what you tried) or stop with report_task_status(failed).` (bản "stop" khi công tắc tắt).

### 3.4. Prompt (`artemis/agents/flash/flash_runner.md`)

Thêm 1 đoạn **cố định**, bọc bằng `{% if "ask_user_choice" in available_tools %}…{% endif %}` giống các tool tuỳ chọn khác trong file (dòng 17-30) — **không** thêm biến Jinja mới vào `_render_system_prompt`. Không chèn biến vào nội dung để giữ cache; trạng thái công tắc người dùng đi qua ToolMessage/notice chứ không qua system prompt:

> **Asking the user.** Do not guess and do not tap at random: only tap when you can predict what the tap will do. (1) If the screen shows several comparable items of the same kind (the same layout repeated: cards, list rows, offers) and the goal does not decide which one (no name, variant, or rule such as cheapest/first/best rated), call `ask_user_choice` with kind='pick_item' and up to 8 of them; scroll first if the visible set is clearly partial and the goal implies comparing. (2) As soon as no element on the current screen clearly serves the next step of the goal (several plausible paths and none clearly right, an unfamiliar screen, unlabeled controls whose purpose you are unsure of, the target missing after scrolling or searching, or you just had to go back from a wrong screen), call it with kind='stuck' and list the next steps you consider. Ask BEFORE trying uncertain taps, not after they fail. Scrolling, searching and tapping an element that plainly matches the goal are not guesses. If the goal already decides, just act. Never ask for commitment actions. If asking is turned off, follow what the tool result tells you.

Sửa thêm 1 chỗ đã có trong `flash_runner.md` dòng 38 (mục `# 4. STAGNATION & LOOP PREVENTION`, gạch đầu dòng **Flexibly Adjust Strategies**): câu *"Adjust your approach or choose a different element rather than repeating the same action."* hiện xui AI bấm thử phần tử khác khi bí. Nối thêm ngay sau câu đó, bọc điều kiện để prompt khi tắt tool không đổi:

```jinja
{% if "ask_user_choice" in available_tools %} Choose a different element only when it plainly serves the goal; if none does, ask the user (`ask_user_choice`, kind='stuck') instead of trying elements whose purpose you are unsure of.{% endif %}
```

Không liệt kê tên app / chữ nút (R1). Tên `ask_user_choice` trong backtick → bắt buộc đã đăng ký ở `action_manifest.py` (R6).

### 3.5. Cấu hình

`FlashUserChoiceConfig` trong `FlashProfileConfig` (`artemis/config/agent.py:858`) + khối trong `config/artemis.jsonc`, `config/artemis.qwen.jsonc`, `config/artemis.gemini.jsonc`:

```jsonc
"flash": {
  // 🙋 AI asks the user (pick among comparable items, or when stuck) instead of guessing.
  // Waits with no time limit. "enabled" is the master switch: turn it off for unattended benchmark/CI runs.
  "user_choice": {
    "enabled": true,
    "ask_on_choice": true,       // default for the user's "ask when several options" switch
    "ask_when_stuck": true,      // default for the user's "ask when stuck" switch
    "stuck_after_no_progress_turns": 2,
    "max_options": 8
  }
}
```

Ghi đè theo task:
- **UI web**: 2 công tắc ở ô nhập task → thêm `ask_on_choice: bool | None`, `ask_when_stuck: bool | None` vào `RunRequest` (`apps/admin_console/schemas/task_schema.py:18`) → `POST /api/run` (`routers/tasks.py:72-196`) truyền vào `task_queue_service.enqueue_tasks(...)` (`services/task_queue_service.py:1048`) → lưu trong queue item (áp cho **mọi** task khi gửi batch `goals`) → truyền xuống runner giống `explorer_mode`/`verification_level` → runner ghi `user_choice_mode.json` lúc session bắt đầu. Gạt giữa chừng → `POST /api/tasks/choice-mode` ghi đè file.
- **MCP** `mobile_run_task` (`mcp_server/tools/task_runner.py`): tham số `ask_on_choice: bool = True`, `ask_when_stuck: bool = True`; `mobile_manage_task` action `set_choice_mode` để đổi giữa chừng.
- **CLI** `artemis run`: không TTY và không có UI theo dõi → tắt công tắc tổng; cờ `--no-ask-user`.
- **Benchmark** (`benchmarks/`): `enabled=false`.

### 3.6. Bề mặt hiển thị

**(a) Admin console + UI web Angular (bề mặt chính)**
- **SSE**: endpoint SSE của task là `GET /api/stream` và `GET /api/stream/{session_id}` trong `apps/admin_console/routers/tasks.py:465-596` (**không phải** `routers/stream.py`, file đó là MJPEG màn hình máy). Không có whitelist event type, nhưng có lọc theo `session_id` (dòng 480-494) → payload `user_choice_requested` / `user_choice_resolved` **bắt buộc có `session_id`** (`data_engine._publish` tự điền `current_session_id` nếu thiếu — `engine.py:531`; vẫn nên điền tường minh). Mẫu phát: `task_paused` ở `artemis/services/llm.py:369`.
- **Endpoint**:
  - `POST /api/tasks/choice` (mẫu `inject_instruction_endpoint` dòng 271): body `{session_id, choice_id, type, option_id?, text?}` → ghi `choice_answer.json`. 409 nếu không có `pending_choice.json` hoặc `choice_id` lệch; 400 nếu `custom` rỗng.
  - `GET /api/tasks/{session_id}/choice`: đọc `pending_choice.json` trong trace dir của session đó → dùng khi F5 / mở lại session.
  - `POST /api/tasks/choice-mode`: ghi `user_choice_mode.json`.
  - **Multi-device**: `GET /api/status` (dòng 362-452) chỉ trả 1 `running_sid` → thêm `awaiting_user` + `pending_choice` vào **từng phần tử `active_tasks`** để danh sách session thấy mọi máy đang chờ.
- **Angular** (`apps/showcase_ui/src/app/`):
  - `services/agent.service.ts`: thêm 2 event vào danh sách (cạnh `task_paused` dòng 866 / xử lý dòng 1014); signal `pendingChoice` **theo session_id**; `answerChoice(...)`, `setChoiceMode(...)`; khi mở session gọi `GET /api/tasks/{id}/choice` để khôi phục thẻ.
  - Trạng thái `'awaiting_user'` chỉ là trạng thái hiển thị suy ra từ cờ; cập nhật các chỗ so `'running' || 'paused'` (chat-interface.component.ts dòng 53-59, 178-189; agent.service.ts dòng 134-209, 386-454) để coi `awaiting_user` là đang chạy.
  - Component `user-choice-card` nhúng vào `agent-stream`: ảnh thu nhỏ; radio option (label đậm + detail mờ); 3 lựa chọn hệ thống; "✋ Tôi tự thao tác" → thẻ chuyển sang "Bạn thao tác xong thì bấm **Tiếp tục**" (gửi `type:"manual"`); đồng hồ đếm lên "⏳ đang chờ bạn m:ss"; gửi xong khoá thẻ, hiện "Bạn đã chọn: …".
  - 2 công tắc "Hỏi tôi khi có nhiều lựa chọn" / "Hỏi tôi khi AI bí" ở ô nhập task và trên thanh điều khiển task đang chạy.
  - Danh sách session: badge "Đang chờ bạn".

**(b) MCP**
- `mobile_manage_task` (`mcp_server/tools/task_manager.py:219`):
  - `status`: nếu thấy `pending_choice.json` → trả `status: "awaiting_user"` + `pending_choice {choice_id, kind, question, options, waiting_seconds}` (status lưu trữ vẫn `running`, liveness vẫn kiểm tra PID như cũ).
  - Action mới `answer_choice` (`instruction` = `"2"` | `"auto"` | `"manual"` | text tự do) và `set_choice_mode`. Docstring giải thích rõ `"auto"` theo `kind`: với `pick_item` = "AI tự chọn 1 mục trong danh sách"; với `stuck` = "chỉ cho AI thử đúng các hướng đã liệt kê, có giới hạn lượt, không tự nghĩ thêm" — để agent MCP diễn đạt đúng cho người dùng khi hỏi. `pending_choice` trả cho MCP cũng mang `label` của lựa chọn hệ thống theo `kind` (3.2).
  - Stop: dọn `pending_choice.json` / `choice_answer.json` (3.2).
- **Docstring** `mobile_run_task` và `mobile_manage_task` phải nói dứt khoát: khi thấy `awaiting_user` → **hỏi người dùng thật ngay** (trình bày câu hỏi + danh sách đánh số), **ngừng đặt timer poll 1 phút**, không tự trả lời thay, không Stop vì chờ lâu; chỉ gọi `answer_choice` sau khi người dùng phản hồi; chỉ gửi `auto` khi người dùng bảo vậy.
- **Notifier** `needs_input` qua `mcp_server/notifiers/*`: `AgentApiNotifier` (`agentapi.py:212-233`) **không gửi `event_type`**, chỉ gửi text `message` → `message` phải tự đủ nghĩa: `trace_id`, `choice_id`, câu hỏi, danh sách option đánh số, và câu nhắc `Ask the user, then call mobile_manage_task(action="answer_choice", trace_id=…, instruction=…)`.

**(c) CLI (pha sau, tuỳ chọn)**: TTY → in danh sách đánh số, đọc stdin bằng `asyncio.to_thread(input, …)`, ghi `choice_answer.json`.

---

## 4. Rủi ro & cách xử lý

| Rủi ro | Xử lý |
|--------|-------|
| Model hỏi quá nhiều | Prompt nói rõ khi nào không hỏi; người dùng tắt được công tắc; đo tỉ lệ hỏi đúng/sai ở P5. Không đặt trần số lần hỏi. |
| Model hỏi lại y hệt | Chống lặp 3.3 mục 3. |
| Model bí mà vẫn bấm bậy | Notice "no progress" phía code (3.3 mục 8) + prompt "do not tap at random"; tắt "hỏi khi bí" thì dừng có báo cáo chứ không đoán. |
| Model nhỏ (qwen3-vl:4b) không bao giờ hỏi | Ghi số liệu baseline ở P5 tầng 2; nếu cần, pha sau thêm notice khi list có ≥ N phần tử lặp cùng cấu trúc (so class/kích thước/bố cục, **không** so chữ). |
| Task chờ vô hạn bị giết nhầm / không phát hiện chết | Giữ `status="running"` (3.2). Kiểm tra thêm: `invoke_llm_with_timeout_message` (`llm.py:1006`) chỉ tính lúc gọi LLM; timeout tổng task (nếu có) ở `mcp_server/background/task_runner.py` / `task_queue_service`; `DeviceExecutionLock` (`device_lock.py`) không hết hạn khi giữ lâu. Test từng cái. |
| Task chờ giữ thiết bị, task khác cùng máy xếp hàng | Chấp nhận (đúng ý). UI hiện badge "Đang chờ bạn". |
| File chờ mồ côi sau kill cứng | Dọn ở Stop và lúc session mới khởi động (3.2). |
| Màn hình đổi/khoá trong lúc chờ | Re-observe + `_force_image=True` sau mọi answer. |
| Option tên bị cắt / trùng tên | ToolMessage cho khớp theo label + detail, không ép `target_text` (3.3 mục 4). |
| Lẫn Commitment Guard | Tool description + prompt cấm; answer không bypass guard. |
| Phá cache prompt | Tool + đoạn prompt cố định theo công tắc tổng; công tắc người dùng đi qua ToolMessage/notice. |

---

## 5. Danh sách việc theo pha

### P1 — Lõi Flash (làm trước)
| Việc | File | Nghiệm thu |
|------|------|-----------|
| P1.1 Config `FlashUserChoiceConfig` + 3 jsonc | `artemis/config/agent.py`, `config/*.jsonc` | Config cũ không có khối mới vẫn load; mặc định như 3.5. |
| P1.2 Đăng ký tool vào manifest | `artemis/mcp/action_manifest.py` | `uv run pytest tests/unit/mcp/test_action_manifest.py` pass sau khi prompt có `ask_user_choice`. |
| P1.3 Tool declaration, `wait_for_user_choice`, đọc/ghi 3 file atomic, cập nhật `status.json` nếu có | `artemis/agents/flash/user_choice.py` (mới) | Test: pending đủ 3 option hệ thống; answer đúng/lệch `choice_id`; 4 loại answer + injected_instruction→custom; chờ lâu (giả lập) không tự kết thúc; huỷ giữa chừng thoát sạch, xoá file; `status` lưu trữ vẫn `running`. |
| P1.4 Gắn vào runner: `_get_tools`, nhánh `_process_tool_calls` (công tắc, text-only, hợp lệ, chống lặp, chờ, re-observe + `_force_image`), ToolMessage 1-1, step riêng + `injected_instruction`, `user_wait_s`, không tính `max_turns`, notice "no progress" | `artemis/agents/flash/runner.py` | Test: tool không có khi công tắc tổng tắt; burst `[ask_user_choice, tap]` → tap "skipped"; 1 ToolMessage/tool_call; công tắc người dùng tắt → error đúng nội dung từng `kind`, không chờ; text-only turn → error + `_force_image=True`; sau answer `_force_image=True`; hỏi lại y hệt chưa thao tác → bị chặn, có thao tác xen giữa → được hỏi; `pick_item` 1 option → error, `stuck` 1 option → hợp lệ; nén chunk xong vẫn còn dòng `Asked user (…)` kèm câu hỏi + câu trả lời; `max_turns=N` hỏi ở lượt N-1 vẫn còn lượt hành động **và số `turn` trong `turns.jsonl` không trùng**; 2 lượt bấm không đổi màn hình → có notice; 2 lượt chỉ `wait_for_screen_change(after="external")` / helper → **không** có notice; màn hình đổi hoặc vừa trả lời → bộ đếm về 0. |
| P1.4b Chế độ "thử có kiểm soát" cho `stuck` + auto: label lựa chọn hệ thống theo `kind`, `_try_budget = 2 × số hướng`, notice khi hết ngân sách / không tiến triển; `back` tính là không tiến triển | `user_choice.py`, `runner.py` | Test: câu `stuck` có "Cho AI thử các hướng trên", không có "Để AI tự chọn"; ToolMessage auto(stuck) liệt kê đúng label và ngân sách; 3 hướng → ngân sách 6, chỉ lượt có thao tác thiết bị mới trừ, về 0 → notice + tắt chế độ; hỏi lại → tắt chế độ; chuỗi bấm→`back`→bấm→`back` làm bộ đếm tăng tới ngưỡng dù màn hình có đổi; định dạng `injected_instruction` đúng cho stuck. |
| P1.5 Prompt "Asking the user" + câu nối ở dòng 38 ("Flexibly Adjust Strategies"), đều bọc `{% if "ask_user_choice" in available_tools %}` | `artemis/agents/flash/flash_runner.md` | `tests/unit/agents/test_prompt_assembly.py`: cả 2 đoạn có khi bật, không khi tắt (prompt lúc tắt giống hệt hiện tại); không chứa tên app/chữ nút. |
| P1.6 Phát SSE `user_choice_requested/resolved` có `session_id` | `user_choice.py` | Test data engine giả. |
| P1.7 Dọn file chờ khi session mới khởi động | `artemis/data_engine/engine.py` (cạnh dọn `PAUSE_FILE` dòng 596) | Test: file mồ côi bị xoá. |

Test đặt ở `tests/unit/agents/test_flash_user_choice.py`.

### P2 — Admin console + UI web
| Việc | File | Nghiệm thu |
|------|------|-----------|
| P2.1 `POST /api/tasks/choice`, `GET /api/tasks/{id}/choice`, `POST /api/tasks/choice-mode`; thêm 2 công tắc vào `RunRequest` → `enqueue_tasks` → runner (cả batch `goals`); `active_tasks[*]` có `awaiting_user` + `pending_choice`; Stop dọn file | `apps/admin_console/routers/tasks.py`, `services/task_queue_service.py` | Test API: 200 ghi đúng; 409 không chờ/lệch id; 400 `custom` rỗng; 2 session chờ song song đều hiện trong `active_tasks`. |
| P2.2 Angular: service theo session, khôi phục thẻ khi F5, component `user-choice-card` (gồm "Tôi tự thao tác" → "Tiếp tục"), 2 công tắc (lúc tạo + giữa chừng), badge "Đang chờ bạn" | `apps/showcase_ui/src/app/...` | `npm test` pass; thử tay: thẻ hiện, chọn → chạy tiếp; F5 / mở session khác rồi quay lại vẫn thấy thẻ; 2 máy cùng chờ đều thấy; để 10 phút vẫn chờ; Stop khi chờ dừng được; gạt tắt công tắc giữa chừng có tác dụng ở lần hỏi sau. |

### P3 — MCP
| Việc | File | Nghiệm thu |
|------|------|-----------|
| P3.1 `mobile_run_task(ask_on_choice, ask_when_stuck)` | `mcp_server/tools/task_runner.py`, `mcp_server/background/task_runner.py` | Cờ tới runner. |
| P3.2 `mobile_manage_task`: `status` → `awaiting_user` + `pending_choice`; `answer_choice`; `set_choice_mode`; Stop dọn file; docstring "hỏi người thật, ngừng poll, cứ chờ" | `mcp_server/tools/task_manager.py` | Test trong `tests/unit/mcp/`: liveness vẫn phát hiện task chết khi đang chờ; Stop/inject vẫn hoạt động khi đang chờ. |
| P3.3 Notifier `needs_input` với `message` tự đủ nghĩa | `mcp_server/notifiers/*` | Test notifier giả: message có trace_id, choice_id, câu hỏi, options đánh số, hướng dẫn gọi `answer_choice`. |

### P4 — CLI (tuỳ chọn)
Hỏi đáp trên terminal khi TTY; tự tắt khi không TTY.

### P5 — Kiểm chứng trên máy & tài liệu
Chạy **nhẹ** (laptop dễ hết RAM với Ollama — từng bài, nghỉ giữa bài, **hỏi chủ dự án trước khi chạy**), dừng trước mọi bước đặt hàng/thanh toán. Thử ≥ 2 app khác loại (mua sắm + đặt xe/đồ ăn).

- **Tầng 1 — kiểm chứng chức năng** trên model đủ mạnh hiện có: **Qwen công ty** (model production; Gemini API đã bỏ vì chi phí):
  1. Đề bài mơ hồ ("tìm X và mở 1 sản phẩm") → hỏi `pick_item`.
  2. Đề bài có tiêu chí ("mở sản phẩm rẻ nhất") → **không** hỏi.
  3. Đề bài có tên cụ thể → **không** hỏi.
  4. Chức năng khó tìm → hỏi `stuck`, không bấm lung tung. **Đo cứng:** đếm số cú bấm vào phần tử không khớp rõ đề bài (icon không nhãn, mục không liên quan) và số lần `back` khỏi màn hình mở nhầm **trước** khi gọi `ask_user_choice(kind='stuck')` — mục tiêu bằng 0 trên Qwen công ty; ghi số liệu (không đặt kỳ vọng) trên `qwen3-vl:4b`.
  5. Trả lời bằng "AI tự chọn", "Yêu cầu khác", "Tôi tự thao tác" → hoàn thành đúng.
  6. Tắt từng công tắc → bài 1 AI tự chọn có lý do; bài 4 AI dừng `failed` có báo cáo, không bấm bậy.
  7. Bài 4 trả lời "🤖 Cho AI thử các hướng trên" → AI chỉ bấm các hướng đã liệt kê (kiểm tra bằng trace), back khi sai, hết hướng thì hỏi lại; **đếm số cú bấm ngoài danh sách = phải bằng 0**.
- **Tầng 2 — baseline model nhỏ** `qwen3-vl:4b` (Ollama local): chạy lại bài 1–4, chỉ **ghi số liệu** (tỉ lệ hỏi đúng/sai/thiếu), không đặt kỳ vọng pass; dùng để quyết định có làm gợi ý phía code ở pha sau.
- Ghi: số lượt AI, `user_wait_s` tách khỏi thời gian máy.

Khi xong, làm theo **AGENTS.md §3**:
1. `uv run pytest tests/unit/mcp/ tests/unit/agents/ tests/unit/llm/` pass 100%.
2. `KE_HOACH_TOI_UU_ARTEMIS.md`: `✅ Xong` ở Mục 2, changelog phiên bản mới, số liệu ở Mục 1.
3. `docs/FLOW_DIEU_KHIEN_THIET_BI.md`: Tầng 4/7 (tool mới, nhánh chờ người dùng, notice "no progress"), Mục 9 (thêm `user_choice.py`), 1 dòng Mục 10.

---

## 6. Phản biện đã xử lý (bản 2 → bản 3)

| # | Phản biện | Kết luận | Đã sửa ở |
|---|-----------|----------|----------|
| A1 | SSE task nằm ở `tasks.py:465-596`, không phải `stream.py`; lọc theo `session_id`; `/api/status` chỉ 1 session | Đúng | 3.6a: sửa file, payload có `session_id`, thêm `GET /api/tasks/{id}/choice`, `active_tasks[*]` |
| A2 | `test_action_manifest.py` bắt tên tool trong `flash_runner.md` | Đúng | R6, 3.1, P1.2 |
| A3 | Đổi `status` thành `awaiting_user` làm gãy liveness/stop/inject | Đúng | 3.2: giữ `running` + cờ, chỉ map ở lớp trình bày |
| A4 | Câu trả lời không sống qua nén transcript | Đúng | 3.3 mục 5: step riêng + `extra_metadata["injected_instruction"]`, không sửa `chunking.py` |
| A5 | Bỏ sót `image_on_demand` | Đúng | 3.3 mục 2.2 và 2.5 |
| A6 | Chưa có screen signature dùng được | Đúng; **làm khác đề xuất**: không dùng signature màn hình mà chặn theo "chưa có thao tác nào kể từ câu trả lời" — đơn giản hơn và không lệch ngữ nghĩa | 3.3 mục 3 |
| A7 | Notifier không gửi `event_type`; agent MCP poll vô ích khi chờ vô hạn | Đúng | 3.6b |
| B1 | Kill cứng không chạy `finally` | Đúng; **bổ sung** dọn cả lúc session mới khởi động | 3.2, P1.7 |
| B2 | Ép `target_text=label` cứng với tên cắt/trùng | Đúng | 3.3 mục 4 |
| B3 | P5 mâu thuẫn với model nhỏ | Đúng về tách tầng; **không** dùng Gemini Flash như đề xuất (đã bỏ Gemini API vì chi phí) → tầng 1 dùng Qwen công ty | P5 |
| B4 | `max_turns > 0` lượt cuối chỉ còn report | Đúng; chọn cách không tính lượt hỏi vào `max_turns` | 3.3 mục 7 |

### Vòng 2 (bản 3 → bản 4)

| # | Phản biện | Kết luận | Đã sửa ở |
|---|-----------|----------|----------|
| C1 | Trừ `turns` làm trùng số lượt trong log/`turns.jsonl` | Đúng | 3.3 mục 7: biến `counted_turns` riêng; **bổ sung**: lượt hỏi bị từ chối vẫn tính |
| C2 | Bộ đếm "no progress" đếm nhầm lượt helper / chờ | Đúng | 3.3 mục 8 |
| C3 | `session_id` chưa có lúc `POST /api/run` | Đúng; **làm khác một chút**: admin không ghi file lúc enqueue mà truyền cờ theo queue item xuống runner, **runner tự ghi** `user_choice_mode.json` khi session bắt đầu (tránh trace dir chưa tồn tại) | 3.2, 3.5, P2.1 |
| C4 | Bọc prompt bằng `{% if "ask_user_choice" in available_tools %}` | Đúng | 3.4, P1.5 |
| C5 | `injected_instruction` cần cả câu hỏi lẫn câu trả lời cho `search_history`/Checker | Đúng | 3.3 mục 5 |

### Vòng 3 (chủ dự án, bản 4 → bản 5)

| # | Vấn đề | Đã sửa ở |
|---|--------|----------|
| D1 | Ở câu hỏi `stuck`, lựa chọn "Để AI tự chọn" đồng nghĩa cho AI bấm bậy | 0 (bảng lựa chọn hệ thống), 0.3, 3.3 mục 4/5/8, P1.4b, P5 bài 7: thay bằng "Cho AI thử các hướng trên" với luật thử có kiểm soát |

### Vòng 4 (bản 5 → bản 6)

| # | Phản biện | Kết luận | Đã sửa ở |
|---|-----------|----------|----------|
| E1 | Prompt "after a reasonable search" + câu "choose a different element" (`flash_runner.md:38`) xui AI thử trước rồi mới hỏi; bộ đếm reset khi màn hình đổi nên bấm mò sang màn khác không bị bắt | Đúng. **Điều chỉnh**: (a) không bắt hỏi "hễ phân vân" chung chung — thêm bảng "được làm / phải hỏi" ở 0.3 và quy tắc "chỉ bấm khi đoán trước được kết quả", để AI không hỏi cả bước hiển nhiên; (b) không so màn hình để phát hiện "quay về màn cũ" (signature không tin cậy) — chỉ cần coi mọi lượt `back` là không tiến triển; ghi rõ giới hạn còn lại | 0.3, 3.3 mục 8, 3.4, P1.5, P5 bài 4 |
| E2 | Điều kiện "AI không còn nhắc tới bí" không đo được bằng code; `_allowed_stuck_steps` không được dùng | Đúng. Bỏ điều kiện đó; dùng ngân sách `2 × số hướng`, chỉ trừ lượt có thao tác thiết bị; notice hết ngân sách có điều kiện để không báo nhầm. `_allowed_stuck_steps` dùng cho notice/lịch sử/benchmark, **không** chặn cứng từng cú bấm (hướng nhiều bước sẽ bị chặn sai) | 3.3 mục 4, P1.4b |
| E3 | Docstring MCP cần nói rõ `auto` với `stuck`; P5 bài 4 cần đo cứng | Đúng | 3.6b, P5 bài 4 |
