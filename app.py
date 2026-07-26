from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, LEFT, RIGHT, Button, Canvas, Checkbutton, DoubleVar, Frame, Label, StringVar, Tk
from tkinter import messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageGrab, ImageTk


APP_TITLE = "PoE Blueprint Rogue Assigner"
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ASSET_DIR = BUNDLE_DIR / "assets" / "equipment"
LOG_DIR = APP_DIR / "logs"
SETTINGS_PATH = APP_DIR / "settings.json"
REFERENCE_HEIGHT = 1368
DETECTION_SCALE = 0.72
HOTKEY_CODES = {f"F{number}": 0x6F + number for number in range(1, 13)}
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

TEMPLATES = {
    "brute_force.png": "Brute Force",
    "counter_thaumaturgy.png": "Counter-Thaumaturgy",
    "perception_l1.png": "Perception L1",
    "demolition.png": "Demolition",
    "engineering.png": "Engineering",
    "agility.png": "Agility",
    "perception_l5.png": "Perception L5",
    "lockpicking.png": "Lockpicking",
    "trap_disarmament.png": "Trap Disarmament",
    "trap_disarmament_l2.png": "Trap Disarmament",
}

# Tọa độ Win32 và ảnh chụp phải cùng đơn vị pixel, đặc biệt khi hai màn hình
# dùng mức scale khác nhau.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


@dataclass
class WindowTarget:
    hwnd: int
    pid: int
    process_name: str
    title: str

    @property
    def label(self) -> str:
        return f"{self.process_name}  |  {self.title}  |  PID {self.pid}"


def _process_name(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return f"PID-{pid}"
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value)
        return f"PID-{pid}"
    finally:
        kernel32.CloseHandle(handle)


def enumerate_window_targets() -> list[WindowTarget]:
    """Liệt kê process có cửa sổ top-level đang hiển thị."""
    user32 = ctypes.windll.user32
    results: list[WindowTarget] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        if user32.GetWindow(hwnd, 4):  # GW_OWNER: bỏ cửa sổ phụ/owned window.
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, length + 1)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = title_buffer.value.strip()
        if title and title != APP_TITLE:
            results.append(WindowTarget(int(hwnd), int(pid.value), _process_name(int(pid.value)), title))
        return True

    callback = callback_type(visit)
    user32.EnumWindows(callback, 0)
    return sorted(results, key=lambda item: (item.process_name.casefold(), item.title.casefold()))


def client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
        raise RuntimeError("Process/cửa sổ đã chọn không còn hoạt động. Hãy bấm Làm mới và chọn lại.")
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("Không lấy được vùng client của process đã chọn.")
    top_left = POINT(rect.left, rect.top)
    bottom_right = POINT(rect.right, rect.bottom)
    user32.ClientToScreen(hwnd, ctypes.byref(top_left))
    user32.ClientToScreen(hwnd, ctypes.byref(bottom_right))
    if bottom_right.x - top_left.x < 200 or bottom_right.y - top_left.y < 200:
        raise RuntimeError("Cửa sổ game đang thu nhỏ hoặc vùng hiển thị không hợp lệ.")
    return top_left.x, top_left.y, bottom_right.x, bottom_right.y


@dataclass
class Detection:
    name: str
    score: float
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.width, self.y + self.height


