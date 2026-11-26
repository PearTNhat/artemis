# KẾ HOẠCH: APP WORKFLOW — PHÁT LẠI LUỒNG THAO TÁC CÓ FALLBACK AI (v4)

File: /home/nhat/Workspace/control-android/artemis/docs/KE_HOACH_APP_WORKFLOW.md
Phạm vi: chỉ Flash (theo KE_HOACH_TOI_UU_ARTEMIS.md Mục 0.1: tối ưu Flash, Pro chỉ sửa lỗi).
Trạng thái: ĐÃ ĐỒNG THUẬN v4 (agents-debate phiên 0906a23d, 2026-11-26; author Claude, critic Gemini, 6/6 tiêu chí đạt). Báo cáo: ~/.review-room/report-0906a23d-7567-4046-af6e-192fd380c738.md

## 0. Mục tiêu và non-goals
Mục tiêu: lần đầu AI làm một tác vụ trên một app (vd đặt cơm, ~20 lượt LLM) → lưu luồng. Lần sau tác vụ tương tự (vd trà sữa) → máy tự phát lại bước có màn hình khớp (cùng bố cục, khác chữ), chỉ gọi AI ở bước lệch. Đo bằng số lượt gọi LLM / tác vụ và tổng thời gian.
Không làm: không luật riêng cho app nào (app chỉ là dữ liệu workflow); không bao giờ tự bấm hành động không hoàn tác; không thay agent AI (autopilot là lớp tăng tốc, AI vẫn gọi report_task_status); không tích hợp Pro; không chạy thiết bị thật ở M0–M1.

## 1. Thuật ngữ
Workflow = đồ thị trạng thái của 1 ý định trên 1 app: node = cụm màn hình, edge = thao tác. Fingerprint = dấu vân tay cấu trúc màn hình. Target descriptor = mô tả phần tử đích không phụ thuộc toạ độ tuyệt đối. Slot = tham số người dùng ({item}...). Autopilot = bộ phát lại trong Flash, không gọi LLM. raw_elements = cây UI gốc đã flatten_hierarchy, chưa qua format list (Mục 2.1).

## 2. Câu hỏi 1 — So khớp màn hình
2.1 Đầu vào (một schema chung cho M0, Recorder, Autopilot) [I1]:
- Hiện ObserveResult (mcp/action_types.py:74) chỉ có `elements` đã lọc bởi format_minimal_list_with_elements (mất container không nhãn, scrollable/focused, đổi key). → Thêm trường `ObserveResult.raw_elements` = flatten_hierarchy(cây gốc trước format), lấy trong observe() (mcp/observation.py) từ cùng snapshot.
- Đường truyền [I7]:
  - Khai báo field mới `State.raw_elements` và `State.observation_id` trong artemis/graph/state.py (State đặt `extra="forbid"`), và thêm cả hai vào NODE_VISIBILITY (artemis/graph/visibility.py) cho mọi node đang được ghi `indexed_elements` (để không lỗi VisibilityError khi ARTEMIS_STRICT_STATE=1).
  - Helper duy nhất `apply_observation(state, elements, raw_elements)` (artemis/workflow/state_sync.py): ghi `indexed_elements`, `raw_elements` và đóng dấu `observation_id` mới lên cả hai. Thay MỌI phép gán trực tiếp `state.indexed_elements` ở Flash/MCP bằng helper: mcp/action_executor.py:387 (poll target_text) và :567; agents/validator/tool_declarations.py:189 (capture_screenshot_and_parse_ui, dùng bởi _prepare_conversation); agents/flash/runner.py:996, :1396, :1469, :1695, :1729; tools/explorer_tool.py:319 (khởi tạo rỗng → raw=[]). Pro operator.py:935 không đổi (Pro không có autopilot).
  - Autopilot chỉ chạy khi `observation_id` của raw và indexed trùng nhau; lệch → tự observe lại trước khi khớp (phòng một chỗ ghi mới bị bỏ sót sau này).
  - Unit test: quét mã nguồn, không còn phép gán trực tiếp `state.indexed_elements =` ngoài helper (trừ Pro operator.py).
