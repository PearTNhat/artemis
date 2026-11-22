# KẾ HOẠCH TỐI ƯU ARTEMIS (MOBILE AUTOMATION AGENT) — BẢN 3

> **Mục tiêu:** Giảm số lượt gọi AI và thời gian tác vụ trên **MỌI APP ANDROID**, bằng tín hiệu đọc từ màn hình, tuyệt đối không dùng luật riêng cho từng app.
> - Tuyệt đối không hardcode hay tối ưu cục bộ thiên vị cho bất kỳ ứng dụng đơn lẻ nào (Grab, Cờ vua, Shopee, v.v. chỉ là benchmark đo đạc thực nghiệm).
> - Giảm thời gian tác vụ từ ~232s xuống ~70–85s, giảm số lượt AI từ 23 lượt xuống 9–11 lượt trên tác vụ phức tạp; giảm từ 2 lượt xuống 1 lượt/thao tác trên các tương tác lặp lại.
>
> **Thay đổi lớn nhất của Bản 3:** Thêm **Trụ cột G — "AI giao việc, thiết bị tự chờ và kiểm tra"**, thay cho cách AI cầm tay chỉ việc từng bước.

---

## LỊCH SỬ PHIÊN BẢN (VERSION CHANGELOG)

### 📌 Bản 3 (Hiện tại - Đột phá: Trụ cột G & Kiến trúc App-Agnostic hoàn thiện)
- **Trụ cột G ("AI giao việc, thiết bị tự chờ và kiểm tra"):**
  - Bổ sung bộ 3 công cụ chờ sự kiện: `wait_for_text` (chờ text xuất hiện), `wait_until_gone` (chờ spinner/dialog biến mất), `wait_for_screen_change` (so khớp XML diff trước và pixel diff 64x64 sau, che nhiễu đồng hồ/status bar).
  - Bổ sung `target_text` xuyên màn hình mới với đầy đủ Anchor Guard, Ambiguity Guard, Occlusion Guard, và Commitment Guard.
  - Chuẩn hóa luồng lấy dữ liệu mới cho `target_text` qua `session.observe()`, chống lỗi dữ liệu thô và bảo vệ `state.indexed_elements`.
  - Cơ chế **Burst Safety**: Sau bước chờ chỉ cho phép `target_text` và các action không có mục tiêu cũ (`press_key`, `input_text` vào ô focus, `swipe(direction)`, `back`, `manage_app`). Chặn ngay lập tức nếu trỏ vào index hoặc tọa độ cũ.
  - **Soft Handover thông minh**: Hết giờ chờ không ngắt ngang tác vụ; chụp ảnh gửi AI kèm thông tin đếm số lần và thời gian chờ để AI nhìn thực tế và phán đoán.
  - **Phòng thủ cấu trúc phẳng (`flatten_hierarchy`)**: Tương thích 100% mọi dòng máy và Accessibility Service dù XML lồng nhau nhiều cấp.
  - **Tín hiệu Timeout có cấu trúc**: Bổ sung `timed_out: bool = False` trên `ActionResult` và metadata thay vì tìm chuỗi thô.
- **Tối ưu Fail-Fast & Phản hồi trung tính độc lập ngôn ngữ (Trace cờ vua & Grab Food):**
  - **Cắt chờ khi màn hình đã đổi xong (`after="my_action" | "external"`):** Thêm tham số `after` cho `wait_for_screen_change`. Khi `after="my_action"` (mặc định), nếu thao tác trước đó của chính AI đã làm màn hình đổi và ổn định xong (ví dụ bấm vào Food, mở giỏ), hệ thống lập tức hoàn thành lệnh chờ và trả về kết quả, tiết kiệm ~66s chờ vô ích mỗi tác vụ (cắt 1/4 tổng thời gian). Khi `after="external"`, hệ thống giữ lệnh chờ để đợi đối thủ/bên ngoài phản hồi (cờ vua, chat).
  - **Bộ nhớ chặn lặp bền vững (`_ineffective_taps`):** Chặn bấm lặp lại cùng một nút trên cùng trạng thái màn hình (ngưỡng 20 đơn vị tọa độ) ngay cả khi có các thao tác khác (như `wait_for_delay`, bấm thử mục khác) xen vào giữa.
  - **Visibility Guard nâng cấp (`find_matching_text_elements`):** Bỏ qua các phần tử có `visible-to-user="false"` hoặc bị cắt ở mép màn hình (chiều cao nhìn thấy `< 14px` hoặc bị che `> 60%`), giải quyết triệt để lỗi "nhìn xuyên thấu" text `Total` trên thanh đáy trước khi chuyển trang.
  - **Quy tắc UI Pattern tổng quát (Sticky Bottom CTA & Form Review):** Xóa toàn bộ hardcode tên nút tiếng Anh khỏi prompt. Hướng dẫn AI nhận diện thanh hành động ghim đáy và cuộn kiểm tra hết các trường bắt buộc khi màn hình có dấu hiệu còn nội dung (`[Scroll: scrollable container detected]`).
  - **Kiểm tra trạng thái có sẵn (Pre-existing state caution):** Bổ sung vào Operator, Planner và Checker quy tắc coi giỏ hàng hoặc form điền sẵn từ phiên trước là trạng thái lạ, bắt buộc đối chiếu nội dung thực tế (tên món, số lượng) với yêu cầu người dùng thay vì "nhận vơ" hoàn thành.
  - **Khóa cứng chuẩn mực trong `AGENTS.md`:** Thêm các mục 2.7, 2.8, 2.9 cấm hardcode tên nút/từ khóa, bắt buộc tư duy theo UI Pattern và thuộc tính Accessibility chuẩn.
- **Kiểm thử hồi quy:** Vượt qua 100% các bộ unit test (toàn bộ 263 tests trong `mcp` và `agents/flash`). Test mới: bỏ chờ ngay khi màn hình đã đổi cho `my_action`, giữ chờ cho `external`, chặn lặp qua thao tác xen giữa, lọc text cắt mép màn hình/ẩn.

### 📌 Bản 3.7 (Gói gửi AI gọn cho Flash — list gọn, O3, output ngắn, 2026-11-20)
- **List phần tử dạng gọn** (`agent.flash.element_list="compact"`): bỏ status bar + trạng thái mặc định, `[n] "text" l,t,r,b`, cảnh báo che khuất ngắn; chỉ Flash (cờ `ctx.flash_compact_list`), Pro giữ dạng cũ.
- **O3 — control không nhãn có index** (`[n] (icon|button|input|toggle) l,t,r,b`): thêm sau bước cảnh báo che khuất, bỏ container/wrapper trùng.
- **Prompt Flash**: ghi chú 1–2 câu (~40 từ), milestone 1 dòng; mô tả list theo dạng đang dùng.
- **LLM theo node**: `media_resolution` (Gemini: low/medium/high) và `max_tokens` đọc từ config node; preset `artemis.qwen.jsonc` ảnh 768, `artemis.gemini.jsonc` có gợi ý `media_resolution`.
- Số liệu Mục 1.11; 10 unit test mới (`tests/unit/mcp/test_compact_element_list.py`); hồi quy 852 pass.
- **`media_resolution` theo từng ảnh** (langchain bỏ qua tham số cấp model với Gemini 3) — đo thật: `low` thêm lượt, không khuyến nghị.
- **`turns.jsonl`** (tự động bật khi bật `ARTEMIS_DEBUG_AI_IO`): 1 dòng/turn với thời gian model/action/chụp, token, cỡ list, từng tool call (điểm bấm, `no_effect`). 7 test mới; hồi quy 863 pass.
- **Sửa O3 sau review** (2026-11-20): ngưỡng icon 15% → **5%** màn hình (ô nhập `EditText` giữ 15% vì rộng cả màn); wrapper bọc control khác → **chỉ giữ control con**; icon bị node vẽ **sau** trong cây (dialog, bottom sheet, scrim) che ≥ 50% → `(may be occluded)` + cờ `occluded` (chỉ trên icon, không lan sang phần tử có nhãn). Bỏ gợi ý `media_resolution: "low"` khỏi preset Gemini (ảnh Gemini luôn ~1078 token, nén ảnh chỉ giảm dung lượng upload). Kiểm trên 36 màn hình thật: 178 icon, 0 cờ sai. 3 test mới; hồi quy 862 pass (5 skip: cần thiết bị/jetski).
- **Cắt chờ thừa phía thiết bị** (2026-11-20, Mục 1.13): observe MCP bỏ settle 0.3 s lần hai (O1); `input_text` nhanh hơn ~0.7 s; Flash xả tóm tắt/capsule tối đa `agent.flash.final_summary_wait_s` (3 s) khi xong thay 30 s + 30 s; `turns.jsonl` có thời gian từng pha chụp. 9 test mới/sửa (`test_capture_speed.py`, `test_actuators.py`, `test_flash_turn_log.py`), fixture `observe_screen` tạo lại; hồi quy 871 pass.
- **Phân chia log AI I/O theo session** (2026-11-20): ghi riêng vào `traces/<session_id>/ai_io.log` bên cạnh `turns.jsonl`, thêm banner phân cách phiên vào file tổng `logs/ai_io.log`, API `/ai-io-log?session_id=...`. 3 unit test mới (`tests/unit/utils/test_ai_io_logger.py`).
- **Khắc phục & Xác thực Model Local Qwen2.5-VL 3B** (Ollama, 2026-11-20): Nâng `num_ctx: 16384` chống cắt cụt system prompt + danh sách UI; bổ sung Modelfile template Tool Calling chuẩn; đạt 13/13 tiêu chí đọc thông tin màn hình Grab và 5/5 pass trên benchmark thao tác đa màn hình thật (~1.86 s/lượt, 100% GPU RTX 3050 6GB).

### 📌 Bản 3.6 (Thu nhỏ ảnh gửi AI ở Flash — O8, 2026-11-20)
- **`artemis/utils/model_image.py` (mới) — `shrink_for_model`:** cạnh dài ≤ `max_side`, resize Lanczos (không làm mờ), JPEG `jpeg_quality`; ảnh đã nhỏ giữ nguyên; lỗi giải mã → gửi ảnh gốc; nhớ kết quả frame gần nhất.
- **Chỉ ảnh trong tail gửi AI** (`FlashRunner._build_tail`) bị thu nhỏ; OCR, guard, so sánh màn hình, trace vẫn dùng ảnh gốc. Toạ độ 0–1000 nên không ảnh hưởng dịch lệnh.
- **Cấu hình `agent.flash.model_image`:** mặc định code `max_side=0` (tắt); `config/artemis.jsonc` bật `1280` / q75.
- Số liệu offline: Mục 1.7. Kiểm chứng: 1010 unit test pass (7 test mới `tests/unit/agents/test_flash_model_image.py`).

### 📌 Bản 3.5 (OCR cục bộ ONNX cho Flash, 2026-11-20)
- **`artemis/utils/local_ocr.py` (mới):** RapidOCR (detector) + recognizer PaddleOCR `latin_PP-OCRv5_mobile_rec` (ONNX, Apache-2.0, ghim commit + SHA-256, tải 1 lần). Chạy trên ảnh điện thoại đã gửi về (không truyền thêm), `use_cls=False`, 4 luồng CPU (2.1 s → ~0.7–1 s/màn hình), cache 4 frame theo hash, lỗi thì bỏ qua OCR không làm hỏng quan sát.
- **Cấu hình `agent.flash.ocr`:** `mode` = `off` (mặc định code) / `auto` (bật trong `config/artemis.jsonc`: chỉ khi < 5 node app có nhãn) / `always`; `threads`; `rec_model_path` + `rec_keys_path` để thay model (vd tiếng Việt).
- **`fuse_ocr_with_xml(xml_first=True)`** (chỉ Flash dùng): XML `text` thắng OCR; dòng OCR ngoài mọi node thành node ảo (trước đây bị bỏ nên OCR vô dụng trên màn hình canvas/game). Pro giữ nguyên hành vi.
- **Cảnh báo che khuất** bỏ qua node OCR ảo (trước đó có thể đặt `occluded=True` sai và khiến Occlusion Guard chặn bấm).
- **Sửa lỗi Pro:** `operator.py` thiếu `import time` (từ commit `0f7693b`) làm Operator crash ngay lượt đầu và 28 unit test hỏng.
- Kiểm chứng: 1003 unit test pass (`tests/unit/mcp`, `agents`, `llm`, `utils`), gồm 19 test mới `tests/unit/mcp/test_local_ocr.py`; chạy thật trên ảnh trace: màn hình XML trống từ 0 → 18 dòng `OCR Text` có toạ độ.

### 📌 Bản 3.4 (Gộp PR upstream google/artemis — đợt 1, 2026-11-19)
- Gộp 4 PR đang mở của `google/artemis` vào nhánh `adopt/upstream-prs-1`: **#216, #244, #181, #171**. Danh sách đầy đủ và trạng thái ở **Mục 11** (tránh gộp lại lần nữa).
- **#216** `launch_app`: `monkey --pct-syskeys 0` — trên emulator / máy không phím cứng, lệnh `monkey` cũ không mở được app (mất ~45s cho 3 lần thử rồi agent phải tự mở từ app drawer).
- **#244** `parse_swipe_parameters`: `duration=0` (số) không còn bị thay bằng 800ms.
- **#181** `ui_filter` / `ocr_xml_fusion`: hộp thoại SystemUI / permissioncontroller toàn màn hình không còn bị coi là thanh trạng thái/điều hướng (nút trong hộp thoại bấm được, tọa độ OCR không lệch). Thanh hệ thống bị giới hạn cao ≤ 12% (trên) / ≤ 15% (dưới).
- **#171** `AndroidAdbDriver.input_text`: chạy local cũng dán qua clipboard (Accessibility Helper), đọc lại ô đang focus để xác nhận chữ đã vào; trả `False` khi thấy rõ ô vẫn trống thay vì luôn báo thành công.
  - **Sửa thêm so với PR gốc:** PR gọi `clear_text` của helper cả khi `clear_existing=False` → xóa mất chữ có sẵn ở chế độ gõ nối. Đã chỉ xóa khi `clear_existing=True`, thêm test `test_input_text_append_mode_does_not_clear_via_helper`.
