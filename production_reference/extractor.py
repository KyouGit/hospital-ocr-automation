"""
EMR Data Extractor - Main Orchestrator (Production Reference)

Manages the full OCR extraction pipeline:
  OCR capture -> left panel analysis (patient names)
  -> right panel analysis (exam codes) -> CSV matching -> save results

Components:
- OCRRunner: Pipeline orchestrator with threading
- click_TDrawGrid: pywinauto-based Delphi grid navigation
- csv_matching: Fuzzy text matching against exam code database

Note: Hospital-specific names, paths, and credentials removed.
"""

import os
import sys
import csv
import queue
import threading
import multiprocessing
from collections import defaultdict
import time
import gc
from datetime import datetime
import re
import win32api
import win32con

import pandas as pd
from pywinauto import Application
from pywinauto.findwindows import ElementNotFoundError

from ocr_manager import OCRManager
from utils import (
    analyze_all_panel_images_with_progress,
    analyze_left_images_with_progress,
    load_config,
)

if getattr(sys, "frozen", False):
    EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    EXE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_DATA_DIR = os.path.join(EXE_DIR, "AppData")
LOG_DIR = os.path.join(APP_DATA_DIR, "logs")
CONFIG_DIR = os.path.join(APP_DATA_DIR, "config")
ERROR_IMG_DIR = os.path.join(APP_DATA_DIR, "errors")

config_path = os.path.join(CONFIG_DIR, "config.txt")


# ══════════════════════════════════════════════════════════════
# Configuration helpers
# ══════════════════════════════════════════════════════════════

def update_config_line(path, key, new_value):
    """Update key=value in config file, preserving comments."""
    lines = []
    found = False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(f"{key}="):
                lines.append(f"{key}={new_value}\n")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={new_value}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ══════════════════════════════════════════════════════════════
# TDrawGrid interaction (Delphi Win32 control)
# ══════════════════════════════════════════════════════════════

