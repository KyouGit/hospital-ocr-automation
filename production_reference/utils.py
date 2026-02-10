"""
OCR Utilities - Template Matching & Win32 Helpers (Production Reference)

Core utility functions for the EMR OCR extraction system:

Template Matching OCR:
- recognize_variable_width: Sliding window character recognition using MSE
- load_variable_width_templates: Load character template images with padding
- extract_cells_and_run_ocr: Grid cell extraction + OCR from panel images

Win32 Window Management:
- capture_full_window: Lossless capture via PrintWindow API
- DPI-aware coordinate handling for multi-monitor setups
- Scroll control (vertical/horizontal) via SendMessage

Color Detection:
- HSV-based row detection (yellow=selected, blue=header)

Multiprocessing:
- Pool-based parallel OCR with 90% CPU utilization

Note: Hospital-specific paths and names removed.
"""

from multiprocessing import Pool
import os
import sys
import platform
import multiprocessing
import numpy as np
from PIL import Image, ImageOps
import cv2
from collections import Counter
from pywinauto import Application
import win32gui
import win32ui
import win32con
import ctypes
from ctypes import wintypes, windll, byref
import win32api
import time
import atexit

from ui_bridge import ui_inc_left, ui_inc_right

# ── PyInstaller compatibility ──
if getattr(sys, "frozen", False):
    real_exe = os.path.realpath(sys.argv[0])
    if os.path.isfile(real_exe):
        sys.executable = real_exe
    if platform.system() == "Windows":
        multiprocessing.set_executable(sys.executable)

    EXE_DIR = os.path.dirname(os.path.realpath(sys.argv[0]))
else:
    EXE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_DATA_DIR = os.path.join(EXE_DIR, "AppData")
CONFIG_DIR = os.path.join(APP_DATA_DIR, "config")
ERROR_IMG_DIR = os.path.join(APP_DATA_DIR, "errors")
config_path = os.path.join(CONFIG_DIR, "config.txt")


def resource_path(relative_path: str) -> str:
    """Resolve path for both dev and PyInstaller environments."""
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, relative_path)


# ══════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════

def load_config(path):
    """Load key=value config file, supporting semicolon-separated path candidates."""
    if ";" in path:
        candidates = [p.strip() for p in path.split(";") if p.strip()]
        path = next((p for p in candidates if os.path.exists(p)), None)
        if not path:
            raise FileNotFoundError(f"No valid config path found among candidates")

    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    config = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    return config


# ══════════════════════════════════════════════════════════════
# Template Matching OCR
# ══════════════════════════════════════════════════════════════

def decode_filename(encoded_name):
    """Decode template filename (Unicode codepoints separated by _)."""
    name = os.path.splitext(encoded_name)[0]
    return "".join(chr(int(code)) for code in name.split("_") if code)


def generate_padded_versions(img, label, target_height):
    """Generate vertical padding variants of a template image.
    Each character may sit at different vertical positions, so we
    create variants with all possible top/bottom padding combinations."""
    h = img.height
    max_pad = target_height - h
    if max_pad < 0:
        return []

    results = []
    for top_pad in range(max_pad + 1):
        bottom_pad = max_pad - top_pad
        padded = ImageOps.expand(img, (0, top_pad, 0, bottom_pad), fill=255)
        if padded.height == target_height:
            new_label = f"{label}_T{top_pad}B{bottom_pad}"
            results.append((new_label, np.array(padded, dtype=np.float32)))
    return results


def load_variable_width_templates(folder, target_height=14):
    """Load character template images for variable-width OCR.

    Each template PNG is named with Unicode codepoints (e.g., '65.png' for 'A').
    Templates are padded to target_height with all vertical offset variants.
    """
    templates = {}
    for fname in os.listdir(folder):
        if not fname.endswith(".png"):
            continue
        try:
            label = decode_filename(fname)
            if len(label) != 1:
                continue
            path = os.path.join(folder, fname)
            img = Image.open(path).convert("L")
            for new_label, padded_arr in generate_padded_versions(img, label, target_height):
                templates[new_label] = padded_arr
        except Exception:
            pass
    return templates


def normalize_image_height(img, target_height=14, fill_color=255):
    """Normalize image height to target via padding or resize."""
    h = img.height
    if h < target_height:
        pad_total = target_height - h
        pad_top = pad_total // 3
        pad_bottom = pad_total - pad_top
        img = ImageOps.expand(img, (0, pad_top, 0, pad_bottom), fill=fill_color)
    elif h > target_height:
        img = img.resize((img.width, target_height))
    return img


