"""
Number-plate recognition.

Pipeline (all local, CPU, no API keys):
  1. fast-alpr: YOLOv9 licence-plate detector (ONNX) + CCT plate OCR (ONNX).
     Model weights download once from GitHub releases and are cached.
  2. Indian plate normalisation: position-aware fixes for common OCR
     confusions (O/0, I/1, B/8, S/5, Z/2, G/6) and format validation.
  3. Optional EasyOCR fallback if fast-alpr finds no plate (only if installed).

The output is advisory. Low-confidence or invalid-format reads are routed to
human review by the gate decision logic; they are never auto-approved.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field, asdict

import cv2
import numpy as np
from django.conf import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- formats
# Standard: PB 11 AB 1234  |  PB 11 A 1234  |  DL 3C AB 1234  |  BH series: 22 BH 1234 AA
STD_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{1,4}$")
STRICT_STD_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{4}$")
BH_RE = re.compile(r"^\d{2}BH\d{4}[A-Z]{1,2}$")
STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "DN", "GA", "GJ", "HP", "HR", "JH",
    "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP", "MZ", "NL", "OD", "OR", "PB", "PY",
    "RJ", "SK", "TN", "TR", "TS", "UK", "UP", "WB",
}

TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "T": "1", "Z": "2", "S": "5",
            "B": "8", "G": "6", "A": "4"}
TO_ALPHA = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A", "7": "T"}


def clean(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _fix(ch: str, want_digit: bool) -> str:
    if want_digit:
        return ch if ch.isdigit() else TO_DIGIT.get(ch, ch)
    return ch if ch.isalpha() else TO_ALPHA.get(ch, ch)


def normalize_plate(text: str) -> tuple[str, bool]:
    """Return (normalised_plate, is_valid_indian_format)."""
    t = clean(text)
    if not t:
        return "", False
    if BH_RE.match(t):
        return t, True

    # BH series repair: NN BH NNNN X(X)
    if len(t) in (9, 10) and t[2:4] in ("BH", "8H", "B4"):
        cand = "".join(_fix(c, True) for c in t[:2]) + "BH" + \
               "".join(_fix(c, True) for c in t[4:8]) + "".join(_fix(c, False) for c in t[8:])
        if BH_RE.match(cand):
            return cand, True

    # Standard repair: AA N(N) [A..AAA] NNNN  (state, RTO digits, series letters, number)
    best = None
    for rto_len in (2, 1):
        for series_len in (2, 1, 3, 0):
            n = 2 + rto_len + series_len + 4
            if len(t) != n:
                continue
            a, b = 2 + rto_len, 2 + rto_len + series_len
            cand = ("".join(_fix(c, False) for c in t[:2]) +
                    "".join(_fix(c, True) for c in t[2:a]) +
                    "".join(_fix(c, False) for c in t[a:b]) +
                    "".join(_fix(c, True) for c in t[b:]))
            if STRICT_STD_RE.match(cand) or (series_len == 0 and re.match(r"^[A-Z]{2}\d{6}$", cand)):
                score = (2 if cand[:2] in STATE_CODES else 0) + (1 if rto_len == 2 else 0) + (1 if series_len else 0)
                if best is None or score > best[0]:
                    best = (score, cand)
    if best:
        return best[1], best[1][:2] in STATE_CODES
    return t, bool(STD_RE.match(t) and t[:2] in STATE_CODES)


def pretty_plate(p: str) -> str:
    p = clean(p)
    m = re.match(r"^([A-Z]{2})(\d{1,2})([A-Z]{0,3})(\d{1,4})$", p)
    if m:
        return " ".join(x for x in m.groups() if x)
    m = re.match(r"^(\d{2})(BH)(\d{4})([A-Z]{1,2})$", p)
    if m:
        return " ".join(m.groups())
    return p


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------- results
@dataclass
class PlateRead:
    plate: str                 # normalised
    raw: str                   # OCR raw text
    confidence: float          # combined detector*ocr confidence 0..1
    det_confidence: float
    ocr_confidence: float
    valid_format: bool
    bbox: list[int] = field(default_factory=list)   # x1,y1,x2,y2
    engine: str = "fast-alpr"

    def to_dict(self):
        d = asdict(self)
        d["pretty"] = pretty_plate(self.plate)
        return d


# ---------------------------------------------------------------- engine
_lock = threading.Lock()
_alpr = None
_easy = None
_easy_failed = False


def get_alpr():
    global _alpr
    if _alpr is None:
        with _lock:
            if _alpr is None:
                from fast_alpr import ALPR
                cfg = settings.VISION
                log.info("Loading ALPR models %s / %s", cfg["PLATE_DETECTOR"], cfg["PLATE_OCR"])
                _alpr = ALPR(
                    detector_model=cfg["PLATE_DETECTOR"],
                    detector_conf_thresh=cfg["PLATE_DET_CONF"],
                    ocr_model=cfg["PLATE_OCR"],
                    ocr_device="cpu",
                )
    return _alpr


def _get_easyocr():
    global _easy, _easy_failed
    if _easy is None and not _easy_failed:
        try:
            import easyocr  # optional
            _easy = easyocr.Reader(["en"], gpu=False, verbose=False)
        except Exception as e:  # not installed or failed
            log.info("EasyOCR fallback unavailable: %s", e)
            _easy_failed = True
    return _easy


def _mean_conf(c) -> float:
    if c is None:
        return 0.0
    if isinstance(c, (list, tuple, np.ndarray)):
        arr = [float(x) for x in c]
        return float(np.mean(arr)) if arr else 0.0
    return float(c)


def decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image")
    # keep things fast on big phone photos
    h, w = img.shape[:2]
    m = settings.VISION["MAX_SIDE"]
    if max(h, w) > m:
        s = m / max(h, w)
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return img


def _ocr_variants(alpr, crop):
    """OCR the crop plus an enhanced copy; return best (text, conf)."""
    outs = []
    try:
        r = alpr.ocr.predict(crop)
        if r and r.text:
            outs.append((r.text, _mean_conf(r.confidence)))
    except Exception as e:
        log.debug("ocr fail %s", e)
    try:
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(g)
        r = alpr.ocr.predict(cv2.cvtColor(g, cv2.COLOR_GRAY2BGR))
        if r and r.text:
            outs.append((r.text, _mean_conf(r.confidence)))
    except Exception:
        pass
    if not outs:
        return "", 0.0

    def score(o):
        _, valid = normalize_plate(o[0])
        return (valid, o[1])
    return max(outs, key=score)


def read_plates(img: np.ndarray, max_plates: int = 5) -> list[PlateRead]:
    """Detect and read all plates in an image (BGR). Best first."""
    reads: list[PlateRead] = []
    alpr = get_alpr()
    H, W = img.shape[:2]
    boxes = [(d.bounding_box.x1, d.bounding_box.y1, d.bounding_box.x2, d.bounding_box.y2, float(d.confidence))
             for d in alpr.detector.predict(img)]
    if not boxes and max(H, W) > 900:
        # small/distant plates: run the detector on overlapping 2x2 tiles
        th, tw = int(H * 0.6), int(W * 0.6)
        for ty in (0, H - th):
            for tx in (0, W - tw):
                for d in alpr.detector.predict(img[ty:ty + th, tx:tx + tw]):
                    b = d.bounding_box
                    boxes.append((b.x1 + tx, b.y1 + ty, b.x2 + tx, b.y2 + ty, float(d.confidence)))
        boxes.sort(key=lambda x: -x[4])
        dedup = []
        for bx in boxes:
            if all(not (bx[0] < o[2] and o[0] < bx[2] and bx[1] < o[3] and o[1] < bx[3]) for o in dedup):
                dedup.append(bx)
        boxes = dedup
    if not boxes and W / max(H, 1) >= 2.2:
        boxes = [(0, 0, W, H, 0.5)]  # the image itself is a tight plate crop

    class _B:  # tiny adapter so the loop below reads like detector output
        def __init__(self, t):
            self.x1, self.y1, self.x2, self.y2, self.confidence = t
    for d in [_B(t) for t in boxes][: max_plates * 2]:
        b = d
        # pad crop a little: OCR prefers a margin around the characters
        pw, ph = int((b.x2 - b.x1) * 0.06), int((b.y2 - b.y1) * 0.12)
        x1, y1 = max(b.x1 - pw, 0), max(b.y1 - ph, 0)
        x2, y2 = min(b.x2 + pw, W), min(b.y2 + ph, H)
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        text, oconf = _ocr_variants(alpr, crop)
        plate, valid = normalize_plate(text)
        if not plate:
            continue
        dconf = float(d.confidence)
        conf = round(oconf * (0.6 + 0.4 * dconf), 4)
        reads.append(PlateRead(plate, text, conf, round(dconf, 4), round(oconf, 4), valid,
                               [int(b.x1), int(b.y1), int(b.x2), int(b.y2)]))

    if not reads and settings.VISION.get("EASYOCR_FALLBACK", True):
        reader = _get_easyocr()
        if reader is not None:
            for (_box, text, conf) in reader.readtext(img):
                plate, valid = normalize_plate(text)
                if len(plate) >= 6:
                    xs = [int(p[0]) for p in _box]; ys = [int(p[1]) for p in _box]
                    reads.append(PlateRead(plate, text, round(float(conf) * 0.9, 4), 0.0,
                                           round(float(conf), 4), valid,
                                           [min(xs), min(ys), max(xs), max(ys)], "easyocr"))

    reads.sort(key=lambda r: (r.valid_format, r.confidence), reverse=True)
    return reads[:max_plates]


def annotate_plates(img: np.ndarray, reads: list[PlateRead]) -> np.ndarray:
    out = img.copy()
    for r in reads:
        if not r.bbox:
            continue
        x1, y1, x2, y2 = r.bbox
        color = (60, 180, 75) if r.valid_format and r.confidence >= 0.8 else (0, 165, 255)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 3)
        label = f"{pretty_plate(r.plate)}  {r.confidence:.0%}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        cv2.rectangle(out, (x1, max(y1 - th - 12, 0)), (x1 + tw + 10, y1), color, -1)
        cv2.putText(out, label, (x1 + 5, y1 - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    return out