def click_Tdrawgrid(mode):
    """
    Connect to the EMR application's TDrawGrid control and click the
    top-left cell. Uses pywinauto to find TApplication windows.

    Args:
        mode: 1 = click leftmost grid,
              2 = click + move right arrow (for second panel)
    """
    CONFIG = load_config(config_path)
    app_name = CONFIG.get("app_name", "")
    delay = float(CONFIG.get("delay", 0.0))

    try:
        app = Application(backend="win32").connect(
            title_re=".*{}.*".format(app_name),
            class_name="TApplication",
        )
        window = app.top_window()
    except ElementNotFoundError:
        try:
            # Fallback: try TEMP window
            app = Application(backend="win32").connect(
                title_re=".*TEMP.*",
                class_name="TApplication",
            )
            window = app.top_window()
        except ElementNotFoundError:
            print("[ERROR] Could not find target window.")
            return None

    window.set_focus()
    tdraw_grids = window.descendants(class_name="TDrawGrid")
    time.sleep(0.05)

    if not tdraw_grids:
        print("[ERROR] TDrawGrid control not found.")
        return

    # Find leftmost grid
    leftmost_grid = min(tdraw_grids, key=lambda g: g.rectangle().left)
    rect = leftmost_grid.rectangle()

    x = rect.left + 5
    y = rect.top - 10
    leftmost_grid.click_input(coords=(x - rect.left, y - rect.top))
    time.sleep(0.05)

    if mode == 2:
        # Navigate to second panel via arrow key
        win32api.keybd_event(win32con.VK_RIGHT, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(win32con.VK_RIGHT, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)


def ensure_directories():
    """Create timestamped capture folder and required directories."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    capture_folder = f"captures/capture_{timestamp}"
    update_config_line(config_path, "capture_folder", capture_folder)
    CAPTURE_DATA_DIR = os.path.join(EXE_DIR, capture_folder)

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(ERROR_IMG_DIR, exist_ok=True)
    os.makedirs(CAPTURE_DATA_DIR, exist_ok=True)


def log_message(message: str):
    """Append timestamped message to log file."""
    timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
    log_path = os.path.join(LOG_DIR, "log.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(timestamp + message + "\n")
    except PermissionError:
        pass


# ══════════════════════════════════════════════════════════════
# OCRRunner - Pipeline orchestrator
# ══════════════════════════════════════════════════════════════

class OCRRunner:
    """
    Full pipeline: capture -> OCR -> analysis -> matching -> export.

    Flow:
        1. OCRManager captures left/right panels (threaded)
        2. on_done callback triggers post-processing:
           a. left_analysis: patient name recognition (multiprocessing)
           b. right_analysis: exam code recognition (multiprocessing)
           c. csv_matching: match OCR text to exam code database
           d. save_data: export to CSV
    """

    def __init__(self):
        self.queue = queue.Queue()
        self.data_dict = {}
        self.matched_data = []
        self.start_time = None
        self.ocr_running = True

    def run(self):
        log_message("Pipeline started")
        self.start_time = time.time()

        self.ocr_manager = OCRManager(
            self.queue,
            is_running_func=lambda: self.ocr_running,
            on_done_callback=self.on_done,
        )
        thread = threading.Thread(target=self.ocr_manager.run_ocr, daemon=True)
        thread.start()
        thread.join()

    def on_done(self):
        """Post-processing after capture completes."""
        log_message("[1] OCR complete -> post-processing")
        log_message(f"[OCR time] {time.time() - self.start_time:.2f}s")

        t1 = time.time()
        self.left_analysis()
        log_message(f"[2] left_analysis done: {time.time() - t1:.2f}s")

        t2 = time.time()
        self.right_analysis()
        log_message(f"[3] right_analysis done: {time.time() - t2:.2f}s")

        t3 = time.time()
        self.csv_matching()
        log_message(f"[4] csv_matching done: {time.time() - t3:.2f}s")

        t4 = time.time()
        self.save_data()
        log_message(f"[5] save_data done: {time.time() - t4:.2f}s")
        log_message(f"[Total time] {time.time() - self.start_time:.2f}s")

    def left_analysis(self):
        """Analyze left panel images (patient names) via multiprocessing."""
        left_results = analyze_left_images_with_progress()

        def extract_index(item):
            base = os.path.basename(item.get("image_path", ""))
            parts = base.split("_")
            if len(parts) >= 2 and parts[1].split(".")[0].isdigit():
                return int(parts[1].split(".")[0])
            return 0

        left_results.sort(key=extract_index)
        for item in left_results:
            self.queue.put(item["result"])

    def right_analysis(self):
        """Analyze right panel images (exam codes) via multiprocessing.
        Handles scroll-overlap deduplication via merge_without_overlap."""
        right_results = analyze_all_panel_images_with_progress()
        grouped = defaultdict(list)

        for item in right_results:
            path = os.path.basename(item["image_path"]).replace(".png", "")
            result = item["result"]
            parts = path.split("_")
            if len(parts) >= 2:
                group_key = f"{parts[0]}_{parts[1]}"
                index = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
                grouped[group_key].append((index, result))

        def merge_without_overlap(a, b):
            """Merge two lists, removing overlapping suffix/prefix."""
            max_overlap = 0
            for i in range(1, min(len(a), len(b)) + 1):
                if a[-i:] == b[:i]:
                    max_overlap = i
            return a + b[max_overlap:]

        for key, group_results in grouped.items():
            group_results.sort(key=lambda x: x[0])
            merged = []
            for _, result in group_results:
                lines = [result] if isinstance(result, str) else [
                    text for (_, _), (text, _) in result
                ]
                merged = lines if not merged else merge_without_overlap(merged, lines)
            self.data_dict[key] = "\n".join(merged)

    def csv_matching(self):
        """Match OCR-extracted exam names against code_list.csv database.

        Uses normalized text matching (I/i/L -> l, remove spaces) to handle
        common OCR misrecognition patterns.
        """
        csv_path = os.path.join(CONFIG_DIR, "code_list.csv")
        left_names = list(self.queue.queue)
        indices = range(len(left_names))

        matched_list = []
        for idx in indices:
            panel_name = left_names[idx] if idx < len(left_names) else f"Panel_{idx}"
            right_key = f"right_{idx}"
            opinion_key = f"opinion_{idx}"

            exam_lines = self.data_dict.get(right_key, "").splitlines()
            opinion_lines = self.data_dict.get(opinion_key, "").splitlines()
            total_lines = [l.strip() for l in (*exam_lines, *opinion_lines) if l.strip()]

            if not total_lines:
                matched_list.append([panel_name, "", "", ""])
                matched_list.append(["", "", "", ""])
            else:
                for line in total_lines:
                    matched_list.append([panel_name, line, "", ""])
                matched_list.append(["", "", "", ""])

        # CSV matching
        if not os.path.exists(csv_path):
            self.matched_data = matched_list
            self.save_data()
            return

        df = pd.read_csv(
            csv_path, header=None,
            names=["user_code", "exam_code", "exam_name"],
            encoding="utf-8",
        ).fillna("")

        code_map = defaultdict(list)
        for _, row in df.iterrows():
            norm = self.normalize_text(str(row["user_code"]).strip())
            code_map[norm].append((row["exam_code"], row["exam_name"], row["user_code"]))

        final_matched, multi_alerts = [], []
        for panel_name, line, _, _ in matched_list:
            norm = self.normalize_text(line)
            if norm in code_map and line:
                cand = code_map[norm]
                exam_code, exam_name, _ = cand[0]
                if len(cand) > 1:
                    multi_alerts.append(
                        f"[{panel_name}] \"{line}\" -> {len(cand)} matches"
                    )
            else:
                exam_code = exam_name = ""
            final_matched.append([panel_name, line, exam_code, exam_name])

        self.matched_data = final_matched

        if multi_alerts:
            print(f"[WARNING] Multiple matches: {len(multi_alerts)} cases")

    def normalize_text(self, text):
        """Normalize OCR text for fuzzy matching.
        Handles common misrecognitions: I/i/L -> l, remove spaces."""
        return text.replace(" ", "").replace("I", "l").replace("i", "l").replace("L", "l").lower()

    def save_data(self):
        """Export matched results to CSV."""
        CONFIG = load_config(config_path)
        capture_folder = CONFIG.get("capture_folder", "")
        result_filename = f"result_{capture_folder[-15:]}.csv"
        output_path = os.path.join(EXE_DIR, "result", capture_folder[-15:], result_filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["user_code", "exam_code", "exam_name"])
            for row in self.matched_data:
                writer.writerow(row)
        print(f"[SAVE] Results saved: {output_path}")


# ══════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════

def main():
    multiprocessing.freeze_support()
    gc.collect()
    ensure_directories()
    click_Tdrawgrid(1)
    runner = OCRRunner()
    runner.run()


if __name__ == "__main__":
    main()
