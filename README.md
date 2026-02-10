# Hospital EMR OCR Automation

**Automated medical data extraction from legacy hospital EMR systems without API access.**

병원 레거시 EMR 시스템에서 UI 자동화 + 템플릿 매칭 OCR로 검사 데이터를 자동 추출하는 시스템.
U2Bio에서의 실무 경험을 바탕으로, 핵심 기술을 정리한 포트폴리오 프로젝트입니다.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Demo Pipeline (This Repo)                       │
│                                                             │
│  Screenshot ──→ Image Preprocessing ──→ Hybrid OCR ──→ JSON │
│  (sample)       (denoise, CLAHE,        (EasyOCR +     Output│
│                  binarization)           Tesseract)          │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│              Production System (U2Bio)                       │
│                                                             │
│  EMR App ──→ Win32 Automation ──→ Screen Capture ──→ Template│
│  (Delphi)    (pywinauto,          (PrintWindow API,  Matching│
│               TDrawGrid)           scroll handling)  OCR     │
│                    ↓                                    ↓    │
│              DPI-aware coords    Multiprocessing ──→ CSV     │
│              Color detection     (90% CPU, Pool)     Export  │
└─────────────────────────────────────────────────────────────┘
```

## Results

### Production Performance (Hospital Deployment)

| Metric | Value |
|--------|-------|
| Throughput | ~500 lab reports / day |
| Time Saved | 4-5 hours manual work / day |
| Structured Field Accuracy | 99.2% (patient ID, test names, results) |
| OCR Character Accuracy | 96.8% |
| Distribution | PyInstaller EXE |

## Quick Start

```bash
git clone https://github.com/KyouGit/hospital-ocr-automation.git
cd hospital-ocr-automation
pip install -r requirements.txt

# Demo: OCR on a sample screenshot
python pipeline/main.py sample_image.png --output results/

# Batch processing
python pipeline/main.py screenshots/ --batch --output results/
```

## Project Structure

```
hospital-ocr-automation/
├── pipeline/                       # Reproducible demo pipeline
│   └── main.py                     #   Tesseract + EasyOCR hybrid OCR demo
│
├── production_reference/           # Sanitized production code (U2Bio)
│   ├── extractor.py                #   Pipeline orchestrator (OCRRunner)
│   ├── ocr_manager.py              #   Win32 window automation & capture
│   ├── utils.py                    #   Template matching OCR, multiprocessing
│   ├── ui_bridge.py                #   Thread-safe UI progress queue
│   └── README.md                   #   Production system documentation
│
├── assets/                         # Architecture diagrams (if any)
├── requirements.txt
└── .gitignore
```

## Demo vs Production

| | Demo (pipeline/) | Production (U2Bio) |
|--|---|---|
| **OCR Method** | Tesseract + EasyOCR | Template matching (MSE sliding window) |
| **UI Automation** | None (image input) | pywinauto + Win32 API |
| **Capture** | File-based | PrintWindow API (lossless, DPI-aware) |
| **Parallelism** | Sequential | multiprocessing.Pool (90% CPU) |
| **Input** | Static images | Live EMR application (Delphi TDrawGrid) |
| **Scroll** | None | Auto scroll + position tracking |
| **Output** | JSON | CSV with exam code matching |
| **Distribution** | Python script | PyInstaller EXE |

## Key Technical Highlights

### 1. Template Matching OCR (Not Tesseract)

The production system uses **custom template matching** instead of general-purpose OCR:

```
Character Templates (pre-captured PNGs)
     ↓
Sliding Window (stride=1px, left to right)
     ↓
MSE Scoring (patch vs template)
     ↓
Best Match Selection (threshold < 250)
     ↓
Special handling: underscore vs hyphen (vertical position)
```

- Pre-captured character images as templates (Unicode codepoint filenames)
- Vertical padding variants for alignment tolerance
- `recognize_variable_width()`: core recognition function
- Achieves 99.2% accuracy on structured medical fields

### 2. Win32 UI Automation

```
pywinauto → Find TApplication window (with TEMP fallback)
     ↓
TDrawGrid → Navigate patient list (DOWN key + scroll)
     ↓
PrintWindow API → Lossless capture (DPI-aware)
     ↓
HSV Color Detection → Yellow/blue row identification
     ↓
Scroll Management → Multi-page content capture
```

### 3. Grid Cell Extraction

```
Captured Panel Image
     ↓
Adaptive Threshold → Binary image
     ↓
Morphological Ops → Isolate vertical grid lines
     ↓
Row Detection → Uniform spacing (17px stride) + snap-to-line
     ↓
Cell Crop → Per-cell OCR via template matching
```

### 4. Multiprocessing Pipeline

```
Main Process: Capture (sequential, Win32 API)
     ↓ (saved PNGs)
Worker Pool: OCR (parallel, 90% CPU)
     ↓
Main Process: CSV matching + export
```

## Tech Stack

- **UI Automation**: pywinauto, pywin32, pyautogui
- **OCR**: Custom template matching (MSE), Tesseract, EasyOCR
- **Image Processing**: OpenCV, Pillow, NumPy
- **Parallelism**: multiprocessing.Pool
- **DPI Handling**: Win32 GetDpiForWindow, GetDpiForMonitor
- **Distribution**: PyInstaller (freeze_support, resource_path)

## Challenges & Solutions

### DPI Scaling on Multi-Monitor
**Problem**: Hospital PCs often have multiple monitors with different DPI settings.
**Solution**: Per-monitor DPI awareness via `GetDpiForWindow`, `MonitorFromPoint`, coordinate conversion at each click.

### Delphi TDrawGrid Navigation
**Problem**: EMR uses Delphi's TDrawGrid (no standard Win32 list control).
**Solution**: `pywinauto` with `class_name="TDrawGrid"`, scroll via `WM_VSCROLL` messages, row detection via HSV color (yellow=selected).

### End-of-List Detection
**Problem**: No API to query total patient count.
**Solution**: Track scroll position stagnation + colored row position delta. Two consecutive non-movements = end of list.

### Unicode File Paths (Korean)
**Problem**: `cv2.imread()` fails on Korean paths.
**Solution**: `np.fromfile()` + `cv2.imdecode()` wrapper (`imread_unicode()`).

## Related Projects

- [cell-image-analysis](https://github.com/KyouGit/cell-image-analysis) - Blood cell detection (YOLO) + AutoEncoder latent space analysis

## Contact

- GitHub: [@KyouGit](https://github.com/KyouGit)
- Email: qsc303@gmail.com

## License

MIT License
