# LỖI ĐÃ GẶP — đọc trước khi sửa phần điều khiển thiết bị / app workflow

Mục đích: không lặp lại lỗi cũ. Mỗi lỗi ghi theo mẫu cố định và **gắn một test hồi quy**
(tài liệu chỉ nhắc, test mới chặn được lỗi quay lại). Lỗi nào chưa có test ghi rõ "CHƯA CÓ TEST".

Thêm lỗi mới: thêm một mục `[L-NNN]` ở cuối mục 2 (số tăng dần, không đánh lại số cũ), cập nhật
mục 1 nếu nó cho thấy một bài học chung mới.

**Ghi khi lỗi đạt ít nhất một điều:**
1. Gây hại thật hoặc suýt gây hại: bấm sai không bị phát hiện, làm hỏng dữ liệu, làm lại việc đã
   xong, suýt bấm nút cam kết (đặt hàng / thanh toán) — **luôn ghi**.
2. Nguyên nhân gốc không hiển nhiên (người khác dễ mắc lại).
3. Đã sửa sai một lần rồi mới đúng — ghi cả lần sửa sai.
4. Lặp ở nhiều chỗ / dạy được một bài học chung (cập nhật mục 1).
5. Làm phiên treo, chạy mãi, chạy ngầm, tốn tài nguyên.
6. Công cụ đo / test báo sai (số đẹp nhưng không chứng minh gì).

**Không cần ghi:** lỗi đánh máy / cú pháp / import sửa ngay; lỗi của code đang viết dở chưa chạy thật;
model làm kém mà không lộ lỗ hổng của hệ thống; lỗi đã có mục (thêm triệu chứng / trace vào mục cũ).

**Chưa rõ nguyên nhân vẫn ghi**, đánh dấu `ĐANG MỞ`, tối thiểu: triệu chứng (thấy gì, app nào),
trace / link workspace (`http://localhost:8000/workspace/<id>`), ảnh nếu có. Nguyên nhân gốc, cách sửa
và test điền sau.

---

## 1. Bài học chung (đọc phần này trước)

1. **Không tin vị trí cố định.** Toạ độ vuốt mặc định, toạ độ đã ghi, ô lưới, thứ tự trong file đều
   đổi theo màn hình / bàn phím / nút nổi / lần gộp. Định danh phần tử bằng thuộc tính
   (resource-id → hint → chữ → loại + ô lưới) và lấy toạ độ từ **cây UI hiện tại**.
   (L-005, L-007, L-012, L-013, L-017, L-020)
2. **Kiểm màn sau khi bấm không bắt được LỰA CHỌN sai.** Chọn nhầm mục trong danh sách vẫn về đúng
   bố cục màn đích. Mọi lựa chọn giữa ≥ 2 mục cùng loại phải giao AI/người dùng; so khớp chữ phải bỏ dấu.
   (L-008, L-021)
3. **Cửa sổ khác (bàn phím, hộp thoại, thanh hệ thống) đổi độc lập với app.** Không đưa chúng vào
   nhận diện màn; không bấm/vuốt vào vùng chúng phủ; thứ tự trong dump không phải thứ tự vẽ giữa các
   cửa sổ. (L-004, L-006, L-012)
4. **Không tin model sẽ nghe lời nhắc.** Model yếu bỏ qua notice và prompt; vòng lặp, JSON hỏng,
   làm lại việc đã xong phải chặn bằng code. (L-009, L-010, L-011)
5. **Đi tiến, không làm lại.** Trong một lần chạy, không phát lại bước đã chạy; quay về màn đầu sau
   khi xong không được khởi động lại luồng (với app đặt hàng = đặt hai lần). (L-009)
6. **Công cụ đo phải mô phỏng được trường hợp xấu.** Thiết bị giả giữ nguyên màn khi bấm sai thì
   chỉ số "bấm sai không phát hiện = 0" đúng theo cách dựng, không chứng minh gì. (L-022)
7. **Chụp màn khi UI đã đứng yên.** Chụp giữa hiệu ứng chuyển cảnh cho AI và recorder một màn
   lẫn lộn. (L-014)
8. **Môi trường test phải sạch và cô lập.** Tắt app giữa các bài, giới hạn thời gian, dừng daemon;
   người code và người phản biện không sửa chung một thư mục làm việc. (L-015, L-016, L-018, L-019)
   Workflow: mỗi đợt test dùng thư mục riêng (`ARTEMIS_WORKFLOW_DIR`) và xoá nó trước khi chạy; sau khi sửa
   recorder / vân tay / nhận diện ô, xoá mọi workflow ghi bằng code cũ — workflow cũ có thể mang lỗi đã sửa
   (L-029) và bộ đếm `seen` cộng dồn làm bước "chờ ≥ 2 lần chạy" (L-026) bật nhờ lần chạy cũ.
9. **Kiểm sau bước chỉ chứng minh "làm giống lúc ghi", không chứng minh lúc ghi đúng.** AI có thể báo
   `completed` sai; một model tự ghi phải kiểm tính nhất quán của chính lần ghi (giá trị gõ còn trong ô khi
   gửi form, không ghi đè lẫn nhau). (L-029)

---

## 2. Danh sách lỗi

### [L-001] Workflow M0: đoán luật từ trace cũ, precision 0.60 < 0.98
- Triệu chứng: cổng M0 trượt; "chỉ tự bấm màn mà AI luôn bấm cùng nút" chỉ phủ 1–2% bước.
- Nguyên nhân gốc: trace cũ ít phiên lặp cùng màn; hành vi AI nhiễu (lúc cuộn, lúc chờ, lúc bấm);
  đo bằng "trùng việc AI đã làm" phạt cả các cách làm khác nhưng vẫn đúng.
- Cách sửa: ghi chủ động sau một lần chạy thành công + kiểm màn đích sau mỗi bước (KE_HOACH_APP_WORKFLOW v5).
- Test: — (quyết định thiết kế).

### [L-002] Nút nổi trên danh sách bị coi là mục danh sách
- Triệu chứng: nút "+" (`id/fab`) của Đồng hồ thành bước "cần chọn", chặn autopilot.
- Nguyên nhân gốc: node nằm trong khung scrollable bị đánh dấu `in_scrollable` theo hình học.
- Cách sửa: mục danh sách = trong vùng cuộn **và** lặp theo mẫu (cùng id, hoặc cùng loại + độ rộng).
- Test: `test_floating_button_over_a_list_is_not_a_list_item`.

### [L-003] Màn thưa (4 node) bị loại khỏi so khớp
- Triệu chứng: màn tìm kiếm của Đồng hồ không bao giờ được ghi/khớp.
- Nguyên nhân gốc: điều kiện ≥ 5 node của app (để loại WebView/game).
- Cách sửa: chấp nhận ≥ 3 node nếu có ≥ 2 resource-id (`matcher.is_matchable`).
- Test: CHƯA CÓ TEST riêng (được phủ gián tiếp qua các test replay màn tìm kiếm).

### [L-004] Vân tay màn bị bàn phím ảo chi phối
- Triệu chứng: màn sau nút "+" khớp 0.79 < 0.8; neo kiểm màn là phím bàn phím.
- Nguyên nhân gốc: ~40 phím Gboard vào vân tay của màn chỉ có 4 node app.
- Cách sửa: vân tay và neo chỉ dùng node của app foreground (`app_fingerprint`, `anchors_for`).
- Test: `test_keyboard_does_not_change_the_app_fingerprint`.

### [L-005] Vân tay lệch vì ô lưới / kích thước / số dòng
- Triệu chứng: cùng màn tìm kiếm khớp 0.54; màn kết quả 2 dòng vs 5 dòng khớp 0.56.
- Nguyên nhân gốc: bàn phím đổi cỡ cửa sổ → tâm node sang ô lưới khác; danh sách ngắn không
  scrollable nên các dòng không được gộp.
- Cách sửa: vân tay bố cục — node có id không kèm vị trí, có/không thay vì số lượng, dòng không id
  lặp theo cột gộp thành 1 token.
- Test: `test_layout_fingerprint_ignores_how_many_results_a_list_shows`.

### [L-006] Phím bàn phím luôn bị coi là "bị che"
- Triệu chứng: autopilot không bấm được phím "Search" của bàn phím.
- Nguyên nhân gốc: dump liệt kê bàn phím trước khung app; khung app toàn màn bị coi là phủ lên phím.
- Cách sửa: chỉ xét che khuất trong cùng package; bỏ khung layout lớn không chữ, không bấm được.
- Test: `test_keyboard_key_is_not_occluded_by_the_app_window_listed_after_it`, `test_app_target_under_the_keyboard_is_occluded`.

### [L-007] Autopilot đã bấm nhưng không báo AI
- Triệu chứng: AI hành động trên ảnh chụp cũ (`target 11` lỗi).
- Nguyên nhân gốc: chỉ bàn giao khi có bước được xác nhận; bước bấm chưa xác nhận bị bỏ qua.
- Cách sửa: hễ autopilot đã gửi thao tác → luôn quan sát lại + báo AI (kể cả bước chưa xác nhận).
- Test: `test_replay_stops_when_destination_differs` (kiểm `acted` và notice).

### [L-008] Chọn nhầm "Reykjavik, MB, Canada" — bấm sai không phát hiện
- Triệu chứng: autopilot tự chọn sai thành phố; AI thêm tiếp thành phố đúng → thừa một mục.
- Nguyên nhân gốc: "Reykjavík" (Iceland) có dấu nên so chuỗi chỉ thấy 1 kết quả; kiểm màn đích không
  bắt được lựa chọn sai vì chọn mục nào cũng về đúng màn danh sách.
- Cách sửa: so khớp bỏ dấu; danh sách ≥ 2 mục cùng loại luôn giao AI (`list_choice`).
- Test: `test_slot_pick_from_a_list_of_several_entries_is_a_choice`, `test_single_listed_entry_is_picked`.

### [L-009] Autopilot làm lại từ đầu sau khi việc đã xong
- Triệu chứng: sau khi AI chọn Nairobi, app về danh sách (màn đầu) → autopilot bấm "+" và gõ lại.
- Nguyên nhân gốc: màn kết thúc trùng màn bắt đầu; không có luật "đi tiến".
- Cách sửa: không phát lại bước đã chạy trong lần này; nhớ màn kết thúc (`end_nodes`) và báo AI
  "việc đã xong". Ban đầu chặn theo thứ tự bước trong file → sai khi workflow gộp nhiều lần
  (bỏ sót bước "Go" ở Chrome) → nay chặn theo tập bước đã chạy.
- Test: `test_autopilot_never_restarts_the_workflow_in_the_same_run`, `test_reaching_the_recorded_end_screen_says_the_task_is_done`, `test_merged_edge_order_does_not_block_a_step_that_never_ran`.