- **Sửa thêm lỗi có sẵn (không do PR):** lệnh xóa `input keyevent --meta 1 122` (Shift+MOVE_HOME) chỉ đưa con trỏ về đầu ô, không bôi đen → `input_text(clear_existing=True)` không xóa được gì. Đổi sang `MOVE_END; Ctrl+A (keycombination 113 29); DEL; 20×backspace` (dùng `;` để vẫn chạy backspace trên Android < 12). Luồng chạy thật của actuator không bị ảnh hưởng (nó xóa bằng `erase_text` trước, rồi gõ với `clear_existing=False`).
- **Kiểm thử:** `tests/unit` 2639 passed, 5 skipped (bao gồm bộ regression bắt buộc `mcp/`, `agents/`, `llm/`).
- **Kiểm thử trên emulator `Grab_API34` (Android 14, 2026-11-19):**
  - #171: `adb input text` cũ với chữ có dấu → exception, ô trống. `input_text` mới (dán qua Accessibility Helper) → đúng nguyên văn `Xin chào, hôm nay trời đẹp quá`, `Đường phố Hà Nội – 36 phố phường`; gõ nối giữ chữ cũ; ~0,6–1,8 s/lần gõ.
  - #216: AVD này khai báo `hw.keyboard=yes` nên lệnh `monkey` cũ vẫn mở được app (Settings, Chrome, Maps); lệnh mới cũng đúng. Lợi ích chỉ thấy trên máy không có phím (chưa đo).
  - Lưu ý môi trường: `-gpu host` (NVIDIA PRIME) làm emulator crash (`DisplaySurfaceGlContextHelper context was preempted`); test chạy bằng `-gpu swiftshader_indirect`.

### 📌 Bản 3.3 (Kiểm chứng phủ toàn bộ đề bài)
- **Vấn đề:** review cuối của Checker chỉ chấm các dòng Planner khai báo; Planner (quy tắc "tiết chế") có khi chỉ khai 1 dòng → Shopee 64c570f2 báo 1/1 passed mà không xem tag Mall, rẻ nhất, phân loại, số lượng.
- **Checker** (`checker.py`, `checker.json`): `CheckReport.goal_requirements` — review cuối liệt kê MỌI yêu cầu cụ thể trong đề gốc chưa có dòng khai báo, chấm từng cái có bằng chứng; `verify` (kết quả phải đạt, failed → Operator nhận gợi ý và sửa) hoặc `assert` (điều cấm / trạng thái cuối). `merge_goal_requirements` đổi chúng thành dòng `[goal] ...` nên ledger, bảng pass/fail, vòng sửa lỗi, giao diện dùng nguyên. Mỗi verify failed phải có `suggestion` cụ thể.
- **Nhất quán giữa các lần kiểm:** dòng `[goal]` của lần trước được khai báo lại nguyên văn ở lần review cuối tiếp theo (`_goal_items_from_ledger` trong `graph.py`).
- **Planner** (`planner.json`): quy tắc "Goal coverage" — mỗi yêu cầu người dùng nêu (đối tượng, giá trị thuộc tính/bộ lọc, số lượng, tiêu chí chọn, điều kiện dừng/điều cấm) thành một dòng kiểm; quy tắc tiết chế chỉ áp dụng cho kiểm tra tự nghĩ ra.
- **Chạy thật qua Gemini CLI:** ca Shopee đúng → 7 yêu cầu `[goal]` đều passed (29 s); ca sai cố ý (không Mall, không rẻ nhất trong nhóm, số lượng 2) → bắt đủ 3 lỗi, mỗi lỗi kèm gợi ý sửa (21 s).
- **Kiểm thử:** 7 test mới (`tests/unit/agents/test_checker_goal_requirements.py`); `tests/unit` 2626 passed.

### 📌 Bản 3.2 (Gemini CLI thay hẳn API key)
- **Vấn đề:** preset "Gemini CLI" chỉ đưa các agent (Flash/Operator/Planner/Checker/Outputter) lên agy; các helper vẫn gọi Gemini API bằng key miễn phí (Hopper, summarizer, step summarizer mỗi bước, nén lịch sử, pixel safety net, planner_validation) → hết quota ngày (flash-lite 500/ngày) là task kẹt (Hopper retry mãi). Giao diện vẫn hiện "Gemini CLI" nên không ai biết.
- **Provider mới `agy_oneshot`** (`artemis/llm/agy_oneshot.py`): mỗi lần gọi là một `agy -p --mode plan --output-format json`, ảnh ghi ra thư mục tạm (`--add-dir`), `with_structured_output` dùng `--json-schema` của agy; tối đa 4 tiến trình song song. Đo thật: Hopper 9,2s, step summary có ảnh 8,7s.
- **`agy:<model>`** cho các helper cấu hình bằng tên model (`flash.step_summarizer.model`, `memory.chunking.model`) qua `get_model_by_spec`; tên thường vẫn là Gemini API.
- **`AgyChatModel.with_structured_output`** đi qua one-shot `--json-schema` thay cho tool-call giả (nguồn gốc khả dĩ của Checker trả report rỗng / sai key).
- **Config** (`artemis.jsonc` + preset `artemis.agy.jsonc`): fallback mặc định chuyển sang agy medium; hopper, summarizer, pixel safety net, planner_validation → `agy_oneshot`; step summarizer + nén lịch sử → `agy:gemini-3.8-flash-low`. Giữ Explorer `flash` (model ER định vị tọa độ, chỉ có trên API, chỉ gọi khi `ask_explorer`).
- **Sửa lỗi có sẵn:** khai báo `planner_validation` / `validator_pixel_safety_net` dưới `nodes` trước đây bị bỏ qua (`_expand_default_into_nodes`).
- **Giao diện:** `/api/system/model-config-env` trả `model_routing` (từng node chạy CLI hay API key); badge chỉ ghi "Gemini CLI" khi mọi node bắt buộc chạy CLI, ngược lại ghi "+ API key (N node)" và liệt kê; node chỉ gọi API khi cần (ER, video) hiện dạng ghi chú.
- **Kiểm thử:** `tests/unit` 2610 passed, 0 failed (2 test admin_console fail sẵn đã sửa để không phụ thuộc config máy). Test mới: `tests/unit/llm/test_agy_oneshot.py` (8, dùng agy giả), `tests/unit/test_model_routing.py` (3).

### 📌 Bản 3.1 (Lớp thực thi dùng chung Flash + Pro)
- **Chờ thông minh chuyển xuống action server** (`artemis/mcp/screen_effect.py`, móc trong `action_server.py`): server ghi nhận màn hình cuối cùng đưa cho agent (`observe_screen` của Flash, `take_screenshot` của Pro) và có thao tác thiết bị nào sau đó không. `wait_for_screen_change(after="my_action")`: màn hình đã đổi → chờ ổn định rồi trả ngay; không đổi mà đã có thao tác → trả `[NO_EFFECT]` (`ActionResult.no_effect=True`); không đổi và chưa thao tác → chờ thật. `after="external"` luôn chờ thật. Không có ảnh để so → chờ thật (uncertain).
- **Flash runner bỏ ~240 dòng logic trùng**; chỉ còn: gặp `[NO_EFFECT]` hoặc timeout thì huỷ phần còn lại của burst (lệnh không chạy báo `error`).
- **Guard nút cam kết bị "biến hình" cho Flash** (`McpActionExecutor._guard_morphed_commitment`): tap tọa độ/index sau thao tác đầu tiên trong burst đọc lại hierarchy thật; nếu điểm đó giờ là nút cam kết (`COMMITMENT_PATTERN`) mà trên màn model đã thấy thì không phải → chặn. Pro đã có safety net riêng.
- **Checker chịu được verdict sai định dạng** (`verdict` thay `status`, thiếu `status`/`kind`).
- **Checker:** review cuối trả report rỗng → hỏi lại một lần, liệt kê từng mục; bản ghi inconclusive kiểu "không có phán quyết" (no verdict / timeout / error) không còn ghi đè verdict có bằng chứng trước đó (`resolve_item_status`).
- **Báo cáo Flash ngang Pro** (`artemis/agents/flash/report.py`): mọi run Flash lưu báo cáo cuối (Markdown, bảng khi so sánh — quy tắc mới trong `flash_runner.md`) vào `notes/output.md`; khi có `expected_output_desc` thì chạy Outputter (như Pro) dựng lại báo cáo từ toàn bộ lịch sử. MCP không còn bỏ `expected_output_desc` với Flash. 5 test mới. `tests/unit`: 2600 passed, 2 failed có sẵn từ trước ở `tests/unit/admin_console/test_model_service.py` (không liên quan).
- Chưa làm: Operator của Pro chưa có `wait_for_screen_change` trong bộ công cụ (chỉ `wait_for_delay`), nên Pro chưa dùng được chờ thông minh.
- **Kiểm thử:** 791 passed, 1 skipped (`tests/unit/mcp/ agents/ llm/`). Test mới: `tests/unit/mcp/test_screen_effect.py` (8, chạy qua MCP tool thật), guard biến hình (4), burst abort khi `[NO_EFFECT]`, cờ `verify_live_target`. Fixture `action_server_manifest.json` tái tạo: chỉ thêm `no_effect` vào output schema.

### 📌 Bản 2 (Tối ưu phản hồi & Giảm tải tài nguyên cơ bản)
- **Trụ cột A:** Làm mới session agy khi context vượt ngưỡng (≥ 80k và tăng ≥ 15k; trần cứng 120k) để chống phình to token.
- **Trụ cột C:** Tự động điều chỉnh thời gian chờ ổn định màn hình (settle) theo loại action (`manage_app` 1500ms, ENTER 1000ms, thao tác thường 400ms). Chụp lại kiểm tra bằng dHash khi màn hình nghi vấn chưa đổi.
- **Trụ cột D:** Chặn thao tác bấm lặp cùng tọa độ (< 2%) khi màn hình không thay đổi; phát hiện gợi ý lỗi validation/form feedback.
- **Trụ cột E1:** Fast-burst thực thi chuỗi thao tác trên cùng một màn hình mà không cần quan sát trung gian (`fast_burst: true`).

### 📌 Bản 1 (Baseline & Phân tích điểm nghẽn ban đầu)
- Khảo sát các trace thực tế, nhận diện nguyên nhân cốt lõi gây chậm: một thao tác = một lượt AI, AI phải chờ cứng `wait_for_delay`, mở ảnh chụp tiêu tốn thêm lượt gọi.
- Thiết lập nguyên tắc cốt lõi: Phân định theo trạng thái kỳ vọng, tuyệt đối không viết luật riêng cho từng app.

---

## 0. NGUYÊN TẮC THIẾT KẾ

1. **App-agnostic (Phân định theo Trạng thái kỳ vọng, không theo tên app):** Không có bất kỳ luật nào dựa trên tên app hay tên nút. Mọi tương tác chỉ phân loại theo điều kiện ta đang chờ đợi:
   - *Biết rõ text sẽ xuất hiện:* Dùng `wait_for_text` (chờ chuyển trang, kết quả tìm kiếm).
   - *Biết rõ text sẽ biến mất:* Dùng `wait_until_gone` (chờ loading spinner, dialog đóng).
   - *Chờ biến chuyển chưa biết trước nội dung:* Dùng `wait_for_screen_change` (đối thủ đi cờ, tin nhắn chat mới, animation — kiểm tra XML diff trước, pixel diff sau).
   - *Bản thân thời gian là mục tiêu:* Dùng `wait_for_delay` (nghe nhạc 30s).
2. **AI quyết định, thiết bị tự chờ và kiểm tra:** Một lượt AI trả về **mọi thao tác nó đã chắc chắn, kèm cách chờ kết quả**. Thiết bị tự chạy, tự chờ, tự kiểm tra. AI chỉ được hỏi lại khi (a) cần một quyết định mới, hoặc (b) có chuyện ngoài dự kiến.
3. **Không chạy mù (Anchor & Visibility Guard):** Mọi bước tự động trên màn hình AI chưa nhìn thấy phải có **điều kiện neo (Anchor Guard)**: thấy đúng text đó mới bấm; không thấy, thấy nhiều kết quả mơ hồ, hoặc màn hình lạ → dừng chuỗi và hỏi AI. Phần tử XML bắt buộc phải có `bounds` hiển thị trên màn hình (loại bỏ prefetch ngoài màn hình hoặc tab ẩn).
4. **Code chặn những lỗi mà prompt không chặn được:** cấu trúc chuỗi (không click màn hình chưa thấy), nút cam kết ở cuối chuỗi, chặn bấm lặp, giới hạn thời gian và số lần timeout liên tiếp.
5. **Model-agnostic & Future-proof (Độc lập mô hình):** Chuẩn hóa toàn bộ hành động thành **Tool phẳng tuần tự** theo chuẩn Function Calling toàn cầu (OpenAI/Anthropic tool schema). Mọi tool call trong lượt đều bắt buộc phải có `ToolMessage` tương ứng (trả về `skipped`/`interrupted` khi chuỗi dừng sớm) để chống lỗi API 400 Bad Request.
6. **Đo trước khi tin:** mọi giả định phải kiểm chứng trên trace thật; mọi phase có tiêu chí đo được (thêm chỉ số tỷ lệ chọn đúng tool chờ, số lần timeout liên tiếp, số lần dừng vì anchor).