- Chuẩn hoá 1 lần qua `workflow/fingerprint.normalize(raw)`: key `resource-id`, `class`, `bounds` → 0–1000, `clickable/scrollable/focused/checkable/enabled/visible-to-user`, `package`. Offline M0 đọc `images.ui_tree` (cùng định dạng cây gốc) qua đúng hàm này → offline và runtime dùng chung đầu vào.
- ui_tree rỗng/null (6/1176 ảnh) → bước đó không ghi, không khớp (luôn AI).
- Không dùng pixel để quyết định khớp.
2.2 Đặc trưng: bỏ node package com.android.systemui, node visible-to-user=false, node < 0.1% diện tích. Mỗi node → token (class, resource-id|"", role, grid); role = clickable|editable|checkable|scrollable|static; grid = (cx//100, cy//100, w-bucket, h-bucket) trên 0–1000. Chữ KHÔNG nằm trong token → cơm/trà sữa cùng fingerprint; chỉ chữ tĩnh (3.2) thêm token phụ ("text", hash). Con của container scrollable chỉ góp (class, resource-id, role), tối đa 1 lần mỗi bộ ba → danh sách dài/ngắn không làm lệch.
2.3 Độ đo: S = weighted Jaccard trên multiset token (resource-id = 3, chữ tĩnh = 2, còn lại = 1). Khớp khi ĐỒNG THỜI: (1) package foreground trùng; (2) S ≥ τ_screen (khởi đầu 0.80, hiệu chỉnh M0); (3) biên S1 − S2 ≥ δ (0.10) giữa node tốt nhất và thứ hai; (4) target của edge giải ra đúng 1 phần tử live, không bị che; (5) màn có ≥ 5 token app (cây nghèo như WebView/game → không bao giờ khớp). Thiếu điều nào → trả quyền AI.
2.4 Kiểm chứng offline (M0, không cần điện thoại) [I4a]:
- Dữ liệu: traces/data_engine.db — 70 phiên, 1170 bước; nhiều tác vụ lặp (Shopee tìm tai nghe ×6, Grab Phở 24 ×8, Grab trà sữa ×2, Cài đặt timeout ×2, Đồng hồ ×2).
- Tách tập theo PHIÊN: ~60% phiên để hiệu chỉnh τ/δ, ~40% phiên để test; không trộn bước của cùng phiên giữa hai tập.
- Gán nhãn cặp màn (scripts/workflow_eval.py):
  - Nút toàn cục = phần tử (resource-id hoặc chữ tĩnh + ô lưới) xuất hiện ở ≥ 3 màn có tập resource-id khác nhau rõ rệt trong cùng app (back, tab bar, nút ghim đáy). Không dùng nút toàn cục làm bằng chứng "cùng màn".
  - Positive: 2 bước ở 2 phiên khác nhau cùng app, AI bấm cùng phần tử KHÔNG toàn cục, VÀ Jaccard tập resource-id (không trọng số, không vị trí) ≥ 0.6.
  - Negative: màn của app khác; hoặc cùng app nhưng Jaccard tập resource-id < 0.3. Vùng 0.3–0.6 không gán nhãn tự động.
  - Bước liên tiếp trên CÙNG màn (cùng phiên, cây gần như không đổi) không bao giờ là negative của nhau.
  - Người kiểm bằng mắt 50 cặp ngẫu nhiên (ảnh có trong traces/images); nhãn tự động sai > 5% → sửa quy tắc trước khi hiệu chỉnh.
- Trên tập hiệu chỉnh: ROC của S → chọn τ sao cho false-match = 0, báo recall.
- Mô phỏng trên tập test: dựng workflow từ phiên A, chạy trên chuỗi bước phiên B cùng app. Đo coverage (% bước autopilot đề xuất), precision (% đề xuất trùng thao tác AI thật: trùng resource-id hoặc bounds giao ≥ 50%), LLM_calls_saved.
- Cổng sang M2: precision ≥ 98%, 0 đề xuất vào nút cam kết (kiểm bằng chốt 5.1), 0 false-match khác app.

## 3. Câu hỏi 2 — Biểu diễn và lưu
3.1 Lưu ở <app_data>/workflows/<package>/<workflow_id>.json (không commit). workflow_id = <package>__<intent_slug>__<n>; kèm app_version (versionCode từ dumpsys package).
3.2 Recorder: chạy sau khi phiên Flash completed và AI báo thành công; đọc DataEngine (steps.action_taken, pre_image_name → images.ui_tree), không đổi đường quan sát.
- Bỏ bước lỗi / no_effect / bị guard chặn / bị hoàn tác ngay (back ngay sau đó) / ui_tree rỗng.
- click_sequence [I1]: tách thành các edge con theo thứ tự, mỗi edge có target riêng và đều phải qua hậu kiểm 4.5; nếu không giải được target cho mọi phần tử của chuỗi (toạ độ thuần không nằm trong node nào) → cả chuỗi là judgement.
- 1 lần gọi LLM text-only (goal + danh sách thao tác + chữ phần tử đích) → JSON: intent_slug, slots (có cờ required), mỗi bước kind ∈ nav | slot:<name> | judgement. Nghi ngờ → judgement.
- Chữ tĩnh = chữ của phần tử nav (và chữ giống hệt ở cùng node qua ≥ 2 lần ghi).
3.3 Định dạng (rút gọn):
{"schema":1,"workflow_id":"com.example.food__search_item_add_to_cart__1","package":"com.example.food","app_version":12345,"intent":"Tìm một món và thêm vào giỏ, dừng trước khi đặt","slots":{"item":{"desc":"tên món","required":true}},"stats":{"runs":3,"ok":3},"updated_at":"…",
 "nodes":{"n1":{"fingerprints":["..."],"static_texts":["Food"],"seen":3,"last_matched_run":3}},
 "edges":[
  {"id":"e1","from":"n1","to":["n2"],"kind":"nav","action":{"tool":"click"},"target":{"resource_id":"…:id/search_bar","class":"android.widget.EditText","text_role":"static","text":"Search","grid":[5,1],"nth":0},"stats":{"ok":3,"fail":0,"consec_fail":0},"state":"active"},
  {"id":"e2","from":"n2","to":["n3"],"kind":"slot:item","action":{"tool":"input_text","text":"{item}"},"target":{"focused":true,"class":"android.widget.EditText"}},
  {"id":"e3","from":"n3","to":["n4"],"kind":"slot:item","action":{"tool":"click"},"target":{"text_role":"slot","slot":"item","match":"contains"}},
  {"id":"e4","from":"n4","to":["n5"],"kind":"judgement"},
  {"id":"e5","from":"n5","to":["n6"],"kind":"nav","action":{"tool":"click"},"target":{"text_role":"static","text":"Add to basket"}},
  {"id":"stop","from":"n6","kind":"commitment_stop"}]}
Chuỗi "Add to basket" là dữ liệu ghi lại, không phải hằng trong code/prompt (AGENTS.md 2.7).
Ví dụ cơm → trà sữa: goal mới → slot item="trà sữa trân châu". e1 nav (autopilot) → e2 gõ {item} (autopilot) → e3 chọn kết quả chứa {item}: đúng 1 khớp → autopilot; 0 hoặc >1 → AI → e4 judgement (màn tuỳ chọn size/đường/đá) → luôn AI → e5 nav "thêm vào giỏ" (autopilot nếu khớp) → commitment_stop → AI kiểm giỏ và báo cáo. Kỳ vọng 3–5 lượt LLM thay ~20.
3.4 Giải target trên raw_elements live:
- resource_id (+nth) → chữ tĩnh / chữ slot qua find_matching_text_elements → class + grid (chỉ icon không nhãn, chỉ khi duy nhất trong ô).
- target.focused [I2]: đúng 1 node có focused="true" và class editable (EditText hoặc editable=true); 0 hoặc >1 → fallback.
- Không bao giờ dùng toạ độ tuyệt đối đã ghi. Kết quả phải duy nhất và không bị che (cùng kiểm tra occlusion của list: node vẽ sau phủ ≥ 50%).
- Cách gửi sang executor [I6] (sau khi đã giải đúng 1 node trên raw_elements live và qua chốt 5.1):
  - Node có mặt trong state.indexed_elements (cùng bounds, và cùng resource-id nếu có) → gửi `target=<index>`: executor dịch qua _resolve_index, giữ metadata resource_id/class/bounds vào action_taken, không tìm lại theo chữ.
  - Node không có trong list (bị lọc: idx > 100, node chỉ có OCR…) → gửi toạ độ tâm live 0–1000 + `target_description` tự sinh từ descriptor (vd `autopilot e5: resource_id=…, text="…"`) + cờ nội bộ `_autopilot=True` (không khai báo cho LLM) để executor bỏ snap_to_named_element và ghi resource_id/class vào semantics.
  - Autopilot KHÔNG dùng target_text (tránh re-lookup trên list đã lọc dẫn tới Ambiguity giả hoặc chọn node khác node đã kiểm 5.1).
3.5 Merge nhánh:
- Thao tác AI ở bước fallback ghi tạm (provisional).
- Phiên thành công: màn trước thao tác khớp node có sẵn theo cùng tiêu chí 2.3 (S ≥ τ_screen và biên δ) → thêm edge từ node đó và thêm fingerprint vào node (tối đa 5 fingerprint/node, bỏ cái cũ nhất); không khớp → node mới. Ngưỡng gom node = τ_screen [I4b].
- Edge mới không ghi đè edge cũ; cùng from nhiều edge → chỉ chọn edge có target giải được duy nhất trên màn live, nhiều edge giải được → fallback.
- provisional → active sau 2 lần thành công độc lập (2 phiên khác nhau).
- Phiên thất bại → bỏ provisional, chỉ tăng stats.fail cho edge đã phát lại.
- Ghi file nguyên tử (ghi tạm + rename), giữ 1 bản .bak trước mỗi merge.
3.6 App cập nhật: app_version khác → stale: τ + 0.05, dừng autopilot ở lần lệch đầu, không resume. Edge consec_fail ≥ 2 → disabled. Node không khớp trong 5 lần chạy liên tiếp của workflow → xoá. > 50% edge disabled → xoá workflow, ghi lại ở lần sau.

## 4. Fallback sang AI (mỗi bước) — dừng khi bất kỳ:
1) không node khớp hoặc 2 node quá gần (2.3);
2) edge judgement, hoặc slot cần dùng thiếu giá trị (missing);
3) target không duy nhất / bị che / mơ hồ;
4) chốt cam kết 5.1 báo cam kết hoặc không đọc được;
5) hậu kiểm sau thao tác: no_effect, hoặc màn mới không khớp node đích `to`. Trước khi kết luận lệch [I4b]: nếu màn mới chưa ổn định hoặc trông như đang tải (ít node nội dung dưới thanh trên, cùng heuristic skeleton đang dùng trong action_executor.py) → chờ có điều kiện bằng wait_for_screen_change(after="external") [I7] (không dùng mặc định after="my_action" vì observe vừa reset tracker → tốn thêm ~1 s judge), tối đa 2 nhịp / tổng 3 s, không record thành step riêng, rồi khớp lại; vẫn lệch → dừng;
6) ok=false / guard chặn;
7) đã phát lại 15 thao tác liên tiếp.
Resume: sau mỗi turn AI, nếu màn mới khớp một node có edge nav/slot active → autopilot tiếp (trừ workflow stale).

