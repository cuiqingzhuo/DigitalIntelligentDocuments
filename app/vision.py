import base64
import binascii
import hashlib
import io
from functools import lru_cache
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_IMAGE = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000
inference_lock = Lock()


def decode_image(encoded, max_size=MAX_IMAGE, limit=1600):
    try:
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > max_size:
            raise ValueError("图片大小超出限制")
        with Image.open(io.BytesIO(data)) as opened:
            if opened.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError("请使用 JPEG、PNG 或 WebP 图片")
            if opened.width * opened.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("图片分辨率过高")
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((limit, limit))
            return image
    except (OSError, binascii.Error, Image.DecompressionBombError) as error:
        raise ValueError("无法读取图片，请重新选择") from error


def jpg_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88, optimize=True)
    return buffer.getvalue()


def peaks(projection, threshold, separation):
    indices = np.flatnonzero(projection > threshold)
    groups = np.split(indices, np.flatnonzero(np.diff(indices) > separation) + 1)
    return [int(group[np.argmax(projection[group])]) for group in groups if group.size]


def rectangular_spines(image):
    """Detect upright spines from persistent color edges and horizontal shelf separators."""
    height, width = image.shape[:2]
    smooth = cv2.GaussianBlur(image, (3, 3), 0).astype(np.float32)
    y_edges = np.abs(smooth[2:] - smooth[:-2]).max(axis=2)
    yline = (y_edges > 22).mean(axis=1)
    separators = [0] + [y+1 for y in peaks(yline, .58, max(9, height//28))
                       if height*.13 < y < height*.87] + [height]
    boxes = []
    for top, bottom in zip(separators, separators[1:]):
        band_height = bottom - top
        if band_height < height*.18:
            continue
        trim = max(1, band_height//12)
        band = smooth[top+trim:bottom-trim]
        xline = (np.abs(band[:, 2:] - band[:, :-2]).max(axis=2) > 20).mean(axis=0)
        interior = [x+1 for x in peaks(xline, .47, max(5, width//85))
                    if width*.018 < x < width*.982]
        bounds = [0, *interior, width]
        for left, right in zip(bounds, bounds[1:]):
            if max(16, width*.019) <= right-left <= min(width*.46, band_height*1.65):
                boxes.append([[left, top], [right, top], [right, bottom], [left, bottom]])
    return boxes


def order_quad(points):
    points = sorted(points, key=lambda item: item[1])
    top = sorted(points[:2], key=lambda item: item[0])
    bottom = sorted(points[2:], key=lambda item: item[0], reverse=True)
    return np.asarray(top + bottom, np.float32)


def perspective_crop(image, points):
    source = order_quad(points)
    widths = [np.linalg.norm(source[0]-source[1]), np.linalg.norm(source[3]-source[2])]
    heights = [np.linalg.norm(source[0]-source[3]), np.linalg.norm(source[1]-source[2])]
    width, height = max(2, int(max(widths))), max(2, int(max(heights)))
    target = np.asarray([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]], np.float32)
    return cv2.warpPerspective(image, cv2.getPerspectiveTransform(source, target), (width, height))


@lru_cache(maxsize=1)
def ocr_engine():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)


@lru_cache(maxsize=3)
def obb_engine(path):
    from ultralytics import YOLO
    return YOLO(path, task="obb")


def check_checkpoint(path, expected):
    file = Path(path)
    if not file.is_file():
        raise ValueError("冻结的检测模型文件不可用")
    with file.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("检测模型文件已变化，请发布新模型版本")


def boxes_for(image, detector=None):
    if detector:
        result = obb_engine(detector).predict(image, imgsz=640, conf=.25, iou=.5, max_det=80, verbose=False)[0]
        return [] if result.obb is None else result.obb.xyxyxyxy.cpu().numpy().tolist()
    return rectangular_spines(image)


def best_ocr(image):
    variants = [(image, "horizontal")]
    if image.shape[0] > image.shape[1] * 1.10:
        variants.extend([(cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE), "vertical"),
                         (cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE), "vertical")])
    observations = []
    for variant, direction in variants:
        border = 8
        padded = cv2.copyMakeBorder(variant, border, border, border, border, cv2.BORDER_REPLICATE)
        result, _ = ocr_engine()(padded)
        if result:
            lines = [(text, float(confidence)) for _, text, confidence in result]
            score = sum(confidence * len(text) for text, confidence in lines if confidence >= .48)
            joined = "\n".join(text for text, confidence in lines if confidence >= .48)
            observations.append((score, joined, direction))
            if direction == "horizontal" and score >= 10 and sum(len(text) >= 3 for text, _ in lines) >= 2:
                break
    if observations:
        _, text, direction = max(observations)
        return text[:400], direction
    return "", "horizontal"


def analyze(image, detector=None):
    bgr = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    height, width = bgr.shape[:2]
    grey = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    flags = []
    if min(height, width) < 275:
        flags.append("分辨率过低，请靠近书架重新拍照")
    scale = min(1., 800 / max(height, width))
    sample = cv2.resize(grey, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    if cv2.Laplacian(sample, cv2.CV_64F).var() < 22:
        flags.append("画面模糊，请稳住手机重拍")
    if (sample >= 249).mean() > .42:
        flags.append("画面有明显反光或过曝，请调整拍摄角度")
    if sample.mean() < 27:
        flags.append("画面过暗，请调整光照")
    if flags:
        return [], flags
    observations = []
    with inference_lock:
        boxes = boxes_for(bgr, detector)[:80]
        if not boxes:
            return [], ["未检测到完整书脊，请正对层板重新取景"]
        for quad in boxes:
            scaled = [[float(np.clip(x/width, 0, 1)), float(np.clip(y/height, 0, 1))] for x, y in quad]
            text, direction = best_ocr(perspective_crop(bgr, quad))
            observations.append({"quad": scaled, "text": text, "direction": direction})
    return observations, flags
