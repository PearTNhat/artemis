# KẾ HOẠCH: APP WORKFLOW — PHÁT LẠI LUỒNG THAO TÁC CÓ FALLBACK AI (v6)

File: /home/nhat/Workspace/control-android/artemis/docs/KE_HOACH_APP_WORKFLOW.md
Phạm vi: chỉ Flash (theo KE_HOACH_TOI_UU_ARTEMIS.md Mục 0.1: tối ưu Flash, Pro chỉ sửa lỗi).
Trạng thái: ĐÃ ĐỒNG THUẬN v4 (agents-debate phiên 0906a23d, 2026-11-26; author Claude, critic Gemini, 6/6 tiêu chí đạt). Báo cáo: ~/.review-room/report-0906a23d-7567-4046-af6e-192fd380c738.md

## v6 (2026-11-27) — Kế hoạch tiếp theo

### Hiện trạng (sau test emulator 26/11, xem KE_HOACH_TOI_UU_ARTEMIS.md 1.15)
- Đã có: ghi workflow từ phiên `completed`, autopilot phát lại có kiểm màn đích, chọn workflow 1 lần LLM chỉ-chữ, chốt vòng lặp, chờ UI đứng yên, xử lý JSON hỏng của model, nhận diện ô nhập bằng `hint`, gộp trạng thái cuộn/gõ của cùng màn, ẩn bàn phím che target, chế độ `record` gộp tác vụ lặp lại (không đẻ file mới). 60 unit test workflow; regression 1006 pass / 2 fail có sẵn.
- Kết quả: có lợi rõ ở luồng app gốc lặp lại (Đồng hồ: 2–3 lượt AI / 48–59 s so với 6–9 lượt / 71–98 s); web (Chrome) không lợi, không hại; Danh bạ: Qwen 4B không tự làm được, Gemini ghi → Qwen phát lại thì autopilot gõ được các ô nhưng chưa bấm được Lưu. 0 lần autopilot bấm sai không phát hiện sau các bản sửa.
- Lỗi đã sửa sau phản biện 27/11: autopilot cuộn tìm target bằng swipe mặc định (y≈896/1280) khi bàn phím đang mở → cú vuốt thành vuốt-gõ trên Gboard ("Lan" → "Lan Hu Hu Hu", trace f68c5042) — bấm sai không bị phát hiện. Sửa: bàn phím mở (nhận bằng vùng bao các phần tử của cửa sổ khác ở nửa dưới, vì Gboard có thể không có khung gốc) → BACK ẩn trước; swipe tính trong vùng cuộn của app, cắt phần bị cửa sổ khác phủ, không có dải an toàn → không vuốt, giao AI. Bài Danh bạ chưa tới bước Lưu là do lỗi này (autopilot kẹt ở cuộn tìm ô Họ), không phải nhãn "Save" mơ hồ.
- Hạn chế: mỗi ô chỉ 1–2 lần chạy (chưa đủ kết luận); màn có dữ liệu khác lúc ghi không khớp; khi autopilot dừng, model yếu không có hướng dẫn nên loay hoay; chọn workflow luôn cần LLM; slot chỉ bắt từ chữ gõ; luôn mất lượt AI đầu để mở app.

### Kịch bản vận hành: MỘT model (quyết định 2026-11-27)
- Production dùng một model duy nhất, mạnh hơn Qwen 4B: `qwen3-vl:8b-32k` (Colab T4, `control-android/scripts/8b-colab-start.sh`, cổng 11435). Không dùng model mạnh ghi hộ cho model yếu.
- Hệ quả: cùng một model **tự ghi** ở lần đầu và **tự phát lại** từ lần sau. Lần ghi do chính model này làm sẽ có đi nhầm / sửa sai nhiều hơn → recorder phải tự làm sạch (W1b); số liệu benchmark phải đo đúng kịch bản tự ghi → tự phát lại, cùng model cho đối chứng (W5).
- Số liệu Qwen 4B ở mục 1.15 chỉ là tham khảo lịch sử; lần Gemini ghi → Qwen phát lại (Danh bạ) là thử nghiệm, không phải cách vận hành.
- Tuỳ chọn (không bắt buộc): workflow "mẫu" do chính model 8B ghi trong lúc test, đóng gói kèm cho app hay dùng — runtime vẫn chỉ một model.

### Thứ tự làm