### [L-010] Vòng lặp vô hạn của AI
- Triệu chứng: Qwen gõ lại "Seoul" ~30 lần, bấm mãi cùng một điểm; phiên chạy tới hết giờ.
- Nguyên nhân gốc: Flash không giới hạn lượt; không có gì phát hiện lặp; model bỏ qua notice.
- Cách sửa: `loop_guard`: chữ ký = nội dung + trạng thái màn + thao tác + thao tác trước; lặp lần 2
  cảnh báo, lần 3 dừng `failed`. Bản đầu chặn nhầm 9/57 phiên thành công (không tính trạng thái,
  không tính bước trước, click_sequence không tính toạ độ) → còn 1/57 sau sửa.
- Test: `test_same_action_on_same_screen_warns_then_stops`, `test_a_redone_sequence_is_caught_on_its_repeated_steps`, `test_repetition_that_changes_the_screen_is_not_a_loop`, `test_fixing_a_form_then_confirming_again_is_not_a_loop`, `test_click_sequences_with_different_points_differ`, `test_loop_guard_stops_a_model_that_repeats_itself_forever`.

### [L-011] Model trả JSON gọi tool hỏng → phiên treo ở "Paused"
- Triệu chứng: Ollama 500 `invalid character '6' looking for beginning of object key string`; UI "LLM Request Failed / Paused".
- Nguyên nhân gốc: lỗi bị xếp "provider unavailable" → thử lại y hệt; temperature 0 → cùng JSON hỏng.
- Cách sửa: `FailureCategory.MALFORMED_OUTPUT` (không thử lại, không Pause); Flash hỏi lại kèm lời nhắc
  (prompt khác → câu trả lời khác); 3 lần liên tiếp → `failed`.
- Test: `test_malformed_tool_call_is_classified_not_retryable_and_not_paused`, `test_malformed_reply_is_re_asked_with_a_note_then_continues`, `test_three_malformed_replies_in_a_row_stop_the_run`.

### [L-012] Vuốt tìm khi bàn phím mở → gõ rác vào ô ("Lan" → "Lan Hu Hu Hu")
- Triệu chứng: ô Tên ở Danh bạ bị gõ thêm "Hu Hu Hu"; autopilot kẹt, không tới bước Lưu. Trace: f68c5042.
- Nguyên nhân gốc: autopilot cuộn bằng swipe mặc định (y≈70% màn) trúng vùng phím Gboard → vuốt-gõ.
  Gboard có thể không có khung gốc trong dump (chỉ các phím) nên nhận "phần tử rộng toàn màn" không thấy.
- Cách sửa: vuốt trong vùng cuộn của app, cắt phần cửa sổ khác phủ (`_scroll_swipe`); không còn dải
  an toàn → BACK ẩn bàn phím trước; nhận bàn phím bằng vùng bao các phần tử của cửa sổ khác.
- Test: `test_scroll_search_swipes_above_the_keyboard_without_hiding_it`, `test_keyboard_over_most_of_the_list_is_hidden_before_scrolling`, `test_swipe_band_stops_above_another_window`.

### [L-013] Nút Lưu chỉ chờ các ô đang thấy trên màn
- Triệu chứng: điều kiện của "Save" chỉ có `slot2, slot3` (ô Tên đã cuộn khuất).
- Nguyên nhân gốc: điều kiện "đã gõ" chỉ đọc các ô hiển thị lúc bấm. Bản sửa đầu tính cả giá trị gõ ở
  màn khác (nút "Thêm vào giỏ" đòi từ khoá ở màn tìm kiếm) → sai lần hai.
- Cách sửa: chờ mọi giá trị gõ vào ô **của cùng màn**, kể cả đã cuộn khuất; autopilot nhớ giá trị đã gõ
  và xác nhận trong lần chạy.
- Test: `test_submit_waits_for_every_field_of_its_form_even_scrolled_away`, `test_form_fields_are_filled_in_recorded_order`.

### [L-014] Chụp màn giữa hiệu ứng chuyển cảnh
- Triệu chứng: ảnh sau thao tác là hai màn chồng mờ lên nhau; AI và recorder thấy màn lẫn lộn.
- Nguyên nhân gốc: chờ cố định 400 ms; hiệu ứng bắt đầu ~0.3–0.5 s sau khi bấm.
- Cách sửa: chờ màn bắt đầu đổi (≤ 1 s) rồi chờ 2 khung liền nhau lệch ≤ 2 bit dHash (≤ 3 s) — chỉ Flash.
- Test: `test_tap_waits_for_the_transition_to_start_and_finish`, `test_unchanged_tap_gives_up_after_the_change_window`, `test_typing_only_waits_for_stillness`, `test_never_still_screen_is_capped`, `test_pro_and_sessions_without_screenshots_keep_the_fixed_delay`.

### [L-015] Bài test sau mở ra form dở dang của bài trước
- Triệu chứng: 3 bài Danh bạ sau đều bắt đầu ở form "Minht" của bài trước.
- Nguyên nhân gốc: `--locked-app` mở lại app đang chạy dở; script test không tắt app.
- Cách sửa: script test `am force-stop <package>` trước mỗi bài; Chrome mở `about:blank`.
- Test: — (quy trình test).

### [L-016] Hết giờ chỉ tắt CLI, phiên vẫn chạy ngầm trong daemon
- Triệu chứng: `timeout` giết lệnh `artemis run` nhưng tác vụ trong daemon tiếp tục điều khiển máy.
- Cách sửa: script test dừng daemon (`apps.admin_console.server`) sau mỗi bài.
- Test: — (quy trình test).

### [L-017] Chế độ `record` lần nào cũng tạo file workflow mới
- Nguyên nhân gốc: bộ chọn workflow chỉ chạy trong `maybe_autopilot`, mà hàm này thoát khi mode ≠ replay.
- Cách sửa: kết thúc phiên `record` thành công → tìm workflow cùng loại để gộp.
- Test: `test_record_mode_merges_a_repeated_task_instead_of_a_new_file`.

### [L-018] Máy ảo crash khi bật / tự tắt khi chạy lâu
- Triệu chứng: emulator thoát mã -11 khi bật; có lần tự tắt giữa đợt test (`DeviceOfflineError`).
- Cách sửa: bật bằng `-gpu swiftshader_indirect -no-snapshot-load`; chạy theo đợt nhỏ. Nguyên nhân tự tắt
  chưa xác định (nghi hết RAM).
- Lưu ý: `pgrep -f "Grab_API34"` khớp cả chính dòng lệnh đang chạy → báo nhầm "máy ảo đang chạy";
  kiểm bằng `adb devices`.

### [L-019] Hai người sửa cùng thư mục làm việc → import hỏng
- Triệu chứng: `ImportError: cannot import name 'screen_content_key'` giữa chừng.
- Nguyên nhân gốc: người code chuyển hàm sang file khác trong lúc người phản biện cũng đang sửa.
- Cách sửa: người code làm trên nhánh/worktree riêng; phản biện chỉ đọc và chạy test khi nộp.

### [L-020] Bước gõ ghi nhầm lần gõ sai; ô nhập không có id
- Triệu chứng: workflow Danh bạ giữ lần gõ "Minh" vào nhầm ô Họ; ô không có resource-id nên nhận theo
  ô lưới (đổi khi cuộn).
- Cách sửa: giữ lần gõ có giá trị còn nằm trong ô ở cuối form; nhận ô nhập bằng thuộc tính `hint`;
  bấm chỉ để chọn ô không thành bước; kiểm bước gõ đúng ô đã ghi.
- Test: `test_focus_tap_on_a_field_is_not_a_step_and_corrections_keep_the_last_typing`, `test_fields_without_ids_are_named_by_their_hint`, `test_typing_into_the_wrong_field_is_not_verified`.

### [L-021] Bấm "item matching 'Phở 24 - The Sun Avenue'" ở Grab khi AI bấm "Food" — ĐÃ SỬA (27/11)
- Triệu chứng (W0 offline): autopilot tự chọn một chi nhánh quán và bấm "Make another", "Sort By",
  "Rating" ở chỗ AI làm việc khác.
- Nguyên nhân gốc: nghi nhận nhầm màn ở app nhiều màn giống nhau; chưa phân tích từng ca.
- Nguyên nhân gốc (đã xác định): tên chi nhánh là dòng chữ `wrap_content` trong `RecyclerView` với
  `scrollable=False` (danh sách gợi ý ngắn) → không bị coi là mục danh sách → thành bước tự chọn.
- Cách sửa: dòng nằm trong vùng chứa danh sách (RecyclerView/ListView/GridView) là mục danh sách, kể cả
  khi danh sách không cuộn được. Còn "Make another" → xem L-026; "Rating" là hợp lệ (cả nhóm cùng yêu
  cầu "nhiều đánh giá nhất").
- Test: `test_l3_grab_wrap_content_branch_names_inside_recyclerview_are_list_items`.

### [L-022] Chỉ số W0 "bấm sai không phát hiện = 0" đúng theo cách dựng — ĐÃ SỬA (27/11)
- Nguyên nhân gốc: thiết bị giả giữ nguyên màn khi autopilot làm khác AI → kiểm màn đích luôn từ chối;
  bước gõ chữ tính "khớp" nếu ô của AI là ô nhập (`or is_editable(gt_node)`), kể cả gõ khác ô.
- Bằng chứng: cho thiết bị chuyển sang màn thật mà bước của AI dẫn tới → lộ 2/9 lần lệch được nhận là
  đúng ("Make another", "Rating").
- Cách sửa: bước gõ phải đúng ô (mô phỏng gõ nhầm ô); bước bấm/phím lệch chuyển thiết bị giả sang `gt.post`
  (màn thật mà bước AI dẫn tới) và đếm số bước non-aux vượt kiểm tra màn đích mà không khớp GT vào `Undet`.
- Test: `test_l1_w0_flags_typing_into_wrong_editable_field_as_mismatch`,
  `test_l1_w0_matches_actual_edited_field_when_gt_target_was_non_editable_overlay`,
  `test_l022_w0_advances_to_gt_post_on_wrong_tap_to_catch_undetected`.

### [L-023] Vuốt theo hướng bắt đầu từ điểm cố định trúng nút nổi — ĐÃ SỬA (27/11)
- Triệu chứng: AI gọi `swipe(direction="up")` ở danh sách Đồng hồ, danh sách không cuộn; AI tưởng chưa
  thêm được thành phố, làm lại → vòng lặp. Trace: 9ecd72f6.
- Nguyên nhân gốc: `utils/coordinates.py:436` vuốt hướng luôn từ `[600, 700] → [600, 300]`; điểm bắt đầu
  nằm trên nút "+" (x 367–633, y 662–812). Ảnh hưởng mọi phiên Flash, không riêng workflow.
