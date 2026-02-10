# Production Reference (U2Bio)

> **Note:** This directory contains sanitized versions of production code used at U2Bio.
> Hospital names, server paths, credentials, and patient data have been removed.

## System Overview

Automated EMR data extraction system deployed in a hospital environment.

### Architecture

```
EMR Application (Delphi TApplication)
     ↓
Win32 UI Automation (pywinauto + win32api)
     ↓
Screen Capture (PrintWindow API, DPI-aware)
     ↓
Image Preprocessing (adaptive threshold, grid line detection)
     ↓
Template Matching OCR (sliding window, MSE scoring)
     ↓
CSV Code Matching (fuzzy text normalization)
     ↓
Structured CSV Output
```

### Components

| File | Role |
|------|------|
| `extractor.py` | Pipeline orchestrator (OCRRunner: capture → analysis → matching → export) |
| `ocr_manager.py` | Win32 window automation, capture loop with scroll handling |
| `utils.py` | Template matching OCR, cell extraction, DPI handling, multiprocessing |
| `ui_bridge.py` | Thread-safe progress queue for UI updates |

### Key Technical Details

**Template Matching OCR** (NOT Tesseract/EasyOCR):
- Pre-captured character images used as templates
- Sliding window `recognize_variable_width()` scans image left-to-right
- MSE (Mean Squared Error) scoring for character matching
- Padding variants generated for vertical alignment tolerance
- Underscore vs hyphen disambiguation by vertical center position

**Win32 Automation**:
- `pywinauto` for Delphi TApplication/TDrawGrid control
- `PrintWindow` API for lossless window capture
- `SendMessage` for scroll control (WM_VSCROLL, WM_HSCROLL)
- Per-monitor DPI awareness (GetDpiForWindow, GetDpiForMonitor)

**Color-based Row Detection**:
- HSV color space → yellow/blue region extraction
- Yellow = selected patient row, Blue = header row
- Contour detection for bounding box extraction

**Multiprocessing**:
- `multiprocessing.Pool` with 90% CPU utilization
- Per-worker template loading (init_worker/init_worker2)
- PyInstaller-compatible (freeze_support, executable path fixes)

### Production Specs
- **Platform**: Windows (Delphi EMR application)
- **Throughput**: ~500 lab reports/day
- **Accuracy**: 99.2% on structured fields
- **Distribution**: PyInstaller EXE with version management