**W0 — Công cụ đo offline (làm trước, để mọi bước sau đo được)**
- `scripts/workflow_replay_eval.py`: chạy lại các phiên trong `traces/data_engine.db` qua recorder + autopilot (thiết bị giả từ cây UI đã lưu), không cần máy/GPU.
- Đo: % bước autopilot làm được (coverage), số lần bước autopilot khác bước thật mà không bị kiểm màn bắt (phải = 0), lý do dừng phổ biến.
- Xong khi: chạy được trên mọi phiên có ≥ 2 lần cùng loại tác vụ, in bảng theo app.

**W1b — Làm sạch lần ghi (cắt vòng đi nhầm)** — đi cùng W1
- Khi chuỗi bước quay về một màn đã qua (n_X → … → n_X) mà ở giữa không có giá trị slot nào được nhập/giữ lại VÀ trạng thái n_X lúc quay về giống hệt lúc đi (cùng nội dung + checked/selected/text, so bằng chữ ký nội dung của loop_guard), bỏ cả đoạn vòng đó. Không cắt nếu trạng thái đã đổi (bật toggle, xác nhận qua dialog rồi quay lại, bước cuối quay về màn đầu như n4 → n1): không thành bước của workflow, không vào playbook. Mở rộng luật đã có (bước bị BACK hoàn tác ngay, gõ sai rồi gõ lại chỉ giữ lần còn trong ô).
- Xong khi: unit test vòng đi nhầm bị cắt, vòng có nhập giá trị được giữ; chạy W0 trên các phiên thật để xem số bước bị cắt.

**W1 — Playbook (hướng dẫn bằng chữ sinh từ workflow)** — ưu tiên cao nhất
- Recorder lưu thêm cho mọi bước (kể cả `judgement`): nhãn phần tử đã bấm và `target_description` AI ghi lúc làm (chỉ để làm hướng dẫn, autopilot vẫn không tự bấm bước judgement).
- Workflow là đồ thị (gộp nhiều lần chạy, có nhánh, có bước disabled): playbook lấy MỘT đường chính từ màn đầu tới `end_nodes` (bỏ bước disabled, ưu tiên bước có stats.ok cao), không liệt kê toàn bộ `edges`. Khi autopilot dừng, notice đánh dấu rõ bước nào của playbook đã xong để model không làm lại.
- Sinh playbook tất định từ các bước đã ghi (không gọi LLM), slot thay bằng giá trị mới, ví dụ: "1) bấm 'Add city' 2) gõ Dublin vào ô tìm 3) bấm tìm trên bàn phím 4) chọn dòng đúng thành phố (nhiều kết quả: chọn theo quốc gia) 5) mục vừa thêm có thể ở cuối danh sách — cuộn kiểm rồi báo xong".
- Đưa vào prompt ngay lượt đầu khi đã chọn được workflow, và nhắc bước kế tiếp trong notice khi autopilot dừng.
- Xong khi: unit test sinh playbook; emulator với `qwen3-vl:8b-32k` tự ghi rồi tự phát lại (Đồng hồ, Danh bạ, Chrome), so số lượt có / không playbook.

**W2 — Chọn workflow không cần LLM + index**
- So câu yêu cầu mới với `goal` đã ghi theo từ: chỉ khác đúng chỗ của slot → chọn luôn, lấy giá trị slot từ phần khác (ví dụ "Madrid" → "Dublin"); khác nhiều → mới gọi LLM như hiện tại.
- `index.json` mỗi app (workflow_id, goal, slots, số bước, thống kê) để không phải đọc toàn bộ vân tay khi chọn.
- Lọc theo màn hiện tại chỉ là **ưu tiên**, không phải điều kiện cứng: đang ở trong app và có workflow khớp màn → xét các workflow đó trước; không có workflow nào khớp màn (đang ở màn hình chính, trang web khác lúc ghi) → vẫn chọn theo câu yêu cầu trên index (cần cho W4 và để có playbook W1 ngay lượt đầu).
- Fast-path không LLM chỉ khi khung chữ tĩnh quanh mọi slot khớp 100%: thay từng giá trị slot trong goal cũ bằng nhóm bắt (regex) neo giữa phần chữ tĩnh hai bên, câu mới phải khớp trọn; khác khung (thứ tự khác, thêm/bớt chữ, hai slot sát nhau không có chữ ngăn cách) → gọi LLM như hiện tại.
- Xong khi: unit test so câu (đúng/sai/nhiều slot); đếm số lần gọi LLM chọn workflow trên các bài test Đồng hồ = 0.