- Cách sửa: `safe_swipe_in_norm` (targets.py) dùng chung cho executor (`compute_smart_swipe_coordinates`)
  và autopilot: vuốt trong vùng cuộn, tránh control tương tác và cửa sổ khác. Trên cây thật của 9ecd72f6:
  nút "+" ở x 367–633, y 662–812 → vuốt bắt đầu (200, 722).
- Test: `test_l4_smart_swipe_avoids_floating_fab_and_keyboard`, `test_l4_smart_swipe_avoids_keyboard_window`.

### [L-024] W4 tự mở app: có thể rời app đang mở, quyết định bằng LLM — ĐÃ SỬA (27/11)
- Nguyên nhân gốc: `workflow_hook.py` thử mở app khác khi chưa chọn được workflow ở lượt đầu (không chỉ
  ở màn hình chính) và dùng `select_workflow` có gọi LLM trên mọi app.
- Cách sửa: chỉ tự mở ở màn hình chính, chỉ chọn bằng so câu tất định (0 LLM); hỏi Android launcher
  mặc định một lần mỗi thiết bị (`cmd package resolve-activity -a android.intent.action.MAIN -c
  android.intent.category.HOME`, cache theo device) thay vì đoán chuỗi tên package.
- Test: `test_l2_w4_never_leaves_open_app_and_never_calls_llm_at_launcher`,
  `test_l024_detects_custom_home_package_via_adb_and_caches_once`.

### [L-025] W3 biến chữ cố định "Clock" thành slot — ĐÃ SỬA (27/11)
- Triệu chứng: playbook có "tap item matching 'Clock'"; khung so câu W2 bị tách tại "Clock".
- Nguyên nhân gốc: nhãn tab "Clock" xuất hiện trong câu yêu cầu ("go to the Clock tab").
- Lần sửa tạm trước dùng danh sách giới từ viết cứng ("open the", "launch", "mo app", "vao app") → trái
  AGENTS.md 2.7 và sót các cách diễn đạt khác ("trong ứng dụng Đồng hồ").
- Cách sửa: bỏ hoàn toàn từ khoá viết cứng; lấy tên app trực tiếp từ lần chạy (`manage_app(app_name=...)`,
  nhãn icon đã bấm ở màn ngoài để mở app vào foreground, và `ctx.package_cache`) rồi loại các nhãn trùng tên đó.
- Test: `test_nho_app_name_in_goal_does_not_become_tap_slot`.

### [L-026] Bước làm thay đổi giỏ hàng ("Make another") bị ghi thành bước điều hướng — ĐÃ SỬA (27/11)
- Triệu chứng (W0 + `review_w0.py`): trên tấm tuỳ chọn món của Grab (Combo 75K, số lượng 1, 111.000),
  lần ghi 88512132 bấm "Make another" (thêm một suất nữa) vì giỏ đã có món; phát lại ở phiên khác cùng
  yêu cầu "add 1 portion" → autopilot bấm "Make another" (AI thật bấm khác) và màn sau vẫn được nhận
  là đúng → thêm suất thứ hai mà không bị phát hiện.
- Nguyên nhân gốc: bước phụ thuộc TRẠNG THÁI có sẵn (giỏ đã có món), không phụ thuộc yêu cầu; recorder
  không biết, ghi thành `nav`; kiểm màn đích không phân biệt được (cùng bố cục). Hơn nữa phiên benchmark
  dừng trước lúc bấm "Đặt đơn" nên workflow có phân nhánh `judgement` dù không có cạnh `commitment_stop`.
- Cách sửa: mỗi cạnh lưu `stats.seen` (số lần chạy độc lập đã thấy cạnh đó); workflow có bước `commitment_stop`
  hoặc có phân nhánh `judgement` thì các bước `nav` khởi đầu ở trạng thái `provisional` và chỉ tự chạy
  (`active`) sau khi đã gặp ở `>= 2` lần chạy độc lập. Bước đặc thù một lần như "Make another" giữ `seen=1`
  nên autopilot luôn giao AI. W0 với giả lập `gt.post` cho `Grab: Mis=1, Undet=0`, toàn bộ DB `Undet=0`.
- Test: `test_l026_commitment_workflow_needs_two_runs_for_nav_steps`.

### [L-027] Trạng thái tồn đọng lặp qua nhiều lần chạy lọt luật "≥ 2 lần" — ĐÃ SỬA (27/11)
- Triệu chứng (W0 + `review_w0.py`, 27/11): autopilot bấm ô số lượng "2" (`tv_quantity`) trên tấm tuỳ
  chọn món Grab ở phiên mà AI bấm ra ngoài; kiểm màn đích có bắt được, nhưng cú bấm vào ô số lượng đã
  được gửi tới máy trước khi bị phát hiện.
- Nguyên nhân gốc: hai lần ghi (a99efb27, f58deac1) đều gặp cùng giỏ hàng tồn đọng 2 suất của tài
  khoản test → bước phụ thuộc trạng thái được thấy 2 lần độc lập nên thành `active` (L-026 không đủ).
- Hướng sửa đề xuất: (1) phần tử định danh bằng nhãn chỉ có số (số lượng, giá, đếm) là dữ liệu, không
  phải điều hướng → không bao giờ tự bấm (thành `judgement`); (2) khi test luồng mua hàng, dọn giỏ
  trước mỗi lần chạy (quy trình test, xem L-015).
- Cách sửa: `targets.is_data_label` (nhãn chỉ gồm chữ số + dấu / ký hiệu / khoảng trắng, theo loại
  ký tự Unicode — không danh sách tiền tệ / chữ): recorder ghi bước bấm vào nhãn như vậy thành
  `judgement`; `resolve` từ chối tự bấm (`data_label`) kể cả với workflow cũ đã lưu bước đó.
  Ô nhập không bị ảnh hưởng (gõ số điện thoại vẫn tự động).
- Test: `test_l027_numeric_labels_are_data_not_navigation`, `test_l027_tap_on_a_quantity_is_never_replayed`.

### [L-028] UI không phân biệt bước workflow đã xác nhận với bước AI phải tiếp quản — ĐÃ SỬA (27/11)
- Triệu chứng: mọi bước autopilot đều hiện "Workflow · Không hỏi AI", kể cả bước bấm xong mà màn sau
  không như lúc ghi (autopilot dừng, AI làm tiếp) → người xem tưởng workflow chạy đúng.
- Nguyên nhân gốc: bước được ghi vào DataEngine khi thực hiện, còn kết quả kiểm màn đích có sau đó;
  bước được ghi ở luồng nền không theo thứ tự nên không cập nhật ngay được.
- Cách sửa: autopilot báo kết quả kiểm màn qua `driver.note_verified`; hook gộp
  `extra_metadata.workflow_verified` vào đúng bước bằng `DataEngine.merge_step_extra_metadata`
  (thử lại tới ~3 s nếu bước chưa được ghi, phát `step_updated`); UI hiện "Workflow · Chưa xác nhận —
  AI tiếp quản" (màu vàng) khi `false`, icon đã xác nhận khi `true`; bảng thời gian cũng vậy.
- Test: `test_workflow_verified_is_reported_for_each_replayed_step`,
  `test_merge_step_extra_metadata_waits_for_a_step_written_later`; `ng build` không lỗi.

### [L-029] Workflow học từ một lần ghi "thành công" giả → phát lại ghi đè dữ liệu, kiểm vẫn báo đúng — ĐÃ SỬA (27/11)
- Triệu chứng (Gemini, 27/11): phát lại Danh bạ "Lan / Pham / 0912345678" lưu ra liên hệ chỉ có "Lan",
  không họ, không số; cả 5 bước autopilot `workflow_verified = True`. Trace phát lại 9d3661f4, lần ghi c403e23a.
- Nguyên nhân gốc:
  1. Lần ghi: sau khi gõ ô đầu, bàn phím bật + form tự cuộn, cây UI chỉ còn ô "First name"; AI gõ "Tran"
     và số điện thoại theo vị trí từ danh sách cũ (y≈716, trong vùng bàn phím) → executor bấm rồi gõ vào
     ô đang focus (First name, `clear_exist`) → ghi đè. AI vẫn báo `completed` "Minh Tran 0901234567".
  2. Recorder tin `completed`: ghi cả 3 bước gõ với đích hint "First name".
  3. Autopilot phát lại đúng workflow sai; kiểm "giá trị nằm trong ô đã ghi" dùng cùng mô tả ô → luôn đúng.
- Bài học: kiểm sau bước chỉ chứng minh "làm giống lúc ghi", không chứng minh lúc ghi đúng. Một model tự
  ghi phải có kiểm tính nhất quán của chính lần ghi (bài học chung mới, mục 1).
- Hướng sửa đề xuất (3 lớp):
  a. Executor (mọi phiên Flash): sau `input_text` có đích, nếu ô nhận chữ (ô focus) không phải ô đích →
     báo lỗi cho AI ("đã gõ vào ô khác"), không im lặng.
  b. Recorder: giá trị của một slot bị ghi đè trước khi gửi form (không còn trong ô ở màn cuối của form),
     hoặc nhiều slot khác nhau gõ vào cùng một ô → lần ghi không nhất quán → không tạo bước gõ / không ghi
     workflow (đã có `_value_persisted`, cần dùng để LOẠI cả lần ghi chứ không chỉ chọn lần gõ).
  c. Autopilot: không bao giờ gõ vào ô đang chứa giá trị mà chính autopilot đã gõ cho slot khác trong lần chạy.
- Cách sửa (đã làm, 3 lớp):
  a. Executor (Flash, `action_executor.py`): TRƯỚC khi gõ (kể cả có chỉ định hay không chỉ định điểm), kiểm
     ô đang focus (`_input_would_go_elsewhere`): nếu ô đang giữ một giá trị (`len >= 2`) chính lần chạy đã
     gõ ở bước trước (khớp nguyên ô hoặc nguyên từ qua boundary, tránh chuỗi con quá ngắn kiểu `"1"` khớp
     vào URL) → từ chối. Quét toàn bộ 134 bước `input_text` có cây UI trong DB: chặn đúng 4 lần ghi đè
     (`4809e63a` bước 5 và bước 8, `c403e23a` bước 4 và bước 5), 0 lần chặn nhầm.
  b. Recorder (`inconsistent_typing` & `record_run`): kiểm từng slot ở màn trước lúc gửi form; nếu ô đã
     cuộn khuất khi bấm gửi, lùi ngược về màn gần nhất còn thấy ô đó (trước khi bấm gửi), đồng thời cấm
     hai slot khác nhau dùng chung một ô (`(submit_idx, key)`). Vi phạm → trả `None` (từ chối cả khi gộp
     vào workflow đã có). Sửa sai rồi gõ lại đúng ô vẫn hợp lệ. `FlashWorkflowHook.finish()` chỉ tăng
     `runs` / `ok_runs` sau khi `record_run()` chấp nhận lần chạy.
  c. Autopilot: không gõ vào ô đang chứa giá trị nó đã gõ cho slot khác trong lần chạy.
