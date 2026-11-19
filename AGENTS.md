# HƯỚNG DẪN DÀNH CHO AI AGENT PHÁT TRIỂN & TỐI ƯU ARTEMIS

## 1. Triết lý & Mục tiêu tối cao: APP-AGNOSTIC TRÊN MỌI ỨNG DỤNG

Dự án tập trung tối ưu hệ thống ARTEMIS (Mobile Automation Agent) để điều khiển thiết bị di động **thông minh hơn, nhanh hơn, chính xác và tin cậy trên MỌI ỨNG DỤNG ANDROID**.

> [!IMPORTANT]
> **NGUYÊN TẮC BẤT BIẾN: KHÔNG ĐƯỢC TẬP TRUNG HAY THIÊN VỊ RIÊNG BẤT KỲ APP NÀO**
> - Tuyệt đối không hardcode, không viết luật riêng hay điều kiện đặc thù cho bất kỳ ứng dụng đơn lẻ nào (Grab, Cờ vua, Shopee, TikTok, Canvas, Game, v.v.).
> - Các app cụ thể chỉ đóng vai trò là **dữ liệu đo đạc thực nghiệm (benchmark/trace)** để kiểm chứng tính tổng quát.
> - Mọi tối ưu hóa bắt buộc phải là giải pháp tổng quát theo trạng thái hệ điều hành/giao diện Android. Bất kỳ thay đổi nào làm lợi cho một app mà gây hại, làm chậm hoặc tạo ngoại lệ cho các app khác đều bị nghiêm cấm.

Mục tiêu cốt lõi: Chuyển đổi từ cơ chế *"AI cầm tay chỉ việc từng bước nhỏ"* sang mô hình *"AI giao việc theo trạng thái kỳ vọng, thiết bị tự chờ và tự kiểm tra"* để giảm tối đa số lượt gọi AI thừa và độ trễ trên mọi loại ứng dụng.

---

## 2. Các nguyên tắc kiến trúc cốt lõi (Bắt buộc tuân thủ)

### 2.1. Phân định theo trạng thái kỳ vọng (Universal State Expectation)
Không suy đoán theo loại app, chỉ phân loại theo điều kiện mà tác vụ đang chờ đợi:
- **Biết rõ text sẽ xuất hiện:** Dùng `wait_for_text` (chờ màn hình mới tải xong, thông báo, kết quả tìm kiếm).
- **Biết rõ text sẽ biến mất:** Dùng `wait_until_gone` (chờ loading spinner kết thúc, dialog/popup đóng).
- **Chờ giao diện biến chuyển chưa biết trước nội dung:** Dùng `wait_for_screen_change` (tin nhắn mới, cập nhật danh sách, animation kết thúc, đối thủ đi lượt mới — tự động kết hợp so khớp XML diff trước và pixel diff 64x64 sau, che nhiễu đồng hồ/status bar).
- **Bản thân thời gian là mục tiêu:** Dùng `wait_for_delay` (nghe âm thanh trong 30s).

### 2.2. Không chạy mù (Anchor Guard & Visibility Guard)
- Khi tương tác xuyên màn hình mà AI chưa nhìn thấy, bắt buộc dùng `target_text`.
- Dữ liệu màn hình mới phải đi qua đầy đủ pipeline `session.observe()` để tự động kích hoạt:
  - **Ambiguity Guard:** Chặn thao tác khi phát hiện nhiều phần tử trùng text ở các vị trí khác nhau để tránh bấm nhầm.
  - **Occlusion Guard:** Chặn thao tác khi phần tử bị che khuất bởi dialog, floating button hoặc banner đè lên.
  - **Commitment Guard:** Ngăn chặn tuyệt đối việc tự động bấm vào các nút cam kết nhạy cảm (thanh toán, xác nhận đơn hàng, chuyển tiền, xóa tài khoản) trên màn hình chưa thấy.