## 5. Chốt an toàn
1) Chốt cam kết LUÔN BẬT của autopilot [I2] — độc lập ARTEMIS_BLOCK_COMMITMENT_TAPS, chạy trước mỗi thao tác autopilot:
   - is_commitment_text trên text/content-desc của node đích live, của node cha bấm được chứa điểm bấm, và các node có chữ nằm trong bounds đích;
   - OCR vùng quanh điểm bấm (tái dùng phần đọc của _guard_commitment_block, action_executor.py ~1129–1189) để bắt nút ghim đáy chỉ có trong ảnh;
   - lỗi đọc màn/OCR → coi như cam kết → dừng.
   Recorder cũng chạy chốt này khi ghi: điểm cam kết được ghi thành commitment_stop, không bao giờ thành edge nav/slot.
2) Autopilot đi qua cùng McpActionExecutor (không đường tắt tới session.call; cờ `_autopilot` chỉ tắt snap, không tắt guard nào). Lưu ý: Commitment/Anchor Guard của executor chỉ chạy với target_text (action_executor.py:336–341) — vì vậy chốt 5.1 là bắt buộc, không dựa vào guard executor; guard executor (Anchor, Ambiguity, Occlusion, Ineffective-tap, morphed-commitment) là lớp thứ hai.
3) Không burst mù: mỗi thao tác = 1 action + observe() + hậu kiểm 4.5.
4) Autopilot không bao giờ gọi report_task_status; AI luôn quan sát màn cuối (AGENTS.md 2.8 về trạng thái có sẵn).
5) Cờ agent.flash.workflow.mode = off | record | replay (mặc định off tới khi qua M3).

