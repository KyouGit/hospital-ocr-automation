"""
OCR Manager - Window Automation & Capture Engine (Production Reference)

Handles the core capture loop for the EMR extraction system:
- Finds and connects to the target Delphi TApplication window
- Iterates through patient list via TDrawGrid navigation
- Captures left panel (patient info) and right panels (exam results, opinions)
- Manages scrolling for multi-page content
- DPI-aware coordinate handling for multi-monitor setups

Note: Hospital-specific window names and paths removed.
"""

import os
import sys
import time
import traceback
import ctypes
from ctypes import wintypes, windll, byref

import cv2
import numpy as np
from PIL import Image, ImageGrab
import pyautogui
import win32gui
import win32con
import re
import win32api
import win32ui
from pywinauto import Application
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.controls.hwndwrapper import HwndWrapper
import glob

pyautogui.PAUSE = 0.01
pyautogui.MINIMUM_DURATION = 0.01
pyautogui.MINIMUM_SLEEP = 0.01

from utils import (
    load_variable_width_templates,
    Patient_Chart_Reader,
    compute_suchi_and_sogyeon_coords,
    load_config,
    find_x_y,
    focus_and_move_down,
    set_dpi_awareness,
    get_scaling_factor_hwnd,
    get_scale_for_point,
    get_grid_row_info,
    is_next_click_out_of_bounds,
    optimize_gui_width_height,
    restore_window,
    explore_tdrawgrid_elements,
    get_vertical_scroll_pos,
    send_horizontal_scroll_to_left,
    send_scroll_message_to_top,
    send_scroll_message,
    find_and_save_regions,
    capture_full_window,
    has_vertical_scroll,
    has_scrollbar,
)
from ui_bridge import ui_set_text, ui_set_patient_total, ui_set_code_total, ui_set_state


if getattr(sys, "frozen", False):
    EXE_DIR = os.path.dirname(os.path.realpath(sys.argv[0]))
else:
    EXE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_DATA_DIR = os.path.join(EXE_DIR, "AppData")
CONFIG_DIR = os.path.join(APP_DATA_DIR, "config")
config_path = os.path.join(CONFIG_DIR, "config.txt")

try:
    CONFIG = load_config(config_path) or {}
except Exception:
    CONFIG = {}

app_name = CONFIG.get("app_name", "") or ""
try:
    delay = float(CONFIG.get("delay", 0.0) or 0.0)
except Exception:
    delay = 0.0