- Test: `test_l029_recorder_rejects_a_run_that_overwrote_its_own_typing`,
  `test_l029_executor_refuses_typing_over_this_runs_own_value`,
  `test_l029_untargeted_typing_into_a_field_holding_this_runs_value_is_refused`,
  `test_l029_autopilot_never_types_over_a_value_it_typed_for_another_field`,
  `test_l029_executor_short_and_whole_word_matching`,
  `test_l029_recorder_rejects_overwrite_then_scrolled_away_and_accepts_correction`,
  `test_ok_runs_unchanged_when_rejected_by_inconsistent_typing`.

### [L-030] Bản ghi bước vuốt lưu toạ độ mặc định, không phải toạ độ đã gửi — ĐÃ SỬA (27/11)
- Triệu chứng: UI vẽ cú vuốt ở `[600,700 → 600,300]`, đúng giữa nút "+" của Đồng hồ, nên trông như
  vuốt trúng icon (trace 40659ee6); thực tế lệnh gửi xuống máy là `input swipe 144 924 144 325`
  (thang 0–1000: bắt đầu (200, 722), ngoài nút "+" x 367–633 và thanh tab y ≥ 838), danh sách có cuộn.
- Nguyên nhân gốc: `_translate_swipe` (vuốt theo hướng) trả ngữ nghĩa rỗng `{}`; runner tự điền toạ độ
  mặc định của hướng vào bản ghi.
- Cách sửa: executor trả toạ độ vuốt thật (`coordinates`, `normalized_coordinates`,
  `normalized_start/end_coordinates`), runner gộp vào bản ghi của bước.
- Test: `test_directional_swipe_needs_no_description` (toạ độ ghi = toạ độ gửi),
  `test_swipe_step_records_the_coordinates_actually_sent`.

### [L-031] Trạng thái đầu và trạng thái đã cuộn của cùng một form là hai màn khác nhau — ĐÃ SỬA (27/11)
- Triệu chứng (Gemini, Danh bạ, 27/11, trace 99cd3b7b): bước "Create contact" lần nào cũng
  "screen is not the recorded next screen", 2 lần → `disabled`; mất một bước tự động mỗi lần chạy.
- Nguyên nhân gốc: lúc ghi, màn sau "Create contact" được lưu ở trạng thái bàn phím đã bật hoặc form đã
  cuộn (ví dụ sau `input_text` Phone của 99cd3b7b độ giống chỉ còn 0.263); lúc phát lại form mở ở
  trạng thái đầu (`First name`, `Last name`). Độ giống dưới ngưỡng `tau=0.80` nên bị tách hai node; khi
  hợp nhất trạng thái đầu/cuộn cũng cần tránh ghi trùng vân tay sang node khác gây `ambiguous_margin`
  hoặc áp nhầm lên ô tìm kiếm chỉ có 1 trường.
- Cách sửa:
  - `targets.py` thêm `editable_field_keys` và `field_target_matches` (so khớp theo `(resource-id, hint)`).
  - `recorder.py`: `_same_form_screens` gộp các trạng thái của form nhiều trường (`>= 2` trường phân biệt)
    qua gõ phím, cuộn và `BACK` ẩn bàn phím; `_form_node_for_dest` hợp nhất node đích của bước điều hướng
    vào form khi có ô của bước gõ tiếp theo; chỉ thêm vân tay khi không trùng ngưỡng với node khác
    (`_conflicts_with_other_node`), giữ nguyên `tau=0.80`.
  - `autopilot.py`: `_verified` chấp nhận đích là form nhiều trường khi có neo và ô của bước gõ kế tiếp;
    `_match_live` / `_shares_form_fields` giữ đúng node form qua các bước tự cuộn/bàn phím.
- Test: `test_l031_99cd3b7b_scrolled_and_initial_form_share_node_and_verify`.

### [L-032] Autopilot không cuộn tìm ô nhập bị bàn phím che khi màn còn bước khác đang chờ — ĐÃ SỬA (27/11)
- Triệu chứng (trace 99cd3b7b, eaa9055c): sau khi gõ ô đầu, autopilot dừng "target not_found" với ô Họ /
  Số điện thoại (bị bàn phím che, không có trong cây UI) → AI phải làm tiếp từng ô.
- Nguyên nhân gốc: `_pick` chỉ trả `"recorded target not found"` để kích hoạt cuộn tìm khi MỌI lý do đều
  là `not_found`; màn còn bước "Save" với lý do `"waits for ... to be typed"` → điều kiện sai → dừng
  luôn. Ngoài ra `_labels` không tính `hint` của ô trống nên cuộn làm lộ ô mới dễ bị coi là không đổi,
  và vòng cuộn thoát nếu màn sau cuộn không còn khớp `tau=0.80`.
- Cách sửa: `_pick` bỏ qua các lý do `"waits for ..."` khi xét kích hoạt cuộn tìm (vẫn giữ thông báo
  `"waits for ..."` nếu không có lý do chặn nào khác); `_labels` tính thêm `hint` của ô hiển thị;
  vòng cuộn tiếp tục trong dải an toàn trên bàn phím (qua `safe_swipe_in_norm` hoặc `BACK` ẩn bàn phím)
  và giữ nguyên node form nếu `_shares_form_fields` còn đúng.
- Test: `test_l032_scrolls_above_keyboard_or_hides_it_to_fill_three_field_form`.

### [L-033] W0 đếm "bấm sai không phát hiện" cả khi dữ liệu chuẩn bị sai hoặc chỉ khác thứ tự ô — ĐÃ SỬA (27/11)
- Triệu chứng (27/11): Danh bạ Undet = 3 sau khi sửa L-031/L-032. Phân tích từng ca (script `review_w0.py`):
  - c403e23a, 9d3661f4: lần chạy THẬT là các phiên đã lưu sai dữ liệu (L-029); autopilot gõ đúng giá trị
    nên lệch với lần chạy thật → bị đếm là lỗi.
  - eac2e233: autopilot gõ số điện thoại vào đúng ô Phone TRƯỚC ô Họ (AI làm ngược thứ tự) → khác thứ tự,
    không sai (trước đó `_same_node` cũng chưa so `hint` khi ô `Phone` bị cuộn lệch toạ độ).
  - Kịch bản xấu (chuyển sang màn thật của bước AI): 0/8 lần lệch được autopilot nhận là đúng.
- Cách sửa (`scripts/workflow_replay_eval.py`):
  1. `evaluate_db` gọi `inconsistent_typing` trước khi chấm từng phiên; loại khỏi dữ liệu chuẩn các phiên
     bản thân lần chạy đã ghi đè dữ liệu (`c403e23a`, `9d3661f4`, `4809e63a`).
  2. `_OfflineTraceDriver._find_unordered_form_match` cho phép khớp các bước gõ trong cùng một form (trước
     bước gửi/điều hướng kế tiếp) không theo thứ tự miễn đúng giá trị và đúng ô; `_same_node` so thêm
     `hint` cho `EditText` không có `resource-id` khi ô dịch vị trí do cuộn. Sau sửa: W0 `Undet = 0` trên
     toàn bộ các app (`Contacts` 26/50 = 52.0%, `Total Undet = 0`).
- Test: `test_l033_w0_matches_unordered_form_typing_and_flags_wrong_field`.

### [L-034] Autopilot có thể gõ đè dữ liệu CÓ SẴN khi một form khác dùng cùng tên ô — ĐÃ SỬA (27/11)
- Rủi ro (phát hiện khi soát L-031, chưa xảy ra trên máy): L-031 coi hai màn có ≥ 2 ô trùng (id / hint)
  là cùng một form. Form "Sửa liên hệ" có cùng các ô "First name / Last name / Phone" với form "Tạo liên
  hệ" → workflow "tạo liên hệ" phát lại trên màn sửa sẽ gõ đè tên / số của một liên hệ có sẵn rồi bấm Lưu.
  Chốt L-029 chỉ bảo vệ giá trị chính lần chạy đã gõ, không bảo vệ dữ liệu có sẵn.
- Cách sửa (quy tắc chung, AGENTS.md 2.8 trạng thái có sẵn):
  - `targets.py`: `field_has_value(node, target_hint)` phân biệt ô đang chứa nội dung thật với ô trống hoặc
    chỉ hiển thị chữ gợi ý (`text == hint`).
  - `store.py` & `recorder.py`: `Edge.empty_on_record` (lưu JSON khi `true`; khi gộp nhiều lần chạy giữ
    `existing.empty_on_record and empty_on_record`) ghi nhận ô có trống lúc ghi hay không.
  - `autopilot.py`: trong `_pick`, nếu bất kỳ bước gõ nào có `empty_on_record=True` mà ô live hiện tại
    đã có nội dung (`field_has_value` đúng) thì không gõ bước nào trên form và bàn giao AI ngay
    (`"field already holds existing data (recorded when empty): handing over to AI"`).
- Test: `test_l034_create_contact_workflow_does_not_overwrite_prefilled_edit_contact`.

### [L-035] Autopilot cuộn tìm ô không chạy được trên máy thật — ĐÃ SỬA (27/11)
- Triệu chứng (Gemini, Danh bạ "Trang Mai", trace ada566df, 27/11): bước 4 autopilot vuốt → executor báo
  `Error during swipe: 'target_description' is required`; autopilot dừng giữa form, AI làm tiếp (và làm sai, L-037).
- Nguyên nhân gốc: `_scroll_swipe` gửi `swipe(coordinates=[...])`; nhánh vuốt theo toạ độ của executor
  bắt buộc `target_description`. Unit test dùng thiết bị giả nên không đi qua kiểm tra tham số của executor.
- Cách sửa: bổ sung `"target_description": "autopilot: scroll to find the recorded field"` vào lệnh `swipe`
  trong `autopilot.py` (mọi lệnh còn lại — `click`, `input_text`, `press_key`, `manage_app` — đều đã qua
  được `McpActionExecutor._translate`).
- Test: `test_l035_autopilot_commands_pass_real_executor_translation_contract` (test hợp đồng qua
  `McpActionExecutor._translate` thật).

### [L-036] Autopilot bấm Lưu khi một giá trị nằm SAI ô — ĐÃ SỬA (27/11)
- Triệu chứng (ada566df bước 6): ô Last name chứa "Mai\n0907770007", ô Phone trống → autopilot vẫn bấm
  Save → lưu "Trang Mai\n0907770007", không có số điện thoại; `workflow_verified=false` nhưng đã lưu.
