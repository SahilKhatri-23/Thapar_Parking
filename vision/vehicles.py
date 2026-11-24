"""
Parking occupancy by vehicle detection.

Model: RF-DETR (COCO, ONNX via open-image-models): a real-time detection
transformer, runs on CPU with onnxruntime, no PyTorch / API keys needed.

The admin enters the number of slots for a lot; we count four-wheel vehicles
(car / truck / bus; campus golf-carts are reported as car/truck by COCO models)
inside an optional detection zone (polygon) and compute
available = total_slots - occupied - active reservations.
Two-wheelers are counted separately.
"""
from __future__ import annotations

import logging
import threading

import cv2
import numpy as np
from django.conf import settings

log = logging.getLogger(__name__)

FOUR_WHEEL = {"car", "truck", "bus"}
TWO_WHEEL = {"motorcycle", "bicycle"}
# per-class minimum confidence (truck/bus produce more false positives on booths, sheds)
CLASS_MIN = {"car": 0.40, "truck": 0.45, "bus": 0.45, "motorcycle": 0.40, "bicycle": 0.45}

_lock = threading.Lock()
_det = None


def get_detector():
    global _det
    if _det is None:
        with _lock:
            if _det is None:
                from open_image_models import create_detector
                name = settings.VISION["VEHICLE_MODEL"]
                log.info("Loading vehicle detector %s", name)
                _det = create_detector(name, conf_thresh=0.3)
    return _det


def _iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua else 0.0


def _contain(a, b):
    """fraction of the smaller box covered by the intersection"""
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    s = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return inter / s if s else 0.0


def _nms(items, iou_thr=0.5, contain_thr=0.85):
    items = sorted(items, key=lambda d: d["confidence"], reverse=True)
    keep = []
    for d in items:
        if all(_iou(d["bbox"], k["bbox"]) < iou_thr and _contain(d["bbox"], k["bbox"]) < contain_thr
               for k in keep):
            keep.append(d)
    return keep


def _in_zone(bbox, zone_px):
    if zone_px is None:
        return True
    cx = (bbox[0] + bbox[2]) / 2
    cy = bbox[1] + (bbox[3] - bbox[1]) * 0.7  # lower-centre ~ where the vehicle touches ground
    return cv2.pointPolygonTest(zone_px, (float(cx), float(cy)), False) >= 0


def detect_vehicles(img: np.ndarray, zone: list | None = None) -> dict:
    """
    img: BGR image. zone: optional list of [x, y] points normalised 0..1.
    Returns counts and boxes (pixel coords).
    """
    det = get_detector()
    H, W = img.shape[:2]
    raw = det.predict(img)
    items = []
    for d in raw:
        lab = d.label
        if lab not in FOUR_WHEEL and lab not in TWO_WHEEL:
            continue
        if d.confidence < CLASS_MIN.get(lab, 0.4):
            continue
        b = d.bounding_box
        items.append({"label": lab, "confidence": round(float(d.confidence), 3),
                      "bbox": [int(b.x1), int(b.y1), int(b.x2), int(b.y2)]})

    fw = _nms([i for i in items if i["label"] in FOUR_WHEEL])
    tw = _nms([i for i in items if i["label"] in TWO_WHEEL])
    # a "car" box that is mostly a detected two-wheeler is a misclassification
    fw = [c for c in fw if not any(_contain(c["bbox"], m["bbox"]) > 0.7 and m["confidence"] >= c["confidence"]
                                   for m in tw)]

    zone_px = None
    if zone and len(zone) >= 3:
        zone_px = np.array([[int(x * W), int(y * H)] for x, y in zone], dtype=np.int32)
    for d in fw + tw:
        d["in_zone"] = _in_zone(d["bbox"], zone_px)

    four = [d for d in fw if d["in_zone"]]
    two = [d for d in tw if d["in_zone"]]
    return {
        "four_wheelers": len(four),
        "two_wheelers": len(two),
        "detections": fw + tw,
        "image_size": [W, H],
        "model": settings.VISION["VEHICLE_MODEL"],
        "zone_px": zone_px.tolist() if zone_px is not None else None,
    }


def annotate_vehicles(img: np.ndarray, result: dict, total_slots: int | None = None) -> np.ndarray:
    out = img.copy()
    H, W = out.shape[:2]
    t = max(2, int(max(H, W) / 500))
    zp = result.get("zone_px")
    if zp:
        overlay = out.copy()
        cv2.fillPoly(overlay, [np.array(zp, dtype=np.int32)], (255, 200, 0))
        out = cv2.addWeighted(overlay, 0.12, out, 0.88, 0)
        cv2.polylines(out, [np.array(zp, dtype=np.int32)], True, (255, 200, 0), t)
    for d in result["detections"]:
        x1, y1, x2, y2 = d["bbox"]
        if not d.get("in_zone", True):
            color = (150, 150, 150)
        elif d["label"] in FOUR_WHEEL:
            color = (43, 30, 180)   # maroon-ish red (BGR)
        else:
            color = (230, 160, 30)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, t)
        lab = f"{'vehicle' if d['label'] in FOUR_WHEEL else '2W'} {d['confidence']:.2f}"
        fs = 0.5 * t / 2 + 0.3
        (tw_, th_), _ = cv2.getTextSize(lab, cv2.FONT_HERSHEY_SIMPLEX, fs, max(1, t // 2))
        cv2.rectangle(out, (x1, max(y1 - th_ - 8, 0)), (x1 + tw_ + 8, y1), color, -1)
        cv2.putText(out, lab, (x1 + 4, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, fs, (255, 255, 255), max(1, t // 2))
    # banner
    occ = result["four_wheelers"]
    txt = f"Occupied: {occ}"
    if total_slots:
        txt += f" / {total_slots}   Available: {max(total_slots - occ, 0)}"
    txt += f"   2W: {result['two_wheelers']}"
    fs = max(0.7, W / 1600)
    (tw_, th_), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, fs, 2)
    cv2.rectangle(out, (0, 0), (tw_ + 30, th_ + 26), (32, 20, 90), -1)
    cv2.putText(out, txt, (15, th_ + 12), cv2.FONT_HERSHEY_SIMPLEX, fs, (255, 255, 255), 2)
    return out