**W3 — Slot chọn bằng bấm + trạng thái `selected`**
- `detect_slots` xét cả bước bấm: nhãn ≥ 3 ký tự, khớp nguyên từ trong câu yêu cầu, không phải nút điều hướng cố định đã ghi → slot (ví dụ "size L", "tab Timer").
- Thêm trạng thái `selected`/`checked` vào vai trò trong vân tay (phân biệt tab đang chọn); không thêm chữ tĩnh vào vân tay (đã có `anchors`).
- Xong khi: unit test nhãn ngắn/từ chung ("OK", "1") không thành slot; tab khác nhau không khớp nhầm.

**W4 — Tự mở app từ màn hình chính**
- Ở màn hình chính (launcher): chọn workflow trên index của mọi app theo câu yêu cầu (W2); chọn được → autopilot gọi `manage_app` mở app rồi chạy tiếp; không chọn được → AI như cũ.
- Xong khi: bài test Đồng hồ bắt đầu từ màn hình chính không tốn lượt AI để mở app.

**W5 — Benchmark trên máy (sau W1–W4)**
- Model: `qwen3-vl:8b-32k` (một model cho cả ghi, phát lại và đối chứng). 3 app (Đồng hồ, Danh bạ, Chrome) × 3 giá trị: lần 1 tự ghi, các lần sau tự phát lại; mỗi ô ≥ 3 lần; đối chứng `mode=off` cùng model, cùng điều kiện (app mở sẵn, tắt app giữa các bài).
- Báo riêng: tỉ lệ lần ghi đầu thành công (không thành công thì không có workflow để phát lại).
- Chỉ số: lượt AI, thời gian, tỉ lệ thành công, số lần autopilot bấm sai không phát hiện (= 0), số lần chốt vòng lặp dừng.
- Chạy từng đợt nhỏ, tắt app giữa các bài, dừng daemon khi hết giờ (máy ảo từng tự tắt khi chạy lâu).

**Để sau**
- Đa package (hộp thoại quyền, bộ chọn file, chuyển app): hộp thoại xin quyền vẫn để AI xử lý, không tự bấm "Cho phép".
- Desktop: lõi (vân tay, so khớp, recorder, autopilot, bộ chọn, chốt vòng lặp) dùng lại qua một `normalize()` riêng; cần chuẩn hoá toạ độ theo cửa sổ đang dùng, xử lý nhiều cửa sổ, thêm double-click/right-click/phím tắt vào recorder, lớp thực thi riêng. Chỉ thiết kế, chưa làm.
- Lỗi LLM "JSON hỏng": nếu còn gặp nhiều với Qwen, thử hỏi lại với temperature cao hơn một chút.

### Quy trình giao việc và phản biện (người code ≠ người phản biện)
Người code làm từng bước một, theo đúng thứ tự; người phản biện (Claude) đánh giá xong bước trước mới làm bước sau.

| Vòng | Bước | Người code nộp | Phản biện kiểm thêm |
|:-:|:--|:--|:--|
| 0 | (trước khi bắt đầu) | — | Commit trạng thái hiện tại làm mốc; người code làm trên nhánh mới từ mốc đó |
| 1 | W0 công cụ đo offline | script + bảng số liệu hiện trạng (coverage, bấm sai không phát hiện, lý do dừng) theo app | Tự chạy lại script; số liệu phải tái lập được; kiểm script không đọc/ghi gì ngoài DB ở chế độ chỉ đọc |
| 2 | W1b làm sạch lần ghi | code + unit test + số liệu W0 trước/sau | Đọc các ca bị cắt trên phiên thật: không cắt nhầm đoạn có nhập giá trị |
| 3 | W1 playbook | code + unit test + ví dụ playbook sinh ra cho 3 workflow có sẵn | Playbook đúng thứ tự bước, không bịa bước, thay đúng giá trị slot; chạy thử trên emulator với `qwen3-vl:8b-32k` |
| 4 | W2 chọn workflow không LLM + index | code + unit test (câu khác đúng chỗ slot / khác nhiều / nhiều slot) | Tìm ca chọn nhầm (câu giống nhưng khác loại tác vụ) |
| 5 | W3 slot chọn bằng bấm + `selected` | code + unit test nhãn ngắn ("OK", "1") không thành slot | Thử nhãn trùng một từ trong câu yêu cầu |
| 6 | W4 tự mở app từ màn hình chính | code + unit test | Không tự mở nhầm app khi câu yêu cầu không khớp workflow nào |
| 7 | W5 benchmark | — (phản biện tự chạy trên emulator) | Theo mục W5; báo cáo số liệu cuối |