- Nguyên nhân gốc: điều kiện "đã gõ đủ" (`typed_slots`) coi một giá trị là đã gõ nếu nó nằm trong BẤT KỲ ô
  nhập nào, không kiểm ĐÚNG ô của giá trị đó.
- Cách sửa (`autopilot.py`): thêm `_slot_in_own_field` / `_held_slots` thay cho `typed_slots` đại trà trong
  `_pick`; từng slot chỉ được tính là đã có khi chính autopilot đã gõ & xác nhận trong lần chạy
  (`_typed_ok`), hoặc đúng ô đã ghi của slot đó (theo `resource-id` / `hint`, kể cả khi đang bị bàn phím
  che) hiển thị trên màn hình và chứa giá trị của slot; đồng thời `_verified` với trường đã định danh không
  chấp nhận rơi sang ô khác nếu không thấy đúng ô.
- Test: `test_l036_save_refuses_when_phone_value_sits_in_last_name_field`.

### [L-037] AI gõ giá trị của ô khác vào ô đang chứa giá trị chính lần chạy đã gõ — ĐÃ SỬA (27/11)
- Triệu chứng (ada566df bước 5): AI `input_text(target=<ô Last name chứa "Mai">, text="\n0907770007")` →
  "Mai\n0907770007". Chốt L-029 không chặn vì điểm gõ nằm TRÊN một ô thật.
- Cách sửa (`action_executor.py`): thêm `_input_overwrites_typed_field` (dùng chung `_matching_typed_value`
  với L-029) trong nhánh `flash` của `input_text`: nếu điểm đích nằm trên một ô nhập đang giữ giá trị `V`
  (`len >= 2`, khớp nguyên ô / nguyên từ) do chính lần chạy đã gõ, mà chữ mới không chứa / không nằm trong
  `V` và tỉ lệ giống `SequenceMatcher` < 0.5 → từ chối với thông báo rõ giá trị hiện tại và yêu cầu dùng
  đúng index ô khác. Sửa chính tả hoặc mở rộng/rút gọn cùng giá trị vẫn cho phép. Quét toàn bộ DB: chặn
  đúng `6/6` lần ghi đè thật (`1fe20ffa` bước 3 & 5, `810e3cf3` bước 4, `9d3661f4` bước 3 & 4, `ada566df`
  bước 5), `0` lần chặn nhầm.
- Test: `test_l037_executor_refuses_unrelated_overwrite_on_targeted_field_but_allows_typo_fix`.

### [L-038] Dữ liệu test tích luỹ qua các đợt làm AI loay hoay — ĐANG MỞ (quy trình test)
- Triệu chứng (27/11): danh sách đồng hồ thế giới và danh bạ trên máy ảo dài dần qua các đợt; thành phố
  mới thêm nằm cuối danh sách dài → AI vuốt nhiều lần, không thấy, thêm lại (Budapest: 17 lượt AI so với
  10 khi đối chứng). Kết quả đo bị nhiễu bởi trạng thái máy, không phải bởi workflow.
- Việc cần làm: đầu mỗi đợt test đưa app về trạng thái sạch giống nhau (xoá các thành phố / liên hệ do test
  tạo, hoặc `pm clear` app test trên máy ảo test — cần người dùng đồng ý vì xoá hết dữ liệu của app đó).

### [L-039] Autopilot báo "việc gần như đã xong" nhưng không tự kiểm; AI làm lại luồng — ĐÃ SỬA (27/11)
- Triệu chứng (Gemini, Đồng hồ "Budapest", trace 1f14e556): tới màn kết thúc autopilot nhắn "the task is
  most likely done"; AI không thấy Budapest (khuất cuối danh sách), bấm "+" thêm lại.
- Cách sửa (mọi app):
  1. `autopilot.py`: khi tới `end_nodes`, `_verify_end_screen` kiểm tra các giá trị slot của workflow trên
     màn kết thúc (tự cuộn an toàn tới khi thấy hết, chạm đáy không đổi hoặc đạt `MAX_SCROLL_SEARCH`);
     `AutopilotReport.notice` báo sự thật đã kiểm (`End-screen check CONFIRMED: verified 'Budapest'...` hoặc
     `did NOT see ...`), không phỏng đoán `"most likely done"`.
  2. `workflow_hook.py` & `action_executor.py`: `arm_workflow_end_guard` nạp bước đầu/lặp lại của workflow
     vào executor sau khi chạm màn kết thúc; nếu AI vẫn bấm lại bước đó thì `_check_workflow_end_guard`
     từ chối **một lần** kèm kết quả kiểm ở (1) (nếu AI thật sự bấm lại lần nữa thì vẫn cho phép).
- Test: `test_l039_autopilot_verifies_end_screen_by_scrolling_and_executor_blocks_entry_once`.

### [L-040] Danh sách phần tử gửi AI bỏ mất trạng thái "đã chọn" → AI bấm vào dòng đã chọn, bỏ chọn nó — ĐÃ SỬA (27/11)
- Triệu chứng (1f14e556 bước 11): ở màn tìm thành phố, dòng "Budapest, Hungary" có dấu ✓ (đã thêm), cây UI
  `selected=true` cho cả dòng; danh sách gửi AI chỉ có `[4] "Budapest, Hungary"` → AI bấm → app BỎ CHỌN (xoá
  thành phố). Lần thêm đầu đã thành công, chính lần "thêm lại" này xoá nó.
- Nguyên nhân gốc: định dạng list gọn chỉ in `checked:` khi "cần", không in `selected`.
- Cách sửa (tầng quan sát & prompt, mọi phiên Flash):
  - `visualization.py`: `generate_minimal_list_and_elements` in ` (selected)` cho mọi phần tử có
    `selected=true` hoặc nằm gọn trong dòng chứa không nhãn (`<= 25%` màn hình, không phải container cuộn)
    có `selected=true` / `checked=true`; đồng thời gắn `element["selected"] = True`.
  - `flash_runner.md`: hướng dẫn rõ mục mang `(selected)` hoặc `checked:true` là đã chọn/đã thêm, bấm lại
    thường sẽ bỏ chọn/xoá.
- Test: `test_l040_compact_list_shows_selected_on_item_and_enclosing_row`.

### [L-041] Luật "vuốt tối đa 3 lần" quá ít với danh sách dài — ĐÃ SỬA (27/11)
- Triệu chứng (1f14e556 lượt 2–6): AI vuốt 4 lần, dừng khi Budapest còn ngay dưới Warsaw (khuất sau thanh tab)
  → tưởng chưa thêm, làm lại.
- Nguyên nhân gốc: quy tắc prompt "at most 3 swipes" (thêm ở phiên trước) và `MAX_SCROLL_SEARCH = 3` của
  autopilot: dừng theo SỐ LẦN thay vì theo TÍN HIỆU hết danh sách.
- Cách sửa: điều kiện dừng chính là vuốt mà màn không đổi (tới cuối danh sách); nâng trần chống lặp vô hạn
  lên `10` trong cả `flash_runner.md` và `autopilot.py` (`MAX_SCROLL_SEARCH = 10`). Với feed tải thêm
  vô hạn (gợi ý / kết quả không liên quan), `flash_runner.md` ưu tiên Search nếu có thanh tìm kiếm và
  chỉ cuộn tối đa 4–5 lần khi không có tín hiệu khớp thay vì đợi chạm trần 10 lần.
- Test: `test_l041_autopilot_scrolls_beyond_three_until_end_or_found`.

### [L-042] Kiểm màn kết thúc (L-039) không chạy khi AI quay về màn danh sách — ĐÃ SỬA (27/11)
- Triệu chứng (Gemini, Đồng hồ "Kyiv", trace 69f6a853): autopilot làm 3 bước, AI chọn "Kyiv, Ukraine" →
  về màn danh sách; autopilot chỉ báo "các bước của màn này đã chạy rồi", không kiểm gì; AI vuốt 4 lần,
  bấm "+" gõ lại Kyiv (8 lượt AI, bằng bản tắt workflow 9 lượt). Dữ liệu cuối vẫn đúng (may: lần 2 không bấm dòng đã chọn).
- Nguyên nhân gốc: (1) phiên ghi kết thúc ở màn TÌM KIẾM (AI báo xong ngay sau khi gõ) nên `end_nodes` =
  màn tìm kiếm, còn màn danh sách là `start_node`; (2) kiểm L-039 và chốt chặn executor chỉ bật khi khớp `end_nodes`.
- Cách sửa (`autopilot.py`, mọi app): thêm `_all_slots_completed` (mọi slot hoạt động của workflow đã có
  bước hoàn thành trong `_done`); trong `_pick`, khi gặp màn thuộc workflow không còn bước chạy tiếp (`not edges`
  hoặc `all(e.id in _done)` — không có `commitment_stop` hay `provisional`) mà `_all_slots_completed()` đúng,
  coi luồng đã xong (`_finished = True`) để `Autopilot.run()` chạy `_verify_end_screen` và `FlashWorkflowHook`
  nạp chốt chặn bước đầu `arm_workflow_end_guard` như khi chạm `end_nodes`.
- Test: `test_l042_verifies_on_revisit_when_slots_done_even_if_end_node_is_search_screen`.

### [L-043] Gõ nhiều ô trong một chuỗi: ô sau bị bàn phím che → bị từ chối, tốn lượt — ĐÃ SỬA (27/11)
- Triệu chứng (Gemini, Danh bạ tắt workflow, trace 2051ab38): gõ "Son" xong, bàn phím bật + form cuộn, trên màn chỉ
  còn ô First name; `input_text(target=12)` (Last name, toạ độ cũ nằm dưới bàn phím) bị chốt L-029 từ chối ĐÚNG.
  Tương tự ô Phone. AI mất thêm lượt ẩn bàn phím / bấm Next (15 lượt so với 10).
- Cách sửa (`visualization.py`, `action_executor.py`, `runner.py`, mọi app có form), trước khi từ chối `input_text(target=<index>)`:
  (1) `_intended_field_descriptor`: lấy định danh phần tử trong `index_elements` cũ — chỉ tin `resource_id` nếu xuất hiện
      duy nhất 1 lần trong danh sách cũ, nếu trùng nhiều ô thì dùng `hint` (gắn kèm từ `format_minimal_list_with_elements`) hoặc `text`;
  (2) `_recover_keyboard_hidden_field`: tìm trên màn live đúng 1 `EditText` cùng định danh và không bị che (`!is_occluded`,
      `!covered_by_other_window`, `!keyboard_covers`) → gõ thẳng vào tâm mới;
  (3) nếu chưa thấy và có bàn phím (`keyboard_box`), gọi `press_key(BACK)` đúng 1 lần (không bao giờ tự vuốt), `observe()` lại,
      lặp bước (2); vẫn không thấy duy nhất 1 ô khớp → từ chối (gửi kèm màn mới đã tắt bàn phím);
  (4) vẫn giữ nguyên chốt L-037 trước khi gõ; nếu đã bấm `BACK`, đặt `recovered_screen_changed=True` để `runner._process_tool_calls`
      bật `has_waited=True` chặn thao tác index/toạ độ cũ còn lại trong chuỗi (`AGENTS 2.4`), và truyền `recovered_pre_observation`
      (màn SAU `BACK`) làm `wf_pre` cho `wf_hook.after_action`.