def recognize_variable_width(img_or_pil, templates, stride=1, threshold=250.0, space_threshold=10):
    """
    Sliding window character recognition using template matching (MSE).

    Algorithm:
    1. Normalize input image to target height
    2. For each x position, try all templates (largest first)
    3. Select best match (lowest MSE) below threshold
    4. Handle special cases: underscore vs hyphen (by vertical position)
    5. Insert spaces when gap exceeds space_threshold

    Args:
        img_or_pil: Input image (path, PIL Image, or numpy array)
        templates: Dict of {label: numpy_array} template images
        stride: Pixel step for sliding window
        threshold: MSE threshold for accepting a match
        space_threshold: Pixel gap to insert space character

    Returns:
        (recognized_text, score_list, has_missing_pixels)
    """
    if isinstance(img_or_pil, str):
        img = Image.open(img_or_pil).convert("L")
    elif isinstance(img_or_pil, Image.Image):
        img = img_or_pil.convert("L")
    else:
        raise ValueError("Input must be file path or PIL.Image")

    img = normalize_image_height(img)
    img_np = np.array(img, dtype=np.float32)
    H, W = img_np.shape

    output = ""
    score_list = []
    templates_sorted = sorted(templates.items(), key=lambda x: -x[1].shape[1])

    x = 0
    x_prev_end = 0
    binary_cell = (img_np < 250).astype(np.uint8) * 255
    mask = np.zeros_like(binary_cell, dtype=np.uint8)

    while x < W:
        best_char = ""
        best_score = float("inf")
        best_width = 0

        for label, tmpl in templates_sorted:
            tH, tW = tmpl.shape
            if x + tW > W:
                continue

            patch = img_np[:, x : x + tW]
            if patch.shape != tmpl.shape:
                continue

            score = np.mean((patch - tmpl) ** 2)
            if score < best_score:
                best_score = score
                best_char = label
                best_width = tW
                if score == 0:
                    break  # Perfect match

        if best_score < threshold:
            if x - x_prev_end >= space_threshold:
                output += " "

            patch = img_np[:, x : x + best_width]
            mask[:, x : x + best_width] = 255

            char_raw = best_char.split("_")[0]

            # Disambiguate underscore vs hyphen by vertical position
            if char_raw in ["_", "-"]:
                y_coords, _ = np.where(patch < 200)
                center_y = np.mean(y_coords) if len(y_coords) > 0 else 0
                char_refined = "_" if center_y >= 8 else "-"
            else:
                char_refined = char_raw

            output += char_refined
            score_list.append(best_score)
            x_prev_end = x + best_width
            x += best_width
        else:
            x += stride

    residual = cv2.bitwise_and(binary_cell, cv2.bitwise_not(mask))
    missed_pixels = np.count_nonzero(residual)
    has_missing = missed_pixels > 0
    return output.strip(), score_list, has_missing


# ══════════════════════════════════════════════════════════════
# Cell Extraction from Grid Images
# ══════════════════════════════════════════════════════════════

def clean_coords(coords, threshold=5):
    """Remove coordinates that are too close together."""
    cleaned = []
    last = -100
    for c in coords:
        if c - last > threshold:
            cleaned.append(c)
            last = c
    return cleaned