**Mỗi lần nộp phải có:**
1. Danh sách file đã sửa; không sửa ngoài phạm vi bước đó (có lý do thì ghi rõ).
2. Unit test mới cho từng hành vi mới, kể cả ca phải TỪ CHỐI (không chỉ ca chạy được).
3. Kết quả `uv run pytest tests/unit/mcp/ tests/unit/agents/ tests/unit/llm/ tests/unit/workflow` — được phép đúng 2 lỗi có sẵn: `test_explorer_tiers.py::test_gemini_model_without_google_key_uses_universal_engine`, `test_flash_turn_log.py::test_final_summary_wait_caps_the_shutdown_flush`.
4. Số liệu W0 trước/sau (từ vòng 2 trở đi).
5. Cập nhật tài liệu theo AGENTS.md mục 3 (KE_HOACH_TOI_UU_ARTEMIS.md, docs/FLOW_DIEU_KHIEN_THIET_BI.md nếu chạm luồng điều khiển).
6. Báo cáo ngắn: làm gì, quyết định gì và vì sao, còn thiếu gì.

**Phản biện từ chối nếu vi phạm bất biến nào:**
- Lưu / dùng toạ độ cố định để bấm; bỏ kiểm màn đích sau bước autopilot.
- Autopilot tự bấm nút cam kết, tự chọn trong danh sách ≥ 2 mục, tự bấm "Cho phép" hộp thoại quyền.
- Hardcode chữ / tên nút / package của một app cụ thể (AGENTS.md 2.7).
- Tạo AIMessage/ToolMessage giả, phá bất biến 1-1 ToolMessage (AGENTS.md 2.6).
- Tính năng workflow bật mặc định; thay đổi hành vi Pro.
- Số liệu "0 lần bấm sai không phát hiện" bị vi phạm trên W0.

**Kết quả mỗi vòng:** Chấp nhận / Cần sửa (kèm `file:dòng` và lý do) / Từ chối. Người code sửa rồi nộp lại trong cùng vòng.

### Không đổi
- Mọi tính năng workflow vẫn tắt mặc định (`agent.flash.workflow.mode = off`); chốt vòng lặp, chờ UI đứng yên, xử lý JSON hỏng đang bật cho Flash.
- Nguyên tắc an toàn: không toạ độ cố định; kiểm màn đích sau mỗi bước; không tự bấm nút cam kết; danh sách ≥ 2 mục luôn giao AI/người dùng; không hardcode chữ theo app (AGENTS.md 2.7).