- Test: `test_l043_executor_recovers_keyboard_hidden_field_via_back_and_unique_hint`, `test_l043_burst_stops_and_recorder_gets_post_back_pre_screen`.

### [L-044] Kiểm màn kết thúc có thể "CONFIRMED" sai khi giá trị chỉ nằm trong ô nhập chưa lưu — ĐÃ SỬA (27/11)
- Nguyên nhân gốc: `_values_on_screen` (`autopilot.py`) đếm cả chữ trong ô nhập (`EditText`). L-042 cho `_finished=True`
  cả ở nhánh "màn khớp workflow nhưng không còn bước chạy tiếp" khi các slot đã gõ xong. Nếu lúc đó màn vẫn là form
  chưa bấm Lưu (vd node chỉ còn cạnh bị disabled/provisional — Danh bạ có 1 cạnh nav disabled), giá trị đang nằm
  trong ô nhập → báo "CONFIRMED" + nạp chốt chặn bước đầu → AI có thể dừng khi chưa lưu.
- Cách sửa (`autopilot.py`): (1) `_values_on_screen` lọc `not is_editable(n)`, bỏ qua mọi ô nhập khi tự kiểm L-039;
  (2) thêm `_form_still_open(norm)` (`bool(typed_slots(norm, self.slot_values))`) và yêu cầu `not self._form_still_open(norm)`
  ở cả hai nhánh `_finished=True` của L-042 (`not edges` và `all(e.id in _done for e in edges)`).
- Test: `test_l044_unsaved_form_with_typed_fields_does_not_confirm_completion`.

### [L-045] Ô nhập có sẵn tiền tố ("+1") → lưu sai "+10916600066"; autopilot cũng sẽ coi là đúng — ĐÃ SỬA (27/11)
- Triệu chứng (Gemini, Danh bạ bản 4.92, trace b68c6f75 bước 5): ô Phone có sẵn "+1" (cây UI: EditText text='+1');
  AI gọi `input_text(..., clear_exist=False)` → ô thành "+10916600066", AI báo xong, liên hệ lưu sai số.
- Nguyên nhân gốc: (1) AI tự đặt `clear_exist=False` trên ô có nội dung KHÔNG phải do lần chạy này gõ, không kiểm lại
  giá trị ô sau khi gõ (vi phạm AGENTS 2.8); (2) autopilot `_verified` / `_held_slots` kiểm `v in field_text`
  (chứa), nên "+10916600066" vẫn được coi là đã gõ đúng "0916600066" — kiểm sau gõ không bắt được lỗi này.
  (Autopilot tự gõ thì đúng vì gửi mặc định clear_exist=True — chỉ phần kiểm bị lỏng.)
- Cách sửa (`targets.py`, `autopilot.py`, `action_executor.py`, mọi app):
  (a) `normalize_field_value` bỏ khoảng trắng và `- ( ) .` sau khi gấp dấu/chữ thường; `_verified`, `_slot_in_own_field`
  và `_held_slots` so khớp đúng (`==`) thay cho kiểm chứa; khi lệch sau bước gõ, `Autopilot.run` dừng và báo rõ
  `after '...' field holds '...', not '...'`; giữ `typed_slots` ở L-044 (`_form_still_open`) để form đang giữ giá trị
  lệch chưa lưu vẫn bị coi là form đang mở.
  (b) `McpActionExecutor._typed_field_mismatch`: sau `input_text` có quan sát (`not skip_observe`), nếu giá trị thực
  sau chuẩn hoá khác chữ vừa yêu cầu thì nối cảnh báo `Warning: field now holds '...', not '...'.` vào `text_summary`
  và ghi `metadata["typed_value_mismatch"]`.
- Test: `test_l045_prefix_remainder_rejected_by_autopilot_and_warned_by_executor`.

### [L-046] Workflow không biết app đã cập nhật; màn/bước cũ không bao giờ được dọn — ĐÃ SỬA (27/11)
- Hiện trạng (Danh bạ tự cập nhật lên 4.92 ngày 27/11): workflow cũ không khớp (similarity 0.03–0.27) → an toàn,
  AI làm và nhánh mới được gộp vào (11 màn). Nhưng: (1) không lưu `versionName`, nên bước nav cũ vẫn "active" nếu một
  màn cũ tình cờ còn khớp; (2) node/edge cũ không bao giờ bị xoá, file phình dần.
- Cách sửa (`store.py`, `recorder.py`, `workflow_hook.py`):
  (1) `Workflow.app_version` lưu `versionName` lấy bằng `dumpsys package <pkg>` (cache theo thiết bị + package).
  Khi đổi version (`Workflow.apply_app_version`), mọi edge `nav` đang `active` hạ về `provisional` và reset `seen = 0`
  (giữ nguyên edge `slot`); ở `record_run`, mọi edge `nav` `provisional` được thăng lại `active` khi `seen >= 2`
  trong bất kỳ workflow nào (không chỉ workflow có `commitment`/`judgement`).
  (2) `Node.miss_count` đếm số lượt chạy thành công liên tiếp không khớp màn đó (tính cả các màn đi qua bởi bước
  `autopilot`, tránh xoá oan màn chạy tự động). Khi `miss_count > 10` (`Workflow.record_node_matches_and_prune`),
  xoá node cùng mọi edge gắn vào nó (`src` hoặc `dst`), giữ nguyên `start_nodes` và `end_nodes`.
- Test: `test_l046_app_version_change_downgrades_nav_and_prunes_stale_nodes`.

### [L-047] Ô "Họ tên" gộp nhiều giá trị của yêu cầu luôn bị coi là judgement → lần nào cũng hỏi AI — ĐÃ SỬA (27/11)
- Hiện trạng (Danh bạ 4.92, trace c8e82b72): form mới có 1 ô Name; AI gõ "Nam Kha" (= first + last name).
  Recorder không tìm thấy "Nam Kha" nguyên văn trong yêu cầu → edge `judgement` → autopilot dừng, AI làm (tốn 2–3 lượt).
- Cách sửa (`targets.py`, `recorder.py`, `autopilot.py`, `playbook.py`):
  - `recorder._slot_for_text(..., goal=...)`: khi chữ gõ không khớp 1 slot đơn nhưng bằng đúng ≥ 2 slot của yêu cầu nối
    bằng đúng `" "` theo thứ tự xuất hiện trong `goal`, ghi thành slot ghép `slot1+slot2` (không đoán hoán vị hay ký tự nối khác).
  - `slot_parts` + `resolve_slot_value` (`targets.py`) dùng chung trong `autopilot.py` (`_pick`, `_held_slots`,
    `_slot_in_own_field`, `_all_slots_completed`, `_expected_end_values`, kiểm đụng hàng L-029), `recorder.py`
    (`inconsistent_typing`, deduplicate gõ lặp) và `playbook.py` (`format_step`).
- Test: `test_l047_composite_slot_recorded_and_replayed_in_goal_order`.

### [L-048] Cho phép prompt can thiệp vào workflow khi có yêu cầu khác mẫu & tắt workflow theo từng task — ĐÃ SỬA (27/11)
- Vấn đề: Autopilot chạy trước mỗi turn và không đọc prompt (ngoài rút slot), nên nếu prompt có lưu ý/yêu cầu mới ngoài khung đã ghi (vd `"save to SIM"`, `"remove +1"`, `"don't..."`) mà bước cũ đã `active`, autopilot vẫn chạy theo khuôn mẫu cũ. Đồng thời chưa có cách tắt workflow riêng cho 1 task qua `mobile_run_task` / CLI mà không sửa `config/artemis.jsonc`.
- Cách sửa:
  (1) Can thiệp khi prompt lệch khung (`selector.goal_deviation_summary`, `workflow_hook.maybe_autopilot`, `flash/runner.py`): khi prompt mới không khớp 100% khung tĩnh sau khi che slot (`match_goal_template is None`), autopilot **tạm dừng không bấm máy**, gửi notice `[Workflow Review]` kèm phần chỉ dẫn sai lệch và danh sách bước dự kiến (`Planned workflow steps:`). AI có thể gọi `workflow_autopilot(decision='proceed')` để duyệt cho autopilot chạy tiếp, hoặc thao tác trực tiếp trên màn hình / gọi `workflow_autopilot(decision='manual')` để khoá autopilot và tự làm toàn bộ lượt còn lại.
  (2) Ghi đè workflow theo từng task: thêm tham số `workflow: "off" | "record" | "replay"` trên `mobile_run_task` (`mcp_server/tools/task_runner.py`), `--workflow` trên CLI (`cli/commands/run.py`, `mcp_server/background/task_runner.py`), REST payload `/api/run` (`daemon_client.py`, `task_schema.py`, `task_queue_service.py`) và `ctx.workflow_mode` trong `resolve_mode`.
- Test: `test_l048_prompt_deviation_pauses_autopilot_for_ai_confirmation_and_supports_manual_or_proceed`, `test_l048_per_task_workflow_override_in_mcp_and_resolve_mode`.

### [L-049] Kiểm giá trị sau gõ (L-045) sai với ô mật khẩu và bỏ qua mất dấu tiếng Việt — ĐÃ SỬA (27/11)
- Nguyên nhân gốc: `normalize_field_value` dùng `fold()` (bỏ dấu + chữ thường) và không xét thuộc tính `password`.
  (1) Ô mật khẩu: cây UI có `password=true`, text là ký tự che ("••••") hoặc rỗng → executor cảnh báo sai
  "field now holds '••••', not '<mật khẩu>'" (AI dễ gõ lại mãi), autopilot không bao giờ xác nhận được bước gõ mật khẩu.
  (2) Gõ "Hà Nội" mà bàn phím ADB làm rơi dấu thành "Ha Noi" → vẫn coi là khớp (kiểm thử: `normalize_field_value('Hà Nội') == normalize_field_value('Ha Noi')` → True).
- Cách sửa (`fingerprint.py`, `targets.py`, `autopilot.py`, `action_executor.py`):
  (1) `fingerprint.normalize` giữ thuộc tính `"password"`; hàm dùng chung `targets.check_field_value` kiểm tra riêng ô `password=True`:
      không so nội dung chữ, chỉ so độ dài chuỗi ký tự che (`•*●...`) == `len(expected)` (nếu text không phải chuỗi che thì coi là
      "không kiểm được" — executor không cảnh báo, autopilot không tính verified và không báo lệch).
  (2) `targets.normalize_field_value`: chuẩn hoá Unicode NFC + `casefold()` + bỏ khoảng trắng/`-().`, **giữ nguyên dấu tiếng Việt**.