def extract_cells_and_run_ocr(image_path, templates, variable_width_templates):
    """
    Extract individual cells from a grid image and OCR each cell.

    Process:
    1. Adaptive thresholding to detect grid lines
    2. Morphological operations to isolate vertical lines
    3. Detect row boundaries via uniform spacing (stride=17px)
    4. Crop each cell, threshold, find contours
    5. Run template matching OCR on each cell

    Returns: List of ((row, col), (text, scores))
    """
    pad, bottom_pad = 1, 1
    base_filename = os.path.basename(image_path)

    with Image.open(image_path) as im:
        pil_img_full = im.convert("RGB")
    image = np.array(pil_img_full)
    img = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

    binary = cv2.adaptiveThreshold(
        img, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 10
    )

    # Detect vertical grid lines
    ver_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 18))
    vertical = cv2.dilate(cv2.erode(binary, ver_kernel, 1), ver_kernel, 1)
    x_coords = clean_coords(np.where(np.sum(vertical, axis=0) > 0)[0])

    h, w = binary.shape[:2]
    x0 = max(0, x_coords[0] + pad)
    x1 = (x_coords[1] - pad) if len(x_coords) > 1 else min(w, x_coords[0] + max(20, w // 10))
    if x1 <= x0:
        x1 = min(w, x0 + 5)
    binary = binary[:, x0:x1]

    # Detect row boundaries via uniform spacing
    h, w = binary.shape[:2]
    stride = 17
    y_candidates = _detect_row_boundaries(binary, h, stride)

    if 0 not in x_coords:
        x_coords = np.insert(x_coords, 0, 0)

    # OCR each cell
    results = []
    for i in range(len(y_candidates) - 1):
        top_y, bottom_y = y_candidates[i], y_candidates[i + 1]
        if top_y < 5:
            continue

        left_x, right_x = x_coords[0], x_coords[1] if len(x_coords) > 1 else w
        t = max(top_y + pad, 0)
        b = max(min(bottom_y - bottom_pad, img.shape[0]), top_y + 1)
        l = max(left_x + pad, 0)
        r = max(min(right_x - pad, img.shape[1]), left_x + 1)

        roi_bgr = image[t:b, l:r]
        roi_gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
        _, binary_cell = cv2.threshold(roi_gray, 250, 255, cv2.THRESH_BINARY_INV)
        contours, _ = cv2.findContours(binary_cell, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue

        all_contours = np.vstack(contours)
        x, y, cw, ch = cv2.boundingRect(all_contours)
        x1c = max(x - pad, 0)
        y1c = max(y - pad, 0)
        x2c = min(x + cw + pad, roi_gray.shape[1])
        y2c = min(y + ch + pad, roi_gray.shape[0])
        final_crop = roi_gray[y1c:y2c, x1c:x2c]
        _, display_img = cv2.threshold(final_crop, 250, 255, cv2.THRESH_BINARY)
        pil_cell = Image.fromarray(display_img).convert("L")

        text, score_list, has_missing = recognize_variable_width(pil_cell, variable_width_templates)
        if not text.strip():
            continue

        results.append(((i, 0), (text, score_list)))
    return results


def _detect_row_boundaries(binary, h, stride=17):
    """Detect row boundaries using uniform spacing with empty-region detection."""

    def is_empty_strip(bi, y0, y1, min_white_ratio=0.002):
        y0, y1 = max(0, min(y0, bi.shape[0] - 1)), max(0, min(y1, bi.shape[0] - 1))
        if y1 <= y0:
            return True
        strip = bi[y0:y1, :]
        white = np.count_nonzero(strip == 255)
        return (white / (strip.size + 1e-6)) < min_white_ratio

    def snap_to_line(bi, y, window=2):
        ys = np.arange(max(0, y - window), min(bi.shape[0] - 1, y + window) + 1)
        if ys.size == 0:
            return y
        row_sums = np.sum(bi[ys] == 255, axis=1)
        return int(ys[np.argmax(row_sums)])

    seed = [y for y in [1, 17, 35] if 0 <= y < h]
    y_candidates = []
    prev_y = None

    for y in seed:
        y_s = snap_to_line(binary, y, window=2)
        if prev_y is not None and is_empty_strip(binary, prev_y, y_s):
            continue
        y_candidates.append(y_s)
        prev_y = y_s

    y = (seed[-1] + stride) if seed else 0
    empty_run = 0
    while y < h - 1:
        y_s = snap_to_line(binary, y, window=2)
        if prev_y is not None and is_empty_strip(binary, prev_y, y_s):
            empty_run += 1
            if empty_run >= 2:
                break
            y += stride
            continue
        empty_run = 0
        y_candidates.append(y_s)
        prev_y = y_s
        y += stride

    bottom = h - 1
    if not y_candidates or y_candidates[-1] != bottom:
        if prev_y is None or not is_empty_strip(binary, prev_y, bottom):
            y_candidates.append(bottom)

    y_coords = np.unique(np.clip(np.array(y_candidates, dtype=int), 0, h - 1))
    y_coords.sort()
    return y_coords.tolist()


# ══════════════════════════════════════════════════════════════
# Patient Name Reader (Color-based ROI + OCR)
# ══════════════════════════════════════════════════════════════

def Patient_Chart_Reader(image_path, templates, templates_30per, templet_code_list, mode):
    """
    Read patient name from left panel image.

    Uses HSV color detection to find the selected row:
    - Blue: header row (bright text on dark background)
    - Yellow: selected row (dark text on bright background)

    Then crops the name column and runs template matching OCR.
    """
    image = imread_unicode(image_path, cv2.IMREAD_COLOR)
    if image is None:
        return ""

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    if mode == "blue":
        lower, upper = np.array([100, 140, 50]), np.array([140, 255, 255])
    elif mode == "yellow":
        lower, upper = np.array([20, 50, 150]), np.array([30, 255, 255])
    else:
        raise ValueError("mode must be 'blue' or 'yellow'")

    mask = cv2.inRange(hsv, lower, upper)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    CONFIG = load_config(config_path)
    name_index = int(CONFIG.get("name_index", 0))
    info_indices = list(map(int, CONFIG.get("info_indices", "").split(",")))

    boxes = [cv2.boundingRect(c) for c in contours if cv2.boundingRect(c)[2] > 20 and cv2.boundingRect(c)[3] > 10]
    sorted_boxes = sorted(boxes, key=lambda b: b[0])
    leftmost = [sorted_boxes[name_index]] if name_index < len(sorted_boxes) else []

    recognized_all = []

    # Name column OCR
    for x, y, w, h in leftmost:
        roi = image[y : y + h, x : x + w]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)

        if mode == "yellow" and binary.shape[0] > 2 and binary.shape[1] > 2:
            binary = cv2.bitwise_not(binary[2:-2, 2:-2])

        cts, _ = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        filtered = [c for c in cts if cv2.contourArea(c) < binary.shape[0] * binary.shape[1] * 0.9]
        if filtered:
            all_pts = np.vstack(filtered)
            x2, y2, w2, h2 = cv2.boundingRect(all_pts)
            cropped = binary[y2 : y2 + h2, x2 : x2 + w2]
            pil_img = Image.fromarray(cropped).convert("L")
            recognized, _, has_missing = recognize_variable_width(pil_img, templates_30per)
            if has_missing:
                recognized, _, _ = recognize_variable_width(pil_img, templates)
            recognized_all.append(recognized)

    # Info columns OCR
    leftmost_info = [sorted_boxes[i] for i in info_indices if i < len(sorted_boxes)]
    for x, y, w, h in leftmost_info:
        roi = image[y : y + h, x : x + w]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)

        if mode == "yellow" and binary.shape[0] > 2 and binary.shape[1] > 2:
            binary = cv2.bitwise_not(binary[2:-2, 2:-2])

        cts, _ = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        filtered = [c for c in cts if cv2.contourArea(c) < binary.shape[0] * binary.shape[1] * 0.9]
        if filtered:
            all_pts = np.vstack(filtered)
            x2, y2, w2, h2 = cv2.boundingRect(all_pts)
            cropped = binary[y2 : y2 + h2, x2 : x2 + w2]
            pil_img = Image.fromarray(cropped).convert("L")
            recognized, _, _ = recognize_variable_width(pil_img, templet_code_list)
            recognized_all.append(recognized)

    return " , ".join(recognized_all)


# ══════════════════════════════════════════════════════════════
# Win32 Window Capture
# ══════════════════════════════════════════════════════════════

def imread_unicode(path, flags=cv2.IMREAD_COLOR):
    """Read image from Unicode path (handles Korean filenames)."""
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, flags)
    except Exception:
        return None