**Chỉ số chính:** **số action trên mỗi lượt AI** (hiện ~1.0, mục tiêu ≥ 2.0) và **số lượt AI mỗi tác vụ**.

### 0.1. HƯỚNG ĐI CHIẾN LƯỢC: CHỈ TỐI ƯU FLASH, SAU ĐÓ GỘP PRO VÀO FLASH (quyết định 2026-11-19)

> [!IMPORTANT]
> **Không đầu tư tối ưu riêng cho Pro nữa.** Mọi công sức tối ưu dồn vào **Flash** (`artemis/agents/flash/runner.py`, `artemis/mcp/action_executor.py`, `artemis/mcp/observation.py` và tầng thiết bị dùng chung). Pro chỉ sửa khi có lỗi làm hỏng tác vụ hoặc test hồi quy, không thêm tính năng/tối ưu mới.

**Lý do:**
- Người dùng chỉ muốn **một chế độ vừa chính xác vừa nhanh**; phải chọn giữa 2 chế độ là gánh nặng.
- Số liệu thực nghiệm: cùng đề, Flash ra **kết quả giống Pro** nhưng nhanh hơn (Mục 1.5: 3p35s vs 4p45s, cùng sản phẩm và tổng tiền; Mục 1.4: ~268s vs ~7 phút). Pro tốn thêm lượt gọi AI ở Planner, Checker và pixel safety net (~67s riêng safety net ở Mục 1.5).

**Đích triển khai (quyết định 2026-11-20): Gemini API key với model bản thấp** (preset `config/artemis.gemini.jsonc`: Flash chính + `flash-lite` cho tác vụ phụ), không phải agy/Gemini CLI. Tối ưu model chỉ nhắm đường API:
- Context do ARTEMIS kiểm soát (transcript ledger: ảnh giữ 3 lượt gần nhất, list XML 1 lượt, nén chunk) → giữ prompt ổn định, không phình như agy.
- **Prompt cache**: giữ phần S (system prompt) byte-ổn định để Gemini cache ngầm; đo `cached_ratio`.
- **Token ảnh**: thu nhỏ ảnh (đã có `agent.flash.model_image`) + cân nhắc `media_resolution` của Gemini API (chưa có trong code).
- **Model bản thấp** suy luận yếu hơn → danh sách phần tử phải rõ (O3 icon không nhãn, OCR) và guard bằng code quan trọng hơn.
- Bỏ các tối ưu riêng cho agy (ngưỡng làm mới phiên `max_context_tokens`, giao thức `finish`).
- Điểm nghẽn ngoài model vẫn giữ: `wait_for_screen_change` (~29% thời gian), `hopper` (~6%).

**Lộ trình:**
1. **Tối ưu Flash** theo danh sách O1–O8 trong [`docs/FLOW_DIEU_KHIEN_THIET_BI.md`](docs/FLOW_DIEU_KHIEN_THIET_BI.md) Mục 8.2:
   - Đợt 1 (tốc độ, rủi ro thấp): O1 bỏ settle 0.3s cộng dồn, O2 bọc HTTP snapshot bằng `asyncio.to_thread`, O6 lưu ảnh nền trong `observe()`.
   - Đợt 2 (chính xác): O3 đưa icon không nhãn nhưng clickable vào list; đưa vào Flash các thành phần **thuần code** của Pro: XML safety net trước khi bấm, Execution Incident.
   - Đợt 3: O5 tap/swipe qua RPC helper thay `adb shell input` (thử trên máy thật).
2. **Kiểm chứng Flash trên các loại task Pro mạnh hơn** trước khi gộp: task dài nhiều nhánh, có nút cam kết (thanh toán/xác nhận/xoá), UI khó (icon không chữ, popup, load chậm), task giám sát/chờ lâu (`[Loop:continuous]`).
3. **Gộp:** Flash thành chế độ duy nhất. Các phần tốn LLM của Pro chỉ bật **theo tình huống**: plan qua tool `update_plan` khi task dài, Checker duyệt 1 lần lúc `report_task_status`, pixel safety net chỉ cho nút cam kết hoặc khi XML không đủ, tự nâng Explorer khi bấm trượt liên tiếp. Tham số `profile` giữ làm alias tương thích; `verification_level` giữ làm tuỳ chọn mức kiểm tra. Chỉ xoá đồ thị LangGraph khi Flash đạt tỉ lệ thành công ≥ Pro trên benchmark.

---

## 1. DỮ LIỆU THỰC NGHIỆM

### 1.1. Trace Grab — `0cdd2531-b4ff-4a81-b2f1-d485dccbd1c2` (trước tối ưu)

23 lượt, ~232s. Model ~172s (74%), chụp ~36s, thao tác ~14s. Token vào 23.8k → 122.2k, thời gian model 3s → 12s. 13/22 lượt "2 lần gọi · mở ảnh". Lượt thừa: 6, 13, 22 (chờ load), 19 (bấm lặp), 12/14 (tưởng màn hình không đổi). Chi tiết từng bước: xem bản 2 (git history).

### 1.2. Trace cờ vua — `2f9cba53-4a97-4984-a73e-a66c33eac724` (sau Trụ cột A, C, D, E1)

Chess.com trên Chrome, emulator 720×1280, profile `flash`. 22 lượt, ~312s.

| Thành phần | Thời gian | Tỷ lệ |
|:--|:-:|:-:|
| Model | 236.9s | 76% |
| Thao tác thiết bị (gồm `wait_for_delay`) | 55.1s | 18% |
| Chụp màn hình | 16.7s | 5% |

**Mỗi nước đi = 2 lượt AI:**

| Lượt | Việc | Thời gian |
|:--|:--|:-:|
| A | Nhìn bàn cờ → `click_sequence(quân, ô đích)` | ~9–12s |
| B | Nhìn lại → `wait_for_delay(5000)` chờ đối thủ | ~13–15s (model ~8–10s + chờ cứng 5s) |

**Nhận xét:**
- Lượt B là lượt thừa: lần nào AI cũng chỉ quyết định "chờ 5 giây". Chờ cứng 5s cũng thừa khi đối thủ đi sau 1s.
- **22/22 lượt "agy 2 calls"**: bàn cờ là canvas, XML không có quân cờ → lượt nào cũng phải `read_file` mở ảnh. Trụ cột B là bắt buộc cho loại app này.
- Context tăng ~6k token/lượt (danh sách phần tử của trang web rất dài, gần như vô dụng cho việc đánh cờ).
- **Trụ cột A hoạt động đúng:** token tụt 81k → 29k (bước 11) và 83k → 30k (bước 21); AI đánh tiếp bình thường. Đây cũng là bằng chứng transcript nén vẫn được tạo khi chạy agy.
- Bước 7 mất 37s model: một lần agy nghẽn đơn lẻ, không lặp lại.

---

### 1.3. Grab & Shopee trên máy thật — sau `after=my_action/external`, chờ ổn định, guard bấm lặp thuần (2026-11-19)
Cùng nhiệm vụ "đi tới màn hình đặt hàng, không bấm đặt", Flash, điện thoại thật:

| Trace | Thời gian | Bước | Lệnh chờ hết giờ | Ghi chú |
|---|---|---|---|---|
| Grab trước — `d912751d-3b47-4b81-82b4-da6e99a2927f` | ~292s | 37 | 9 (≈66s) | Bấm Confirm address 4 lần; không cuộn xem lựa chọn giao hàng |
| Grab sau — `3fddc4e1-1632-44a2-9d70-92f09bcd824b` | 181s | 21 | **0** | Lệnh chờ trả về ngay khi màn hình ổn định; Confirm không tác dụng → bỏ chờ, cuộn xem lựa chọn rồi chọn "Meet at lobby" |
| Shopee — `7a16cfb5-4da0-44bf-990b-5a63a145a885` | 115s | 14 | 1 (≈14s) | Lệnh chờ mở đầu lượt khi kết quả đã tải xong → đã sửa: chờ mở đầu lượt cũng so với ảnh model vừa thấy |

Cả hai dừng đúng ở màn hình đặt hàng, không bấm đặt.

**Pro, Grab, `verification_level=checkpoints` — `a8445e7d-5dcf-4155-aa2f-bb7b6018cee9`** (giỏ có sẵn "Phở Bò Tái lớn", yêu cầu "Phở Bò Tái vừa"): ~9 phút. Operator làm đúng: tự phát hiện và xoá món cũ, giỏ cuối đúng 1 Phở Bò Tái vừa, ghi đủ chi tiết vào note. Safety net chặn 1 cú bấm tọa độ cũ của "Review address" khi nút đó đã đổi thành "Place Order". **Checker hỏng hoàn toàn:** model trả verdict thiếu `status`/`kind` (hoặc dùng key `verdict`) → `CheckReport` validation lỗi, run bị pause, 2 checkpoint timeout 180s, kết quả `inconclusive=4` và run vẫn được đánh PASS (fail-open). Đã sửa: `CheckVerdict` chấp nhận `verdict`, thiếu `status` → `inconclusive`, thiếu `kind` → lấy theo check item được trích dẫn (4 test mới). Chưa chạy lại trên máy.

### 1.4. Bài so giá Grab (4 quán, rẻ nhất có điều kiện) — Flash vs Pro sau lớp thực thi chung (2026-11-19)
| | Flash `a693e54c-93b9-4b83-b725-b24abba6d874` | Pro `14f3ebb7-ca1c-4434-8dec-a1aaca50b7b6` |
|---|---|---|
| Giỏ lúc bắt đầu | Có 1 món cũ của quán khác (khó hơn) | Trống |
| Thời gian | ~268s, 43 bước | ~7 phút, 41 bước |
| 4 quán đã xem | O Thuỷ 57k, Phở Trang 75k, Phở 24 Sun Ave 66k, Phở 24 Lexington 66k+1k | O Thuỷ 57k, Phở Trang 75k, Phở 24 Sun Ave 66k, **Phở Gánh 4 48k+2k** |
| Chọn | O Thuỷ — tổng thật ở màn thanh toán **73.000₫** | Phở Gánh 4 — tổng thật **75.000₫** (phí giao 24k + phí đơn nhỏ 3k) |
| Ghi chú | Lệnh chờ trả về ngay sau mỗi tap (server) | Ghi chú so sánh có bảng; gõ nhầm "Meet at lobby" vào ô Block/floor; Checker checkpoint pass 2/2 nhưng review cuối rỗng → bị ghi đè thành inconclusive=3 (đã sửa) |

Bài học: phí giao hiển thị ở danh sách ("Free", "2.000đ") khác xa phí thật ở màn thanh toán (12–24k + phí đơn nhỏ). Cả hai đều chọn theo giá danh sách, nên "rẻ nhất" chưa chắc đúng; cần xác nhận tổng thật của 1–2 ứng viên đầu.

### 1.5. Shopee lọc nhiều điều kiện + "Mua ngay" — Flash vs Pro, chế độ Gemini CLI hoàn toàn (2026-11-19)
Cả hai bắt đầu từ Shopee đã force-stop, cùng đề, dừng ở trang Thanh toán (không đặt). 0 lỗi quota API.

| | Flash `c08ce0fa-456e-4b57-87cf-934a4f833360` | Pro `64c570f2-0342-46c9-9720-8f42397e006f` (checkpoints) |
|---|---|---|
| Thời gian | ~3 phút 35 giây, 33 bước | ~4 phút 45 giây, 24 bước |
| Sản phẩm chọn | Cáp Type-C 30cm QMP HOME (Mall) 11.900₫ | Giống Flash |
| Tổng thanh toán | 17.900₫ | 17.900₫ |
| Báo cáo | `output.md` Markdown, bảng 2 sản phẩm Mall | Ghi chú liệt kê 10 sản phẩm đầu (có dòng lặp) + lý do bật lọc Mall |
| Kiểm chứng | — | Checker 1/1 passed, có bằng chứng (lần đầu không lỗi định dạng / không treo) |
| Chi phí CLI | step summarizer qua CLI: 0 lỗi | pixel safety net qua CLI: 5 lần, 8–23 s/lần (~67 s) |

Ghi chú: sản phẩm rẻ nhất có tag Mall chỉ có phân loại 30cm, cả hai áp dụng quy tắc dự phòng "gần 1m nhất" và ghi rõ trong báo cáo. Pro chỉ sinh 1 mục kiểm chứng (thiếu mục phân loại/giá).

### 1.6. Thử OCR cục bộ bằng ONNX (RapidOCR) trên ảnh trong `traces/` — offline (2026-11-20)
30 ảnh màn hình (5 ảnh/phiên: Grab ×2, Shopee ×2, Lichess/Chrome, app cờ vua), so với XML accessibility lưu cùng ảnh. CPU i5-12450HX, onnxruntime 1.32 (CPU), cắt status bar trước khi OCR.

| Chỉ số | RapidOCR mặc định (`ch_PP-OCRv4`, Trung+Anh) | Latin chính thức (`latin_PP-OCRv5_mobile_rec_onnx`) |
|---|---|---|
| Thời gian/ảnh (trung vị / p90) | ~2.1 s / 2.5 s | ~1.9 s / 2.35 s |
| Chữ OCR khớp đúng nguyên văn XML | 28% | 39% |
| Lỗi dấu tiếng Việt | ~99% chữ có dấu bị mất dấu ("Mức độ cay" → "Murc do cay") | 37%; bỏ hẳn ký tự thiếu trong từ điển ("Trà Đen Sữa" → "Trà Đen Sa") |