### 2.3. Soft Handover thông minh (Không tự ngắt ngang khi timeout)
- Không đặt trần cứng tự ý dừng tác vụ khi hết giờ chờ (trừ khi người dùng cấu hình rõ).
- Khi hết thời gian chờ: Chụp ảnh màn hình gửi AI kèm thông tin đếm (ví dụ: *"Lần chờ thứ 3, tổng 90s, màn hình vẫn như cũ"*). AI sẽ nhìn thực tế (spinner còn quay hay màn hình bị đơ) để quyết định chờ tiếp hay xử lý sự cố.

### 2.4. Bảo vệ chuỗi thao tác (Burst Safety)
- Sau một bước chờ trong chuỗi, **CHỈ cho phép** chạy tiếp các thao tác dùng `target_text` hoặc các thao tác **không có mục tiêu màn hình cũ** (`press_key`, `input_text` vào ô đang focus, `swipe` theo hướng, `manage_app`, `back`).
- Nếu thao tác phía sau chứa index hoặc tọa độ `[x, y]` của màn hình cũ: Bắt buộc dừng chuỗi ngay lập tức để AI quan sát lại màn hình mới, tránh bấm nhầm phần tử khác.
- Sau bất kỳ thao tác `target_text` nào, phải đánh dấu trạng thái màn hình đã có thể thay đổi (`has_waited = True`) để bảo vệ các thao tác index tiếp theo.

### 2.5. Phòng thủ cấu trúc phẳng (`flatten_hierarchy`)
- Mọi hàm xử lý và so khớp UI (`compare_xml_hierarchy`, `find_matching_text_elements`, `clean_ui_elements`) đều phải bọc qua `flatten_hierarchy`.
- Đảm bảo hệ thống hoạt động ổn định 100% trên mọi dòng máy, ROM và phiên bản Android dù Accessibility Service trả về danh sách phẳng hay cây phân cấp lồng nhau (`children`).

### 2.6. Bất biến 1-to-1 ToolMessage
- Mỗi `tool_call` gửi lên mô hình LLM bắt buộc phải có đúng một `ToolMessage` tương ứng (trả về kết quả thực thi hoặc trạng thái lỗi/bị chặn an toàn) nhằm đảm bảo tính tương thích Function Calling chuẩn toàn cầu và không bao giờ gây lỗi HTTP 400 Bad Request.

### 2.7. Quy tắc CẤM Hardcode chuỗi & Tư duy theo UI Pattern tổng quát
- **Tuyệt đối KHÔNG hardcode danh sách text cụ thể vào code hoặc prompt:**
  - Không liệt kê các tên nút riêng của một app (ví dụ: KHÔNG viết `"Add to Basket"`, `"Place Order"`, `"Confirm address"`, `"Vui lòng nhập"`, `"Bắt buộc"`, v.v.).
  - Sang các app khác hoặc ngôn ngữ khác (tiếng Việt, tài chính, đặt vé, dịch vụ công), các chuỗi này sẽ hoàn toàn khác và làm hệ thống phán đoán sai lệch.
- **Luôn quy về UI Pattern và thuộc tính Accessibility chuẩn:**
  - Thay vì gọi tên nút bấm, hãy gọi đúng bản chất UI: **"Sticky Bottom CTA / Primary Action Bar"** (nút hành động chính ghim cố định ở đáy màn hình).
  - Thay vì tìm chuỗi text báo lỗi tiếng Việt/Anh, hãy đọc thuộc tính chuẩn của Accessibility node: `el.get("error")`, `el.get("enabled")`, `el.get("checked")`, `el.get("selected")`, `el.get("focused")`.
  - Khi màn hình đứng yên, chỉ trả về thông báo trung tính: `(no visible change)` hoặc `(no visible change; element error: '{err}')`.

### 2.8. Quy tắc Form có nút ghim đáy & Trạng thái có sẵn (Pre-existing State)
- **Nút ghim đáy không có nghĩa là đã xem hết trang:**
  - Khi màn hình có thanh hành động chính ghim ở đáy nhưng nội dung form ở giữa còn cuộn được (`screen_scrollable` hoặc phần tử bị che khuất ở mép dưới), AI bắt buộc phải cuộn kiểm tra các trường bắt buộc ẩn bên dưới trước khi bấm xác nhận.