def capture_full_window(handle, file_name):
    """Capture window contents via Win32 PrintWindow API (lossless, DPI-aware)."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(handle)
        width, height = right - left, bottom - top

        hwndDC = win32gui.GetWindowDC(handle)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
        saveDC.SelectObject(saveBitMap)

        result = ctypes.windll.user32.PrintWindow(handle, saveDC.GetSafeHdc(), 1)
        if result != 1:
            return None

        bmpinfo = saveBitMap.GetInfo()
        bmpstr = saveBitMap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr, "raw", "BGRX", 0, 1,
        )

        # Skip black/empty captures
        gray = img.convert("L")
        min_val, max_val = gray.getextrema()
        if max_val < 10:
            return None

        img.save(file_name)

        win32gui.DeleteObject(saveBitMap.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(handle, hwndDC)
        return img

    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# DPI Handling
# ══════════════════════════════════════════════════════════════

def set_dpi_awareness():
    """Enable per-monitor DPI awareness."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_scaling_factor_hwnd(hwnd):
    """Get DPI scaling factor for a specific window handle."""
    set_dpi_awareness()
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        return dpi / 96.0
    except AttributeError:
        try:
            monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 1)
            dpiX = ctypes.c_uint()
            ctypes.windll.shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpiX), ctypes.byref(ctypes.c_uint()))
            return dpiX.value / 96.0
        except Exception:
            return 1.0


def get_scale_for_point(x, y):
    """Get DPI scaling factor for a screen coordinate."""
    try:
        hmon = ctypes.windll.user32.MonitorFromPoint(
            wintypes.POINT(x, y), 2  # MONITOR_DEFAULTTONEAREST
        )
        dpiX = ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpiX), ctypes.byref(ctypes.c_uint()))
        return dpiX.value / 96.0
    except Exception:
        return 1.0


def get_scale_for_monitor(hmon):
    """Get DPI scale for a monitor handle."""
    try:
        dpiX = ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpiX), ctypes.byref(ctypes.c_uint()))
        return dpiX.value / 96.0 if dpiX.value else 1.0
    except Exception:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi_sys = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi_sys / 96.0