## 6. Câu hỏi 3 — Tích hợp
- artemis/workflow/ (mới): fingerprint.py, store.py, recorder.py, matcher.py, autopilot.py — không import gì theo app.
- mcp/observation.py + mcp/action_types.py: thêm ObserveResult.raw_elements.
- graph/state.py + graph/visibility.py: field State.raw_elements, State.observation_id và quyền ghi tương ứng (2.1).
- workflow/state_sync.apply_observation thay mọi phép gán state.indexed_elements ở Flash/MCP (danh sách ở 2.1).
- mcp/action_executor.py: cờ nội bộ `_autopilot` ở nhánh toạ độ của _resolve_target (bỏ snap, ghi resource_id/class vào semantics) (3.4).
- Hook trong FlashRunner._run_reactive_loop (agents/flash/runner.py:2576) [I3]: ở đầu mỗi vòng, trước khi build tail và gọi _invoke_model, nếu autopilot bật và màn hiện tại khớp → chạy liên tiếp các thao tác autopilot (mỗi thao tác qua executor + hậu kiểm) cho tới khi gặp điều kiện dừng Mục 4; sau đó mới tới turn AI bình thường.
- Transcript ledger [I3]: KHÔNG tạo AIMessage/ToolMessage giả (tránh sai thứ tự role và thiếu thought_signature, tránh phá ScrubEdgeCompressor). Khi bàn giao, tail (HumanMessage) của turn AI kế tiếp thêm 1 khối có cấu trúc:
  `[Autopilot] workflow X: ran N steps: 1) click "Search" → ok; 2) input_text "trà sữa" → ok; …; stopped: <lý do Mục 4>`
  Khối này nằm trong HumanMessage của turn đó nên ledger/scrub xử lý như mọi tail; bất biến 1-1 ToolMessage (AGENTS.md 2.6) không bị chạm vì không có tool_call giả.
