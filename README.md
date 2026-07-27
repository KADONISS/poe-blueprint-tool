# PoE Blueprint Batch Planner

Phiên bản hiện tại: **v0.3.1**

Tool tự động quét inventory 12×5, chỉ Ctrl+click các ô có Blueprint vào Planning Table, gán Rogue
cho các equipment, thử Confirm Plans tối đa hai lần và tiếp tục Blueprint kế tiếp.

## Chạy

```powershell
Set-Location -LiteralPath "C:\Users\Admin\Desktop\Tool PathOfExlie"
.\run.ps1
```

## Chuẩn bị

1. Chạy game ở Windowed Fullscreen hoặc Windowed.
   - Đã tối ưu cho client `1920×1080` trên màn 2K.
   - Hỗ trợ client `1768×992` và `1680×1050`; inventory được neo theo cạnh phải thay vì theo tỷ lệ chiều rộng.
2. Mở Planning Table và mở Inventory; inventory phải hiển thị đủ lưới 12×5.
3. Chọn đúng `PathOfExile.exe` trong **Process game**.
4. Đặt tool trên màn hình phụ để không che vùng game.
5. Điều chỉnh **Tốc độ plan** từ 0,5 đến 5,0 giây cho mỗi equipment.
6. Dùng hotkey chạy/dừng đã chọn trong tab **Cài đặt**.

## Batch 60 ô

Tool quét toàn bộ inventory một lần, sau đó xử lý danh sách Blueprint từ trái sang phải,
trên xuống dưới:

1. Chia inventory thành 60 ô cố định.
2. Nhận dạng icon Blueprint trong từng ô và kiểm tra con dấu đỏ.
3. Bỏ qua toàn bộ ô trống cùng Blueprint đã plan; chỉ Ctrl+click Blueprint chưa plan.
4. Khi Blueprint mở, chạy module plan hiện tại.
5. Mỗi equipment: click thẻ, nhận dạng huy hiệu cấp `5`, click phía trên huy hiệu.
6. Tool không tự gửi phím Escape.
7. Khi **Confirm Plans** sáng, dừng ngay các vùng quét còn lại và thử xác nhận tối đa hai lần.
8. Luôn Ctrl+click icon Blueprint phía trên nút để lấy nó ra, bất kể Confirm thành công hay không.
9. Tiếp tục Blueprint kế tiếp trong danh sách đã nhận dạng.

## Giao diện

- **Planning Heist**: chạy/dừng quy trình plan Blueprint và xem kết quả.
- **Reveal Room**: đọc số `Wings Revealed` và mở tự động các wing còn thiếu.
- **Cài đặt**: process game, tốc độ, ngưỡng, hotkey và màn hình Debug.
- Mặc định UI ở chế độ thu gọn.
- Bật **Debug** để xem ảnh chụp, khung nhận dạng và danh sách kết quả.
- **Tốc độ plan** là mục tiêu tổng thời gian cho chuỗi click thẻ → chọn Rogue, ví dụ `0.8 giây` hoặc `1.2 giây`.
- **Ngưỡng** điều chỉnh độ nhạy detector equipment.
- **Nấc zoom mỗi vùng** điều chỉnh số lần cuộn chuột khi tool lần lượt quét lưới 3×3 trên Planning Table.
- Lưới 3×3 dùng tâm quét giãn rộng và tự tránh panel trái/phải ở cửa sổ `1768×992` và `1680×1050`.
- Thẻ đã gán Rogue được quy đổi về tọa độ bản đồ gốc để không bị nhận dạng lại ở vùng zoom chồng lấn.
- Detector xử lý ở độ phân giải nội bộ thấp hơn với các mức scale đã hiệu chỉnh, sau đó quy đổi tọa độ click về ảnh game gốc.
- Cài đặt được lưu trong `settings.json` cạnh chương trình.

## Phím nóng

- Mặc định `F6`: quét inventory và chạy batch các ô Blueprint.
- Mặc định `F8`: dừng trước thao tác kế tiếp.
- Có thể đổi hai phím từ `F1` đến `F12` trong tab **Cài đặt**.

Mỗi lần chạy được ghi vào `logs/`.

## Reveal Room

1. Mở bàn Reveal và Inventory, để trống ô Blueprint trên bàn.
2. Chọn tab **Reveal Room** rồi nhấn `F6` hoặc nút **Reveal Wings**.
3. Tool nhận dạng các Blueprint trong inventory, đưa con trỏ ra khỏi ô cũ rồi đọc mỗi
   item hai lần; hai kết quả phải trùng nhau mới chấp nhận `Wings Revealed: x/3` hoặc `x/4`.
4. Blueprint `3/3` và `4/4` được bỏ qua.
5. Blueprint chưa đủ wing được Ctrl+click vào bàn Reveal.
6. Tool dùng khung wing đỏ lớn làm điều kiện chính ổn định, ghép vị trí mắt qua nhiều
   frame và quan sát trọn một chu kỳ nhấp nháy trước khi kết luận đã hết wing; biểu tượng
   mắt chỉ là mục tiêu click bên trong khung.
7. Sau mỗi wing, tool chụp và nhận dạng lại vì vị trí các khung còn lại có thể thay đổi.
8. Khi đã mở đủ wing, tool Ctrl+click Blueprint ra khỏi bàn và tiếp tục cuốn kế tiếp.

Detector Planning chỉ học phần hình nghề phía trên card. Vùng chữ `Level 1–5` và tên nghề
được trung hòa trong mẫu nhưng kích thước toàn card vẫn được giữ để tránh nhầm icon phòng,
portrait Rogue hoặc ký hiệu rời trên bản đồ.

Log của chức năng này được ghi riêng với tên `logs/reveal-run-*.json`.

## Lưu ý

Tool không tự ẩn. Cơ chế chụp lấy pixel vùng game trên màn hình, vì vậy cửa sổ khác che lên
game cũng sẽ xuất hiện trong ảnh. Tự động hóa nhiều thao tác có thể không phù hợp với quy định
hiện hành của trò chơi; người dùng chịu trách nhiệm kiểm tra trước khi sử dụng.
