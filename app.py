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

cv2.setUseOptimized(True)
cv2.setNumThreads(max(1, min(4, os.cpu_count() or 1)))


APP_VERSION = "0.2.0"
APP_TITLE = f"PoE Blueprint Rogue Assigner v{APP_VERSION}"
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ASSET_DIR = BUNDLE_DIR / "assets" / "equipment"
LOG_DIR = APP_DIR / "logs"
SETTINGS_PATH = APP_DIR / "settings.json"
REFERENCE_HEIGHT = 1368
DETECTION_SCALE = 0.58
TEMPLATE_SCALE_FACTORS = (0.62, 0.69, 0.82, 0.89, 0.95, 1.09, 1.15, 1.29, 1.42)
PLANNING_ZOOM_FACTOR_PER_STEP = 1.135
HOTKEY_CODES = {f"F{number}": 0x6F + number for number in range(1, 13)}
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_WHEEL = 0x0002, 0x0004, 0x0800
WHEEL_DELTA = 120
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

TEMPLATES = {
    "brute_force.png": "Brute Force",
    "counter_thaumaturgy.png": "Counter-Thaumaturgy",
    "deception.png": "Deception",
    "perception_l1.png": "Perception L1",
    "perception_l3.png": "Perception L3",
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
            for scale_factor in TEMPLATE_SCALE_FACTORS:
                scale = base_scale * scale_factor
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

    @staticmethod
    def _has_equipment_card_top_edge(gray: np.ndarray, detection: Detection) -> bool:
        """Loại icon bản đồ rời bằng cạnh trên tương phản của thẻ giấy."""
        x1, y1, x2, _y2 = detection.box
        band = max(2, round(min(detection.width, detection.height) * 0.08))
        if y1 < band or x1 < 0 or x2 > gray.shape[1]:
            return False
        inside = gray[y1:y1 + band, x1:x2].astype(np.float32)
        outside = gray[y1 - band:y1, x1:x2].astype(np.float32)
        if inside.shape != outside.shape or inside.size == 0:
            return False
        return float(np.mean(np.abs(inside - outside))) >= 10.0

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
                candidate = Detection(
                    name,
                    float(result[py, px]),
                    round(px / DETECTION_SCALE) + x0,
                    round(py / DETECTION_SCALE) + y0,
                    round(tw / DETECTION_SCALE),
                    round(th / DETECTION_SCALE),
                )
                if self._has_equipment_card_top_edge(gray, candidate):
                    candidates.append(candidate)

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
        self.root.geometry("620x300")
        self.root.minsize(480, 260)
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
        self.confirm_ready_template = cv2.imread(
            str(BUNDLE_DIR / "assets" / "confirm_plans_ready.png"),
            cv2.IMREAD_GRAYSCALE,
        )
        if self.confirm_ready_template is None:
            raise FileNotFoundError("Không đọc được assets/confirm_plans_ready.png")
        self.inventory_blueprint_template = cv2.imread(
            str(BUNDLE_DIR / "assets" / "inventory_blueprint.png"),
            cv2.IMREAD_GRAYSCALE,
        )
        if self.inventory_blueprint_template is None:
            raise FileNotFoundError("Không đọc được assets/inventory_blueprint.png")
        self.threshold = DoubleVar(value=0.68)
        self.plan_speed = DoubleVar(value=1.6)
        self.speed_input = StringVar(value="1.6")
        self.threshold_input = StringVar(value="0.68")
        self.zoom_steps_input = StringVar(value="3")
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
        self.active_threshold = 0.68
        self.active_zoom_steps = 3
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
        self.notebook.pack(fill=BOTH, expand=True, padx=6, pady=(6, 3))
        self.planning_tab = Frame(self.notebook, padx=8, pady=8)
        self.reveal_tab = Frame(self.notebook, padx=10, pady=10)
        self.settings_tab = Frame(self.notebook, padx=8, pady=8)
        self.notebook.add(self.planning_tab, text="Planning Heist")
        self.notebook.add(self.reveal_tab, text="Reveal Room")
        self.notebook.add(self.settings_tab, text="Cài đặt")

        action_row = Frame(self.planning_tab)
        action_row.pack(fill="x")
        self.run_button = Button(action_row, text="Batch Blueprint (F6)", width=19, command=self.start_run)
        self.run_button.pack(side=LEFT, padx=(0, 6))
        self.stop_button = Button(action_row, text="Dừng (F8)", width=11, command=self.stop)
        self.stop_button.pack(side=LEFT)
        Label(
            self.planning_tab,
            textvariable=self.summary,
            justify=LEFT,
            anchor="nw",
            wraplength=560,
        ).pack(fill="x", pady=(8, 4))
        Label(
            self.planning_tab,
            text="Mở Planning Table + Inventory, sau đó chạy batch. Tool chỉ Ctrl+click các ô được nhận dạng là Blueprint.",
            justify=LEFT,
            anchor="w",
            fg="#444",
            wraplength=560,
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
        process_row.pack(fill="x", pady=(0, 8))
        Label(process_row, text="Process game:", width=13, anchor="w").pack(side=LEFT)
        self.process_box = ttk.Combobox(process_row, textvariable=self.process_choice, state="readonly")
        self.process_box.pack(side=LEFT, fill="x", expand=True, padx=(0, 8))
        self.process_box.bind("<<ComboboxSelected>>", lambda _event: self._save_settings())
        Button(process_row, text="Làm mới", width=8, command=self.refresh_processes).pack(side=RIGHT)

        settings_row = Frame(self.settings_tab)
        settings_row.pack(fill="x", pady=(0, 8))
        Label(settings_row, text="Tốc độ plan:", width=13, anchor="w").pack(side=LEFT)
        ttk.Scale(
            settings_row,
            from_=0.5,
            to=5.0,
            variable=self.plan_speed,
            length=150,
            command=self._speed_changed,
        ).pack(side=LEFT, padx=8)
        self.speed_entry = ttk.Entry(settings_row, textvariable=self.speed_input, width=6, justify="center")
        self.speed_entry.pack(side=LEFT)
        self.speed_entry.bind("<Return>", self._apply_speed_input)
        self.speed_entry.bind("<FocusOut>", self._apply_speed_input)
        Label(settings_row, text="giây").pack(side=LEFT, padx=(4, 0))

        threshold_row = Frame(self.settings_tab)
        threshold_row.pack(fill="x", pady=(0, 8))
        Label(threshold_row, text="Ngưỡng:", width=13, anchor="w").pack(side=LEFT)
        ttk.Scale(
            threshold_row,
            from_=0.58,
            to=0.92,
            variable=self.threshold,
            length=150,
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

        zoom_row = Frame(self.settings_tab)
        zoom_row.pack(fill="x", pady=(0, 8))
        Label(zoom_row, text="Nấc zoom:", width=13, anchor="w").pack(side=LEFT)
        self.zoom_steps_entry = ttk.Spinbox(
            zoom_row,
            from_=1,
            to=6,
            textvariable=self.zoom_steps_input,
            width=6,
            justify="center",
        )
        self.zoom_steps_entry.pack(side=LEFT, padx=8)
        self.zoom_steps_entry.bind("<Return>", self._apply_zoom_steps_input)
        self.zoom_steps_entry.bind("<FocusOut>", self._apply_zoom_steps_input)
        Label(zoom_row, text="cuộn lên/xuống mỗi vùng").pack(side=LEFT)

        hotkey_row = Frame(self.settings_tab)
        hotkey_row.pack(fill="x", pady=(0, 8))
        Label(hotkey_row, text="Hotkey chạy:", width=13, anchor="w").pack(side=LEFT)
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
            text="Hiện Debug trong tab Planning Heist",
            variable=self.debug_enabled,
            onvalue="1",
            offvalue="0",
            command=self.toggle_debug,
        ).pack(anchor="w")

        self.body = Frame(self.planning_tab, pady=8)
        self.canvas = Canvas(self.body, bg="#111318", highlightthickness=0)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.render())

        panel = Frame(self.body, width=220, padx=10, pady=6)
        panel.pack(side=RIGHT, fill="y")
        panel.pack_propagate(False)
        Label(panel, text="KẾT QUẢ", font=("Segoe UI", 14, "bold"), anchor="w").pack(fill="x")
        Label(panel, textvariable=self.summary, justify=LEFT, anchor="nw", wraplength=200).pack(fill=BOTH, expand=True, pady=8)
        self.status_label = Label(self.root, textvariable=self.status, anchor="w", padx=12, pady=8, relief="sunken")
        self.status_label.pack(fill="x")

    def _load_settings(self) -> None:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        speed = data.get("plan_speed", 1.6)
        threshold = data.get("threshold", 0.68)
        zoom_steps = data.get("zoom_steps", 3)
        if isinstance(speed, (int, float)) and 0.5 <= float(speed) <= 5.0:
            self.plan_speed.set(round(float(speed), 1))
            self.speed_input.set(f"{float(speed):.1f}")
        if isinstance(threshold, (int, float)) and 0.58 <= float(threshold) <= 0.92:
            self.threshold.set(round(float(threshold), 2))
            self.threshold_input.set(f"{float(threshold):.2f}")
        if isinstance(zoom_steps, int) and 1 <= zoom_steps <= 6:
            self.zoom_steps_input.set(str(zoom_steps))
            self.active_zoom_steps = zoom_steps
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
        try:
            zoom_steps = int(self.zoom_steps_input.get())
        except ValueError:
            zoom_steps = self.active_zoom_steps
        data = {
            "plan_speed": round(float(self.plan_speed.get()), 1),
            "threshold": round(float(self.threshold.get()), 2),
            "zoom_steps": zoom_steps,
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

    def _apply_zoom_steps_input(self, _event: object = None) -> bool:
        try:
            value = int(self.zoom_steps_input.get().strip())
            if not 1 <= value <= 6:
                raise ValueError
        except ValueError:
            self.zoom_steps_input.set(str(self.active_zoom_steps))
            self.status.set("Số nấc zoom phải là số nguyên trong khoảng 1–6.")
            return False
        self.zoom_steps_input.set(str(value))
        self._save_settings()
        return True

    def toggle_debug(self) -> None:
        if self.debug_enabled.get() == "1":
            self.body.pack(fill=BOTH, expand=True, pady=(8, 0))
            width = min(1040, max(640, self.root.winfo_screenwidth() - 80))
            height = min(720, max(460, self.root.winfo_screenheight() - 120))
            self.root.geometry(f"{width}x{height}")
            self.root.minsize(560, 420)
            self.render()
        else:
            self.body.pack_forget()
            self.root.minsize(480, 260)
            self.root.geometry("620x300")
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
        if (
            not self._apply_speed_input()
            or not self._apply_threshold_input()
            or not self._apply_zoom_steps_input()
        ):
            return
        self.busy = True
        self.stop_event.clear()
        self.active_speed = round(float(self.plan_speed.get()), 1)
        self.active_threshold = float(self.threshold.get())
        self.active_zoom_steps = int(self.zoom_steps_input.get())
        threading.Thread(target=self._run_worker, daemon=True).start()

    @staticmethod
    def _click(x: int, y: int) -> None:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
        time.sleep(0.025)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.025)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    @staticmethod
    def _mouse_wheel_at(x: int, y: int, steps: int) -> None:
        """Cuộn thật trong game tại một điểm; steps dương zoom vào, âm zoom ra."""
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        delta = WHEEL_DELTA if steps > 0 else -WHEEL_DELTA
        for _ in range(abs(steps)):
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
            time.sleep(0.04)

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

    @staticmethod
    def _first_rogue_card_target(image: Image.Image) -> tuple[int, int] | None:
        """Tìm tâm portrait Rogue đầu tiên bên trái khi template số 5 không ổn định."""
        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        edges = cv2.Canny(gray, 40, 120)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        cards: list[tuple[int, int, int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if (
                width * 0.36 < x < width * 0.66
                and height * 0.32 < y < height * 0.50
                and width * 0.045 < w < width * 0.085
                and height * 0.12 < h < height * 0.19
            ):
                cards.append((x, y, w, h))
        if not cards:
            return None

        # Canny thường tạo 2–3 contour lồng nhau cho cùng một card. Gom theo
        # tâm X và giữ contour lớn nhất để lấy tọa độ portrait ổn định.
        groups: list[list[tuple[int, int, int, int]]] = []
        for card in sorted(cards, key=lambda item: item[0] + item[2] / 2):
            center_x = card[0] + card[2] / 2
            group = next(
                (
                    existing
                    for existing in groups
                    if abs(center_x - (existing[0][0] + existing[0][2] / 2)) < width * 0.018
                ),
                None,
            )
            if group is None:
                groups.append([card])
            else:
                group.append(card)
        # Popup luôn có ít nhất hai Rogue. Một contour đơn lẻ thường là phòng/ô
        # trên bản đồ Planning, không được phép dùng làm điểm click.
        if len(groups) < 2:
            return None
        leftmost = min(
            (max(group, key=lambda item: item[2] * item[3]) for group in groups),
            key=lambda item: item[0] + item[2] / 2,
        )
        x, y, w, h = leftmost
        return x + w // 2, y + round(h * 0.45)

    def _rogue_choice_target(
        self,
        image: Image.Image,
    ) -> tuple[int, int, float | None, str] | None:
        # Khung card Rogue là dấu hiệu popup đáng tin cậy hơn đường viền panel:
        # đường viền của bản đồ Planning đôi khi có hình học tương tự panel popup.
        first_card = self._first_rogue_card_target(image)
        if first_card is None:
            return None
        level_five = self._level_five_click_target(image)
        if level_five is not None:
            return level_five[0], level_five[1], level_five[2], "level_5"
        return first_card[0], first_card[1], None, "leftmost_card"

    def _popup_present(self, image: Image.Image) -> bool:
        # Không dùng riêng detector panel ở đây: nó có false-positive trên chính
        # giao diện Planning và khiến tool tưởng popup chưa đóng sau khi chọn Rogue.
        return self._first_rogue_card_target(image) is not None

    def _wait_for_rogue_choice(
        self,
        timeout: float,
    ) -> tuple[tuple[int, int, float | None, str] | None, bool]:
        deadline = time.perf_counter() + timeout
        popup_seen = False
        while time.perf_counter() < deadline and not self.stop_event.is_set():
            image = self.capture()
            popup_seen = popup_seen or self._popup_present(image)
            target = self._rogue_choice_target(image)
            if target is not None:
                return target, True
            time.sleep(0.035)
        return None, popup_seen

    def _wait_for_popup_closed(self, timeout: float) -> bool:
        deadline = time.perf_counter() + timeout
        consecutive_closed = 0
        while time.perf_counter() < deadline and not self.stop_event.is_set():
            if self._popup_present(self.capture()):
                consecutive_closed = 0
            else:
                consecutive_closed += 1
                if consecutive_closed >= 3:
                    return True
            time.sleep(0.04)
        return False

    def _select_open_rogue_popup(
        self,
        initial_target: tuple[int, int, float | None, str] | None,
    ) -> tuple[bool, tuple[int, int, float | None, str] | None, int]:
        """Chỉ retry Rogue và xác nhận popup đóng; không bao giờ click lại equipment."""
        deadline = time.perf_counter() + max(2.5, min(5.0, self.active_speed * 2.5))
        target = initial_target
        attempts = 0
        y_offsets = (0.0, -0.012, 0.012, 0.0, -0.02, 0.02)
        last_target = target

        while attempts < len(y_offsets) and time.perf_counter() < deadline and not self.stop_event.is_set():
            if target is None:
                target, popup_seen = self._wait_for_rogue_choice(0.45)
                if target is None:
                    if not popup_seen:
                        return True, last_target, attempts
                    continue
            rogue_x, rogue_y, score, method = target
            image = self.capture()
            adjusted_y = rogue_y + round(image.height * y_offsets[attempts])
            origin_x, origin_y = self.capture_origin
            self._click(rogue_x + origin_x, adjusted_y + origin_y)
            attempts += 1
            last_target = (rogue_x, adjusted_y, score, method)
            close_timeout = max(1.0, min(1.8, self.active_speed * 1.5))
            if self._wait_for_popup_closed(close_timeout):
                return True, last_target, attempts
            target = self._rogue_choice_target(self.capture())

        return False, last_target, attempts

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

    def _find_ready_confirm_plans(self, image: Image.Image) -> tuple[int, int, float] | None:
        """Chỉ nhận nút Confirm đang sáng, không dùng thay detector Planning Table."""
        # Không bao giờ chuyển sang Confirm khi popup chọn Rogue còn che bản đồ.
        if self._popup_present(image):
            return None
        rgb = np.asarray(image.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        x0, x1 = int(width * 0.28), int(width * 0.72)
        y0, y1 = int(height * 0.78), int(height * 0.98)
        roi = gray[y0:y1, x0:x1]
        best_score = -1.0
        best_box: tuple[int, int, int, int] | None = None

        for scale in (height / 1080.0) * np.linspace(0.75, 1.35, 25):
            template = cv2.resize(
                self.confirm_ready_template,
                None,
                fx=float(scale),
                fy=float(scale),
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
            )
            template_height, template_width = template.shape
            if template_height >= roi.shape[0] or template_width >= roi.shape[1]:
                continue
            result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            _minimum, maximum, _min_location, location = cv2.minMaxLoc(result)
            if maximum > best_score:
                best_score = float(maximum)
                best_box = (
                    x0 + location[0],
                    y0 + location[1],
                    template_width,
                    template_height,
                )

        if best_box is None or best_score < 0.80:
            return None

        button_x, button_y, button_width, button_height = best_box
        # Nút sẵn sàng có dải đỏ sáng rõ ở nửa dưới. Kiểm tra màu giúp phân
        # biệt với nút tối dù chữ và khung của hai trạng thái gần giống nhau.
        lower_half = rgb[
            button_y + button_height // 2:button_y + button_height,
            button_x:button_x + button_width,
        ]
        if lower_half.size == 0:
            return None
        red = lower_half[:, :, 0].astype(np.float32)
        green = lower_half[:, :, 1].astype(np.float32)
        blue = lower_half[:, :, 2].astype(np.float32)
        red_glow = (red > 90) & (red > green * 1.25) & (red > blue * 1.15)
        if float(np.mean(red_glow)) < 0.025:
            return None

        return (
            button_x + button_width // 2,
            button_y + button_height // 2,
            best_score,
        )

    @staticmethod
    def _inventory_slot_point(width: int, height: int, index: int) -> tuple[int, int]:
        row, column = divmod(index, 12)
        # Grid inventory có kích thước theo chiều cao UI và neo vào cạnh phải.
        # Công thức này giữ đúng cả client 1920×1080 lẫn 800×600.
        cell_size = height * 0.0490
        right_margin = height * 0.0367
        x = width - right_margin - (11 - column) * cell_size
        y = height * (0.543 + (row + 0.5) * 0.0490)
        return round(x), round(y)

    def _detect_inventory_blueprints(self, image: Image.Image) -> list[tuple[int, float]]:
        """Quét lưới 12×5 và trả về (slot index, confidence) của Blueprint."""
        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        half_cell_width = round(height * 0.0245)
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

    @staticmethod
    def _planning_zoom_centers(width: int, height: int) -> list[tuple[int, int]]:
        """Tâm lưới 3×3, đi theo cột để luôn ưu tiên từ trái sang phải."""
        left_x = max(width * 0.20, height * 0.34)
        right_x = min(width * 0.80, width - height * 0.25)
        x_positions = (left_x, width * 0.50, right_x)
        y_positions = (height * 0.12, height * 0.36, height * 0.60)
        return [
            (round(x), round(y))
            for x in x_positions
            for y in y_positions
        ]

    def _overview_equipment_point(
        self,
        zoom_center: tuple[int, int],
        target: Detection,
    ) -> tuple[float, float]:
        """Quy đổi tâm thẻ trong ảnh zoom về tọa độ Planning Table trước khi zoom."""
        scale = PLANNING_ZOOM_FACTOR_PER_STEP ** self.active_zoom_steps
        center_x, center_y = zoom_center
        target_x, target_y = target.center
        return (
            center_x + (target_x - center_x) / scale,
            center_y + (target_y - center_y) / scale,
        )

    @staticmethod
    def _equipment_point_already_processed(
        point: tuple[float, float],
        processed_points: list[tuple[float, float]],
        width: int,
        height: int,
    ) -> bool:
        # Sai số sau phép cuộn/hoàn nguyên chỉ vài pixel. Giới hạn này nhỏ hơn
        # khoảng cách giữa hai thẻ liền nhau nên không làm mất thẻ thật.
        tolerance_x = width * 0.012
        tolerance_y = height * 0.018
        return any(
            ((point[0] - old_x) / tolerance_x) ** 2
            + ((point[1] - old_y) / tolerance_y) ** 2
            <= 1.0
            for old_x, old_y in processed_points
        )

    def _assign_equipment_target(
        self,
        blueprint_number: int,
        region_number: int,
        index: int,
        total_targets: int,
        target: Detection,
    ) -> tuple[dict, bool, tuple[int, int, float] | None]:
        item_started = time.perf_counter()
        self._worker_status(
            f"Blueprint {blueprint_number}: vùng {region_number}/9 · "
            f"thẻ {index}/{total_targets} — {target.name}"
        )
        tx, ty = target.center
        origin_x, origin_y = self.capture_origin
        self._click(tx + origin_x, ty + origin_y)

        equipment_clicks = 1
        popup_wait = max(1.25, min(2.20, self.active_speed))
        rogue_target, popup_seen = self._wait_for_rogue_choice(popup_wait)
        # Chỉ click equipment lần hai khi chắc chắn popup chưa từng xuất hiện.
        # Nếu popup đã mở nhưng detector đang chuyển frame, chỉ tiếp tục chờ.
        if rogue_target is None and not popup_seen:
            self._click(tx + origin_x, ty + origin_y)
            equipment_clicks += 1
            rogue_target, popup_seen = self._wait_for_rogue_choice(popup_wait)
        elif rogue_target is None:
            rogue_target, popup_seen_late = self._wait_for_rogue_choice(0.90)
            popup_seen = popup_seen or popup_seen_late

        rogue_selected = False
        selection_attempts = 0
        selected_target: tuple[int, int, float | None, str] | None = None
        if rogue_target is not None or popup_seen:
            rogue_selected, selected_target, selection_attempts = self._select_open_rogue_popup(
                rogue_target
            )

        action = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "blueprint": blueprint_number,
            "zoom_region": region_number,
            "equipment": target.name,
            "score": round(target.score, 4),
            "equipment_point_game": [tx, ty],
            "equipment_clicks": equipment_clicks,
            "rogue_selection_attempts": selection_attempts,
            "success": rogue_selected,
        }
        if rogue_selected and selected_target is not None:
            rogue_x, rogue_y, level_five_score, selection_method = selected_target
            action["rogue_point_game"] = [rogue_x, rogue_y]
            action["rogue_selection_method"] = selection_method
            if level_five_score is not None:
                action["level_five_score"] = round(level_five_score, 4)
        elif not popup_seen:
            action["error"] = "Equipment không mở popup Rogue sau 2 lần click."
        else:
            action["error"] = (
                "Popup Rogue vẫn còn mở sau các lần retry; không click thẻ khác để tránh lệch."
            )

        elapsed = time.perf_counter() - item_started
        if elapsed < self.active_speed:
            time.sleep(self.active_speed - elapsed)
        action["duration_seconds"] = round(time.perf_counter() - item_started, 3)
        final_image = self.capture()
        popup_clear = not self._popup_present(final_image)
        confirm_ready = self._find_ready_confirm_plans(final_image) if popup_clear else None
        return action, popup_clear, confirm_ready

    def _plan_current_blueprint(self, blueprint_number: int) -> list[dict]:
        """Zoom thật lần lượt 9 vùng, quét và chọn thẻ trong từng góc nhìn."""
        actions: list[dict] = []
        overview = self.capture()
        centers = self._planning_zoom_centers(overview.width, overview.height)
        popup_blocked = False
        ready = self._find_ready_confirm_plans(overview)
        confirm_ready = ready is not None
        processed_points: list[tuple[float, float]] = []
        if confirm_ready:
            self._worker_status(
                f"Blueprint {blueprint_number}: Confirm Plans đã sáng; bỏ qua quét 9 vùng."
            )
            return actions

        for region_number, (center_x, center_y) in enumerate(centers, 1):
            if self.stop_event.is_set() or len(actions) >= 20:
                break
            origin_x, origin_y = self.capture_origin
            self._worker_status(
                f"Blueprint {blueprint_number}: zoom vùng {region_number}/9 "
                f"({self.active_zoom_steps} nấc)"
            )
            self._mouse_wheel_at(
                center_x + origin_x,
                center_y + origin_y,
                self.active_zoom_steps,
            )
            time.sleep(max(0.16, min(0.40, self.active_speed * 0.18)))
            try:
                zoomed = self.capture()
                found = self.detector.scan(zoomed, self.active_threshold)
                remaining = 20 - len(actions)
                targets: list[tuple[Detection, tuple[float, float]]] = []
                for target in found:
                    overview_point = self._overview_equipment_point(
                        (center_x, center_y),
                        target,
                    )
                    if self._equipment_point_already_processed(
                        overview_point,
                        processed_points,
                        overview.width,
                        overview.height,
                    ):
                        continue
                    targets.append((target, overview_point))
                    if len(targets) >= remaining:
                        break

                for index, (target, overview_point) in enumerate(targets, 1):
                    if self.stop_event.is_set():
                        break
                    action, popup_clear, ready = self._assign_equipment_target(
                        blueprint_number,
                        region_number,
                        index,
                        len(targets),
                        target,
                    )
                    action["overview_point_game"] = [
                        round(overview_point[0], 1),
                        round(overview_point[1], 1),
                    ]
                    actions.append(action)
                    if action.get("success"):
                        processed_points.append(overview_point)
                    if not popup_clear:
                        popup_blocked = True
                        break
                    if ready is not None:
                        action["confirm_ready"] = True
                        action["confirm_ready_score"] = round(ready[2], 4)
                        confirm_ready = True
                        self._worker_status(
                            f"Blueprint {blueprint_number}: Confirm Plans đã sáng; "
                            "dừng tìm thẻ để xác nhận."
                        )
                        break
            finally:
                # Luôn trả lại đúng mức zoom ban đầu trước khi sang vùng khác,
                # kể cả khi người dùng nhấn hotkey dừng giữa vùng.
                origin_x, origin_y = self.capture_origin
                self._mouse_wheel_at(
                    center_x + origin_x,
                    center_y + origin_y,
                    -self.active_zoom_steps,
                )
                time.sleep(max(0.12, min(0.30, self.active_speed * 0.12)))
            if popup_blocked or confirm_ready:
                break
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