- **Thận trọng với dữ liệu có sẵn (Giỏ hàng cũ, form điền sẵn):**
  - Khi app mở ra đã có sẵn giỏ hàng (ví dụ: `1 Item`) hoặc form điền sẵn từ phiên trước, AI tuyệt đối không được "nhận vơ" là tác vụ đã xong.
  - Phải coi đó là trạng thái lạ, bắt buộc phải vào kiểm tra chi tiết nội dung (tên món, số lượng, thông số) có trùng khớp chính xác với yêu cầu của người dùng hay không rồi mới được tiến hành tiếp.

### 2.9. Tối ưu thời gian chờ & Bộ nhớ chặn lặp bền vững
- **Cắt chờ ngay khi màn hình đã đổi xong (`wait_for_screen_change`):**
  - Hỗ trợ tham số `after="my_action"` (mặc định) và `after="external"`.
  - Nếu sau cú bấm của chính mình mà màn hình đã đổi và ổn định xong (`after="my_action"`), lệnh chờ phải trả về ngay (`"Screen already changed..."`), tuyệt đối không được ngồi chờ đến hết giờ timeout.
  - Chỉ khi chờ bên ngoài (`after="external"`, như cờ vua/chat) mới giữ lệnh chờ sau khi thao tác của mình đã hoàn tất.
- **Chặn bấm lặp bền vững (`_ineffective_taps`):**
  - Bộ nhớ ghi nhận các cú bấm không tác dụng phải lưu theo vùng và hash/signature màn hình, **không được bị reset** bởi các thao tác xen giữa (như lệnh chờ `wait`, hoặc thao tác thử nghiệm khác). Chặn ngay việc bấm lặp lại cùng một nút trên cùng trạng thái màn hình.

---

## 3. Quy trình làm việc bắt buộc cho AI Agent sau khi hoàn thành nhiệm vụ

Mỗi khi AI Agent hoàn thành bất kỳ thay đổi mã nguồn, tính năng mới hoặc vá lỗi nào:

1. **Kiểm tra hồi quy toàn diện (Regression Test):**
   Chạy lệnh kiểm thử sau trong thư mục `artemis`:
   ```bash
   uv run pytest tests/unit/mcp/ tests/unit/agents/ tests/unit/llm/
   ```
   Chỉ được kết luận hoàn thành khi **toàn bộ test cases đều pass 100%**.

2. **Cập nhật Roadmap & Nhật ký tối ưu:**
   Mở file [`KE_HOACH_TOI_UU_ARTEMIS.md`](file:///home/nhat/Workspace/artemis/KE_HOACH_TOI_UU_ARTEMIS.md):
   - Cập nhật trạng thái `✅ Xong` tại bảng **Mục 2 ("TRẠNG THÁI TRIỂN KHAI")**.
   - Cập nhật mục **"LỊCH SỬ PHIÊN BẢN (VERSION CHANGELOG)"** nếu có thay đổi hoặc nâng cấp phiên bản kiến trúc.
   - Ghi chú tóm tắt giải pháp kỹ thuật và số lượng test case kiểm chứng.
   - Nếu có thực hiện benchmark/trace mới, bổ sung số liệu (lượt AI, thời gian) vào **Mục 1 ("DỮ LIỆU THỰC NGHIỆM")**.

3. **Cập nhật tài liệu luồng điều khiển thiết bị:**
   Nếu thay đổi chạm vào bất kỳ file nào trong **Mục 9 ("Bản đồ file")** của [`docs/FLOW_DIEU_KHIEN_THIET_BI.md`](docs/FLOW_DIEU_KHIEN_THIET_BI.md) (capture màn hình, perception, định dạng UI list, prompt, dịch lệnh, thực thi, safety net, đồ thị Pro, Flash runner):
   - Sửa lại mô tả tầng tương ứng và tham chiếu `file:line` cho khớp code mới.
   - Nếu xử lý một mục trong **8.2 ("Cơ hội tối ưu")**, xoá/đánh dấu mục đó.
   - Thêm 1 dòng vào **Mục 10 ("Nhật ký thay đổi luồng")**: ngày, thay đổi, file, ảnh hưởng tới luồng.