# ══════════════════════════════════════════════════════════════
# Scroll & Grid Navigation
# ══════════════════════════════════════════════════════════════

def send_scroll_message(handle):
    """Send scroll-down message to a window handle."""
    try:
        win32gui.SendMessage(handle, win32con.WM_VSCROLL, win32con.SB_LINEDOWN, None)
    except Exception:
        pass


def send_scroll_message_to_top(handle):
    """Scroll window to top."""
    try:
        win32gui.SendMessageTimeout(
            handle, win32con.WM_VSCROLL, win32con.SB_TOP, None,
            win32con.SMTO_ABORTIFHUNG, 100,
        )
        time.sleep(0.001)
    except Exception:
        pass


def send_horizontal_scroll_to_left(handle):
    """Scroll window to leftmost position."""
    try:
        win32gui.SendMessageTimeout(
            handle, win32con.WM_HSCROLL, win32con.SB_LEFT, None,
            win32con.SMTO_ABORTIFHUNG, 100,
        )
        time.sleep(0.002)
    except Exception:
        pass


def get_vertical_scroll_pos(handle):
    """Get current vertical scroll position via SCROLLINFO."""
    class SCROLLINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT), ("fMask", wintypes.UINT),
            ("nMin", wintypes.INT), ("nMax", wintypes.INT),
            ("nPage", wintypes.UINT), ("nPos", wintypes.INT),
            ("nTrackPos", wintypes.INT),
        ]

    si = SCROLLINFO()
    si.cbSize = ctypes.sizeof(SCROLLINFO)
    si.fMask = win32con.SIF_ALL
    ctypes.windll.user32.GetScrollInfo(handle, win32con.SB_VERT, ctypes.byref(si))
    return si.nPos


def has_vertical_scroll(hwnd):
    """Check if window has vertical scroll range."""
    min_pos, max_pos = wintypes.INT(), wintypes.INT()
    res = windll.user32.GetScrollRange(hwnd, 1, byref(min_pos), byref(max_pos))
    return res != 0 and max_pos.value > min_pos.value


def has_scrollbar(hwnd):
    """Check if window has vertical scrollbar via window style."""
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    return bool(style & win32con.WS_VSCROLL)


def get_grid_row_info(hwnd):
    """Get total rows and current position from TDrawGrid scroll range."""
    min_pos, max_pos = wintypes.INT(), wintypes.INT()
    res = windll.user32.GetScrollRange(hwnd, 1, byref(min_pos), byref(max_pos))
    if res == 0:
        return (0, 0)
    cur_pos = windll.user32.GetScrollPos(hwnd, 1)
    return max_pos.value - min_pos.value + 1, cur_pos


def explore_tdrawgrid_elements(window):
    """Recursively find all TDrawGrid controls in a window."""
    def recursive_collect(element, visited):
        results = []
        try:
            class_name = element.class_name()
            if "TDrawGrid" in class_name:
                handle = element.handle
                if handle not in visited:
                    visited.add(handle)
                    results.append((handle, class_name, element.rectangle()))
            for child in element.descendants():
                results.extend(recursive_collect(child, visited))
        except Exception:
            pass
        return results

    return recursive_collect(window, set())


def focus_and_move_down(hwnd):
    """Set focus to grid and press DOWN key."""
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    from pywinauto.keyboard import send_keys
    send_keys("{DOWN}")
    time.sleep(0.2)


def is_next_click_out_of_bounds(grid_handle, region_results, offset=3):
    """Check if the next row click would be outside grid bounds."""
    try:
        grid_rect = win32gui.GetWindowRect(grid_handle)
        grid_left, grid_top, grid_right, grid_bottom = grid_rect

        for color, coordinates in region_results.items():
            if coordinates:
                x_start, x_end, y_start, y_end = coordinates[0]
                height = y_end - y_start
                x_click = grid_left + (x_start + x_end) // 2
                y_click = grid_top + (y_start + y_end) // 2 + offset * height

                if not (grid_left <= x_click <= grid_right and grid_top <= y_click <= grid_bottom):
                    return True
        return False
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
# Window Management
# ══════════════════════════════════════════════════════════════