- DataEngine: record_step cho từng thao tác autopilot như thường, extra_metadata.autopilot = {workflow_id, edge_id} → history tools (search_history/replay_steps) và chunker vẫn thấy.
- turns.jsonl: dòng riêng cho mỗi khối autopilot {autopilot:true, llm_s:0, steps, stop_reason}; tổng hợp llm_calls, autopilot_steps, fallback_reasons.
- Safety net Pro, Pro: không đổi.
6.1 Chọn workflow cho task mới [I5]:
- Xác định package theo thứ tự: (a) package foreground của ảnh đầu do _prepare_conversation chụp (app đã mở sẵn); (b) tên app trong goal → package như manage_app; (c) package foreground sau thao tác đầu của AI.
- Có ứng viên → 1 LLM text-only (goal + intent + slots của các ứng viên) → {workflow_id|none, slot_values}.
- Nhiều ứng viên hợp lệ: LLM chọn; nếu LLM trả nhiều/không rõ → ưu tiên tỷ lệ ok/runs cao hơn, rồi updated_at mới hơn; vẫn hoà → none.
- Slot required thiếu giá trị: không bỏ workflow; slot đánh dấu missing → mọi edge dùng slot đó là fallback AI; các edge nav vẫn chạy.
- none → chạy AI bình thường ở chế độ record.
6.2 Đo: llm_calls/task, total_s/task, autopilot_steps/task, tỷ lệ thành công, số lần chốt/guard chặn autopilot. Offline M0: coverage, precision, ước tính lượt tiết kiệm. Thiết bị M4: 3 tác vụ × 2 app, mỗi tác vụ 1 lần ghi + 3 lần phát lại với slot khác, so baseline mode=off cùng model.

## 7. Lộ trình
- M0: fingerprint.normalize + matcher + scripts/workflow_eval.py trên traces/data_engine.db → cổng 2.4.
- M1: recorder + store offline từ trace cũ → unit test định dạng/slot/click_sequence/commitment_stop; review bằng mắt 3 workflow.
- M2: raw_elements (ObserveResult + State field + NODE_VISIBILITY + apply_observation, test quét không còn gán trực tiếp, test chạy với ARTEMIS_STRICT_STATE=1) + autopilot + hook _run_reactive_loop sau cờ → unit test executor giả cho từng điều kiện Mục 4, chốt 5.1 khi env tắt, gửi index khi node có trong list / toạ độ + `_autopilot` khi không (không snap, metadata giữ resource_id), tail bàn giao đúng định dạng, không có AIMessage giả.
- M3: merge/stats/stale → unit test không ghi đè, provisional→active, ghi file nguyên tử.
- M4: benchmark thiết bị → giảm ≥ 50% llm_calls ở lần phát lại, tỷ lệ thành công không giảm, 0 lần bấm cam kết.
- Mỗi mốc: `uv run pytest tests/unit/mcp/ tests/unit/agents/ tests/unit/llm/` pass 100%, cập nhật KE_HOACH_TOI_UU_ARTEMIS.md và docs/FLOW_DIEU_KHIEN_THIET_BI.md (Mục 9–10).

## 8. Rủi ro đã biết
- Màn giống bố cục khác ngữ cảnh → biên δ + chữ tĩnh + hậu kiểm node đích.
- Recorder LLM phân loại sai nav/judgement → active sau 2 lần, nghi ngờ thì judgement.
- Cây nghèo (WebView/game) → màn < 5 token app không bao giờ khớp.
- Trạng thái có sẵn (giỏ cũ) → AI kiểm ở commitment_stop (AGENTS.md 2.8).
- Màn loading/skeleton → chờ có điều kiện trước khi kết luận lệch (4.5).