class OCRManager:
    """
    Core capture engine that automates EMR window interaction.

    Process per patient:
        1. Click patient row in left TDrawGrid
        2. Capture left panel (patient name/info)
        3. Detect colored rows (yellow=selected, blue=header) via HSV
        4. Capture right panel (exam results) with scroll handling
        5. Capture opinion panel (clinical notes) with scroll handling
        6. Move to next patient row (DOWN key + scroll)
        7. Detect end of list via scroll position stagnation

    Handles:
        - Window detection with 3-retry fallback (TApplication -> TEMP)
        - DPI-aware coordinate calculation for multi-monitor
        - Scroll position tracking to detect end-of-content
        - Automatic cleanup of duplicate captures
    """

    def __init__(self, panel_queue, is_running_func=None, on_done_callback=None):
        self.panel_queue = panel_queue
        self.is_running_func = is_running_func or (lambda: True)
        self.on_done_callback = on_done_callback

    def run_ocr(self):
        window = None
        left = top = width = height = None

        try:
            # ── Window detection with retry ──
            for attempt in range(3):
                try:
                    try:
                        app = Application(backend="win32").connect(
                            title_re=".*{}.*".format(app_name),
                            class_name="TApplication",
                        )
                        window = app.top_window()
                    except ElementNotFoundError:
                        # Fallback to TEMP window
                        try:
                            app = Application(backend="win32").connect(
                                title_re=re.compile(".*TEMP.*", re.IGNORECASE),
                                class_name="TApplication",
                            )
                            window = app.top_window()
                        except ElementNotFoundError:
                            window = None

                    if window is not None:
                        win32gui.ShowWindow(window.handle, win32con.SW_RESTORE)
                        time.sleep(0.1)
                        window = app.top_window()
                        break
                except Exception:
                    window = None
                time.sleep(0.3)

            if window is None:
                print("[ERROR] Could not connect to target window")
                return

            # ── Window setup ──
            time.sleep(0.1)
            left, top, width, height = optimize_gui_width_height(window.handle)
            time.sleep(1)

            # Find exam data panel (suchi) and opinion panel (sogyeon) handles
            s_handle, o_handle, _, _ = compute_suchi_and_sogyeon_coords(window)
            _, _, click_x, click_y = find_x_y(window)

            # If click coordinates not found, navigate grid to find them
            if click_x is None:
                grid_handles = explore_tdrawgrid_elements(window)
                if grid_handles:
                    sorted_by_right = sorted(grid_handles, key=lambda x: x[2].right)
                    left_grid_handle = sorted_by_right[0][0]
                    HwndWrapper(left_grid_handle).set_focus()
                    window.type_keys("{HOME}")
                    send_scroll_message_to_top(left_grid_handle)
                    window.type_keys("{HOME}")
                    for _ in range(100):
                        focus_and_move_down(left_grid_handle)
                        s_handle, o_handle, _, _ = compute_suchi_and_sogyeon_coords(window)
                        _, _, click_x, click_y = find_x_y(window)
                        if click_x is not None:
                            break

            scale = get_scaling_factor_hwnd(window.handle)
            offset = 100
            scale_at_click = get_scale_for_point(click_x, click_y)
            adjusted_x = click_x + int(offset * scale_at_click)

            # ── Grid initialization ──
            grid_handles = explore_tdrawgrid_elements(window)
            if not grid_handles:
                return

            sorted_by_right = sorted(grid_handles, key=lambda x: x[2].right)
            left_grid_handle = sorted_by_right[0][0]
            right_grid_handle = sorted_by_right[1][0]

            time.sleep(1)
            HwndWrapper(left_grid_handle).set_focus()
            time.sleep(1)

            # Reset scroll to top
            send_scroll_message_to_top(left_grid_handle)
            time.sleep(1)
            win32api.keybd_event(win32con.VK_HOME, 0, 0, 0)
            time.sleep(1)
            window.type_keys("{HOME}")
            time.sleep(1)
            send_scroll_message_to_top(left_grid_handle)
            window.type_keys("{HOME}")
            time.sleep(0.1)
            send_scroll_message_to_top(s_handle)
            send_horizontal_scroll_to_left(s_handle)

            # ── Main capture loop (up to 1000 patients) ──
            prev_scroll_pos_left = get_vertical_scroll_pos(left_grid_handle)
            pre_region_region_y = None
            is_scroll_stopped_left = False
            CONFIG = load_config(config_path)
            capture_folder = CONFIG.get("capture_folder", "")
            left_has_scroll = has_scrollbar(left_grid_handle)

            for i in range(1000):
                time.sleep(delay)

                if not self.is_running_func():
                    print("[OCRManager] Stop signal received")
                    break

                base_capture_folder = os.path.join(EXE_DIR, capture_folder)
                os.makedirs(base_capture_folder, exist_ok=True)

                # ── Capture left panel (patient info) ──
                left_tmp_file = os.path.join(base_capture_folder, f"left_{i}.png")
                if click_x and click_y:
                    pyautogui.click(click_x, click_y)
                capture_full_window(left_grid_handle, left_tmp_file)

                # Detect colored rows (yellow/blue) for patient selection
                region_results, dominant_color = find_and_save_regions(
                    left_tmp_file, base_capture_folder, left_grid_handle
                )
                filename_with_color = left_tmp_file.replace(".png", f"_{dominant_color}.png")
                os.rename(left_tmp_file, filename_with_color)

                # ── End-of-list detection ──
                new_region_results = region_results
                new_region_region_y = (
                    new_region_results["yellow"][0]
                    if new_region_results["yellow"]
                    else new_region_results["blue"][0]
                    if new_region_results["blue"]
                    else None
                )[3]

                if pre_region_region_y is None:
                    diff_region_region_y = float("inf")
                else:
                    diff_region_region_y = abs(pre_region_region_y - new_region_region_y) / scale
                    if (diff_region_region_y < 10 and is_scroll_stopped_left) or (
                        diff_region_region_y < 10 and not left_has_scroll
                    ):
                        # No movement detected -> end of patient list
                        if os.path.exists(filename_with_color):
                            os.remove(filename_with_color)
                        # Cleanup any partial right/opinion captures
                        for pattern in [f"right_{i}_*.png", f"opinion_{i}_*.png"]:
                            for f in glob.glob(os.path.join(base_capture_folder, pattern)):
                                try:
                                    os.remove(f)
                                except Exception:
                                    pass
                        break

                ui_set_state("capturing_left")
                ui_set_patient_total()

                # ── Capture right panel (exam results) with scroll ──
                if s_handle and win32gui.IsWindowVisible(s_handle):
                    send_scroll_message_to_top(s_handle)
                    send_horizontal_scroll_to_left(s_handle)
                    self._capture_panel_with_scroll(
                        s_handle, base_capture_folder, f"right_{i}", max_groups=20, step=5
                    )
                    if click_x and click_y:
                        pyautogui.click(adjusted_x, click_y)

                time.sleep(0.1)

                # ── Capture opinion panel (clinical notes) with scroll ──
                if o_handle and win32gui.IsWindowVisible(o_handle):
                    send_scroll_message_to_top(o_handle)
                    send_horizontal_scroll_to_left(o_handle)
                    self._capture_panel_with_scroll(
                        o_handle, base_capture_folder, f"opinion_{i}", max_groups=20, step=5
                    )

                # ── Navigate to next patient ──
                time.sleep(0.01)
                focus_and_move_down(left_grid_handle)
                time.sleep(0.1)
                send_scroll_message(left_grid_handle)

                pre_region_region_y = new_region_region_y
                new_scroll_pos = get_vertical_scroll_pos(left_grid_handle)
                is_scroll_stopped_left = new_scroll_pos == prev_scroll_pos_left
                prev_scroll_pos_left = new_scroll_pos

            # Done
            if self.on_done_callback:
                self.on_done_callback()

            restore_window(window.handle, (left, top, width, height))

        except Exception:
            traceback.print_exc()
        finally:
            try:
                if self.on_done_callback:
                    self.on_done_callback()
            except Exception:
                pass
            try:
                if window is not None and left is not None:
                    restore_window(window.handle, (left, top, width, height))
            except Exception:
                pass

    def _capture_panel_with_scroll(self, handle, base_folder, prefix, max_groups=20, step=5):
        """Capture a scrollable panel, taking screenshots at each scroll position."""
        if has_vertical_scroll(handle):
            prev_scroll_pos = get_vertical_scroll_pos(handle)
            for group_idx in range(max_groups):
                tmp_file = os.path.join(base_folder, f"{prefix}_{group_idx}.png")
                capture_full_window(handle, tmp_file)
                ui_set_code_total()
                for _ in range(step):
                    send_scroll_message(handle)
                    time.sleep(0.1)
                current_pos = get_vertical_scroll_pos(handle)
                if current_pos == prev_scroll_pos:
                    break
                prev_scroll_pos = current_pos
        else:
            tmp_file = os.path.join(base_folder, f"{prefix}_0.png")
            capture_full_window(handle, tmp_file)
            ui_set_code_total()