class EquipmentDetector:
    def __init__(self) -> None:
        self.templates: list[tuple[str, np.ndarray]] = []
        self.scaled_templates: dict[int, list[tuple[str, np.ndarray]]] = {}
        for filename, name in TEMPLATES.items():
            path = ASSET_DIR / filename
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise FileNotFoundError(f"Không đọc được ảnh mẫu: {path}")
            if filename == "trap_disarmament.png":
                # Đường route đỏ chạy ngang phần dưới thay đổi theo từng Blueprint.
                # Chỉ giữ khung trên + biểu tượng bẫy rồi chuẩn hóa mẫu phóng
                # lớn về kích thước thẻ game thực tế.
                image = image[: round(image.shape[0] * 0.68), :]
                normalized_width = 43
                normalized_height = round(image.shape[0] * normalized_width / image.shape[1])
                image = cv2.resize(image, (normalized_width, normalized_height), interpolation=cv2.INTER_AREA)
            # Viền và chữ ít ổn định hơn biểu tượng; gradient giúp chống đổi sáng.
            image = cv2.GaussianBlur(image, (3, 3), 0)
            self.templates.append((name, image))

    def _templates_for_height(self, height: int) -> list[tuple[str, np.ndarray]]:
        cached = self.scaled_templates.get(height)
        if cached is not None:
            return cached
        base_scale = (height / REFERENCE_HEIGHT) * DETECTION_SCALE
        scaled: list[tuple[str, np.ndarray]] = []
        for name, original in self.templates:
            for scale in base_scale * np.linspace(0.62, 1.42, 13):
                tw = max(9, round(original.shape[1] * float(scale)))
                th = max(13, round(original.shape[0] * float(scale)))
                template = cv2.resize(
                    original,
                    (tw, th),
                    interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
                )
                scaled.append((name, template))
        self.scaled_templates[height] = scaled
        return scaled

    @staticmethod
    def _iou(a: Detection, b: Detection) -> float:
        ax1, ay1, ax2, ay2 = a.box
        bx1, by1, bx2, by2 = b.box
        ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
        intersection = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        union = a.width * a.height + b.width * b.height - intersection
        return intersection / union if union else 0.0

    def scan(self, screenshot: Image.Image, threshold: float) -> list[Detection]:
        rgb = np.asarray(screenshot.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        height, width = gray.shape

        # Loại HUD hai bên/dưới nhưng giữ toàn bộ vùng bản Blueprint.
        x0, x1 = int(width * 0.12), int(width * 0.89)
        # Equipment của Blueprint chỉ xuất hiện ở phần trên của bản đồ. Bỏ vùng
        # HUD/phòng phía dưới giúp giảm khoảng 22% số pixel phải template-match.
        y0, y1 = int(height * 0.07), int(height * 0.58)
        roi = cv2.GaussianBlur(gray[y0:y1, x0:x1], (3, 3), 0)
        roi = cv2.resize(
            roi,
            None,
            fx=DETECTION_SCALE,
            fy=DETECTION_SCALE,
            interpolation=cv2.INTER_AREA,
        )

        candidates: list[Detection] = []

        for name, template in self._templates_for_height(height):
            th, tw = template.shape
            if tw >= roi.shape[1] or th >= roi.shape[0]:
                continue
            result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            # Lấy cực đại cục bộ thay vì mọi pixel vượt ngưỡng.
            dilated = cv2.dilate(result, np.ones((7, 7), np.uint8))
            ys, xs = np.where((result >= threshold) & (result >= dilated - 1e-6))
            for px, py in zip(xs.tolist(), ys.tolist()):
                candidates.append(
                    Detection(
                        name,
                        float(result[py, px]),
                        round(px / DETECTION_SCALE) + x0,
                        round(py / DETECTION_SCALE) + y0,
                        round(tw / DETECTION_SCALE),
                        round(th / DETECTION_SCALE),
                    )
                )

        # NMS dùng chung cho mọi template để một thẻ chỉ xuất hiện một lần.
        kept: list[Detection] = []
        for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
            if all(self._iou(candidate, existing) < 0.32 for existing in kept):
                kept.append(candidate)
        return sorted(kept, key=lambda item: (item.center[0], item.center[1]))


class BlueprintAssigner:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("780x340")
        self.root.minsize(680, 300)
        self.detector = EquipmentDetector()
        self.level_five_template = cv2.imread(
            str(BUNDLE_DIR / "assets" / "rogue_level_5.png"),
            cv2.IMREAD_GRAYSCALE,
        )
        if self.level_five_template is None:
            raise FileNotFoundError("Không đọc được assets/rogue_level_5.png")
        confirm_reference = cv2.imread(
            str(BUNDLE_DIR / "assets" / "confirm_plans_reference.png"),
            cv2.IMREAD_GRAYSCALE,
        )
        if confirm_reference is None:
            raise FileNotFoundError("Không đọc được assets/confirm_plans_reference.png")
        self.confirm_template = confirm_reference[80:130, 15:215]
        self.inventory_blueprint_template = cv2.imread(
            str(BUNDLE_DIR / "assets" / "inventory_blueprint.png"),
            cv2.IMREAD_GRAYSCALE,
        )
        if self.inventory_blueprint_template is None:
            raise FileNotFoundError("Không đọc được assets/inventory_blueprint.png")
        self.threshold = DoubleVar(value=0.72)
        self.plan_speed = DoubleVar(value=1.6)
        self.speed_input = StringVar(value="1.6")
        self.threshold_input = StringVar(value="0.72")
        self.debug_enabled = StringVar(value="0")
        self.run_hotkey = StringVar(value="F6")
        self.stop_hotkey = StringVar(value="F8")
        self.run_hotkey_name = "F6"
        self.stop_hotkey_name = "F8"
        self.run_hotkey_code = HOTKEY_CODES["F6"]
        self.stop_hotkey_code = HOTKEY_CODES["F8"]
        self.status = StringVar(value="F6: chạy · F8: dừng khẩn cấp")
        self.summary = StringVar(value="Chưa quét.")
        self.image: Image.Image | None = None
        self.preview: ImageTk.PhotoImage | None = None
        self.detections: list[Detection] = []
        self.window_targets: list[WindowTarget] = []
        self.process_choice = StringVar(value="")
        self.capture_origin = (0, 0)
        self.active_speed = 1.6
        self.active_threshold = 0.72
        self.stop_event = threading.Event()
        self.busy = False
        self._hotkeys_down: set[int] = set()
        self._load_settings()
        self._build()
        self.refresh_processes()
        self._apply_hotkeys(save=False)
        if self.debug_enabled.get() == "1":
            self.toggle_debug()
        threading.Thread(target=self._hotkey_loop, daemon=True).start()

    def _build(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=BOTH, expand=True, padx=10, pady=(10, 4))
        self.planning_tab = Frame(self.notebook, padx=12, pady=12)
        self.reveal_tab = Frame(self.notebook, padx=18, pady=18)
        self.settings_tab = Frame(self.notebook, padx=12, pady=12)
        self.notebook.add(self.planning_tab, text="Planning Heist")
        self.notebook.add(self.reveal_tab, text="Reveal Room")
        self.notebook.add(self.settings_tab, text="Cài đặt")

        action_row = Frame(self.planning_tab)
        action_row.pack(fill="x")
        self.run_button = Button(action_row, text="Batch Blueprint (F6)", width=21, command=self.start_run)
        self.run_button.pack(side=LEFT, padx=(0, 8))
        self.stop_button = Button(action_row, text="Dừng (F8)", width=14, command=self.stop)
        self.stop_button.pack(side=LEFT)
        Label(
            self.planning_tab,
            textvariable=self.summary,
            justify=LEFT,
            anchor="nw",
            wraplength=720,
        ).pack(fill="x", pady=(14, 6))
        Label(
            self.planning_tab,
            text="Mở Planning Table + Inventory, sau đó chạy batch. Tool chỉ Ctrl+click các ô được nhận dạng là Blueprint.",
            justify=LEFT,
            anchor="w",
            fg="#444",
        ).pack(fill="x")

        Label(
            self.reveal_tab,
            text="Reveal Room",
            font=("Segoe UI", 16, "bold"),
            anchor="w",
        ).pack(fill="x")
        Label(
            self.reveal_tab,
            text="Tab này đã được tách riêng để bổ sung quy trình Reveal Room ở bước phát triển tiếp theo.",
            justify=LEFT,
            anchor="nw",
            fg="#555",
        ).pack(fill="x", pady=(12, 0))

        process_row = Frame(self.settings_tab)
        process_row.pack(fill="x", pady=(0, 12))
        Label(process_row, text="Process game:", width=15, anchor="w").pack(side=LEFT)
        self.process_box = ttk.Combobox(process_row, textvariable=self.process_choice, state="readonly")
        self.process_box.pack(side=LEFT, fill="x", expand=True, padx=(0, 8))
        self.process_box.bind("<<ComboboxSelected>>", lambda _event: self._save_settings())
        Button(process_row, text="Làm mới", width=11, command=self.refresh_processes).pack(side=RIGHT)

        settings_row = Frame(self.settings_tab)
        settings_row.pack(fill="x", pady=(0, 12))
        Label(settings_row, text="Tốc độ plan:", width=15, anchor="w").pack(side=LEFT)
        ttk.Scale(
            settings_row,
            from_=0.5,
            to=5.0,
            variable=self.plan_speed,
            length=210,
            command=self._speed_changed,
        ).pack(side=LEFT, padx=8)
        self.speed_entry = ttk.Entry(settings_row, textvariable=self.speed_input, width=6, justify="center")
        self.speed_entry.pack(side=LEFT)
        self.speed_entry.bind("<Return>", self._apply_speed_input)
        self.speed_entry.bind("<FocusOut>", self._apply_speed_input)
        Label(settings_row, text="giây").pack(side=LEFT, padx=(4, 0))

        threshold_row = Frame(self.settings_tab)
        threshold_row.pack(fill="x", pady=(0, 12))
        Label(threshold_row, text="Ngưỡng:", width=15, anchor="w").pack(side=LEFT)
        ttk.Scale(
            threshold_row,
            from_=0.58,
            to=0.92,
            variable=self.threshold,
            length=210,
            command=self._threshold_changed,
        ).pack(side=LEFT, padx=8)
        self.threshold_entry = ttk.Entry(
            threshold_row,
            textvariable=self.threshold_input,
            width=6,
            justify="center",
        )
        self.threshold_entry.pack(side=LEFT)
        self.threshold_entry.bind("<Return>", self._apply_threshold_input)
        self.threshold_entry.bind("<FocusOut>", self._apply_threshold_input)

        hotkey_row = Frame(self.settings_tab)
        hotkey_row.pack(fill="x", pady=(0, 12))
        Label(hotkey_row, text="Hotkey chạy:", width=15, anchor="w").pack(side=LEFT)
        self.run_hotkey_box = ttk.Combobox(
            hotkey_row,
            textvariable=self.run_hotkey,
            values=list(HOTKEY_CODES),
            state="readonly",
            width=7,
        )
        self.run_hotkey_box.pack(side=LEFT, padx=(8, 22))
        Label(hotkey_row, text="Hotkey dừng:").pack(side=LEFT)
        self.stop_hotkey_box = ttk.Combobox(
            hotkey_row,
            textvariable=self.stop_hotkey,
            values=list(HOTKEY_CODES),
            state="readonly",
            width=7,
        )
        self.stop_hotkey_box.pack(side=LEFT, padx=8)
        self.run_hotkey_box.bind("<<ComboboxSelected>>", self._hotkeys_changed)
        self.stop_hotkey_box.bind("<<ComboboxSelected>>", self._hotkeys_changed)

        Checkbutton(
            self.settings_tab,
            text="Hiển thị màn hình Debug trong tab Planning Heist",
            variable=self.debug_enabled,
            onvalue="1",
            offvalue="0",
            command=self.toggle_debug,
        ).pack(anchor="w")

        self.body = Frame(self.planning_tab, pady=8)
        self.canvas = Canvas(self.body, bg="#111318", highlightthickness=0)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.render())

        panel = Frame(self.body, width=280, padx=14, pady=8)
        panel.pack(side=RIGHT, fill="y")
        panel.pack_propagate(False)
        Label(panel, text="KẾT QUẢ", font=("Segoe UI", 14, "bold"), anchor="w").pack(fill="x")
        Label(panel, textvariable=self.summary, justify=LEFT, anchor="nw", wraplength=255).pack(fill=BOTH, expand=True, pady=12)
        self.status_label = Label(self.root, textvariable=self.status, anchor="w", padx=12, pady=8, relief="sunken")
        self.status_label.pack(fill="x")

    def _load_settings(self) -> None:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        speed = data.get("plan_speed", 1.6)
        threshold = data.get("threshold", 0.72)
        if isinstance(speed, (int, float)) and 0.5 <= float(speed) <= 5.0:
            self.plan_speed.set(round(float(speed), 1))
            self.speed_input.set(f"{float(speed):.1f}")
        if isinstance(threshold, (int, float)) and 0.58 <= float(threshold) <= 0.92:
            self.threshold.set(round(float(threshold), 2))
            self.threshold_input.set(f"{float(threshold):.2f}")
        run_key = str(data.get("run_hotkey", "F6")).upper()
        stop_key = str(data.get("stop_hotkey", "F8")).upper()
        if run_key in HOTKEY_CODES and stop_key in HOTKEY_CODES and run_key != stop_key:
            self.run_hotkey.set(run_key)
            self.stop_hotkey.set(stop_key)
            self.run_hotkey_code = HOTKEY_CODES[run_key]
            self.stop_hotkey_code = HOTKEY_CODES[stop_key]
        if data.get("debug") in ("0", "1"):
            self.debug_enabled.set(data["debug"])
        if isinstance(data.get("process"), str):
            self.process_choice.set(data["process"])

    def _save_settings(self) -> None:
        data = {
            "plan_speed": round(float(self.plan_speed.get()), 1),
            "threshold": round(float(self.threshold.get()), 2),
            "run_hotkey": self.run_hotkey.get(),
            "stop_hotkey": self.stop_hotkey.get(),
            "debug": self.debug_enabled.get(),
            "process": self.process_choice.get(),
        }
        try:
            SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.status.set("Không thể lưu cài đặt cạnh chương trình.")

    def _apply_hotkeys(self, save: bool = True) -> bool:
        run_key = self.run_hotkey.get()
        stop_key = self.stop_hotkey.get()
        if run_key == stop_key:
            self.status.set("Hotkey chạy và dừng phải khác nhau.")
            return False
        self.run_hotkey_code = HOTKEY_CODES[run_key]
        self.stop_hotkey_code = HOTKEY_CODES[stop_key]
        self.run_hotkey_name = run_key
        self.stop_hotkey_name = stop_key
        self.run_button.configure(text=f"Batch Blueprint ({run_key})")
        self.stop_button.configure(text=f"Dừng ({stop_key})")
        self.status.set(f"{run_key}: chạy · {stop_key}: dừng khẩn cấp")
        self._hotkeys_down.clear()
        if save:
            self._save_settings()
        return True

    def _hotkeys_changed(self, event: object = None) -> None:
        if self._apply_hotkeys():
            return
        widget = getattr(event, "widget", None)
        if widget is self.run_hotkey_box:
            self.run_hotkey.set(next(name for name, code in HOTKEY_CODES.items() if code == self.run_hotkey_code))
        else:
            self.stop_hotkey.set(next(name for name, code in HOTKEY_CODES.items() if code == self.stop_hotkey_code))

    def _speed_changed(self, _value: str = "") -> None:
        self.speed_input.set(f"{self.plan_speed.get():.1f}")

    def _threshold_changed(self, _value: str = "") -> None:
        self.threshold_input.set(f"{self.threshold.get():.2f}")

    def _apply_speed_input(self, _event: object = None) -> bool:
        try:
            value = float(self.speed_input.get().strip().replace(",", "."))
            if not 0.5 <= value <= 5.0:
                raise ValueError
        except ValueError:
            self.speed_input.set(f"{self.plan_speed.get():.1f}")
            self.status.set("Tốc độ plan phải nằm trong khoảng 0.5–5.0 giây.")
            return False
        value = round(value, 1)
        self.plan_speed.set(value)
        self.speed_input.set(f"{value:.1f}")
        self._save_settings()
        return True

    def _apply_threshold_input(self, _event: object = None) -> bool:
        try:
            value = float(self.threshold_input.get().strip().replace(",", "."))
            if not 0.58 <= value <= 0.92:
                raise ValueError
        except ValueError:
            self.threshold_input.set(f"{self.threshold.get():.2f}")
            self.status.set("Ngưỡng nhận dạng phải nằm trong khoảng 0.58–0.92.")
            return False
        value = round(value, 2)
        self.threshold.set(value)
        self.threshold_input.set(f"{value:.2f}")
        self._save_settings()
        return True

    def toggle_debug(self) -> None:
        if self.debug_enabled.get() == "1":
            self.body.pack(fill=BOTH, expand=True, pady=(8, 0))
            self.root.geometry("1180x780")
            self.root.minsize(900, 620)
            self.render()
        else:
            self.body.pack_forget()
            self.root.minsize(680, 300)
            self.root.geometry("780x340")
        self._save_settings()

    def refresh_processes(self) -> None:
        previous = self.process_choice.get()
        self.window_targets = enumerate_window_targets()
        labels = [item.label for item in self.window_targets]
        self.process_box["values"] = labels
        if previous in labels:
            self.process_choice.set(previous)
            return
        preferred = next(
            (item.label for item in self.window_targets if "pathofexile" in item.process_name.casefold()),
            next((item.label for item in self.window_targets if "path of exile" in item.title.casefold()), ""),
        )
        self.process_choice.set(preferred)

    def selected_target(self) -> WindowTarget:
        selected = self.process_choice.get()
        target = next((item for item in self.window_targets if item.label == selected), None)
        if target is None:
            raise RuntimeError("Hãy chọn process game ở danh sách phía trên.")
        return target

    def capture(self) -> Image.Image:
        target = self.selected_target()
        left, top, right, bottom = client_rect_on_screen(target.hwnd)
        self.capture_origin = (left, top)
        # all_screens=True cho phép bbox ở màn hình phụ/toạ độ âm, nhưng kết quả
        # vẫn chỉ là vùng client của process đã chọn.
        return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")

    def start_run(self) -> None:
        if self.busy:
            return
        if not self._apply_speed_input() or not self._apply_threshold_input():
            return
        if not messagebox.askokcancel(
            APP_TITLE,
            "Mở Planning Table và Inventory trước khi chạy. Tool sẽ quét 60 ô và chỉ "
            f"Ctrl+click các ô có Blueprint. {self.stop_hotkey.get()} để dừng.",
        ):
            return
        self.busy = True
        self.stop_event.clear()
        self.active_speed = round(float(self.plan_speed.get()), 1)
        self.active_threshold = float(self.threshold.get())
        threading.Thread(target=self._run_worker, daemon=True).start()

    @staticmethod
    def _click(x: int, y: int) -> None:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
        time.sleep(0.025)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.025)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    @classmethod
    def _ctrl_click(cls, x: int, y: int) -> None:
        user32 = ctypes.windll.user32
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        try:
            time.sleep(0.025)
            cls._click(x, y)
        finally:
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    @staticmethod
    def _popup_panel_visible(image: Image.Image) -> bool:
        """Nhận dạng panel chọn Rogue lớn; không nhầm với card đã gán trên bảng."""
        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 40, 120)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        height, width = gray.shape
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if (
                width * 0.20 < x < width * 0.65
                and height * 0.18 < y < height * 0.65
                and width * 0.20 < w < width * 0.45
                # Ở một số Blueprint, nét bản đồ chạm vào viền popup làm contour
                # cao hơn viền thật một chút (video kiểm thử: 40.4% chiều cao).
                and height * 0.16 < h < height * 0.46
            ):
                return True
        return False

    def _level_five_click_target(self, image: Image.Image) -> tuple[int, int, float] | None:
        """Tìm huy hiệu số 5 và trả về điểm click nằm thẳng phía trên nó."""
        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        x0, x1 = int(width * 0.28), int(width * 0.72)
        y0, y1 = int(height * 0.38), int(height * 0.68)
        roi = gray[y0:y1, x0:x1]
        base_scale = height / 1080.0
        best_score = -1.0
        best_center: tuple[int, int] | None = None
        for scale in base_scale * np.linspace(0.74, 1.34, 19):
            template = cv2.resize(
                self.level_five_template,
                None,
                fx=float(scale),
                fy=float(scale),
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
            )
            if template.shape[0] >= roi.shape[0] or template.shape[1] >= roi.shape[1]:
                continue
            result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            _minimum, maximum, _min_location, location = cv2.minMaxLoc(result)
            if maximum > best_score:
                best_score = float(maximum)
                best_center = (
                    x0 + location[0] + template.shape[1] // 2,
                    y0 + location[1] + template.shape[0] // 2,
                )
        if best_center is None or best_score < 0.72:
            return None
        badge_x, badge_y = best_center
        return badge_x, badge_y - round(height * 0.065), best_score

    def _wait_for_level_five(self, timeout: float) -> tuple[Image.Image | None, tuple[int, int, float] | None]:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline and not self.stop_event.is_set():
            image = self.capture()
            target = self._level_five_click_target(image)
            if target is not None and self._popup_panel_visible(image):
                return image, target
            time.sleep(0.025)
        return None, None

    def _focus_selected_game(self) -> None:
        target = self.selected_target()
        user32 = ctypes.windll.user32
        user32.ShowWindow(target.hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(target.hwnd)

    def _worker_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status.set(text))

    def _find_confirm_plans(self, image: Image.Image) -> tuple[int, int, float] | None:
        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        x0, x1 = int(width * 0.28), int(width * 0.72)
        y0, y1 = int(height * 0.78), int(height * 0.98)
        roi = gray[y0:y1, x0:x1]
        best_score = -1.0
        best_point: tuple[int, int] | None = None
        for scale in (height / 1080.0) * np.linspace(0.75, 1.35, 19):
            template = cv2.resize(
                self.confirm_template,
                None,
                fx=float(scale),
                fy=float(scale),
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
            )
            if template.shape[0] >= roi.shape[0] or template.shape[1] >= roi.shape[1]:
                continue
            result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            _minimum, maximum, _min_location, location = cv2.minMaxLoc(result)
            if maximum > best_score:
                best_score = float(maximum)
                best_point = (
                    x0 + location[0] + template.shape[1] // 2,
                    y0 + location[1] + template.shape[0] // 2,
                )
        if best_point is None or best_score < 0.70:
            return None
        return best_point[0], best_point[1], best_score

    def _wait_for_confirm(self, timeout: float) -> tuple[Image.Image | None, tuple[int, int, float] | None]:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline and not self.stop_event.is_set():
            image = self.capture()
            confirm = self._find_confirm_plans(image)
            if confirm is not None:
                return image, confirm
            time.sleep(0.035)
        return None, None

    @staticmethod
    def _inventory_slot_point(width: int, height: int, index: int) -> tuple[int, int]:
        row, column = divmod(index, 12)
        # Inventory 12×5 trong vùng client 1920×1080, chuẩn hóa để hỗ trợ
        # các độ phân giải khác: grid x=66.2–99.3%, y=54.3–78.8%.
        x = width * (0.662 + (column + 0.5) * 0.0276)
        y = height * (0.543 + (row + 0.5) * 0.0490)
        return round(x), round(y)

    def _detect_inventory_blueprints(self, image: Image.Image) -> list[tuple[int, float]]:
        """Quét lưới 12×5 và trả về (slot index, confidence) của Blueprint."""
        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        half_cell_width = round(width * 0.0140)
        half_cell_height = round(height * 0.0245)
        base_scale = height / 1080.0
        matches: list[tuple[int, float]] = []

        for index in range(60):
            center_x, center_y = self._inventory_slot_point(width, height, index)
            cell = gray[
                max(0, center_y - half_cell_height):min(height, center_y + half_cell_height),
                max(0, center_x - half_cell_width):min(width, center_x + half_cell_width),
            ]
            best_score = -1.0
            for scale in base_scale * np.linspace(0.65, 1.20, 12):
                template = cv2.resize(
                    self.inventory_blueprint_template,
                    None,
                    fx=float(scale),
                    fy=float(scale),
                    interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
                )
                if template.shape[0] > cell.shape[0] or template.shape[1] > cell.shape[1]:
                    continue
                result = cv2.matchTemplate(cell, template, cv2.TM_CCOEFF_NORMED)
                best_score = max(best_score, float(cv2.minMaxLoc(result)[1]))
            if best_score >= 0.62:
                matches.append((index, best_score))
        return matches

    def _plan_current_blueprint(self, blueprint_number: int) -> list[dict]:
        """Module plan hiện tại: giữ detector thẻ + số 5, không chờ popup đóng."""
        actions: list[dict] = []
        initial = self.capture()
        found = self.detector.scan(initial, self.active_threshold)
        total_targets = min(len(found), 20)
        for index, target in enumerate(found[:20], 1):
            if self.stop_event.is_set():
                break
            item_started = time.perf_counter()
            self._worker_status(
                f"Blueprint {blueprint_number}: thẻ {index}/{total_targets} — {target.name}"
            )
            tx, ty = target.center
            origin_x, origin_y = self.capture_origin
            self._click(tx + origin_x, ty + origin_y)

            popup, level_five = self._wait_for_level_five(0.45)
            if popup is None or level_five is None:
                self._click(tx + origin_x, ty + origin_y)
                popup, level_five = self._wait_for_level_five(0.35)

            action = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "blueprint": blueprint_number,
                "equipment": target.name,
                "score": round(target.score, 4),
                "equipment_point_game": [tx, ty],
                "success": False,
            }
            if level_five is not None:
                time.sleep(0.18)
                stable = self._level_five_click_target(self.capture())
                if stable is not None:
                    level_five = stable
                rogue_x, rogue_y, level_five_score = level_five
                origin_x, origin_y = self.capture_origin
                self._click(rogue_x + origin_x, rogue_y + origin_y)
                action.update({
                    "success": True,
                    "rogue_point_game": [rogue_x, rogue_y],
                    "level_five_score": round(level_five_score, 4),
                })
            else:
                action["error"] = "Không tìm thấy số 5; tiếp tục thẻ kế tiếp."

            elapsed = time.perf_counter() - item_started
            if elapsed < self.active_speed:
                time.sleep(self.active_speed - elapsed)
            action["duration_seconds"] = round(time.perf_counter() - item_started, 3)
            actions.append(action)
        return actions

    def _confirm_twice_or_extract(self, blueprint_number: int) -> bool:
        """Thử Confirm tối đa 2 lần, sau đó luôn Ctrl+click lấy Blueprint ra."""
        success = False
        last_point: tuple[int, int] | None = None
        for attempt in range(1, 3):
            if self.stop_event.is_set():
                return False
            image = self.capture()
            height, width = image.height, image.width
            confirm = self._find_confirm_plans(image)
            if confirm is None:
                success = True
                break
            cx, cy, score = confirm
            last_point = (cx, cy)
            self._worker_status(
                f"Blueprint {blueprint_number}: Confirm Plans lần {attempt}/2 ({score:.0%})"
            )
            origin_x, origin_y = self.capture_origin
            self._click(cx + origin_x, cy + origin_y)
            time.sleep(max(0.25, min(0.60, self.active_speed * 0.25)))
            if self._find_confirm_plans(self.capture()) is None:
                success = True
                break

        # Blueprint vẫn nằm phía trên nút sau khi Confirm. Luôn lấy nó ra để
        # Planning Table trở về trạng thái sẵn sàng cho item inventory kế tiếp.
        image = self.capture()
        height, width = image.height, image.width
        current_confirm = self._find_confirm_plans(image)
        if current_confirm is not None:
            last_point = (current_confirm[0], current_confirm[1])
        cx, cy = last_point or (round(width * 0.5), round(height * 0.927))
        blueprint_y = cy - round(height * 0.065)
        origin_x, origin_y = self.capture_origin
        outcome = "thành công" if success else "không xác nhận được"
        self._worker_status(
            f"Blueprint {blueprint_number}: Confirm {outcome}; Ctrl+click lấy Blueprint ra."
        )
        self._ctrl_click(cx + origin_x, blueprint_y + origin_y)
        time.sleep(max(0.30, min(0.70, self.active_speed * 0.30)))
        return success

    def _run_worker(self) -> None:
        actions: list[dict] = []
        message = ""
        blueprint_count = 0
        try:
            self._focus_selected_game()
            time.sleep(0.20)

            # Nếu hotkey chạy được bấm khi một Blueprint đã mở, xử lý nó trước.
            current = self.capture()
            if self._find_confirm_plans(current) is not None:
                blueprint_count += 1
                actions.extend(self._plan_current_blueprint(blueprint_count))
                self._confirm_twice_or_extract(blueprint_count)

            # Planning Table + Inventory: quét một lần và chỉ giữ ô Blueprint.
            inventory_image = self.capture()
            blueprint_slots = self._detect_inventory_blueprints(inventory_image)
            if blueprint_slots:
                slot_list = ", ".join(str(index + 1) for index, _score in blueprint_slots)
                self._worker_status(
                    f"Tìm thấy {len(blueprint_slots)} Blueprint tại ô: {slot_list}"
                )
            else:
                self._worker_status("Không tìm thấy Blueprint trong inventory 12×5.")

            for batch_index, (slot_index, inventory_score) in enumerate(blueprint_slots, 1):
                if self.stop_event.is_set():
                    message = f"Đã dừng bằng {self.stop_hotkey_name}."
                    break
                image = self.capture()
                slot_x, slot_y = self._inventory_slot_point(image.width, image.height, slot_index)
                origin_x, origin_y = self.capture_origin
                row, column = divmod(slot_index, 12)
                self._worker_status(
                    f"Blueprint {batch_index}/{len(blueprint_slots)} — ô {slot_index + 1} "
                    f"(hàng {row + 1}, cột {column + 1}, {inventory_score:.0%})"
                )
                self._ctrl_click(slot_x + origin_x, slot_y + origin_y)

                # Ô trống không đổi giao diện; Blueprint sẽ mở màn có Confirm.
                _plan_image, confirm = self._wait_for_confirm(0.45)
                if confirm is None:
                    continue

                blueprint_count += 1
                actions.extend(self._plan_current_blueprint(blueprint_count))
                self._confirm_twice_or_extract(blueprint_count)
                time.sleep(max(0.15, min(0.45, self.active_speed * 0.15)))
            else:
                message = (
                    f"Quét 60 ô, tìm thấy {len(blueprint_slots)} và đã xử lý "
                    f"{blueprint_count} Blueprint."
                )

            if not message:
                message = f"Đã xử lý {blueprint_count} Blueprint."
        except Exception as error:
            message = f"Lỗi: {error}"
        finally:
            self._write_log(actions, message)
            self.root.after(0, lambda: self._finish_run(message, actions))

    @staticmethod
    def _write_log(actions: list[dict], message: str) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps({"result": message, "actions": actions}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _finish_run(self, message: str, actions: list[dict]) -> None:
        self.busy = False
        self.root.deiconify()
        self.root.lift()
        self.status.set(message)
        details = "\n".join(f"{i}. {item['equipment']} ({item['score']:.1%})" for i, item in enumerate(actions, 1))
        self.summary.set(f"{message}\n\nĐã xử lý {len(actions)} mục." + (f"\n\n{details}" if details else ""))

    def stop(self) -> None:
        self.stop_event.set()
        self.status.set("Đã gửi lệnh dừng; tool sẽ dừng trước click kế tiếp.")

    def _hotkey_loop(self) -> None:
        while True:
            hotkeys = (
                (self.run_hotkey_code, self.start_run),
                (self.stop_hotkey_code, self.stop),
            )
            for key, callback in hotkeys:
                down = bool(ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000)
                if down and key not in self._hotkeys_down:
                    self._hotkeys_down.add(key)
                    self.root.after(0, callback)
                elif not down:
                    self._hotkeys_down.discard(key)
            time.sleep(0.05)

    def _error(self, message: str) -> None:
        self.busy = False
        self.root.deiconify()
        self.status.set("Có lỗi.")
        messagebox.showerror(APP_TITLE, message)

    def render(self) -> None:
        if self.image is None or self.canvas.winfo_width() < 20:
            return
        image = self.image.copy()
        draw = ImageDraw.Draw(image)
        for index, detection in enumerate(self.detections, 1):
            x1, y1, x2, y2 = detection.box
            width = max(2, round(image.height / 450))
            draw.rectangle((x1, y1, x2, y2), outline=(40, 255, 80), width=width)
            label = f"{index} {detection.name} {detection.score:.0%}"
            draw.rectangle((x1, max(0, y1 - 25), x1 + max(155, len(label) * 8), y1), fill=(0, 0, 0))
            draw.text((x1 + 4, max(0, y1 - 22)), label, fill=(80, 255, 110))
        image.thumbnail((self.canvas.winfo_width(), self.canvas.winfo_height()))
        self.preview = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2, image=self.preview)


if __name__ == "__main__":
    root = Tk()
    try:
        BlueprintAssigner(root)
    except Exception as exc:
        messagebox.showerror(APP_TITLE, str(exc))
        raise
    root.mainloop()