- Test: `test_l049_password_field_masks_and_accent_preservation`.

### [L-050] Ô nhập không có nhãn trong cây UI bị bỏ khỏi danh sách gửi AI → AI gõ vào nút ảnh đại diện — ĐÃ SỬA (27/11)
- Triệu chứng (Gemini, Danh bạ 4.92, trace 325ba852, task FAILED): màn "Create contact" có ô Name (chữ "Name" chỉ là nhãn
  nổi vẽ trên khung, OCR đọc được). Danh sách gửi AI không có ô Name; AI chọn `[47] (button)` (ảnh đại diện) làm
  "Name input field" → mở màn "Choose picture", lặp 5 lần BACK/gõ lại → loop guard dừng. Ở 8db2bee1/54fa6897 AI dùng
  index không tồn tại (133) rồi toạ độ.
- Nguyên nhân gốc: (1) ô Name đang focus là `View` (Compose) chưa lộ `EditText`; ô `EditText` không nhãn (Company) lại bị
  `_unlabeled_controls` bỏ vì nhãn `TextView` nằm bên trong box của nó; (2) phần tử không nhãn chỉ in "(button)" / "(input)",
  còn executor vẫn cho `input_text(target=47)` bấm nhầm vào `Button`.
- Cách sửa (`visualization.py`, `action_executor.py`, `flash_runner.md`, mọi app):
  (a) `McpActionExecutor._non_input_target_error`: từ chối `input_text(target=i)` khi phần tử `i` không phải `EditText`/`editable`,
      không phải `ocr`/`hint` và không có ô `EditText` nào bao điểm đó, nói rõ không phải ô nhập và gợi ý nếu con trỏ đang
      nhấp nháy trong ô thì gọi `input_text(text=...)` không kèm `target`.
  (b) `visualization.py`: đổi loại control ô nhập thành `(input field)`, không bỏ qua `EditText` chỉ vì bên trong chứa `TextView` nhãn tĩnh,
      và gắn nhãn gần nhất lấy từ text XML/OCR (`_nearest_input_label`) thành `[n] (input field: "<nhãn>")`.
- Test: `test_l050_executor_refuses_input_text_on_non_field_and_labels_unlabeled_inputs`.

### [L-051] Cảnh báo lệch giá trị (L-045) bị AI bỏ qua → vẫn lưu "+10913400134" — ĐÃ SỬA (27/11)
- Triệu chứng (b57e57b3, prompt có thêm "label Work"): AI gõ `clear_exist=False` 2 lần, cả 2 lần nhận
  "Warning: field now holds '+10913400134', not '0913400134'" nhưng vẫn bấm Save và báo xong với số sai.
- Nguyên nhân gốc: lệch chỉ là cảnh báo nối vào kết quả "thành công".
- Cách sửa (`action_executor.py`, `agents/flash/runner.py`):
  - Sau khi `observe()` lấy xong ảnh/XML mới, nếu ô nhập lệch giá trị (trừ trường hợp ô mật khẩu không đọc được ở L-049),
    `input_text` trả về kết quả lỗi (`status="error"`, ngắt burst sau đó) kèm thông điệp yêu cầu xoá (`clear_exist=true`)
    và gõ lại (hoặc giải thích nếu cố ý), và ghi nhận vào `_unfixed_typed_mismatches`. Khi AI sửa khớp thì tự xoá khỏi danh sách.
  - Khi AI gọi `report_task_status(completed)` mà vẫn còn ô lệch chưa sửa, `FlashRunner` từ chối nhắc lại đúng 1 lần
    (`consume_unfixed_typed_mismatches`).
- Test: `test_l051_typed_mismatch_fails_and_reminds_once_on_report_completion`.

### [L-052] Bản app có ô đang focus không có trong cây UI → recorder bỏ ghi mãi (an toàn, nhưng không học được) — GHI NHẬN
- 8db2bee1/54fa6897: "Workflow recording skipped, inconsistent run: 'Phuc Ly' (slot1+slot2) ... holds ''": recorder kiểm
  nhầm sang ô Company vì ô Name không có trong cây. Đúng hướng an toàn (không ghi workflow sai). Phụ thuộc L-050;
  sau L-050 xem lại: ô slot không tìm thấy trong cây → đánh dấu "không kiểm được" thay vì "không nhất quán".

### [L-053] Bàn phím chiếm ~39 dòng đầu danh sách gửi AI, đẩy phần tử app xuống cuối — ĐÃ SỬA (27/11)
- Hiện trạng (325ba852, lượt 1): `[1]..[39]` là phím (Q, W, E…, Done, Back), phần tử app ở `[40]..[49]`; model nhẹ chọn `[47] (button)` cho "Name input field" mà không viết ghi chú suy luận (39 token output).
- Cách sửa (`visualization.py`, `mcp/observation.py`, mọi app): `_keyboard_window_info` phát hiện cửa sổ IME tổng quát ở phần dưới màn hình (không hardcode tên bàn phím, chỉ cần khác app foreground và có ≥6 phím/cụm IME), `_is_ime_action_key` giữ riêng các phím hành động (`Done`/`Next`/`Search`/`Enter`/`Hide keyboard`…); `format_minimal_list_with_elements(..., compact=True)` in phần tử app TRƯỚC, gộp phím chữ/số thành 1 dòng `(keyboard open ...: N keys folded)` và đánh index liên tục cho các phím hành động giữ lại (`is_keyboard=True`); `observation.py` truyền `ignore_boxes` cho `merge_ocr_into_list` để không gộp lại chữ phím từ OCR.
- Test: `test_l053_compact_list_folds_keyboard_keys_keeps_action_keys_and_orders_app_first`.

### [L-054] Đánh số phần tử không nhãn lên ảnh gửi AI (Set-of-Mark có chọn lọc) — ĐÃ SỬA (27/11)
- Lý do: 325ba852 — model nhẹ chọn `[47] (button)` (ảnh đại diện) làm ô Name; số 47 vẽ trên ảnh đại diện sẽ lộ nhầm ngay.
- Số đo đã có (KE_HOACH_TOI_UU_ARTEMIS.md, benchmarks/payload_grounding, 52–56 bài): list gọn + ảnh thật đánh số phần tử không nhãn `compact_somU` 49/52 (Qwen) / 50/52 (Gemini Flash-Lite) > ảnh + list gọn 48/52 / 46/52; bản v2 + lấp chỗ trống OCR 51/56 (tốt nhất, bắt được 3/4 phần tử ngoài cây). Flash HIỆN CHƯA dùng — không cần A/B lại.
- Cách sửa (`visualization.py`, `agents/flash/{runner.py,flash_runner.md}`, `config/{agent.py,artemis.jsonc}`, mọi app): port `som_image_v2` vào `visualization.py` (`select_som_elements`, `draw_som_marks`): khung đỏ `(230,20,20)`, nhãn đỏ chữ trắng viền trắng `W//55` đặt ngoài khung (trên → trái → dưới → phải → trong) không đè nhãn khác, gộp khung chồng `>60%` giữ khung nhỏ; vẽ trên ảnh gốc rồi mới resize về `max_side` và lưu JPEG 1 lượt (`< 50 ms`/màn). Chỉ đánh dấu phần tử không nhãn, ô nhập và dòng `ocr`, bỏ qua phần tử XML có nhãn và phím bàn phím; cấu hình `flash.model_image.marks: "unlabeled" | "off"` (mặc định `"unlabeled"`).
- Test: `test_l054_selective_som_draws_only_unlabeled_inputs_and_ocr_within_budget`. Cần test máy Danh bạ + 1 app khác.

### [L-055] L-051 báo "thất bại" sai khi app tự định dạng giá trị hoặc khi AI cố ý gõ nối — ĐÃ SỬA (27/11)
- Nguyên nhân gốc: sau L-051, mọi lệch giá trị (không phải password) đều trả `failure`. `normalize_field_value` chỉ bỏ khoảng trắng và `-().` nên `'1,000,000'` vs `'1000000'`, `'+84 912 345 678'` vs `'0912345678'`, `"100.000 đ"`, ô ngày, hoặc gõ nối có chủ ý (`clear_exist=False`) đều thành lệch và lặp tới loop guard.
- Cách sửa (`workflow/targets.py`, `mcp/action_executor.py`, mọi app):
  (1) `McpActionExecutor._typed_field_mismatch`: chỉ trả `status="error"` khi là nội dung có sẵn không do lần chạy gõ dính vào chữ vừa gõ (`actual == pre_text + expected`, vd `"+1"`), mất dấu tiếng Việt (L-049) hoặc sai độ dài mặt nạ mật khẩu;
  (2) Gõ nối có chủ ý (`clear_exist=False` trên chữ đã gõ trong lượt/phiên, bỏ qua guard L-037 khi `clear_exist is False`) so với `pre_text + expected`, không tính lệch;
  (3) Nếu `clear_exist=True` gõ lại mà ô vẫn giữ nguyên giá trị lệch như cũ (`_typed_mismatch_history`) hoặc app tự định dạng (`+84...`, `100.000 đ`), hạ xuống ghi chú mềm (`status="success"`), xoá khỏi `_unfixed_typed_mismatches`, không nhắc khi `report_task_status(completed)`;
  (4) `targets.normalize_field_value`: bỏ dấu `,` phân cách nghìn khi toàn bộ giá trị là số (`1,000,000` == `1000000`).
- Test: `test_l055_numeric_commas_auto_format_retry_append_and_prefill_failure`.

### [L-056] Bản sửa L-055 tắt chốt L-037 khi `clear_exist=False` → "Son" + "Dao" lại thành "SonDao" không bị chặn — ĐÃ SỬA (27/11, Claude)
- Nguyên nhân gốc: L-055 cho "gõ nối có chủ ý" bằng cách bỏ qua `_input_overwrites_typed_field` khi `clear_exist=False`,
  và kiểm sau gõ so với "cũ + mới" → AI gõ nhầm Last name vào ô First name (đang giữ giá trị đã gõ) với
  `clear_exist=False` sẽ hỏng dữ liệu mà không có cảnh báo nào. AI đã từng tự đặt `clear_exist=False` (b57e57b3, Mai Phan).
- Cách sửa: L-037 luôn chạy; chỉ bỏ qua khi `clear_exist=False` VÀ (giá trị đang giữ + chữ mới) có nguyên văn trong yêu cầu
  của task (`_append_is_in_goal`, so sau `normalize_field_value`). Runner gán `executor.task_goal = goal`. Thông báo từ
  chối thêm gợi ý "gõ lại toàn bộ giá trị với clear_exist=true".
