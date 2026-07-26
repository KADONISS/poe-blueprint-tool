# PoE Blueprint Batch Planner

Tool tự động quét inventory 12×5, chỉ Ctrl+click các ô có Blueprint vào Planning Table, gán Rogue
cho các equipment, thử Confirm Plans tối đa hai lần và tiếp tục Blueprint kế tiếp.

## Chạy

```powershell
Set-Location -LiteralPath "C:\Users\Admin\Desktop\Tool PathOfExlie"
.\run.ps1
```

## Chuẩn bị

1. Chạy game ở Windowed Fullscreen hoặc Windowed.
2. Mở Planning Table và mở Inventory; inventory phải hiển thị đủ lưới 12×5.
3. Chọn đúng `PathOfExile.exe` trong **Process game**.
4. Đặt tool trên màn hình phụ để không che vùng game.
5. Điều chỉnh **Tốc độ plan** từ 0,5 đến 5,0 giây cho mỗi equipment.
6. Dùng hotkey chạy/dừng đã chọn trong tab **Cài đặt**.

## Batch 60 ô

Tool quét toàn bộ inventory một lần, sau đó xử lý danh sách Blueprint từ trái sang phải,
trên xuống dưới:

1. Chia inventory thành 60 ô cố định.
2. Nhận dạng icon Blueprint trong từng ô.
3. Bỏ qua toàn bộ ô trống và chỉ Ctrl+click ô có Blueprint.
4. Khi Blueprint mở, chạy module plan hiện tại.
5. Mỗi equipment: click thẻ, nhận dạng huy hiệu cấp `5`, click phía trên huy hiệu.
6. Tool không tự gửi phím Escape.
7. Sau tất cả equipment, thử **Confirm Plans** tối đa hai lần.
8. Luôn Ctrl+click icon Blueprint phía trên nút để lấy nó ra, bất kể Confirm thành công hay không.
9. Tiếp tục Blueprint kế tiếp trong danh sách đã nhận dạng.

## Giao diện

- **Planning Heist**: chạy/dừng quy trình plan Blueprint và xem kết quả.
- **Reveal Room**: khu vực riêng dành cho chức năng Reveal Room tiếp theo.
- **Cài đặt**: process game, tốc độ, ngưỡng, hotkey và màn hình Debug.
- Mặc định UI ở chế độ thu gọn.
- Bật **Debug** để xem ảnh chụp, khung nhận dạng và danh sách kết quả.
- **Tốc độ plan** hiển thị trực tiếp, ví dụ `1.2 giây` hoặc `3.2 giây`.
- **Ngưỡng** điều chỉnh độ nhạy detector equipment.
- Cài đặt được lưu trong `settings.json` cạnh chương trình.

## Phím nóng

- Mặc định `F6`: quét inventory và chạy batch các ô Blueprint.
- Mặc định `F8`: dừng trước thao tác kế tiếp.
- Có thể đổi hai phím từ `F1` đến `F12` trong tab **Cài đặt**.

Mỗi lần chạy được ghi vào `logs/`.

## Lưu ý

Tool không tự ẩn. Cơ chế chụp lấy pixel vùng game trên màn hình, vì vậy cửa sổ khác che lên
game cũng sẽ xuất hiện trong ảnh. Tự động hóa nhiều thao tác có thể không phù hợp với quy định
hiện hành của trò chơi; người dùng chịu trách nhiệm kiểm tra trước khi sử dụng.