- Từ điển Latin chính thức **thiếu 38 ký tự tiếng Việt** (dấu hỏi, nặng, dấu chồng: ả ạ ậ ể ộ ợ ự…) → chưa có model ONNX chính thức đọc đúng tiếng Việt. Các model tiếng Việt cộng đồng trên Hugging Face đều ở dạng Paddle (cần chuyển ONNX), ít lượt dùng, có model cấm thương mại (CC-BY-NC).
- Trên app native (Grab, Shopee) XML **đã có gần đủ text**; phần OCR "thêm" chủ yếu là bản đọc sai của text đã có. OCR chỉ thật sự thêm thông tin ở màn hình XML trống (1/30 ảnh).
- Thiếu hụt lớn hơn là **phần tử bấm được không có nhãn**: trung bình ~22–24/màn hình Shopee, ~6 ở app cờ — OCR không giải quyết được (→ ưu tiên O3 trong `docs/FLOW_DIEU_KHIEN_THIET_BI.md`).
- Thu nhỏ ảnh xuống rộng 576px trước khi OCR chỉ nhanh hơn ~15% nhưng mất ~50% chữ → OCR phải chạy trên ảnh gốc; chỉ ảnh gửi cho AI mới thu nhỏ.
- XML lưu trong trace có cả node `com.android.systemui` (đồng hồ, icon thông báo) → lọt vào danh sách UI như `[1] Text: '5:07'`, là nhiễu.

**Vòng 2 — tìm OCR đọc tốt tiếng Việt (cùng 30 ảnh, cùng bộ tìm vùng chữ RapidOCR, chỉ đổi bộ đọc chữ; đúng = chữ OCR nằm trong text của node XML chứa nó):**

| Bộ đọc chữ | Đúng (mọi chữ) | Đúng (chữ có dấu) | Thời gian đọc/màn hình | Định dạng / license |
|---|---|---|---|---|
| RapidOCR mặc định (Trung+Anh) | 40% | 3% | ~0.58 s | ONNX / Apache-2.0 |
| PaddleOCR Latin v5 chính thức | 48% | 19% | ~0.30 s | ONNX / Apache-2.0 |
| **VietOCR `vgg_seq2seq`** | **58%** | **56%** | ~0.94 s | PyTorch / Apache-2.0 |
| VietOCR `vgg_transformer` | 60% | 61% | ~2.2 s | PyTorch / Apache-2.0 |

- Tìm vùng chữ (det) ~0.37 s/màn hình sau khi chỉnh `use_cls=False`, `intra_op_num_threads=4` (mặc định -1 tranh luồng trên CPU P/E-core: 2.1 s → 0.9 s cho cả pipeline RapidOCR).
- Tỉ lệ "đúng" là cận dưới: phần lớn lỗi còn lại của VietOCR là chữ bị app cắt "..." trên màn hình hoặc chữ nằm trong ảnh (banner) mà XML không có — tức OCR đọc đúng cái hiển thị.
- **Vòng 3 — EasyOCR (`vi`+`en`, PyTorch, Apache-2.0)** và tách theo ngôn ngữ / app Grab (lần đo này máy tải khác nên thời gian lệch ±30% so với bảng trên):

| | Đúng: tiếng Anh | Đúng: tiếng Việt có dấu | Đúng: Grab, tiếng Anh | Thời gian/màn hình |
|---|---|---|---|---|
| RapidOCR mặc định | 59% | 3% | 73% | det 0.33 s + đọc 0.24 s |
| **RapidOCR + Latin v5 (ONNX)** | **63%** | 19% | **76%** | det 0.33 s + đọc 0.23 s |
| VietOCR seq2seq | 59% | 56% | 73% | det 0.33 s + đọc 0.72 s |
| EasyOCR (chỉ phần đọc) | 56% | 55% | 70% | det 0.33 s + đọc 0.44 s |
| EasyOCR trọn bộ (CRAFT riêng) | 71% | 67% | 70% | **~9.5 s** (quá chậm trên CPU) |

  Phần lớn chữ tiếng Anh bị chấm "sai" thực ra là chữ trong banner quảng cáo mà XML chỉ ghi nhãn kỹ thuật (`FEED_ADS_EXTERNAL`, `Ad`) → OCR đem lại thông tin mới; hoặc OCR gộp giá gạch + giá mới ("225.000250.000"). Lỗi đọc thật: dính chữ ("results inDelivery"), `₫` → `d`, icon → ký tự lạ.
- `Vphuc/PP-OCRv5-mobile-rec-vi` (Apache-2.0, có lớp ký tự tiếng Việt) bị gated trên Hugging Face → chưa thử; model card tự báo WER 32% trên đề thi.

Kết luận: chưa đưa OCR ONNX vào luồng chính. Cần (a) model nhận dạng có từ điển tiếng Việt đầy đủ (hỏi công ty có sẵn chưa), (b) chỉ chạy khi XML nghèo text, (c) cân nhắc `onnxruntime-gpu` vì ~2 s/ảnh trên CPU quá chậm cho mỗi lượt.

### 1.7. Thu nhỏ ảnh gửi AI — đo offline trên 60 ảnh `traces/` (2026-11-20)
Ảnh gốc trung bình 1280×2772 (JPEG q80 của helper). Token là **ước tính theo công thức công khai** (Claude ≈ w·h/750 sau khi tự resize về cạnh dài 1568; Gemini ≈ 258 token × số ô 768×768), chưa phải số đo — cần so `prompt_tokens` trên task thật.

| Mức | Kích thước | KB/ảnh | base64 KB | Thời gian nén | Token ước tính (Claude) | Token ước tính (Gemini, chia ô) |
|---|---|---|---|---|---|---|
| Gốc | 1280×2772 | 203 | 270 | — | ~1440 | ~1650 |
| 1600 q80 | 739×1600 | 106 | 142 | ~57 ms | ~1440 | ~705 |
| **1280 q75 (chọn)** | **591×1280** | **76 (−63%)** | **101** | ~54 ms | **~1070 (−26%)** | **~516 (−69%)** |
| 1066 q70 | 492×1066 | 54 | 72 | ~52 ms | ~740 | ~516 |

So sánh bằng mắt trên màn hình Shopee nhiều chữ: ở 1280 q75 chữ cỡ thường kể cả dấu tiếng Việt ("Đã sử dụng", "Khoảng Giá") vẫn sắc; 1066 bắt đầu nhoè viền chữ. Gemini 3 có thể tính token ảnh theo `media_resolution` cố định thay vì theo kích thước → khi đó lợi ích chính là dung lượng truyền/ghi file (agy), không phải token.

### 1.8. ADB qua Wi-Fi vs USB + chạy thật Flash trên Grab (máy `25069PTEBG`, Android 16, 1280×2772, 2026-11-20)
Code đã có OCR `auto` + ảnh 1280 q75; model `agy:gemini-3.8-flash-low`; tắt quay video; trước mỗi lần chạy `force-stop` Grab + về Home.

**Đường truyền (đo riêng, chỉ đọc, 10 lần):**

| | Wi-Fi (`192.168.1.132:38825`) | USB (`L7HAK7UCXCXCT8WK`) |
|---|---|---|
| `adb shell echo` (trung vị / max) | 37 / 70 ms | 23 / 31 ms |
| Helper `/snapshot` XML+JPEG (trung vị / p90 / max) | 514 / 548 / 658 ms | 438 / 450 / 464 ms |
| `adb screencap` PNG | 463 ms | 407 ms |

→ USB nhanh hơn ~15% và ổn định hơn rõ; phần lớn thời gian `/snapshot` là chụp + nén trên máy, không phải truyền.

**Chạy thật (2 đề khác nhau → không so tốc độ trực tiếp):**

| | Wi-Fi — xem 3 món đầu (`d57f7120`) | USB — tới màn thanh toán (`2d0c038b`) |
|---|---|---|
| Kết quả | ✅ đúng (Phở 24 - The Sun Avenue + 3 món) | ⚠️ dừng đúng ở màn thanh toán, không đặt; **nhưng dùng món có sẵn trong giỏ từ phiên cũ ("Phở Bò Tái vừa"), không thêm "món đầu tiên"** |
| Thời gian / lượt AI / bước | 146.6 s / 10 / 18 | 153.1 s / 12 / 24 |
| Model mỗi lượt | 3.7–8.4 s | 3.8–9.5 s |
| Chụp màn hình mỗi lượt | 1.0–2.6 s | 0.9–1.9 s |
| Lượt 1 | 44.3 s (`hopper` 7.2 s + `wait_for_screen_change` 28.8 s) | 20.8 s (`hopper` 8.8 s + wait 5.0 s) |
| Input token lượt 1 → cuối (cache) | 24.7k → 53.4k (0 cached) | 25.1k → 59.6k (0 cached) |
| OCR (`auto`) | 1 lần, 1.4 s (màn tải) | 0 lần |
| Ảnh có gửi AI? | Có, mọi lượt (agy chế độ ACP gửi ảnh inline; nhãn log "screenshot skipped" là hằng số sai ở `agy_model.py`, không phản ánh thực tế) | Có, mọi lượt |

Phát hiện: (1) **vi phạm quy tắc 2.8 (trạng thái có sẵn)** — AI thấy `Basket • 1 Item` nhưng không kiểm tra món có khớp đề; (2) AI tự đổi chỉ dẫn giao hàng ("Not applicable", "Meet at lobby") để qua form bắt buộc; (3) `hopper` tốn 7–9 s mỗi lần mở app chỉ để đổi tên app → package; (4) `wait_for_screen_change(after=my_action)` có lần mất 28.8 s mới "ổn định" (màn mở đầu có banner động); (5) token tăng ~3k/lượt và **không có cache**: agy giữ nguyên mọi tin nhắn và mọi ảnh đã nhận trong phiên, nên việc ARTEMIS tự xoá ảnh/list cũ không làm agy nhỏ lại; (6) `wait_for_screen_change` tổng ~44 s/153 s (USB): mỗi lần "nhìn" là một `/snapshot` đầy đủ ~0.44 s, judge 1–2 lần + settle tới 6 lần.

### 1.9. Cùng một màn hình, gửi kiểu nào nhanh + đúng? — Gemini API `gemini-3.5-flash-lite`, free tier (2026-11-20)

Màn Grab kết quả "phở bò" (`traces/images/7db633f8c6b3…`, 1280×2772), câu hỏi đọc 6 món + giá, 2 quán (sao, số đánh giá, phí ship cũ/mới, phút), "Min. spend" chữ nhỏ → 13 tiêu chí. Mỗi kiểu 2 lần. Script: scratchpad `bench_payload.py`.

| Kiểu gửi | Ảnh (KB) | Token ảnh | Token chữ | Thời gian | Đúng |
|---|---|---|---|---|---|
| Ảnh gốc + list XML | 363 | 1078 | 1597 | 2.3–3.3 s | 13/13 |
| Ảnh 1280 q75 + list XML (**hiện tại**) | 112 | 1078 | 1597 | 2.1–3.0 s | 13/13 |
| Ảnh 960 q60 + list XML | 59 | 1078 | 1597 | 2.0–2.5 s | 13/13 |
| Ảnh 768 q50 + list XML | 38 | 1078 | 1597 | 1.7–4.6 s | 13/13 |
| Ảnh 1280 q75 + `mediaResolution=LOW` | 112 | **264** | 1597 | 2.5 s | 13/13 |
| Ảnh 1280 q75 + list XML+OCR | 112 | 1078 | 2958 | 2.0–2.5 s | 13/13 |
| Không ảnh, list XML+OCR | 0 | 0 | 2958 | 2.1–2.2 s | 12/13, 13/13 |
| Chỉ ảnh, không list | 112 | 1078 | 85 | 2.0–2.1 s | 13/13 |

Kết luận: (1) **token ảnh cố định ~1078 bất kể kích thước/độ nét** — nén ảnh chỉ giảm byte upload, không giảm token; chỉ `mediaResolution` giảm token ảnh (LOW −75%, vẫn đọc đủ trên màn này); (2) **list phần tử tốn token hơn ảnh** (XML 1597, thêm OCR +1361) → rút gọn list là chỗ giảm token lớn nhất; (3) thời gian ~2 s/lần chủ yếu theo số token output, chênh lệch input ở mức này nằm trong nhiễu; (4) gọi API trực tiếp ~2 s/lần so với ~5.7 s/lượt qua agy. Giới hạn: bài đọc, chưa đo bài chọn đúng phần tử để bấm (grounding).

### 1.10. Cùng bài trên với Qwen công ty — `Qwen3.8-27B-UD-Q4_K_M.gguf:no_think` (llama.cpp, LAN, `cache_prompt=false`, 2026-11-20)

| Kiểu gửi | Token vào | Prefill | Sinh (≈400 token) | Tổng chờ | Đúng |
|---|---|---|---|---|---|
| Ảnh 1280 q75 + XML (**hiện tại**) | 2253 | 10.7–11.0 s | 11.4 s | 28–65 s | 13/13 |
| Ảnh 960 + XML | 1953 | 7.7 s | 11.1 s | 42–60 s | 12/13 |
| **Ảnh 768 + XML** | 1797 | **6.4 s** | 11.3 s | 36–40 s | **13/13** |
| Ảnh 640 + XML | 1713 | 5.8 s | 11.4 s | 49–53 s | 12/13 |
| Không ảnh, chỉ XML | 1531 | 4.3 s | 11.9 s | 61 s | **6/13** |
| Không ảnh, XML+OCR | 2904 | 7.3 s | 13.6 s | 68–70 s | 13/13 |
| Ảnh 960 + XML+OCR | 3326 | 11.1 s | 10.7 s | 71–75 s | 13/13 |
| Chỉ ảnh 960 | 514 | 4.6 s | 12.1 s | 48–68 s | 11/13 |

