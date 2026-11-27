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
  a. Executor (Flash): TRƯỚC khi gõ có chỉ định điểm, trên cùng màn hình: điểm không nằm trên ô nhập nào
     mà ô đang focus chứa một giá trị chính lần chạy đã gõ → từ chối, báo AI dùng index ô hiện tại / ẩn
     bàn phím / cuộn. (Kiểm SAU khi gõ thì sai: bàn phím làm form tự cuộn, ô dời chỗ.) Ô trống và nội dung
     không do lần chạy gõ (URL, từ khoá cũ) vẫn cho gõ. Quét 111 lần gõ có điểm trong DB: chặn đúng 1 lần
     (Gemini "Tran" đè "Minh"); bản đầu chặn nhầm 17, bản hai 3 (Chrome thay "about:blank").
  b. Recorder (`inconsistent_typing`): lần gõ CUỐI của mỗi giá trị phải còn trong ô lúc gửi form (màn
     trước thao tác không-gõ kế tiếp); không còn → không ghi workflow từ lần chạy đó (log lý do). Sửa sai
     (gõ lại đúng ô) vẫn hợp lệ. Lần ghi c403e23a bị từ chối: "'Minh' không còn trong ô First name".
  c. Autopilot: không gõ vào ô đang chứa giá trị nó đã gõ cho slot khác trong lần chạy.
- Test: `test_l029_recorder_rejects_a_run_that_overwrote_its_own_typing`,
  `test_l029_executor_refuses_typing_over_this_runs_own_value`,
  `test_l029_autopilot_never_types_over_a_value_it_typed_for_another_field`.

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

### [L-031] Trạng thái đầu và trạng thái đã cuộn của cùng một form là hai màn khác nhau — ĐANG MỞ
- Triệu chứng (Gemini, Danh bạ, 27/11, trace 99cd3b7b): bước "Create contact" lần nào cũng
  "screen is not the recorded next screen", 2 lần → `disabled`; mất một bước tự động mỗi lần chạy.
- Nguyên nhân gốc: lúc ghi, màn sau "Create contact" được lưu ở trạng thái bàn phím đã bật, form đã tự
  cuộn (n2); lúc phát lại form mở ở trạng thái đầu (n4: có nút Close, phần tài khoản). Độ giống bố cục giữa
  hai trạng thái dưới ngưỡng gộp nên thành hai node; verify khớp n4 (1.00) thay vì n2.
- Hướng sửa đề xuất: khi một bước gõ chữ bắt đầu từ node A mà ô của nó (cùng hint / id) cũng có ở node B
  liền trước trên cùng lần chạy, coi A và B là cùng một form (gộp vân tay); hoặc kiểm màn đích chấp nhận
  node có chứa mọi ô của các bước gõ kế tiếp. CHƯA CÓ TEST.

### [L-032] Autopilot không cuộn tìm ô nhập bị bàn phím che khi màn còn bước khác đang chờ — ĐANG MỞ
- Triệu chứng (trace 99cd3b7b, eaa9055c): sau khi gõ ô đầu, autopilot dừng "target not_found" với ô Họ /
  Số điện thoại (bị bàn phím che, không có trong cây UI) → AI phải làm tiếp từng ô.
- Nguyên nhân gốc: chỉ cuộn tìm khi MỌI lý do loại bước đều là `not_found`; màn còn bước "Lưu" với lý do
  "chờ gõ đủ các ô" → điều kiện sai → dừng luôn.
- Hướng sửa đề xuất: bỏ qua các lý do "chờ điều kiện" (waits for … typed, already typed) khi quyết định
  cuộn; cuộn trong dải trên bàn phím (đã có `safe_swipe_in_norm`) hoặc ẩn bàn phím rồi thử lại. CHƯA CÓ TEST.
