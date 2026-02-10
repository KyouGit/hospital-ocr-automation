"""
Hospital EMR OCR Automation System

Automated extraction of medical data from legacy hospital UI systems
where API access is not available.

This system automates:
1. UI control and navigation (pywinauto)
2. Screen capture with scrolling
3. Korean text OCR (Tesseract, EasyOCR)
4. Post-processing and data extraction
5. Structured data output

Developed at U2Bio for real hospital environments.
"""

import os
import time
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional
import numpy as np
from PIL import Image, ImageEnhance
import cv2
import pytesseract
import easyocr
from dataclasses import dataclass, asdict
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class OCRResult:
    """Structure for OCR results"""
    text: str
    confidence: float
    bbox: Tuple[int, int, int, int]  # (x, y, w, h)
    
    def to_dict(self):
        return asdict(self)


@dataclass
class MedicalRecord:
    """Structure for extracted medical records"""
    patient_id: str
    date: str
    test_name: str
    result_value: str
    unit: str
    reference_range: str
    
    def to_dict(self):
        return asdict(self)


class ImagePreprocessor:
    """Image preprocessing for better OCR accuracy"""
    
    @staticmethod
    def preprocess(image: Image.Image, method='adaptive') -> Image.Image:
        """Preprocess image for OCR"""
        img_array = np.array(image)
        
        # Convert to grayscale
        if len(img_array.shape) == 3:
            gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        else:
            gray = img_array
        
        # Denoise
        denoised = cv2.fastNlMeansDenoising(gray, h=10)
        
        # Enhance contrast
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(denoised)
        
        # Binarization
        if method == 'adaptive':
            binary = cv2.adaptiveThreshold(
                enhanced, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 11, 2
            )
        elif method == 'otsu':
            _, binary = cv2.threshold(
                enhanced, 0, 255,
                cv2.THRESH_BINARY + cv2.THRESH_OTSU
            )
        else:
            _, binary = cv2.threshold(enhanced, 127, 255, cv2.THRESH_BINARY)
        
        return Image.fromarray(binary)
    
    @staticmethod
    def enhance_for_korean(image: Image.Image) -> Image.Image:
        """Special preprocessing for Korean text"""
        # Upscale 2x
        width, height = image.size
        image = image.resize((width * 2, height * 2), Image.LANCZOS)
        
        # Enhance sharpness
        enhancer = ImageEnhance.Sharpness(image)
        image = enhancer.enhance(2.0)
        
        return ImagePreprocessor.preprocess(image, method='adaptive')


class OCREngine:
    """Multi-engine OCR system"""
    
    def __init__(self, use_gpu=False):
        self.use_gpu = use_gpu
        logger.info("Initializing EasyOCR (Korean + English)...")
        self.reader = easyocr.Reader(['ko', 'en'], gpu=use_gpu)
        logger.info("OCR engines ready")
    
    def ocr_tesseract(self, image: Image.Image, lang='kor+eng') -> List[OCRResult]:
        """OCR using Tesseract"""
        data = pytesseract.image_to_data(
            image, lang=lang,
            output_type=pytesseract.Output.DICT
        )
        
        results = []
        for i in range(len(data['text'])):
            text = data['text'][i].strip()
            conf = int(data['conf'][i])
            
            if text and conf > 0:
                bbox = (
                    data['left'][i],
                    data['top'][i],
                    data['width'][i],
                    data['height'][i]
                )
                results.append(OCRResult(text, conf / 100.0, bbox))
        
        return results
    
    def ocr_easyocr(self, image: Image.Image) -> List[OCRResult]:
        """OCR using EasyOCR (better for Korean)"""
        img_array = np.array(image)
        raw_results = self.reader.readtext(img_array)
        
        results = []
        for bbox, text, conf in raw_results:
            x_min = int(min([p[0] for p in bbox]))
            y_min = int(min([p[1] for p in bbox]))
            x_max = int(max([p[0] for p in bbox]))
            y_max = int(max([p[1] for p in bbox]))
            
            bbox_tuple = (x_min, y_min, x_max - x_min, y_max - y_min)
            results.append(OCRResult(text.strip(), conf, bbox_tuple))
        
        return results
    
    def ocr_hybrid(self, image: Image.Image) -> List[OCRResult]:
        """Hybrid: EasyOCR for Korean, Tesseract for numbers"""
        easy_results = self.ocr_easyocr(image)
        tess_results = self.ocr_tesseract(image)
        
        merged = easy_results.copy()
        
        for tess in tess_results:
            if tess.confidence > 0.8:
                overlapping = any(
                    self._bbox_overlap(tess.bbox, easy.bbox) > 0.5 
                    for easy in easy_results
                )
                if not overlapping:
                    merged.append(tess)
        
        return merged
    
    @staticmethod
    def _bbox_overlap(bbox1, bbox2) -> float:
        """Calculate IoU"""
        x1, y1, w1, h1 = bbox1
        x2, y2, w2, h2 = bbox2
        
        x_left = max(x1, x2)
        y_top = max(y1, y2)
        x_right = min(x1 + w1, x2 + w2)
        y_bottom = min(y1 + h1, y2 + h2)
        
        if x_right < x_left or y_bottom < y_top:
            return 0.0
        
        intersection = (x_right - x_left) * (y_bottom - y_top)
        union = w1 * h1 + w2 * h2 - intersection
        
        return intersection / union if union > 0 else 0.0