Kết luận: (1) khác Gemini, **token ảnh Qwen tỉ lệ với số pixel** (1280→~720 token, 768→~270, 640→~180) và ảnh to tốn thêm thời gian bộ mã hoá ảnh → giảm pixel có tác dụng thật; 768 là điểm cân bằng trên màn này (lỗi 12/13 ở 960/640 là chữ nhỏ gạch ngang màu cam, có nhiễu); (2) **sinh output ~35 token/s → 400 token = 11 s**, chiếm phần lớn thời gian tính toán → output ngắn là đòn bẩy số 1 với Qwen; (3) prefill ~350 token/s → mỗi 1000 token chữ ≈ 3 s, nên list gọn + cache tiền tố (`cache_prompt`, system prompt cố định) rất quan trọng: system prompt Flash ~5k token ≈ 14 s nếu không cache; (4) bỏ ảnh là sai nhiều (XML thiếu giá/sao) → luôn gửi ảnh; (5) **tổng chờ 28–75 s ≫ prefill+sinh 16–22 s** → server dùng chung đang xếp hàng, cần hỏi đội vận hành số slot song song.

### 1.11. Sau khi làm list gọn + control không nhãn (cùng màn, cùng câu hỏi, 3 lần/kiểu, 2026-11-20)

| Model | Kiểu gửi | Token vào | Thời gian tính | Đúng |
|---|---|---|---|---|
| Gemini 3.5 Flash-Lite | Cũ: ảnh 1280 + list đầy đủ | 2675 (ảnh 1078 + chữ 1597) | 2.1 s | 13/13 ×3 |
| Gemini 3.5 Flash-Lite | Mới: ảnh 1280 + list gọn | 2102 (chữ **1024**, −36%) | 1.7–2.4 s | 13/13 ×3 |
| Gemini 3.5 Flash-Lite | Mới + `media_resolution=low` | **1288 (−52%)** | 1.4–3.3 s | 13/13 ×3 |
| Qwen3.8-27B (server đang đông) | Cũ: ảnh 1280 + list đầy đủ | 2253 | prefill 13.6–14.9 s + sinh 14.7 s; 1 lần 503 "no capacity" | 13/13 ×2 |
| Qwen3.8-27B | Mới: ảnh 768 + list gọn | **1297 (−42%)** | prefill **6.9–7.1 s** + sinh 14.3 s | 13/13 ×3 |

**Task thật (2026-11-20, máy `25069PTEBG` qua ADB Wi-Fi, Gemini API `gemini-3.5-flash-lite` free tier, task chỉ đọc "mở Grab → Food → tìm pho → quán đầu → báo 3 món đầu", không bấm đặt hàng):**

| Lần | Cấu hình | Thời gian task | Lượt | Model (tổng) | Token vào/lượt | Cache | Ghi chú |
|---|---|---|---|---|---|---|---|
| agy (1.8) | list đầy đủ, ảnh 1280 | 146.6 s | 10 | — | 24.7k → 53.4k | 0% | trước khi chuyển API |
| Vòng 1 | Cũ: list đầy đủ | 73.4 s | 9 | — | 9.9k → 17.1k | 43% | `hopper` 1.5 s (agy 7–9 s) |
| Vòng 1 | Mới: list gọn | 79.7 s | 12 | — | 10.1k → 18.1k | 45% | cuộn thêm 2 lần |
| Vòng 2 (`turns.jsonl`) | Cũ: list đầy đủ | 96.1 s | 11 | 56.6 s (5.1 s/lượt) | 10.7k → 18.1k | 43% | — |
| Vòng 2 | **Mới: list gọn** | **77.2 s** | 11 | **40.3 s (3.7 s/lượt)** | 10.1k → 18.4k | 44% | đúng quán, 3 món |
| Vòng 2 | Mới + `media_resolution=low` (đã gửi thật theo từng ảnh) | 134.8 s | **16** | 90.5 s (5.7 s/lượt) | 9.3k → 14.4k | 58% | cuộn vùng nhỏ 8 lần, nhìn ảnh kém → **không dùng LOW** |

Kết luận: (1) API key nhanh ~2× agy; (2) token vào mỗi lượt ~10k là **system prompt + khai báo 16 tool**, list chỉ chiếm vài trăm–1k token → giảm list không đổi nhiều token nhưng vòng 2 model nhanh hơn 1.4 s/lượt (cần thêm lần chạy để loại nhiễu mạng); (3) `low` giảm token nhưng làm model nhìn kém, thêm lượt → chậm hơn; (4) điểm nghẽn còn lại theo `turns.jsonl`: model ~52–59%, chụp màn hình sau action ~20–26%, action ~13–19%.

List gọn có thêm 7 dòng control không nhãn (6 nút "+" thêm món + nút lọc) mà dạng cũ không có. Thời gian Gemini ở mức ~2 s do output (~400 token) quyết định; Qwen giảm nửa thời gian prefill. Chưa đo trên task thật (cần cắm máy).

### 1.12. Rút gọn system prompt / mô tả tool có làm AI nhanh hơn không? — Gemini API `gemini-3.5-flash-lite`, free tier (2026-11-20)
Phần cố định mỗi lượt: system prompt Flash ~18 100 ký tự + 16 tool ~16 400 ký tự ≈ 10k token (ảnh chỉ ~1078). Gọi REST thẳng với prompt + tool thật, ảnh 1280 q75 + list gọn, đo giây của một lần gọi.

| Kiểu | Token vào | 8 màn khác nhau: TB / trung vị | Quyết định |
|---|---|---|---|
| FULL (đang dùng) | ~10.2k | 5.66 / 7.27 s | chuẩn (dùng index) |
| Mô tả tool chỉ giữ câu đầu | ~9.4k | 6.15 / 6.58 s | 2/8 màn đổi sang toạ độ / thiếu target |
| + bỏ nửa sau system prompt (mục 4–6) | ~6.4k | 5.98 / 6.41 s | 5/8 màn bấm bằng toạ độ thay vì index |

- **Không nhanh hơn**: −4k token không đổi thời gian; độ trễ **2 cụm** ~1.5–2.5 s và ~5–9 s, không phụ thuộc độ dài.
- **Cùng một payload thì luôn cùng cụm**, thêm **1 dấu cách** cuối system prompt → FULL từ ~6 s xuống ~1.5 s (3/3 lần), dấu cách trong mô tả `click` → vẫn ~6 s. ⇒ phía Google định tuyến theo nội dung request, một số máy chủ chậm (free tier). Ngoài đời mỗi lượt ảnh khác nên rơi ngẫu nhiên.
- **Kết luận**: không rút gọn prompt/tool (không lợi tốc độ, AI chọn kém hơn). Đòn bẩy thật: gửi request dự phòng (hedge) khi quá ~2.5 s, hoặc tier trả phí.

### 1.13. Cắt chờ thừa phía thiết bị — Flash, Gemini API flash-lite, máy `25069PTEBG` qua Wi-Fi ADB (2026-11-20)
Đo bằng `turns.jsonl` (mới có `obs`: settle/capture/ocr/list/persist cho từng lần chụp). Thời gian AI không so được (free tier 503/504: 9–54 lỗi/lần chạy), chỉ so phần thiết bị:

| | Trước (`base`) | Sau (`fix1`–`fix4`) |
|---|---|---|
| Chụp sau thao tác (capture, trung vị) | 0.72 s | 0.41–0.61 s (bỏ settle 0.3 s lần hai trong driver) |
| `input_text` | 2.6–3.2 s | 1.8–2.5 s (focus đọc cây UI thay chụp ảnh; poll focus thay sleep 1.0 s; xoá 1 RPC helper thay 4 lệnh `input`; kiểm dán ngay thay sleep 0.35 s) |
| Lượt cuối sau `report_task_status` | 18.7–30.3 s (+30 s nữa sau "Session ended") | 3.4–4.1 s, không còn chờ sau "Session ended" |
| Task Grab (đọc quán phở nhiều đánh giá nhất) | 501.9 s / 24 lượt (API quá tải) | 84.9 s / 11 lượt (`fix4`) |

- Chụp đôi khi 2.9–3.6 s ngay sau khi bấm chuyển trang: helper chờ luồng UI của app rảnh (app đang tải) → thời gian chờ thật, giữ nguyên.
- Tóm tắt bước chạy nền cũng gọi Gemini → cạnh tranh quota free với AI chính (khi API quá tải có lúc trễ 5–8 bước).
- Model local `qwen2.5vl:3b` (Ollama, RTX 3050 6GB): Ban đầu 0/3 màn chọn đúng do Ollama thiếu template tool calling và `context_length` mặc định 4k/8k < prompt 10k cắt mất danh sách UI. Sau khi cấu hình Modelfile với `num_ctx: 16384` + template Tool Calling (`<tools>`/`<tool_call>`) và luật index số nguyên (2026-11-20): đạt **5/5 màn chọn đúng (100% PASS)** trên trace Grab thật (`manage_app`, `click(target=3)`, `input_text(target=1, text="phở bò")`, `click(target=2)`, `click(target=7)`), thời gian trung bình **1.86 s/lượt** (100% GPU, ~4.5 GB VRAM, 49°C), hoàn toàn đủ điều kiện làm AI chính chạy offline.

### 1.14. Gửi gì để chọn đúng phần tử nhanh nhất? — ảnh vs phác thảo vs 2 tầng vs list chữ (2026-11-21/22)
Bộ test tự chứa `benchmarks/payload_grounding/` (18 lần bấm thật từ trace: Clock, Shopee, Grab, YouTube, Play Store; đã che địa chỉ/thông báo). Mỗi bài: ảnh + cây UI + ý định của agent (bỏ index/toạ độ) → model trả `{"index": N}`; chấm theo nhãn, tính cả radio/checkbox không nhãn cùng hàng. List lấy từ `format_minimal_list_with_elements` (đúng hàm Flash).

| Cách gửi | Qwen3-VL 4B local (Ollama, 17 bài\*) đúng / thời gian / token | Gemini 3.5 Flash-Lite (18 bài) đúng / token |
|---|---|---|
| Ảnh 768 + list gọn (**Flash hiện tại**) | 15/17 · 1.80 s · 1943 | 16/18 · 1925 |
| Ảnh phác thảo (wireframe đánh số) + list | 13/17 · 1.81 s · 1943 | 15/18 · 1925 |
| 2 tầng: mô tả ảnh → quyết định bằng chữ | 14/17 · 3.04 s · 2050 | 17/18 · 2095 |
| List đầy đủ, không ảnh | 14/17 · 0.83 s · 1384 | 17/18 · 1408 |
| List gọn, không ảnh | 13/17 · 0.72 s · 884 | 16/18 · 847 |
| **Nhãn + tâm** `[9] "Add alarm" @281,340` | **15/17 · 0.53 s · 605** | 16/18 · 580 |
| Chỉ nhãn `[9] "Add alarm"` | 15/17 · 0.36 s · 378 | 17/18 · 377 |

\* Qwen local chấm theo cách cũ (chỉ khớp nhãn), file kết quả bị mất khi dọn thư mục tạm — chạy lại bằng `bench.py --backend ollama` để có số theo cách chấm mới. Thời gian Gemini (free tier) dao động 1–10 s theo hàng chờ/mạng, không dùng để so tốc độ.

- **Ollama tính mọi ảnh ~1058 token** bất kể 59×128 hay 591×1280, nén q20, trắng đen, làm mờ (Ollama tự đưa về kích thước cố định) → trên Ollama nén/thu nhỏ/phác thảo ảnh **không giảm token, không nhanh hơn**; chỉ bỏ ảnh mới giảm. (llama.cpp công ty tính theo pixel — §1.10.)
- Bỏ ảnh, gửi "nhãn + tâm": nhanh hơn ~3.4×, giảm ~69% token vào, đúng ngang ảnh trên cả 2 model. Phác thảo tốn như ảnh mà kém nhất; 2 tầng chậm nhất.
- Giới hạn: chỉ có bài nhắm phần tử **có nhãn**; ý định do agent đã thấy ảnh viết ra → chưa đo icon không nhãn, màu/hình, và việc tự lập bước tiếp theo khi không có ảnh. Hướng đề xuất: mặc định list "nhãn + tâm", gửi ảnh khi cần (nhiều phần tử không nhãn, lệnh về hình/màu, thao tác trước không tác dụng) — cần chạy task thật trước khi đưa vào Flash.

## 2. TRẠNG THÁI TRIỂN KHAI (đến bản 3)

