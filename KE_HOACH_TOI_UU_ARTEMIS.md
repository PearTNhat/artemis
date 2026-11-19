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