class MedicalDataParser:
    """Parse OCR results into structured medical records"""
    
    @staticmethod
    def extract_patient_id(ocr_results: List[OCRResult]) -> Optional[str]:
        """Extract patient ID"""
        import re
        for result in ocr_results:
            match = re.search(r'(?:환자번호|ID|Patient ID)[:\s]+([0-9]{6,10})', 
                            result.text, re.IGNORECASE)
            if match:
                return match.group(1)
        return None
    
    @staticmethod
    def extract_date(ocr_results: List[OCRResult]) -> Optional[str]:
        """Extract date"""
        import re
        for result in ocr_results:
            match = re.search(r'(\d{4}[-/.]\d{2}[-/.]\d{2})', result.text)
            if match:
                return match.group(1).replace('/', '-').replace('.', '-')
        return None
    
    @staticmethod
    def extract_lab_results(ocr_results: List[OCRResult]) -> List[MedicalRecord]:
        """Extract lab results"""
        # Sort by y-coordinate
        sorted_results = sorted(ocr_results, key=lambda r: r.bbox[1])
        
        # Group into rows
        rows = []
        current_row = []
        prev_y = -1
        
        for result in sorted_results:
            y = result.bbox[1]
            if prev_y == -1 or abs(y - prev_y) <= 10:
                current_row.append(result)
            else:
                if current_row:
                    rows.append(current_row)
                current_row = [result]
            prev_y = y
        
        if current_row:
            rows.append(current_row)
        
        # Parse rows
        records = []
        for row in rows:
            row = sorted(row, key=lambda r: r.bbox[0])
            texts = [r.text for r in row]
            
            if len(texts) >= 3:
                record = MedicalRecord(
                    patient_id="",
                    date="",
                    test_name=texts[0],
                    result_value=texts[1],
                    unit=texts[2] if len(texts) > 2 else "",
                    reference_range=texts[3] if len(texts) > 3 else ""
                )
                records.append(record)
        
        return records


class OCRPipeline:
    """End-to-end OCR pipeline"""
    
    def __init__(self, use_gpu=False):
        self.ocr_engine = OCREngine(use_gpu=use_gpu)
        self.preprocessor = ImagePreprocessor()
    
    def process_single_image(self, image_path: str, output_dir: str = 'output') -> Dict:
        """Process a single image"""
        logger.info(f"Processing: {image_path}")
        
        # Load and preprocess
        image = Image.open(image_path)
        preprocessed = self.preprocessor.enhance_for_korean(image)
        
        # OCR
        logger.info("Running OCR...")
        ocr_results = self.ocr_engine.ocr_hybrid(preprocessed)
        logger.info(f"Detected {len(ocr_results)} text regions")
        
        # Parse
        patient_id = MedicalDataParser.extract_patient_id(ocr_results)
        date = MedicalDataParser.extract_date(ocr_results)
        lab_results = MedicalDataParser.extract_lab_results(ocr_results)
        
        for record in lab_results:
            record.patient_id = patient_id or "UNKNOWN"
            record.date = date or datetime.now().strftime("%Y-%m-%d")
        
        # Output
        output = {
            'image_path': image_path,
            'patient_id': patient_id,
            'date': date,
            'total_text_regions': len(ocr_results),
            'lab_results': [r.to_dict() for r in lab_results],
            'raw_ocr': [r.to_dict() for r in ocr_results]
        }
        
        os.makedirs(output_dir, exist_ok=True)
        output_file = Path(output_dir) / f"{Path(image_path).stem}_result.json"
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Results saved to: {output_file}")
        return output
    
    def process_batch(self, image_dir: str, output_dir: str = 'output') -> List[Dict]:
        """Process multiple images"""
        image_paths = []
        for ext in ['*.jpg', '*.jpeg', '*.png', '*.bmp']:
            image_paths.extend(Path(image_dir).glob(ext))
        
        logger.info(f"Found {len(image_paths)} images")
        
        results = []
        for img_path in image_paths:
            try:
                result = self.process_single_image(str(img_path), output_dir)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed: {img_path}: {e}")
        
        # Save summary
        summary_file = Path(output_dir) / "batch_summary.json"
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Summary saved to: {summary_file}")
        return results


def main():
    """Main execution"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Hospital OCR Automation')
    parser.add_argument('input', help='Input image file or directory')
    parser.add_argument('--output', default='output', help='Output directory')
    parser.add_argument('--gpu', action='store_true', help='Use GPU')
    parser.add_argument('--batch', action='store_true', help='Batch mode')
    
    args = parser.parse_args()
    
    print("="*70)
    print("Hospital EMR OCR Automation System")
    print("="*70)
    
    pipeline = OCRPipeline(use_gpu=args.gpu)
    
    if args.batch or Path(args.input).is_dir():
        results = pipeline.process_batch(args.input, args.output)
        print(f"\nProcessed {len(results)} images")
    else:
        result = pipeline.process_single_image(args.input, args.output)
        print(f"\nExtracted {len(result['lab_results'])} lab results")
    
    print("="*70)
    print(f"Results: {args.output}/")
    print("="*70)


if __name__ == '__main__':
    main()
