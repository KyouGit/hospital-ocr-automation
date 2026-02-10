"""
UI Bridge - Progress Reporting Queue (Production Reference)

Thread-safe progress communication between OCR workers and the UI.
Uses a queue-based pattern to decouple processing from UI updates.

Status tracking:
- patient_done / patient_total: Left panel (patient list) progress
- code_done / code_total: Right panel (exam code) progress
"""

import queue

UI_QUEUE = queue.Queue()

UI_STATUS = {
    "patient_done": 0,
    "patient_total": 0,
    "code_done": 0,
    "code_total": 0,
}


def ui_set_text(text):
    """Push arbitrary text message to UI."""
    UI_QUEUE.put({"type": "text", "text": text})


def ui_close():
    """Signal UI to close."""
    UI_QUEUE.put({"type": "stop"})


def ui_inc_left():
    """Increment patient analysis completion count."""
    UI_STATUS["patient_done"] += 1
    UI_QUEUE.put({"type": "status", "status": UI_STATUS})


def ui_inc_right():
    """Increment exam code analysis completion count."""
    UI_STATUS["code_done"] += 1
    UI_QUEUE.put({"type": "status", "status": UI_STATUS})


def ui_set_patient_total():
    """Increment total patient count (discovered during capture)."""
    UI_STATUS["patient_total"] += 1
    UI_QUEUE.put({"type": "status", "status": UI_STATUS})


def ui_set_code_total():
    """Increment total exam code count (discovered during capture)."""
    UI_STATUS["code_total"] += 1
    UI_QUEUE.put({"type": "status", "status": UI_STATUS})


def ui_set_state(state: str):
    """Push state change notification to UI."""
    UI_QUEUE.put({"type": "state", "state": state})