## v5 (2026-11-26) — thay đổi so với v4 (ưu tiên hơn các mục bên dưới khi mâu thuẫn)
Lý do: cổng M0 (Mục 2.4) trượt — precision 0.597 so với yêu cầu ≥ 0.98. Thử thêm "chỉ tự bấm màn mà mọi lần trước AI đều bấm cùng nút" (scratchpad nav_only_eval.py) chỉ phủ 1–2% số bước, vì trace cũ ít phiên lặp lại cùng màn và hành vi AI bị nhiễu (lúc cuộn, lúc chờ, lúc bấm). Kết luận: không suy luật từ trace cũ, không đo bằng "trùng thao tác AI", mà ghi chủ động và kiểm tra sau mỗi bước.
- V5.1 Ghi chủ động: sau 1 phiên Flash báo `completed`, runner ghi workflow từ chính các thao tác của phiên (cây UI gốc trước/sau mỗi thao tác lấy từ `ctx.last_raw_observation` do observe() công bố; không cần đọc DataEngine). Phân loại tất định, không gọi LLM, không chuỗi cứng: gõ chữ có trong goal → slot; bấm phần tử có nhãn chứa giá trị slot → slot; bấm mục trong vùng cuộn, bấm ngay sau ask_user_choice, gõ chữ không có trong goal → judgement; nút cam kết → commitment_stop; còn lại (và ENTER/SEARCH/BACK) → nav. Cuộn/chờ/click_sequence không tạo bước. (thay 3.2)
- V5.2 Bước nav/slot dùng được ngay từ lần chạy thứ 2 (bỏ "provisional → active sau 2 lần", 3.5), vì mỗi bước đều được kiểm tra màn đích.
- V5.3 Kiểm màn đích kép sau mỗi bước autopilot: màn mới phải khớp node đích (vân tay, τ/δ như 2.3) VÀ có ít nhất 1 "neo" (resource-id hoặc nhãn ngắn có ở màn đích mà không có ở màn trước, ngoài vùng cuộn, không chứa giá trị slot). Bước gõ chữ kiểm bằng giá trị đã nằm trong ô nhập. Không dùng wait_for_text (chỉ đọc XML, chờ mặc định 8 s): poll tối đa 4 lần cách 0.6 s bằng ui_hierarchy. Sai → dừng, giao AI, edge.fail+1; 2 lần sai liên tiếp → disabled.
- V5.4 Điều kiện "đã gõ": gõ chữ không đổi cấu trúc màn hình nên "gõ từ khoá" và "bấm tìm" cùng một node. Edge ghi kèm `typed=<slot>` khi ô nhập ở màn trước đã chứa giá trị slot; autopilot chỉ chạy edge đó khi ô nhập live chứa giá trị, và bỏ qua bước gõ khi đã gõ rồi (không bao giờ bấm tìm với ô trống).
- V5.5 Một node có cả bước judgement và bước nav → luôn giao AI (màn đó từng cần lựa chọn). Node có commitment_stop → luôn giao AI.
- V5.6 Chọn workflow: 1 lần gọi LLM chỉ-chữ / package / phiên; goal trùng nguyên văn → không gọi. Giá trị slot LLM trả về phải xuất hiện nguyên văn trong goal mới, nếu không coi như thiếu (chống bịa).
- V5.7 Bàn giao: khối `[Autopilot] … Stopped: <lý do>` vào notices của tail + ép gửi ảnh ở lượt đó (`_force_image`), quan sát lại màn hình trước khi gọi AI.
- V5.8 Popup chen ngang: popup xin quyền thuộc package hệ thống khác → không bao giờ khớp workflow của app (tự loại). Popup trong app được AI đóng bằng BACK/nút đóng sẽ được ghi như bước nav và chỉ phát lại khi khớp đúng màn popup + kiểm màn đích. KHÔNG tự bấm "cho phép/đồng ý" — chốt cam kết + màn đích vẫn áp dụng. (Mở rộng có giới hạn mẹo (2) của Gemini.)
- V5.9 Cổng mới thay 2.4 (đo trên máy/emulator, không offline): (a) 0 lần autopilot bấm sai mà không bị phát hiện; (b) 0 lần bấm nút cam kết; (c) ≥ 90% bước autopilot tới đúng màn đích; (d) số lượt LLM ở lần chạy lại giảm so với mode=off cùng tác vụ.
- V5.10 (bổ sung sau chạy emulator 2026-11-26) Vân tay workflow = bố cục, không dữ liệu (`fingerprint.app_fingerprint`): chỉ node của app foreground (bàn phím/overlay đổi độc lập), có/không thay vì số lượng (danh sách ngắn không cuộn → các dòng không được gộp), node có resource-id không kèm vị trí (bàn phím đổi cỡ cửa sổ làm lệch ô lưới), dòng không id lặp theo cột gộp thành 1 token. Màn thưa khớp được khi ≥ 3 node app và ≥ 2 resource-id (`matcher.is_matchable`). Vân tay M0 (`tokenize`) không đổi.
- V5.11 Chọn trong danh sách chỉ tự động khi danh sách có đúng 1 mục cùng loại và so khớp bỏ dấu; ≥ 2 mục → giao AI/người dùng (lỗi thật: chọn "Reykjavik, MB, Canada" thay "Reykjavík, Iceland" — kiểm màn đích không bắt được lựa chọn sai).
- V5.12 Chỉ đi tiến trong 1 lần chạy: không chạy lại bước đã chạy, không quay lùi (lỗi thật: sau khi xong, app về màn đầu → autopilot làm lại từ đầu). Về lại màn đã qua → nhắc AI 1 lần, kèm nhắc cuộn nếu màn cuộn được.
- V5.13 Hễ autopilot đã gửi thao tác (kể cả chưa xác nhận được màn đích) → luôn quan sát lại và báo AI. Che khuất: chỉ xét trong cùng package (thứ tự dump không phải thứ tự vẽ giữa các cửa sổ); target của app bị cửa sổ khác (bàn phím, hộp thoại) phủ ≥ 50% → không bấm.
- Kết quả emulator: xem KE_HOACH_TOI_UU_ARTEMIS.md mục 1.15.
- Hoãn: gọi AI chỉ-chữ ở màn danh sách (đề xuất B), nhớ sở thích người dùng (mẹo (5)), truyền raw_elements qua State (2.1 — v5 dùng `ctx.last_raw_observation` thay thế, đủ cho Flash).
- Cờ: `agent.flash.workflow.mode = off | record | replay` (mặc định off), env `ARTEMIS_WORKFLOW_MODE`, `ARTEMIS_WORKFLOW_DIR`. File: artemis/workflow/{targets,store,recorder,autopilot,selector}.py, artemis/agents/flash/workflow_hook.py.

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