| Hạng mục | Trạng thái | Ghi chú |
|:--|:-:|:--|
| **A.** Làm mới session agy (≥ 80k **và** tăng ≥ 15k; trần cứng 120k) | ✅ Xong | Đã thấy chạy đúng trong trace cờ vua |
| **C.** Settle theo loại action (`manage_app` 1500ms, ENTER 1000ms, còn lại 400ms) | ✅ Xong | |
| **C.** Bấm mà màn hình không đổi → chờ 800ms chụp lại 1 lần (XML + dHash, ngưỡng từ config) | ✅ Xong | |
| **C.** Nhận diện skeleton (≤ 1 text trong vùng nội dung) | ⚠️ Chưa kiểm chứng | Cần XML thật của bước 5 trace Grab |
| **D.** Chặn bấm lặp (< 2%, màn hình không đổi), reset khi có action khác / màn hình đổi | ✅ Xong | |
| **D.** Gợi ý lỗi validation (diff XML / form feedback khi màn hình "đứng yên") | ✅ Xong | Đã thêm phát hiện lỗi validation form feedback khi màn hình không đổi |
| **E1.** Burst cùng màn hình, bỏ observe trung gian (`fast_burst: true`) | ✅ Xong | |
| **E1.** Chặn nút cam kết giữa chuỗi (kể cả bấm theo index), dừng chuỗi khi action lỗi | ✅ Xong | |
| **E1.** Cập nhật màn hình gốc khi runner tự chụp (`record_observation`) | ✅ Xong | |
| **F1.** Gợi ý cuộn 3 mức (scrollable / fixed / unknown) | ✅ Xong | |
| **G-1.** `wait_for_screen_change` (chờ ổn định, che nhiễu đồng hồ/status bar, so pixel 64x64 + XML) | ✅ Xong | Chờ đối thủ cờ vua / chat / animation, 1 lượt AI/nước |
| **G-2.** `wait_for_text`, `wait_until_gone` (kèm Visibility Guard, đa ngôn ngữ, không dấu) | ✅ Xong | Chờ chuyển trang, spinner biến mất |
| **G-3.** `target_text` xuyên màn hình + Anchor Guard + Ambiguity Guard + Commitment Guard | ✅ Xong | Tự neo text trên màn hình mới, an toàn 100% |
| **H.** Rút gọn danh sách phần tử & lọc phần tử ngoài màn hình | ✅ Xong | Giảm bloat token, tránh vượt context trên web/canvas |
| **Soft Handover thông minh.** Timeout không ngắt ngang, gửi ảnh chụp để AI nhìn thực tế chẩn đoán | ✅ Xong | AI tự đánh giá spinner/đối thủ trước khi quyết định |
| Prompt "bắt đầu từ màn hình hiện tại, không mở lại app" | ✅ Xong | |
| Prompt phân luồng công cụ chờ (G-1..G-3) & ví dụ mẫu | ✅ Xong | Đã cập nhật vào flash_runner.md và agy_model.py |
| Nhãn `Model: Pro` → đúng `Flash` | ✅ Code xong | Cần restart MCP server; trace cờ vua vẫn hiển thị `Pro` |
| **B.** Bỏ lượt mở ảnh `read_file` | ⚠️ Đang xử lý | Thử nghiệm cho thấy agy stream-json chỉ hỗ trợ text; đã bổ sung thuộc tính UI & gợi ý |
| **E2.** Chuỗi xuyên màn hình | ✅ Xong | Đã hoàn thành trong **Trụ cột G** |
| **F2.** `find_and_reveal`, `scroll_and_collect` | ❌ Chưa | |
| **O8. Thu nhỏ ảnh gửi AI ở Flash** (`agent.flash.model_image`, 1280 q75) | ✅ Code xong (2026-11-20) | −63% dung lượng ảnh; còn phải đo `prompt_tokens` và độ chính xác trên task thật (Mục 1.7) |
| **List gọn + O3 control không nhãn + output ngắn (Flash)** (`agent.flash.element_list`) | ✅ Code xong (2026-11-20) | Token list −36% (Gemini), Qwen prefill 14→7 s, 13/13 giữ nguyên (Mục 1.11); còn đo trên task thật |
| **Cắt chờ thừa phía thiết bị (Flash)** — settle đôi (O1), `input_text`, chờ tóm tắt khi xong (`agent.flash.final_summary_wait_s`) | ✅ Xong (2026-11-20) | Mục 1.13: chụp 0.72→0.41–0.61 s, `input_text` −0.7 s, lượt cuối 18–30 s→3–4 s; 871 test pass |
| **OCR cục bộ ONNX cho Flash** (RapidOCR + PaddleOCR Latin v5, `agent.flash.ocr`) | ✅ Xong (2026-11-20) | `auto` chỉ chạy khi XML nghèo; ghép `xml_first` + node OCR ảo; 19 unit test mới; tiếng Việt cần model riêng (Mục 1.6) |
| Gộp PR upstream đợt 1 (#216, #244, #181, #171) | ✅ Xong | Xem Mục 11; 2635 unit tests passed |
| Gộp PR upstream đợt 2 (#189, #217, #219, #221) | ❌ Chưa | Xung đột với code đã sửa, phải gộp tay |
| Test suite cho Trụ cột G & kiểm tra hồi quy | ✅ Xong | 757 unit tests passed (27/27 tests Trụ cột G & Turn Snapshot passed), khắc phục triệt để toàn bộ các lỗi phát hiện qua review (cho phép action không mục tiêu sau wait, lấy dữ liệu qua pipeline observe chuẩn cho target_text, flatten XML cây phân cấp lồng nhau, chuẩn hóa cờ timed_out có cấu trúc) |

---

## 3. NGUYÊN NHÂN GỐC CÒN LẠI

| # | Nguyên nhân | Bằng chứng | Trụ cột xử lý |
|:-:|:--|:--|:-:|
| N2 | Mở ảnh tốn thêm 1 lượt gọi | Grab 13/22, cờ vua 22/22 lượt | **B** |
| N5 | Một thao tác = một lượt AI | Grab: Food → ô tìm kiếm → gõ → Enter tách lượt | **G** |
| **N6** | **Lượt AI chỉ để chờ** | Cờ vua: 1 lượt `wait_for_delay` mỗi nước; Grab bước 6, 13, 22 | **G** |
| **N7** | **Lượt AI chỉ để kiểm tra "đã được chưa"** | Grab bước 12 → 13, 14 → 15 | **G** |
| N8 | Danh sách phần tử dài, vô ích trên màn hình canvas/web | Cờ vua +6k token/lượt | **H** |
| N4 | Lỗi validation không được báo | Grab bước 16–19 | **D** (phần còn lại) |

---

## 4. TRỤ CỘT G — AI GIAO VIỆC, THIẾT BỊ TỰ CHỜ VÀ KIỂM TRA (TRỌNG TÂM BẢN 3)

### 4.1. Ý tưởng

AI trả về một **danh sách tool phẳng** theo đúng format function calling (`{"name", "args"}`), trong đó các bước chờ/kiểm tra là **tool độc lập**, không lồng cú pháp `expect`. Executor chạy lần lượt, không chụp ảnh, không hỏi AI giữa chừng:

```json
[
  {"name": "click",         "args": {"target": 5}},
  {"name": "wait_for_text", "args": {"text": "Order summary", "timeout_s": 8}},
  {"name": "click",         "args": {"target_text": "Review address"}}
]
```

- Bước đúng như mong đợi → chạy bước tiếp.
- Bước sai mong đợi (timeout, không thấy text, text mơ hồ, app đổi, action lỗi) → **dừng chuỗi**, chụp 1 lần, hỏi AI kèm lý do cụ thể (vd. `wait_for_text "Order summary": not found after 8s`).
- Hết chuỗi → chụp 1 lần, hỏi AI quyết định tiếp.

**Lý do chọn danh sách phẳng:** tương thích 100% với function calling chuẩn và protocol agy hiện tại (`tool_calls`), không cần parser riêng; model nhỏ (`gemini-3.8-flash-low`) ít sinh sai cú pháp hơn.

### 4.2. Ba thành phần

#### G1. `wait_for_screen_change(timeout_s=25, region=None)`

Dùng cho: cờ vua / game (chờ đối thủ), chat (chờ trả lời), chờ animation / chuyển cảnh trên app canvas.

Thuật toán:
1. **Chờ màn hình ổn định trước khi lấy ảnh gốc:** chụp liên tiếp cho đến khi 2 khung liền nhau giống nhau (tối đa ~1.5s). Không dùng thời gian cố định, vì hiệu ứng mỗi app một khác. → Tránh bị **chính nước đi của mình** đánh thức.
2. **Che vùng nhiễu, không phụ thuộc app:**
   - Thanh trạng thái Android (đồng hồ hệ thống).
   - **Phần tử XML có text dạng đồng hồ** (`^\d{1,2}:\d{2}(\.\d)?$`) — tự che đồng hồ đếm ngược của đối thủ/của mình.
   - Con trỏ nhập liệu (phần tử đang `focused` là ô nhập).
3. **So sánh:** ảnh xám thu nhỏ (~64×64, hoặc chỉ trong `region` nếu AI chỉ định — AI nhìn ảnh nên biết bàn cờ ở đâu), đếm số ô thay đổi rõ rệt. **Không dùng dHash 64-bit toàn màn hình**: một quân cờ di chuyển chỉ đổi 0–3 bit, ngang với đồng hồ nhảy số.
4. **Kết hợp XML khi có:** chữ ký XML (bỏ text dạng đồng hồ) đổi cũng tính là "đã đổi" (vd. danh sách nước đi trên Chess.com web thêm "2. Nf3 Nc6").
5. **Trả về** ngay khi đổi: `changed after 3.4s` + diff XML ngắn nếu có. Hết giờ: `no change in 25s` — AI tự nhìn lại.

Giới hạn: timeout mặc định 25s, tối đa 120s. Tần suất quét thực tế ~1 lần/giây trên emulator này (mỗi lần chụp + dump ~0.8s).

#### G2. `wait_for_text(text, timeout_s=8)` và `wait_until_gone(text, timeout_s=15)`

Dùng cho: chờ chuyển trang, chờ kết quả tìm kiếm, chờ nút xuất hiện ("Place Order"), chờ spinner / "Đang tìm tài xế…" biến mất.

- `text` nhận một chuỗi **hoặc danh sách phương án** (`["Order summary", "Tóm tắt đơn hàng"]`) — vì AI có thể không chắc ngôn ngữ giao diện.
- So khớp: **chứa**, không phân biệt hoa thường, tìm trong `text`, `content-desc`, `hint`; tùy chọn bỏ dấu tiếng Việt.
- **Visibility Guard (Kiểm tra hiển thị):** Dump XML mất ~0.7–0.8s (ngang chụp ảnh). Phần tử tìm thấy bắt buộc phải có `bounds` nằm trong màn hình hiển thị `[0, 0, width, height]` và không bị ẩn (loại trừ các phần tử được RecyclerView/ListView tải trước ngoài khung nhìn hoặc tab ẩn).
- Trả về phần tử tìm thấy; **cây XML vừa dump được giữ lại** để G3 dùng ngay, không dump lần nữa.

#### G3. `target_text` cho `click`, `long_press`, `input_text` (có Anchor Guard)

Bắt buộc cho chuỗi xuyên màn hình: số thứ tự `[n]` thuộc màn hình cũ, còn phần tử ở màn hình mới AI chưa nhìn thấy.

- Tìm trên cây XML mới nhất (từ G2, hoặc dump mới). Thứ tự ưu tiên: khớp chính xác → bắt đầu bằng → chứa.
- **Anchor Guard:**
  - Không thấy → chờ ngầm tối đa ~3s → vẫn không thấy → dừng chuỗi.
  - Nhiều phần tử khớp ngang nhau, khác vị trí → **dừng chuỗi** (không đoán).
  - Phần tử bị che (overlap warning) hoặc nằm ngoài màn hình → dừng chuỗi.
- **Chặn nút cam kết:** Luật cấu trúc là rào chắn bất biến: nút cam kết **không được** nhắm bằng `target_text` trên màn hình AI chưa nhìn thấy, và chỉ được ở cuối chuỗi. Regex từ khóa chỉ là lớp phòng thủ chiều sâu (defense-in-depth), không coi là tuyệt đối.

### 4.3. Luật an toàn của chuỗi (code kiểm tra)

| Luật | Giá trị |
|:--|:--|
| Số action thiết bị tối đa mỗi lượt | 6 (không tính tool chờ) |
| Tổng thời gian chờ tối đa mỗi lượt | 120s |
| **Xử lý Timeout (Soft Handover thông minh)** | Hết giờ chờ **không bao giờ là lỗi chí mạng (Task Failure)** và **không được tự ý ngắt tác vụ**. Thiết bị tự động chụp ảnh màn hình và cập nhật UI mới nhất, gửi kết quả về cho AI: *"Đã chờ X giây, chưa thấy thay đổi/text. Màn hình hiện tại đính kèm."* AI nhìn ảnh để chẩn đoán: nếu đối thủ còn đang nghĩ / app còn đang quay spinner $\rightarrow$ AI quyết định chờ tiếp; nếu thấy popup lỗi / app văng $\rightarrow$ AI xử lý; nếu thấy màn hình hoàn toàn bất động qua nhiều lượt kiểm tra (đối thủ AFK, app đơ) $\rightarrow$ AI mới chủ động kết luận và báo người dùng. |
| Dừng chuỗi khi | action lỗi; tool chờ hết giờ; `target_text` không thấy / mơ hồ / bị che; nút cam kết giữa chuỗi; **app ở foreground đổi ngoài dự kiến** |
| Nút cam kết | **Luật cấu trúc cứng:** chỉ ở cuối chuỗi, không nhắm bằng `target_text` trên màn hình chưa thấy. Regex từ khóa là phòng thủ bổ trợ |
| Khi dừng | Chụp 1 lần, gửi AI: bước nào dừng, vì sao, những bước đã chạy. **Bắt buộc sinh đủ `ToolMessage` cho toàn bộ các `tool_call_id` trong turn** (các bước chưa chạy trả về `status="error"` hoặc `content="skipped: chain interrupted"`) để tương thích chuẩn OpenAI/Anthropic API |

### 4.4. Prompt

Thêm vào `flash_runner.md` (và protocol của `agy_model.py`):

> *Each reply should carry every action you are already sure of, executed SEQUENTIALLY, followed by how to wait for its result. Decision rule based on WHAT YOU ARE WAITING FOR:*
> 1. *If you know the specific text that will APPEAR (e.g. search results, page header): use `wait_for_text`.*
> 2. *If you know the specific text that will DISAPPEAR (e.g. loading spinner, progress bar): use `wait_until_gone`.*
> 3. *If waiting for an UNKNOWN change (opponent's move, incoming chat message, graphic scene transition): use `wait_for_screen_change`.*
> 4. *If the duration itself is the explicit goal (e.g. preview audio for 30s): use `wait_for_delay`.*
> 5. *Use `target_text` for elements on a screen you have not seen yet, and only when you know its text. Only stop when you genuinely need to see a new screen to decide.*
> 6. *Never use fixed `wait_for_delay` to wait for something you can name or see change.*

Kèm 3 ví dụ ngắn: cờ vua (đi + chờ đối thủ), tìm kiếm (bấm ô → gõ → Enter → chờ kết quả), form (chọn nhiều ô → Confirm).

### 4.5. Ví dụ áp dụng

| App | Một lượt AI trả về | Thay cho |
|:--|:--|:--|
| Cờ vua | `click_sequence(quân, ô đích)` → `wait_for_screen_change(region=bàn cờ, timeout_s=30)` | 2 lượt |
| Grab tìm món | `click(Food)` → `wait_for_text("What shall we deliver?")` → `click(target_text="What shall we deliver?")` → `input_text("phở")` → `press_key(ENTER)` → `wait_until_gone`/`wait_for_text(tên món)` | 4–5 lượt |
| Grab giỏ hàng | `click(Basket)` → `wait_for_text("Review address")` → `click(target_text="Review address")` → `wait_for_text("Address details")` | 3–4 lượt |
| Chat | `input_text(tin nhắn)` → `click(nút gửi)` → `wait_for_screen_change(timeout_s=60)` | 2 lượt + nhiều lượt chờ |

---

## 5. CÁC TRỤ CỘT CÒN LẠI

### B — Bỏ lượt gọi mở ảnh (ưu tiên cao)
1. Thử agy stream-json nhận ảnh trực tiếp → gửi ảnh thu nhỏ (~768px) ngay trong lượt.
2. Nếu không được: bổ sung vào danh sách phần tử các thông tin thường khiến model phải mở ảnh (`checked/selected/enabled`, `focused`, nhãn icon từ `content-desc`).
3. Phương án cuối: lượt cần ảnh gọi Gemini API trực tiếp (1 lần gọi, có cache).

### H — Rút gọn danh sách phần tử (mới, sửa N8)
- Khi màn hình chủ yếu là canvas/web dài: chỉ gửi phần tử nằm trong vùng hiển thị và có thể tương tác, giới hạn ~60 phần tử, gộp các phần tử text liền nhau.
- Mục tiêu: token tăng mỗi lượt ≤ 3k thay vì ~6k → model nhanh hơn, ít phải làm mới session.

### C — Phần còn lại
- Kiểm chứng tiêu chí skeleton bằng XML thật của bước 5 trace Grab; điều chỉnh nếu cần.

### D — Phần còn lại
- Màn hình "đứng yên" sau khi bấm nút nhưng XML có thay đổi nhỏ (text lỗi mới, `selected` đổi) → thêm dòng `Screen stayed; something changed: <diff ngắn>`.

### F2 — Cuộn theo mục đích
- `find_and_reveal(target_text, max_swipes=6)`: biết cần tìm gì → tự vuốt chậm (≥ 500ms) đến khi thấy.
- `scroll_and_collect(max_pages=5, focus=...)`: cần so sánh để chọn → gom text từng trang (lưu ngay lúc trang còn trên màn hình), khử trùng lặp, gắn số trang → AI chọn trong 1 lượt → `find_and_reveal` quay lại.

### I — Nhớ đường đi (về sau, rủi ro cao)
- Lưu chuỗi action đã thành công theo app + loại mục tiêu; lần sau chạy lại **với Anchor Guard ở mọi bước** (phải thấy đúng text mới bấm; lệch là dừng và hỏi AI).
- **Không bao giờ phát lại macro mù.** Popup, banner, vị trí thay đổi mỗi ngày.

---

## 6. LỘ TRÌNH

| Phase | Nội dung | File chính | Tiêu chí hoàn thành |
|:--|:--|:--|:--|
| **G-1** | `wait_for_screen_change` + cho phép tool chờ ở cuối chuỗi + prompt | `artemis/mcp/action_executor.py`, `artemis/mcp/action_manifest.py`, `artemis/agents/flash/runner.py`, `artemis/agents/flash/flash_runner.md`, `artemis/llm/agy_model.py` | Cờ vua: 1 lượt AI mỗi nước; 0 lượt `wait_for_delay`; không bị đánh thức bởi nước đi của mình / đồng hồ; tỷ lệ chọn đúng tool chờ $\ge 90\%$; số lần timeout liên tiếp $\le 2$ |
| **G-2** | `wait_for_text`, `wait_until_gone` (kèm Visibility Guard) | như trên | Grab: 0 lượt AI chỉ để chờ chuyển trang; số lần dừng chuỗi vì `target_text` không thấy được ghi nhận và theo dõi |
| **G-3** | `target_text` + Anchor Guard + chặn cam kết cho `target_text` + dừng khi đổi app | như trên | Grab: ≤ 11 lượt; test: nút cam kết không bao giờ chạy qua `target_text` trên màn hình chưa thấy |
| **B** | Bỏ lượt `read_file` | `artemis/llm/agy_model.py`, `artemis/mcp/observation.py` | Tỷ lệ lượt "2 calls" < 20% |
| **H** | Rút gọn danh sách phần tử | `artemis/utils/visualization.py`, `artemis/mcp/observation.py` | Token tăng ≤ 3k/lượt trên Chess.com web |
| **C/D** | Kiểm chứng skeleton; diff validation | `artemis/mcp/action_executor.py` | Grab: 0 lượt chờ load; bước 16–19 không lặp lại |
| **F2** | `find_and_reveal`, `scroll_and_collect` | `artemis/controllers/device_controller.py`, `artemis/agents/flash/runner.py` | "Tìm món rẻ nhất trong 5 trang" ≤ 3 lượt AI |
| **I** | Nhớ đường đi có Anchor Guard | (thiết kế riêng) | Chạy lại tác vụ quen ≤ 30% số lượt AI ban đầu |

**Sau mỗi phase:** chạy lại Grab (dừng ở "Place Order") và cờ vua, cộng ít nhất 1 app khác loại (Cài đặt hoặc chat). Ghi lại: số lượt AI, action/lượt, số lần dừng chuỗi theo lý do, số lần tool chờ hết giờ, tổng thời gian.

---

## 7. KẾT QUẢ DỰ KIẾN

| Chỉ số | Hiện tại | Sau G-1..G-3 | Sau G + B + H |
|:--|:-:|:-:|:-:|
| Cờ vua, thời gian mỗi nước (không tính đối thủ nghĩ) | ~25s | ~11–13s | **~8–10s** |
| Cờ vua, lượt AI mỗi nước | 2 | 1 | 1 |
| Grab, số lượt AI | 23 | ~9–11 | ~9–11 |
| Grab, tổng thời gian | ~232s | ~85–100s | **~70–85s** |
| Action trên mỗi lượt AI | ~1.0 | ≥ 2.0 | ≥ 2.0 |

**Cách tính (Grab):** ~10 lượt × (model ~4–5s + chụp ~1s) ≈ 55s + chờ thật của app (tìm kiếm, chuyển trang) ~10–15s + mở app ~7s. Không tính được thời gian đối thủ suy nghĩ (cờ vua) hay người khác trả lời (chat).

---

## 8. RỦI RO & CÁCH GIẢM

| Rủi ro | Giảm thiểu |
|:--|:--|
| `wait_for_screen_change` bị đánh thức bởi animation của chính mình | Chờ 2 khung liền nhau giống nhau rồi mới lấy ảnh gốc |
| Đồng hồ / con trỏ / thanh trạng thái làm "đổi" giả | Tự che phần tử text dạng đồng hồ, ô nhập đang focus, thanh trạng thái; so ô ảnh thay vì dHash toàn màn hình |
| **Đối thủ (bot) đi nhanh hơn thời gian ổn định → ảnh gốc đã chứa nước của đối thủ → chờ đến hết giờ** | Timeout vừa phải (25–30s); khi hết giờ báo rõ để AI nhìn lại; ghi số lần xảy ra vào chỉ số |
| AI đoán sai text của màn hình chưa thấy (vd. giao diện tiếng Anh) → chờ hết giờ còn chậm hơn | Timeout ngắn (5–8s); `text` nhận danh sách phương án; prompt chỉ cho gộp xuyên màn hình khi biết chắc text |
| `target_text` khớp nhầm phần tử | Ưu tiên khớp chính xác; nhiều kết quả ngang nhau → dừng chuỗi |
| Nút cam kết bị bấm tự động | Chặn theo text cả với index và `target_text`; chỉ ở cuối chuỗi; không trên màn hình chưa thấy |
| Popup / quảng cáo bật giữa chuỗi | Anchor Guard (không thấy text → dừng); dừng khi app foreground đổi |
| Chuỗi dài làm AI mất dấu những gì đã xảy ra | Khi dừng/kết thúc, gửi AI danh sách bước đã chạy + kết quả từng bước |

---

## 9. PHẢN HỒI REVIEW & PHẢN BIỆN THIẾT KẾ

### 9.1. Đánh giá phản hồi thiết kế kỹ thuật (Round 1)

| Ý kiến review | Kết luận | Đưa vào bản 3 |
|:--|:-:|:--|
| `wait_for_screen_change` bị chính nước đi của mình đánh thức | ✅ Đúng | Chờ ổn định bằng 2 khung giống nhau (không dùng 800ms cố định); thêm rủi ro ngược khi đối thủ đi quá nhanh |
| Đồng hồ nhảy số → dùng ngưỡng dHash ≥ 4 hoặc `exclude_top_pct` | ⚠️ Vấn đề đúng, cách sửa sai | dHash 64-bit toàn màn hình không phân biệt được nước cờ với đồng hồ; `exclude_*_pct` là luật riêng từng app → thay bằng che phần tử dạng đồng hồ + so ô ảnh / `region` + so XML |
| Dùng tool phẳng thay cho `expect` lồng nhau | ✅ Đúng, tốt hơn đề xuất cũ | Toàn bộ G dùng tool độc lập |
| Không phát lại macro mù, cần Anchor Guard | ✅ Đúng | Nguyên tắc 0.3; Anchor Guard trong G3 và trụ cột I |
| "Chỉ cần 2 tool" | ❌ Thiếu | Chuỗi xuyên màn hình bắt buộc có `target_text` (G3); ví dụ Grab của review không chạy được nếu thiếu |
| Grab ~6–8 lượt (~50–70s) | ⚠️ Lạc quan | Ước tính 9–11 lượt (~70–85s): vẫn cần lượt chọn món, form địa chỉ, validation |
| Quét XML mỗi 400ms | ⚠️ Không đúng thực tế | Mỗi lần dump ~0.7–0.8s trên emulator → ~1 lần/giây |

### 9.2. Phản biện & Bổ sung về Tính Tổng quát (App-Agnostic) và Tương thích Đa Model (Model-Agnostic) (Round 2)

| Vấn đề / Băn khoăn cốt lõi | Phân tích & Phản biện kỹ thuật | Giải pháp chuẩn hóa trong kế hoạch |
|:--|:--|:--|
| **1. Có bị thiên lệch theo cờ vua làm hỏng các app khác (Grab, Shopee, Chat, Cài đặt)?** | **Tuyệt đối không.** Không phân luồng theo loại app (vì app cờ web có text, app chat native lại chờ tin chưa biết). Phân luồng theo **thứ đang chờ**: gọi tên được text thì dùng `wait_for_text`/`wait_until_gone`; không gọi tên được thì dùng `wait_for_screen_change` (so XML trước, so pixel sau). | - **Anchor & Visibility Guard:** Text phải nằm trong màn hình `[0, 0, width, height]` và không bị che. Thấy mơ hồ/lạ là dừng ngay.<br>- **Phân luồng Prompt chuẩn:** Theo trạng thái kỳ vọng, không cấm tool theo app.<br>- **Commitment Guard:** Kết hợp luật cấu trúc (không nhắm màn hình chưa thấy, chỉ ở cuối chuỗi) + từ khóa giảm rủi ro. |
| **2. Hiện tại chỉ có `agy`, tương lai đổi sang các model khác (OpenAI, Anthropic, DeepSeek, Qwen...) có đúng không?** | **Đúng hướng.** Bản 3 dùng **Danh sách Tool phẳng chạy tuần tự** theo chuẩn Function Calling toàn cầu.<br>- Phải ghi rõ thứ tự tuần tự trong prompt để tránh model hiểu nhầm là song song.<br>- **Quy tắc 1-1 Tool Result:** Mọi tool call (kể cả skipped khi chuỗi dừng) đều phải trả về ToolMessage để API không trả lỗi 400 Bad Request.<br>- Graceful Fallback: Model 1 call vẫn chạy an toàn, model multi-call sẽ bứt tốc. | - Không dùng bất kỳ cú pháp hay tính năng độc quyền nào của `agy`.<br>- Toàn bộ logic an toàn và thiết bị nằm ở tầng Python Executor, độc lập với model. |
| **3. Xử lý thế nào khi tool chờ bị timeout (hết giờ mà text chưa hiện hoặc ảnh chưa đổi)?** | Timeout không phải Task Failure: Tuyệt đối không tự ý ngắt tác vụ vì app có thể load lâu hoặc đối thủ đang suy nghĩ. Phải chụp ảnh gửi AI xem có phải lỗi không rồi AI mới quyết định. | - **Soft Handover thông minh:** Khi hết giờ chờ, thiết bị chụp ảnh + dump XML gửi AI kèm số lần/thời gian đã chờ.<br>- AI nhìn ảnh trực tiếp: nếu spinner còn quay / đồng hồ còn chạy $\rightarrow$ AI chờ tiếp; nếu có popup lỗi $\rightarrow$ xử lý lỗi; nếu đơ/AFK nhiều lượt $\rightarrow$ AI mới quyết định dừng và báo người dùng. |

### 9.3. Phản biện Round 2 (Claude)

**Kết luận chung:** Hướng Round 2 đúng (Soft Handover, tool phẳng, không phụ thuộc agy). Nhưng **luật phân luồng theo "loại app" mâu thuẫn với chính các ví dụ trong kế hoạch**, và vài khẳng định đang quá tuyệt đối. Cần sửa 6 điểm dưới đây trước khi code G-1.

#### Điểm 1 — Phân luồng theo *loại app* là sai trục; phải phân luồng theo *điều mình đang chờ* (❌ cần sửa)

- **Mâu thuẫn với trace cờ vua thật:** Chess.com chạy trong **Chrome (WebView)**, danh sách phần tử có rất nhiều text (tên người chơi, đồng hồ, danh sách nước đi). Theo luật 4.4.1 (*"If the app has text in the element list, ALWAYS use `wait_for_text`… DO NOT use `wait_for_screen_change`"*), AI **bị cấm** dùng `wait_for_screen_change` cho chính ván cờ này — nhưng `wait_for_text` không diễn tả được "chờ đối thủ đi" vì không biết trước nước đi sẽ là gì.
- **Mâu thuẫn với ví dụ Chat trong 4.5:** Zalo/Messenger là app native có XML, nhưng ví dụ lại dùng `wait_for_screen_change` để chờ tin trả lời — đúng về mặt nhu cầu, sai theo luật 4.4.1.
- **Màn hình lai rất phổ biến:** app native có bản đồ (Grab theo dõi tài xế), trình phát video, WebView trong app, game có menu native. Không chia được gọn thành "XML" hay "Canvas".
- **Sửa:** phân luồng theo **thứ mình chờ**, không theo loại app:

| Đang chờ | Tool | Ví dụ |
|:--|:--|:--|
| Một thứ **gọi tên được** sẽ xuất hiện | `wait_for_text` | "Order summary", "Place Order", tên món |
| Một thứ **gọi tên được** sẽ biến mất | `wait_until_gone` | "Đang tìm tài xế", "Loading" |
| **Một thay đổi chưa biết trước nội dung** | `wait_for_screen_change` | Đối thủ đi cờ, tin nhắn trả lời, cảnh game đổi |
| Một **khoảng thời gian** (chính nó là yêu cầu) | `wait_for_delay` | "Xem video 30 giây", quảng cáo đếm ngược không có text |

- **Hệ quả cho G1:** `wait_for_screen_change` phải **so XML trước** (rẻ, chính xác với app native/web), **so ảnh sau** (cho canvas) — như mục 4.2 G1 bước 4 đã ghi. Như vậy nó dùng tốt cho cả native lẫn canvas, không cần cấm.
- **Sửa prompt 4.4:** thay luật 1–2 bằng: *"Prefer `wait_for_text`/`wait_until_gone` whenever you can name what will appear or disappear. Use `wait_for_screen_change` when you are waiting for a change you cannot name in advance (an opponent's move, a reply, a scene change)."* Luật 4 sửa thành: *"Use `wait_for_delay` only when the duration itself is the requirement."*

#### Điểm 2 — "XML chính xác tuyệt đối, siêu nhẹ" là khẳng định quá mức (⚠️ cần sửa câu chữ + thêm kiểm tra)

- **Không nhẹ hơn chụp ảnh nhiều:** trên emulator này mỗi lần dump XML ~0.7–0.8s, ngang một lần chụp. "0 tốn GPU" không phải chỉ số liên quan; chỉ số đúng là **0 lượt gọi AI**.
- **Không chính xác tuyệt đối:** text có thể **có trong XML nhưng không nhìn thấy** — RecyclerView prefetch phần tử ngoài màn hình, WebView giữ DOM của tab ẩn, phần tử bị dialog che, text nằm trong `alpha=0` của hiệu ứng chuyển trang.
- **Sửa G2:** chỉ tính là "thấy" khi phần tử có bounds **nằm trong màn hình**, diện tích > 0, và **không bị che** (dùng overlap warning đã có trong danh sách phần tử). Sửa câu ở mục 0.1 thành "nhẹ hơn và chính xác hơn so ảnh trong đa số app native".

#### Điểm 3 — Tương thích đa model: đúng hướng nhưng "100%" là quá mức (⚠️ cần thêm 3 việc)

- **Thứ tự thực thi:** trong chuẩn OpenAI/Anthropic, nhiều tool call trong một lượt mang nghĩa *song song, độc lập*. Nhiều model được huấn luyện như vậy và **không quen sinh một chuỗi phụ thuộc thứ tự**; một số model local nhỏ chỉ sinh 1 call/lượt; một số API còn có cờ tắt multi-call. → Phải **ghi rõ trong mô tả tool và prompt** rằng danh sách được chạy *tuần tự theo thứ tự*, và đo "action/lượt" riêng cho từng model khi đổi model.
- **Mỗi tool call phải có tool result:** OpenAI và Anthropic **từ chối request** nếu có tool call không có kết quả tương ứng. Khi chuỗi bị dừng giữa chừng, **mọi call còn lại phải nhận kết quả `skipped`** (code hiện tại đã làm cho burst — phải giữ nguyên cho G1–G3 và viết test). Đây là điều kiện bắt buộc để "đổi model không phải sửa code".
- **Graceful Fallback đúng**, nhưng lợi ích của G **phụ thuộc vào việc model chịu gộp**. Model chỉ sinh 1 call/lượt sẽ không nhanh hơn — vẫn an toàn, nhưng không đạt mục tiêu ≥ 2 action/lượt.
- Bỏ tên model cụ thể (GPT-4o, Claude 3.5/3.7…) khỏi kế hoạch — sẽ lỗi thời nhanh; ghi chung "các provider có function calling".

#### Điểm 4 — Soft Handover đúng, nhưng thiếu giới hạn lặp (⚠️ cần bổ sung)

- Timeout không phải lỗi — **đồng ý**. Nhưng nếu đối thủ bỏ ván, người kia không trả lời, hay app treo, AI sẽ gọi lại tool chờ **vô hạn**, mỗi lần tốn một lượt AI.
- **Sửa:** đếm số lần **chờ hết giờ liên tiếp cho cùng một mục tiêu**; từ lần thứ 3, kết quả tool ghi rõ *"đã chờ X lần, tổng Y giây, không có thay đổi"* để AI tự quyết định dừng hoặc báo người dùng. Tổng thời gian chờ của cả tác vụ cũng nên có trần (cấu hình được).
- Kết quả timeout phải kèm **danh sách bước đã chạy** trong chuỗi (đã ghi ở 4.3) — giữ nguyên.

#### Điểm 5 — "Commitment Guard khóa cứng, không thể bị bấm ngầm" là khẳng định sai (❌ cần sửa câu chữ)

- Guard hiện dựa vào **danh sách từ khóa** (place order, đặt hàng, thanh toán…). Nó **không bắt được**: nút chỉ có icon, ngôn ngữ khác ("Bestellen", "下单", "Commander"), cách diễn đạt khác ("Continue to payment", "Xác nhận & trả tiền", "Swipe to pay").
- **Phần chắc chắn thật sự** là các luật *cấu trúc*: nút cam kết chỉ được ở cuối chuỗi, và `target_text` không được nhắm vào màn hình AI chưa nhìn thấy khi text khớp từ khóa. Phần từ khóa chỉ là *lớp giảm rủi ro*.
- **Sửa câu chữ:** "Commitment Guard giảm mạnh rủi ro bấm nhầm nút cam kết; không thay thế được việc AI tự nhận diện". Bổ sung từ khóa đa ngôn ngữ và nhãn icon (`content-desc`) dần theo log thực tế.

#### Điểm 6 — Thiếu tiêu chí đo cho Round 2 (⚠️ cần bổ sung vào mục 6)

- Thêm vào tiêu chí G-1/G-2: **tỷ lệ chọn đúng tool chờ** (đếm số lần AI dùng `wait_for_delay` khi lẽ ra có thể dùng tool chờ theo sự kiện), **số lần timeout liên tiếp**, và **số lần chuỗi bị dừng vì `target_text` không thấy** — để biết luật phân luồng ở Điểm 1 có được model tuân theo không.

#### Tóm tắt việc cần sửa trong kế hoạch

| # | Vị trí | Sửa |
|:-:|:--|:--|
| 1 | Mục 0.1, 4.4 (luật 1, 2, 4), 9.2 dòng 1 | Phân luồng theo *thứ đang chờ*, bỏ lệnh cấm `wait_for_screen_change` trên app có XML |
| 2 | Mục 0.1, 4.2 G2 | Bỏ "chính xác tuyệt đối / siêu nhẹ"; thêm kiểm tra *nhìn thấy được* (bounds, không bị che) |
| 3 | Mục 0.5, 4.1, 9.2 dòng 2 | Ghi rõ thứ tự tuần tự trong mô tả tool; mọi call bị bỏ qua phải có kết quả `skipped`; bỏ "100%" và tên model cụ thể |
| 4 | Mục 4.3 | Giới hạn số lần chờ hết giờ liên tiếp + trần tổng thời gian chờ |
| 5 | Mục 9.2 dòng 1 | Sửa "khóa cứng, không thể bị bấm ngầm" thành "giảm mạnh rủi ro" |
| 6 | Mục 6 | Thêm chỉ số: chọn đúng tool chờ, timeout liên tiếp, dừng chuỗi vì `target_text` |

---

## 10. THAY ĐỔI SO VỚI BẢN 2

| Bản 2 | Bản 3 | Lý do |
|:--|:--|:--|
| E2 là một phần của burst | Tách thành **Trụ cột G** (G1–G3) và làm trọng tâm | Lượt chờ / lượt kiểm tra là nguồn lượt thừa lớn nhất (cờ vua: 50% số lượt) |
| Chưa có tool chờ theo sự kiện | `wait_for_screen_change`, `wait_for_text`, `wait_until_gone` | Thay `wait_for_delay` cố định và lượt AI chỉ để chờ |
| `expect` (ý tưởng) | Tool phẳng | Tương thích function calling, ít lỗi cú pháp |
| Không có dữ liệu cờ vua | Thêm mục 1.2 | Kiểm chứng trên app canvas, không chỉ Grab |
| Phase 0 "kiểm chứng nén khi không có key" | Đã có bằng chứng (token tụt sau khi làm mới) | Trace cờ vua |
| Không có trạng thái triển khai | Thêm mục 2 | Theo dõi phần đã xong / còn lại |
| — | Thêm **Trụ cột H** (rút gọn danh sách phần tử) | Trang web dài làm context tăng ~6k token/lượt |
| Trụ cột F2, I nằm rải rác | Gom vào mục 5, I ghi rõ "không macro mù" | Theo review |

---

## 11. PR UPSTREAM (`google/artemis`) ĐÃ GỘP — KHÔNG GỘP LẠI

> Trước khi gộp một PR của `google/artemis`, tra bảng này. Khi upstream merge chính các PR này, lúc `git merge upstream/main` sẽ đụng đúng các đoạn code đó: giữ bản đã có ở đây (riêng #171 giữ bản có sửa `clear_existing`).

| PR | Tiêu đề | Gộp ngày | Nhánh | File chính | Ghi chú |
|:-:|:--|:-:|:--|:--|:--|
| [#216](https://github.com/google/artemis/pull/216) | fix(adb): launch apps with monkey --pct-syskeys 0 | 2026-11-19 | `adopt/upstream-prs-1` | `artemis/drivers/android/adb_driver.py` | Nguyên bản |
| [#244](https://github.com/google/artemis/pull/244) | fix(coordinates): honor an explicit zero swipe/scroll duration | 2026-11-19 | `adopt/upstream-prs-1` | `artemis/utils/coordinates.py` | Nguyên bản |
| [#181](https://github.com/google/artemis/pull/181) | fix(perception): keep system dialogs clickable, only filter true status/nav bars | 2026-11-19 | `adopt/upstream-prs-1` | `artemis/utils/ui_filter.py`, `artemis/utils/ocr_xml_fusion.py` | Nguyên bản |
| [#171](https://github.com/google/artemis/pull/171) | fix(android): use clipboard paste and verify text entry in input_text | 2026-11-19 | `adopt/upstream-prs-1` | `artemis/drivers/android/adb_driver.py` | **Có sửa:** chỉ gọi helper `clear_text` khi `clear_existing=True` |
| [#193](https://github.com/google/artemis/pull/193) (thay #189) | fix: render checked/enabled state for labeled controls in compact observation | 2026-11-19 | `main` | `artemis/utils/visualization.py` | Gộp tay, giữ occlusion check |
| [#217](https://github.com/google/artemis/pull/217) | fix(flash): stop after consecutive tool-less turns instead of looping | 2026-11-19 | `main` | `artemis/agents/flash/runner.py`, `artemis/config/agent.py` | Gộp tay, giữ fast_burst |
| [#219](https://github.com/google/artemis/pull/219) | fix(prompts): ask for a progress note, not for written-out reasoning | 2026-11-19 | `main` | `flash_runner.md`, `prompts.py` | Gộp tay |
| [#221](https://github.com/google/artemis/pull/221) | fix(checker): stop final-check retries from copying the previous final verdict | 2026-11-19 | `main` | `checker.py`, `checker.json` | Gộp tay, giữ goal_requirements |

### Đã xem, chưa gộp
| PR | Lý do |
|:-:|:--|
| #226, #148, #130 | Có thể gộp (vòng lặp Operator không ra action; Checker dùng tool calling cho endpoint OpenAI; gõ Unicode khi clipboard bị chặn) |
| #163, #176, #175, #112, #161 | Không gộp lúc này — tính năng lớn hoặc dựa trên lịch sử repo cũ |