def optimize_gui_width_height(hwnd):
    """Resize window to 3/4 monitor width, full height. Returns original position."""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top

    monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    mon_info = win32api.GetMonitorInfo(monitor)
    ml, mt, mr, mb = mon_info["Monitor"]
    new_width = ((mr - ml) // 4) * 3
    new_height = mb - mt

    win32gui.MoveWindow(hwnd, ml, mt, new_width, new_height, True)
    return (left, top, width, height)


def restore_window(hwnd, old_pos):
    """Restore window to original position."""
    left, top, width, height = old_pos
    win32gui.MoveWindow(hwnd, left, top, width, height, True)


def compute_suchi_and_sogyeon_coords(window):
    """Find exam data (suchi) and opinion (sogyeon) panel handles and click coordinates."""
    try:
        page_controls = [e for e in window.descendants() if "TPageControl" in e.class_name()]
        if not page_controls:
            return None, None, None, None

        page_control = page_controls[0]
        tabsheets = [e for e in page_control.descendants() if e.class_name() == "TTabSheet"]
        if not tabsheets:
            return None, None, None, None

        result = {"suchi": None, "sogyeon": None}

        for tab in tabsheets:
            name = tab.window_text().strip()
            # Match tab by Korean label
            matched = None
            if "\uc218\uce58" in name:  # suchi (exam data)
                matched = "suchi"
            elif "\uc18c\uacac" in name:  # sogyeon (opinion)
                matched = "sogyeon"
            else:
                continue

            grids = [e for e in tab.descendants() if "TDrawGrid" in e.class_name()]
            grid_handle = None
            if grids:
                grids_sorted = sorted(
                    grids,
                    key=lambda e: max(1, (e.rectangle().right - e.rectangle().left))
                    * max(1, (e.rectangle().bottom - e.rectangle().top)),
                    reverse=True,
                )
                grid_handle = grids_sorted[0].handle

            if matched == "sogyeon":
                result[matched] = {"handle": grid_handle, "click_x": None, "click_y": None}
                continue

            # Compute click coordinates for suchi tab
            result[matched] = {"handle": grid_handle or tab.handle, "click_x": None, "click_y": None}

        s = result["suchi"]
        o = result["sogyeon"]
        return (
            s["handle"] if s else None,
            o["handle"] if o else None,
            s["click_x"] if s else None,
            s["click_y"] if s else None,
        )
    except Exception:
        return None, None, None, None


def find_x_y(window):
    """Find click coordinates for the exam data tab via contour detection."""
    try:
        page_controls = [e for e in window.descendants() if "TPageControl" in e.class_name()]
        if not page_controls:
            return None, None, None, None

        page_control = page_controls[0]
        tabsheets = [e for e in page_control.descendants() if e.class_name() == "TTabSheet"]

        for tab in tabsheets:
            name = tab.window_text().strip()
            if "\uc218\uce58" not in name:  # suchi
                continue

            grids = [e for e in tab.descendants() if "TDrawGrid" in e.class_name()]
            grid_handle = grids[0].handle if grids else None

            # Capture tab area for contour detection
            img_page = _capture_by_printwindow(page_control.handle)
            img_tab = _capture_by_printwindow(tab.handle)
            if not img_page or not img_tab:
                continue

            wp, hp = img_page.size
            _, ht = img_tab.size
            if ht >= hp:
                continue

            cropped_top = img_page.crop((0, 0, wp, hp - ht - 8))
            boxes = _extract_contour_boxes(cropped_top)
            if not boxes:
                continue

            min_x = min(x for x, _, _, _ in boxes)
            min_y = min(y for _, y, _, _ in boxes)
            max_y = max(y + h for _, y, _, h in boxes)

            page_left, page_top, _, _ = win32gui.GetWindowRect(page_control.handle)
            offset_x = (img_page.size[0] - cropped_top.size[0]) // 2

            click_x = int(page_left + min_x + offset_x + 2)
            click_y = int(page_top + min_y + (max_y - min_y) // 2)

            return grid_handle, None, click_x, click_y

        return None, None, None, None
    except Exception:
        return None, None, None, None


def _capture_by_printwindow(handle):
    """Helper: capture window via PrintWindow API."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(handle)
        width, height = right - left, bottom - top
        hwndDC = win32gui.GetWindowDC(handle)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
        saveDC.SelectObject(saveBitMap)
        result = ctypes.windll.user32.PrintWindow(handle, saveDC.GetSafeHdc(), 1)
        if result != 1:
            return None
        bmpinfo = saveBitMap.GetInfo()
        bmpstr = saveBitMap.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (bmpinfo["bmWidth"], bmpinfo["bmHeight"]), bmpstr, "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(saveBitMap.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(handle, hwndDC)
        return img
    except Exception:
        return None


def _extract_contour_boxes(pil_img):
    """Extract contour bounding boxes from image for click target detection."""
    img_gray = np.array(pil_img.convert("L"))
    _, binary = cv2.threshold(img_gray, 150, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if 2 <= w <= 17 and 2 <= h <= 17:
            boxes.append((x, y, w, h))
    return sorted(boxes, key=lambda b: (b[1], b[0]))


# ══════════════════════════════════════════════════════════════
# Color Detection (Row Selection)
# ══════════════════════════════════════════════════════════════

def find_colored_regions(image_path, lower_color, upper_color, min_width=20, min_height=20):
    """Detect colored regions via HSV thresholding."""
    image = imread_unicode(image_path, cv2.IMREAD_COLOR)
    if image is None:
        return [], None

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(lower_color), np.array(upper_color))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    regions = [(x, y, w, h) for x, y, w, h in
               (cv2.boundingRect(c) for c in contours) if w >= min_width and h >= min_height]
    return (regions, image) if regions else ([], image)


def find_and_save_regions(image_path, capture_folder, left_grid_handle):
    """Detect yellow and blue colored regions for patient row identification."""
    colors = {
        "yellow": ((20, 50, 150), (30, 255, 255)),
        "blue": ((100, 140, 50), (140, 255, 255)),
    }
    results = {}
    dominant_color = None

    for color_name, (lower, upper) in colors.items():
        regions, image = find_colored_regions(image_path, lower, upper, min_width=30, min_height=5)
        if regions:
            xy_start_end = [(x, x + w, y, y + h) for x, y, w, h in regions]
            results[color_name] = xy_start_end
            if dominant_color is None:
                dominant_color = color_name
        else:
            results[color_name] = []

    return results, dominant_color


def sanitize_name(text):
    """Remove invalid filename characters."""
    invalid_chars = r'\/:*?"<>|'
    for ch in invalid_chars:
        text = text.replace(ch, "")
    return text.replace(" ", "_").replace("#", "_").strip()


# ══════════════════════════════════════════════════════════════
# Multiprocessing OCR
# ══════════════════════════════════════════════════════════════

_templates = None
_templete_paitient_dir = None
_templete_paitient_30per_dir = None
_templete_paitient_num_dir = None
_templete_code_dir = None
_templete_code_list_100_dir = None


def init_worker():
    """Worker initializer: load patient name templates per process."""
    global _templete_paitient_dir, _templete_paitient_30per_dir, _templete_paitient_num_dir
    _templete_paitient_dir = load_variable_width_templates(resource_path("templete_paitient"))
    _templete_paitient_30per_dir = load_variable_width_templates(resource_path("templete_paitient_30per"))
    _templete_paitient_num_dir = load_variable_width_templates(resource_path("templete_paitient_num"))


def init_worker2():
    """Worker initializer: load exam code templates per process."""
    global _templete_code_dir, _templete_code_list_100_dir
    _templete_code_dir = resource_path("templete_code")
    _templete_code_list_100_dir = load_variable_width_templates(resource_path("templete_code_list_100"))


def safe_extract_with_progress(image_path):
    """OCR a single right/opinion panel image (worker function)."""
    global _templete_code_dir, _templete_code_list_100_dir
    try:
        fname = os.path.basename(image_path).lower()
        if "opinion" in fname:
            result = extract_opinion_ocr(image_path, _templete_code_dir, _templete_code_list_100_dir)
        else:
            result = extract_cells_and_run_ocr(image_path, _templete_code_dir, _templete_code_list_100_dir)
        return {"image_path": image_path, "result": result}
    except Exception as e:
        return {"image_path": image_path, "result": f"Error: {e}"}


def safe_extract_left_name_with_progress(image_path):
    """OCR a single left panel image for patient name (worker function)."""
    global _templete_paitient_dir, _templete_paitient_30per_dir, _templete_paitient_num_dir
    try:
        filename = os.path.basename(image_path)
        dominant_color = "blue" if "_blue" in filename else "yellow" if "_yellow" in filename else "blue"
        result = Patient_Chart_Reader(
            image_path, _templete_paitient_dir, _templete_paitient_30per_dir,
            _templete_paitient_num_dir, mode=dominant_color,
        ).strip()
        if not result:
            result = os.path.basename(image_path).replace(".png", "")
        return {"image_path": image_path, "result": result}
    except Exception as e:
        return {"image_path": image_path, "result": f"Error: {e}"}


def analyze_all_panel_images_with_progress(max_cpu_ratio=0.9):
    """Parallel OCR of all right/opinion panel images (90% CPU utilization)."""
    CONFIG = load_config(config_path)
    capture_folder = CONFIG.get("capture_folder", "")
    base_dir = os.path.join(EXE_DIR, capture_folder)

    image_paths = _find_panel_images(base_dir, prefixes=("right", "opinion"))
    if not image_paths:
        return []

    cpu_total = multiprocessing.cpu_count()
    target_cpu = max(1, int(cpu_total * max_cpu_ratio))

    with Pool(processes=target_cpu, initializer=init_worker2) as pool:
        results = []
        for item in pool.imap_unordered(safe_extract_with_progress, image_paths):
            ui_inc_right()
            results.append(item)
    return results


def analyze_left_images_with_progress(max_cpu_ratio=0.9):
    """Parallel OCR of all left panel images for patient names."""
    CONFIG = load_config(config_path)
    capture_folder = CONFIG.get("capture_folder", "")
    base_dir = os.path.join(EXE_DIR, capture_folder)

    image_paths = _find_panel_images(base_dir, prefixes=("left_",))
    if not image_paths:
        return []

    cpu_total = multiprocessing.cpu_count()
    target_cpu = max(1, int(cpu_total * max_cpu_ratio))

    with Pool(processes=target_cpu, initializer=init_worker) as pool:
        results = []
        for item in pool.imap_unordered(safe_extract_left_name_with_progress, image_paths):
            ui_inc_left()
            results.append(item)

    path_to_index = {p: i for i, p in enumerate(image_paths)}
    results.sort(key=lambda x: path_to_index.get(x["image_path"], 99999))
    return results


def _find_panel_images(base_dir, prefixes):
    """Find panel images matching given prefixes in capture directory."""
    image_paths = []
    for fname in os.listdir(base_dir):
        if fname.lower().endswith(".png") and any(fname.startswith(p) for p in prefixes):
            image_paths.append(os.path.join(base_dir, fname))

    def sort_key(path):
        parts = os.path.basename(path).replace(".png", "").split("_")
        return tuple(int(p) for p in parts[1:] if p.isdigit()) or (9999,)

    image_paths.sort(key=sort_key)
    return image_paths


def extract_opinion_ocr(image_path, templates, variable_width_templates):
    """Extract text from opinion (clinical notes) panel."""
    image = imread_unicode(image_path, cv2.IMREAD_COLOR)
    if image is None:
        return []

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower, upper = np.array([100, 140, 50]), np.array([140, 255, 255])
    mask = cv2.inRange(hsv, lower, upper)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = [cv2.boundingRect(c) for c in contours if cv2.boundingRect(c)[2] > 20 and cv2.boundingRect(c)[3] > 10]
    leftmost = sorted(boxes, key=lambda b: b[0])[:1]

    recognized_all = []

    if leftmost:
        x, y, w, h = leftmost[0]
        roi = image[y : y + h, x : x + w]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)

        cts, _ = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        filtered = [c for c in cts if cv2.contourArea(c) < binary.shape[0] * binary.shape[1] * 0.9]
        if filtered:
            all_pts = np.vstack(filtered)
            x2, y2, w2, h2 = cv2.boundingRect(all_pts)
            cropped = binary[y2 : y2 + h2, x2 : x2 + w2]
            pil_img = Image.fromarray(cropped).convert("L")
            recognized, _, _ = recognize_variable_width(pil_img, variable_width_templates)
            recognized_all.append(recognized)

        roi_rest = image[y + h :, x : x + w]
    else:
        roi_rest = image

    # Split by horizontal dividers and OCR each section
    gray = cv2.cvtColor(roi_rest, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    row_sums = np.sum(binary == 255, axis=1)
    row_percent = row_sums / binary.shape[1] * 100

    divider_rows = np.where(row_percent > 95)[0]
    split_lines = []
    if len(divider_rows) > 0:
        prev = divider_rows[0]
        for r in divider_rows[1:]:
            if r - prev > 5:
                split_lines.append(prev)
            prev = r
        split_lines.append(prev)

    splits = []
    prev_y = 0
    for line in split_lines:
        splits.append(roi_rest[prev_y + 1 : line - 1, :])
        prev_y = line

    for i, part in enumerate(splits):
        if i == 0 or part.shape[0] == 0 or part.shape[1] == 0:
            continue
        gray_part = cv2.cvtColor(part, cv2.COLOR_BGR2GRAY)
        _, binary_part = cv2.threshold(gray_part, 180, 255, cv2.THRESH_BINARY)

        cts, _ = cv2.findContours(binary_part, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        filtered = [c for c in cts if cv2.contourArea(c) < binary_part.shape[0] * binary_part.shape[1] * 0.9]
        if filtered:
            all_pts = np.vstack(filtered)
            x2, y2, w2, h2 = cv2.boundingRect(all_pts)
            cropped = binary_part[y2 : y2 + h2, x2 : x2 + w2]
            pil_img = Image.fromarray(cropped).convert("L")
            recognized, _, _ = recognize_variable_width(pil_img, variable_width_templates)
            recognized_all.append(recognized)

    results = [((i, 0), (text, [1.0])) for i, text in enumerate(recognized_all)]
    return results