- Test hồi quy: `test_l055_numeric_commas_auto_format_retry_append_and_prefill_failure` (bước 3b: gõ nối "Dao" không có
  trong yêu cầu → bị từ chối; "Hello"+" world" có trong yêu cầu → được phép).

### [L-057] Recorder học workflow SAI từ lần chạy "completed" nhưng thiếu dữ liệu (Qwen 8B, Danh bạ) — ĐÃ SỬA (27/11)
- Triệu chứng (qwen3-vl:8b-32k, 27/11, trace 4b93c2fe + 8fcc3df7): AI gõ tên vào ô Name, HỌ vào ô Company, không nhập số điện thoại, bấm Save, báo `completed`. Dữ liệu thật (content provider): `Toan` / organization=`Le` / KHÔNG có phone. Recorder lưu workflow từ cả 2 lần (6 màn, cạnh "slot2 → Company" active, nav "Company"/"Save" thấy 2 lần → active) → lần phát lại sau sẽ autopilot TỰ lặp lại quy trình sai mà không hỏi AI. (Workflow này đã được chuyển ra scratchpad/bad_wf, không dùng.)
- Nguyên nhân gốc: recorder tin `report_task_status(completed)`; kiểm "inconsistent run" chỉ xét slot ĐÃ gõ, không xét giá trị yêu cầu chưa từng được gõ. Với 1 model tự ghi tự phát lại (không có model thầy) lỗi của model thành quy trình cố định.
- Cách sửa (`workflow/targets.py`, `workflow/recorder.py`, mọi app): thêm `targets.extract_requested_literals` (số ≥3 chữ số không phải đơn vị thời gian/kích thước, chuỗi ngoặc kép, email) và `recorder._unverified_run_outcome`: (1) bỏ qua ghi nếu có giá trị yêu cầu/slot chưa từng được gõ và không thấy trên màn kết quả, hoặc màn kết quả có lặp lại dữ liệu đã lưu nhưng làm rơi một giá trị (trừ ô `password=True`); (2) từ chối ghi nếu giá trị bị gõ vào ô có nhãn khác trong khi ngay trên màn đó có ô `EditText` khác mang nhãn đứng sát giá trị ấy trong prompt (vd `"last name Le"` mà gõ vào `"Company"`).
- Test: `test_l057_recorder_skips_incomplete_or_wrong_field_runs_and_handles_passwords`.

### [L-058] Model nhẹ báo xong khi thiếu dữ liệu yêu cầu — thêm kiểm "giá trị yêu cầu trên màn cuối" trước khi nhận completed — ĐÃ SỬA (27/11)
- Triệu chứng: như L-057; Qwen báo "Toan Le has been successfully created" dù không có số điện thoại.
- Cách sửa (`agents/flash/runner.py`, mọi phiên Flash, không riêng workflow): khi AI gọi `report_task_status(completed)`, `_consume_missing_requested_literals` đối chiếu các literal từ `extract_requested_literals(goal)` với nội dung màn hiện tại (UI + OCR) và các giá trị đã gõ thành công (`executor._typed_values` + `_typed_field_history` + `autopilot._typed_ok`, qua `normalize_field_value`); thiếu cái nào → trả `ToolMessage(status=error)` **đúng 1 lần** nhắc bổ sung hoặc giải trình (giữ 1-1 `ToolMessage`).
- Test: `test_l058_and_l059_completion_check_and_typed_progress_tracker`.

### [L-059] Bảng theo dõi giá trị đã gõ và giá trị yêu cầu gửi kèm mỗi lượt cho model nhẹ — ĐÃ SỬA (27/11)
- Triệu chứng: Qwen 8B quên mình đã gõ gì vào ô nào (đưa `"Le"` vào `Company`, bỏ sót số điện thoại `"0916200162"`).
- Cách sửa (`agents/flash/runner.py`, mọi app): `_record_typed_field_entry` ghi lại cặp `(nhãn ô, chữ đã gõ)` mỗi khi `input_text` thành công; `_typed_progress_line` đồng bộ với các ô `EditText` trên màn hiện tại (nếu đang ở màn form và ô bị xoá thì tự cập nhật) và chèn 1 dòng ngắn `[Typed so far: 'Toan' → Name · 'Le' → Company | Requested values: "0916200162" ✗ not entered yet]` vào đầu quan sát. Không sinh nhiễu ở các tác vụ không có literal nhập liệu và chưa gõ gì.
- Test: `test_l058_and_l059_completion_check_and_typed_progress_tracker`.

### [L-060] Giải thích chiều vuốt, diff phần tử mới/ẩn và trạng thái mép cuộn khi đụng mép — ĐÃ SỬA (27/11)
- Vấn đề: `swipe` trước đây chỉ trả `"Swiped down."` hoặc `"(no visible change)"`, không nói rõ ngón tay kéo xuống làm trang trôi về đầu (`TOP`) hay những phần tử nào vừa hiện ra/ẩn đi, dễ làm model nhẹ vuốt ngược chiều hoặc lặp khi đã kịch mép. (Android `uiautomator2` chỉ cung cấp cờ `scrollable`, không có sẵn vị trí cuộn trước khi vuốt.)
- Cách sửa (`mcp/action_executor.py`, `agents/flash/runner.py`, mọi app): `_apply_swipe_outcome_feedback` so sánh `elements` trước và sau `swipe` để báo rõ chiều ngón tay ↔ chiều trang + danh sách `New:` / `Hidden:`; khi `swipe(direction=...)` gặp `screen_unchanged` mới ghi nhận trạng thái mép (`scroll_edge_state`: `'top'` / `'bottom'`), gợi ý đổi sang chiều ngược lại và hiển thị trong dòng `[Scroll: hit TOP/BOTTOM edge ...]` cho tới khi trang cuộn được hoặc chuyển màn.
- Test: `test_l060_swipe_diff_direction_and_scroll_edge_feedback`.

### [L-060] Model nhầm hướng vuốt ("scroll down" ≠ `swipe(direction="down")`) và không thử hướng ngược khi màn đứng yên — ĐANG MỞ
- Triệu chứng (Qwen 8B, 4b93c2fe bước 5–6): muốn xem ô Phone ở PHÍA DƯỚI form, gọi `swipe(direction="down")` 2 lần
  (ngón tay 300→700 = nội dung chạy về ĐẦU trang); lần 2 "(no visible change)" vì đã ở đầu trang; model không thử
  `up`, chuyển sang bấm Save "để xem có hiện thêm ô không" → lưu thiếu số điện thoại.
- Nguyên nhân gốc: `direction` là hướng NGÓN TAY, ngược với cách nói thường "cuộn xuống"; kết quả trả về chỉ "Swiped down."
  nên model nhỏ không biết mình đã đi về phía nào và đã chạm mép.
- Việc cần làm (executor, mọi app, không thêm quy tắc prompt): kết quả swipe nói rõ hiệu ứng theo NỘI DUNG:
  "Swiped down (finger down): the page moved toward its TOP." Khi "(no visible change)" trên màn cuộn được: "Already at the
  TOP edge — content further DOWN the page needs direction='up'." (tương tự cho mép dưới/trái/phải). Test: swipe không
  đổi màn → thông điệp nêu mép và hướng ngược. CHƯA CÓ TEST.

### [L-061] L-057/L-058/L-059 coi chuỗi trong ngoặc kép là "giá trị phải có" → đòi cả "Place Order" — ĐÃ SỬA (27/11, Claude)
- Nguyên nhân gốc: `extract_requested_literals` lấy mọi chuỗi trong ngoặc kép. Yêu cầu thật trong traces trích nhãn nút/chỉ dẫn:
  `"Place Order"`, `"Đặt đơn"`, `"Yes"`, `"Remove"`, `"Mua ngay"`. Hậu quả: L-058 nhắc AI "task asks for 'Place Order' but it
  is not on the screen — finish it" (đẩy AI về đúng nút bị CẤM bấm); L-059 hiện "Place Order ✗ not entered yet" mỗi lượt;
  L-057 bỏ ghi workflow Grab (W0: Grab rơi từ 17 xuống 9 phiên).
- Cách sửa: mặc định KHÔNG lấy chuỗi ngoặc kép (`include_quoted=False`); chỉ số ≥3 chữ số và email là giá trị bắt buộc.
  Chuỗi ngoặc kép AI thật sự gõ (vd từ khoá tìm kiếm) vẫn được theo dõi qua lịch sử gõ.
- Kiểm chứng: W0 Grab trở lại 17 phiên, TOTAL 66 phiên, Undet=0. Test hồi quy: `test_l061_quoted_ui_labels_are_not_required_literals`.

### [L-062] L-060 báo sai "this screen has no scrollable container" ngay sau khi trang vừa cuộn được — ĐANG MỞ
- Triệu chứng (Qwen 8B, 03b4b720 bước 4–5): vuốt lần 1 trang chạy lên đầu ("New: Picture & calling card…"); vuốt lần 2 đứng
  yên → "No visible change…: this screen has no scrollable container." Model hiểu là không cuộn được nữa → bấm Save, thiếu số.
- Nguyên nhân gốc: form (Compose) không có node `scrollable=true` trong cây; `_apply_swipe_outcome_feedback` tin cờ này.
- Việc cần làm: nếu trong lần chạy đã có một lần vuốt làm màn (cùng app, cùng màn) đổi, coi màn là cuộn được → báo mép
  ("already at the TOP edge — content further DOWN needs direction='up'"); chỉ nói "no scrollable container" khi chưa có lần
  vuốt nào làm đổi màn này. Test: vuốt 1 đổi + vuốt 2 đứng yên trên cây không có cờ scrollable → báo mép TOP. CHƯA CÓ TEST.

### [L-063] Model báo "completed" lần 2 dù L-058 đã nhắc thiếu giá trị → kết quả vẫn ghi "completed" — ĐANG MỞ
- Triệu chứng (Qwen 8B, 03b4b720): L-058 nhắc "task asks for '0917400174'… never typed"; model bấm Save lần nữa rồi báo
  completed; phiên kết thúc `completed` dù danh bạ lưu KHÔNG có số (đã kiểm content provider).
- Việc cần làm (runner, mọi phiên): lần báo completed thứ 2 mà giá trị vẫn thiếu → nhận báo cáo nhưng kết thúc phiên với
  trạng thái trung thực (vd `failed` kèm lý do "requested value '0917400174' never entered / not on screen"); recorder không
  ghi; UI hiển thị lý do. Không nhắc lần 3 (tránh vòng lặp). Test: 2 lần báo thiếu → status failed + lý do. CHƯA CÓ TEST.
