#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频标注工具 — 本地 Web 版。含标注、截帧、序列抽帧、线下切分标注。"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import webbrowser
from pathlib import Path
from queue import Queue
from urllib.parse import unquote

import cv2
from flask import Flask, jsonify, render_template, request, send_file

from q_label import offline_cut

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "web_templates"
STATIC_DIR = APP_DIR / "web_static"

LABEL_CSV_HEADERS = [
    "序号", "MP4文件名称", "用户编号", "躯干长cm", "正式最高点时间秒",
    "抬腿高度判断", "最高点膝盖高度cm", "同帧髋部离地高度cm",
    "抖动等级0-2", "抖动类型", "试探次数", "测量方法", "测量可信度", "是否有效", "备注",
]

CAPTURE_CSV_HEADERS = ["序号", "视频文件名称", "图片文件名", "时间戳秒"]

SEQUENCE_CSV_HEADERS = [
    "序号", "图片文件名", "来源视频", "动作序号", "段内序号",
    "时间戳秒", "帧号", "动作起始秒", "动作结束秒", "动作类型",
]
SEQUENCE_STYLE_CHOICES = ["刻意", "自然"]

LEGACY_FIELD_MAP = {
    "最高点脚踝离地高度cm": "最高点膝盖高度cm",
}

FIELD_CHOICES = {
    "抬腿高度判断": ["", "不足", "合适", "过高"],
    "抖动等级0-2": ["", "0", "1", "2"],
    "抖动类型": ["", "无", "横向", "纵向", "兼有"],
    "测量方法": ["", "ruler", "grid", "visual_only"],
    "测量可信度": ["", "高", "中", "低"],
    "是否有效": ["", "是", "否"],
}

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}
STANDARD_NAME = re.compile(r"^[a-z][a-z0-9]*_s\d+_r\d+\.mp4$", re.I)
IMAGE_SEQ_PATTERN = re.compile(r"_(\d+)\.jpg$", re.I)
SEGMENT_DIR_PATTERN = re.compile(r"^(\d+)_(刻意|自然)$", re.I)
LEGACY_SEGMENT_DIR_PATTERN = re.compile(r"^a(\d+)(?:_(刻意|自然))?$", re.I)
DONE_MARKER = ".annotated"
DONE_PROGRESS_FILE = ".annotator-done.json"
ALL_DONE_MSG = "该文件夹中的所有视频均已标注完成，可以退出了。"

app = Flask(
    __name__,
    template_folder=str(TEMPLATE_DIR),
    static_folder=str(STATIC_DIR),
    static_url_path="/static",
)

label_cfg = {
    "folder": "",
    "csv_path": str(APP_DIR / "labels.csv"),
    "action_code": "bd",
    "user_id": "s001",
    "trunk_cm": "",
}

capture_cfg = {
    "folder": "",
    "output_root": "",
}

sequence_cfg = {
    "folder": "",
    "output_root": "",
    "sample_count": 5,
}

offline_cfg = {
    "folder": "",
    "output": "",
    "root": "",
    "cam": "",
    "user": {
        "user_id": "s004",
        "height_cm": "169",
        "torso_cm": "41.3",
        "shake_level": "0",
        "shake_type": "无",
        "probe_count": "0",
        "measurement_method": "ruler",
        "measurement_confidence": "高",
        "is_valid": "是",
    },
}

offline_jobs: dict[str, dict] = {}
offline_job_lock = threading.Lock()
offline_export_queue: Queue = Queue()
_offline_worker: threading.Thread | None = None


def _offline_export_worker() -> None:
    while True:
        job_id = offline_export_queue.get()
        with offline_job_lock:
            job = offline_jobs.get(job_id)
        if not job:
            continue
        with offline_job_lock:
            job["status"] = "running"
        try:
            result = offline_cut.export_take(**job["args"])
            with offline_job_lock:
                job["status"] = "done"
                job["result"] = {
                    "clips": result["clips"],
                    "rows": result["rows"],
                    "missing": result.get("missing") or [],
                }
        except Exception as exc:
            with offline_job_lock:
                job["status"] = "error"
                job["error"] = str(exc)


def _ensure_offline_worker() -> None:
    global _offline_worker
    if _offline_worker is not None and _offline_worker.is_alive():
        return
    _offline_worker = threading.Thread(target=_offline_export_worker, daemon=True)
    _offline_worker.start()


def _offline_jobs_payload() -> list[dict]:
    with offline_job_lock:
        items = list(offline_jobs.values())
    payload = []
    for job in items:
        payload.append({
            "id": job["id"],
            "take": job["take"],
            "status": job["status"],
            "error": job.get("error") or "",
            "result": job.get("result") or {},
        })
    return payload


def normalize_user_id(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("s"):
        return raw
    if raw.isdigit():
        return f"s{raw.zfill(3)}"
    return raw


def normalize_action_code(raw: str) -> str:
    raw = (raw or "bd").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "", raw)
    return raw or "bd"


def run_osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "已取消选择").strip())
    return result.stdout.strip()


def read_csv_rows(csv_path: Path, headers: list[str]) -> list[dict[str, str]]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        rows = []
        for row in reader:
            fixed = {h: row.get(h, "") for h in headers}
            for old_key, new_key in LEGACY_FIELD_MAP.items():
                if old_key in row and not fixed.get(new_key):
                    fixed[new_key] = row.get(old_key, "")
            rows.append(fixed)
        return rows


def ensure_csv(csv_path: Path, headers: list[str]) -> None:
    if csv_path.exists():
        return
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()


def write_csv_rows(csv_path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    ensure_csv(csv_path, headers)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def next_csv_seq(rows: list[dict[str, str]]) -> int:
    max_seq = 0
    for row in rows:
        val = (row.get("序号") or "").strip()
        if val.isdigit():
            max_seq = max(max_seq, int(val))
    return max_seq + 1


def list_videos(folder: Path) -> list[str]:
    return sorted(
        [p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
        key=str.lower,
    )


def video_stem(filename: str) -> str:
    return Path(filename).stem


def capture_output_dir(stem: str) -> Path:
    return Path(capture_cfg["output_root"]) / stem


def capture_csv_path(stem: str) -> Path:
    return capture_output_dir(stem) / "captures.csv"


def next_image_seq(stem: str) -> int:
    max_seq = 0
    out_dir = capture_output_dir(stem)
    prefix = f"{stem}_"
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            if p.is_file() and p.name.lower().startswith(prefix.lower()) and p.suffix.lower() == ".jpg":
                m = IMAGE_SEQ_PATTERN.search(p.name)
                if m:
                    max_seq = max(max_seq, int(m.group(1)))
    for row in read_csv_rows(capture_csv_path(stem), CAPTURE_CSV_HEADERS):
        img = (row.get("图片文件名") or "").strip()
        m = IMAGE_SEQ_PATTERN.search(img)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def capture_frame(video_path: Path, timestamp_sec: float, out_path: Path) -> None:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path.name}")
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp_sec) * 1000)
        ret, frame = cap.read()
        if not ret or frame is None:
            raise RuntimeError("无法读取该时间点的帧")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise RuntimeError("保存图片失败")
    finally:
        cap.release()


def save_jpeg_bytes(out_path: Path, raw: bytes) -> None:
    if not raw.startswith(b"\xff\xd8"):
        raise RuntimeError("无效的图片数据")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)


def decode_canvas_jpeg(image_data: str) -> bytes:
    payload = (image_data or "").strip()
    if not payload:
        raise RuntimeError("缺少图片数据")
    if "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise RuntimeError("图片数据解码失败") from exc
    if not raw:
        raise RuntimeError("图片数据为空")
    return raw


def label_row_for_video(csv_path: Path, mp4_name: str) -> dict[str, str] | None:
    for row in read_csv_rows(csv_path, LABEL_CSV_HEADERS):
        if row.get("MP4文件名称") == mp4_name:
            return row
    return None


def annotated_names(csv_path: Path) -> set[str]:
    done: set[str] = set()
    for row in read_csv_rows(csv_path, LABEL_CSV_HEADERS):
        name = (row.get("MP4文件名称") or "").strip()
        peak = (row.get("正式最高点时间秒") or "").strip()
        seq = (row.get("序号") or "").strip()
        if name and peak and seq.isdigit():
            done.add(name)
    return done


def pending_videos(all_videos: list[str], done_names: set[str]) -> list[str]:
    return [v for v in all_videos if v not in done_names]


def done_progress_path(root: Path) -> Path:
    return root / DONE_PROGRESS_FILE


def read_done_stems(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    stems: set[str] = set()
    progress = done_progress_path(root)
    if progress.is_file():
        try:
            data = json.loads(progress.read_text(encoding="utf-8"))
            if isinstance(data, list):
                stems = {str(item).strip() for item in data if str(item).strip()}
        except (OSError, json.JSONDecodeError):
            pass
    migrated = False
    for marker in root.glob(".*.annotated"):
        legacy_stem = marker.name[1:].removesuffix(".annotated")
        if legacy_stem:
            stems.add(legacy_stem)
            migrated = True
    for child in root.iterdir():
        if not child.is_dir():
            continue
        legacy_marker = child / DONE_MARKER
        if legacy_marker.is_file():
            stems.add(child.name)
            migrated = True
    if migrated:
        write_done_stems(root, stems)
        cleanup_legacy_done_markers(root)
    return stems


def write_done_stems(root: Path, stems: set[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    done_progress_path(root).write_text(
        json.dumps(sorted(stems), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def cleanup_legacy_done_markers(root: Path) -> None:
    for marker in root.glob(".*.annotated"):
        marker.unlink(missing_ok=True)
    for child in root.iterdir():
        if not child.is_dir():
            continue
        legacy_marker = child / DONE_MARKER
        if legacy_marker.is_file():
            legacy_marker.unlink(missing_ok=True)


def mark_video_done(root: str, filename: str) -> None:
    if not root:
        return
    root_path = Path(root)
    stems = read_done_stems(root_path)
    stems.add(video_stem(filename))
    write_done_stems(root_path, stems)
    cleanup_legacy_done_markers(root_path)


def is_capture_video_done(filename: str) -> bool:
    if not capture_cfg["output_root"]:
        return False
    return video_stem(filename) in read_done_stems(Path(capture_cfg["output_root"]))


def mark_capture_video_done(filename: str) -> None:
    mark_video_done(capture_cfg["output_root"], filename)


def capture_done_names(videos: list[str]) -> set[str]:
    return {v for v in videos if is_capture_video_done(v)}


def is_sequence_video_done(filename: str) -> bool:
    if not sequence_cfg["output_root"]:
        return False
    stem = video_stem(filename)
    if stem in read_done_stems(Path(sequence_cfg["output_root"])):
        return True
    return sequence_has_output(stem)


def mark_sequence_video_done(filename: str) -> None:
    mark_video_done(sequence_cfg["output_root"], filename)


def sequence_done_names(videos: list[str]) -> set[str]:
    return {v for v in videos if is_sequence_video_done(v)}


def sequence_output_dir(stem: str) -> Path:
    return Path(sequence_cfg["output_root"]) / stem


def sequence_style_folder_name(stem: str, style: str) -> str:
    style = style if style in SEQUENCE_STYLE_CHOICES else "自然"
    return f"{stem}-{style}"


def sequence_style_output_dir(stem: str, style: str) -> Path:
    return Path(sequence_cfg["output_root"]) / sequence_style_folder_name(stem, style)


def sequence_has_output(stem: str) -> bool:
    if not sequence_cfg["output_root"]:
        return False
    root = Path(sequence_cfg["output_root"])
    for style in SEQUENCE_STYLE_CHOICES:
        rows = read_sequence_csv_rows(root / sequence_style_folder_name(stem, style) / "labels.csv")
        if rows:
            return True
    video_dir = root / stem
    if video_dir.is_dir():
        for seg_dir in video_dir.iterdir():
            if seg_dir.is_dir() and not seg_dir.name.startswith("."):
                rows = read_sequence_csv_rows(seg_dir / "labels.csv")
                if rows:
                    return True
    return False


def sequence_segment_dir_name(action_seq: int, style: str = "自然") -> str:
    style = style if style in SEQUENCE_STYLE_CHOICES else "自然"
    return f"{action_seq:03d}_{style}"


def parse_segment_dir(name: str) -> tuple[int, str] | None:
    m = SEGMENT_DIR_PATTERN.match(name)
    if m:
        return int(m.group(1)), m.group(2)
    m = LEGACY_SEGMENT_DIR_PATTERN.match(name)
    if m:
        return int(m.group(1)), m.group(2) or "自然"
    return None


def sequence_segment_output_dir(stem: str, action_seq: int, style: str = "自然") -> Path:
    return sequence_output_dir(stem) / sequence_segment_dir_name(action_seq, style)


def list_sequence_segment_dirs(stem: str) -> list[tuple[int, str, Path]]:
    video_dir = sequence_output_dir(stem)
    if not video_dir.is_dir():
        return []
    found: list[tuple[int, str, Path]] = []
    for p in video_dir.iterdir():
        if not p.is_dir() or p.name.startswith("."):
            continue
        parsed = parse_segment_dir(p.name)
        if parsed:
            found.append((parsed[0], parsed[1], p))
    return sorted(found, key=lambda x: x[0])


def list_sequence_data_sources(stem: str) -> list[tuple[str, str, Path]]:
    sources: list[tuple[str, str, Path]] = []
    root = Path(sequence_cfg["output_root"])
    if not root.is_dir():
        return sources
    seen: set[str] = set()
    for style in SEQUENCE_STYLE_CHOICES:
        folder_name = sequence_style_folder_name(stem, style)
        out_dir = root / folder_name
        if out_dir.is_dir():
            key = str(out_dir.resolve())
            if key not in seen:
                sources.append((style, folder_name, out_dir))
                seen.add(key)
    for action_seq, style, seg_dir in list_sequence_segment_dirs(stem):
        key = str(seg_dir.resolve())
        if key not in seen:
            sources.append((style, seg_dir.name, seg_dir))
            seen.add(key)
    return sources


def next_sequence_action_seq_for_style(stem: str, style: str) -> int:
    rows = read_sequence_csv_rows(sequence_style_output_dir(stem, style) / "labels.csv")
    max_seq = 0
    for row in rows:
        val = (row.get("动作序号") or "").strip()
        if val.isdigit():
            max_seq = max(max_seq, int(val))
    return max_seq + 1


def next_global_image_seq(stem: str, style: str) -> int:
    out_dir = sequence_style_output_dir(stem, style)
    max_seq = 0
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            if p.is_file() and p.suffix.lower() == ".jpg":
                m = IMAGE_SEQ_PATTERN.search(p.name)
                if m:
                    max_seq = max(max_seq, int(m.group(1)))
    for row in read_sequence_csv_rows(out_dir / "labels.csv"):
        img = (row.get("图片文件名") or "").strip()
        m = IMAGE_SEQ_PATTERN.search(img)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def build_sequence_segment_rows(
    filename: str,
    action_seq: int,
    start_sec: float,
    end_sec: float,
    style: str,
    frame_indices: list[int],
    fps: float,
    stem: str,
    global_seq_start: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    start_str = f"{start_sec:.3f}"
    end_str = f"{end_sec:.3f}"
    style = style if style in SEQUENCE_STYLE_CHOICES else "自然"

    for seg_idx, frame_idx in enumerate(frame_indices, start=1):
        global_seq = global_seq_start + seg_idx - 1
        image_name = f"{stem}_{global_seq:03d}.jpg"
        timestamp_sec = frame_to_timestamp(frame_idx, fps)
        rows.append({
            "序号": str(global_seq),
            "图片文件名": image_name,
            "来源视频": filename,
            "动作序号": str(action_seq),
            "段内序号": str(seg_idx),
            "时间戳秒": f"{timestamp_sec:.3f}",
            "帧号": str(frame_idx),
            "动作起始秒": start_str,
            "动作结束秒": end_str,
            "动作类型": style,
        })
    return rows


def sequence_rows_to_captures(
    folder_name: str,
    action_seq: str,
    rows: list[dict[str, str]],
) -> list[dict]:
    captures = []
    for row in rows:
        img = (row.get("图片文件名") or "").strip()
        if not img:
            continue
        captures.append({
            "seq": row.get("序号", ""),
            "action_seq": row.get("动作序号", action_seq),
            "segment_idx": row.get("段内序号", ""),
            "style": row.get("动作类型", ""),
            "folder_name": folder_name,
            "image_name": img,
            "image_url": f"/api/sequence/output/{folder_name}/{img}",
            "timestamp": row.get("时间戳秒", ""),
            "frame_number": row.get("帧号", ""),
            "row": {k: row.get(k, "") for k in SEQUENCE_CSV_HEADERS},
        })
    return captures


def _segments_from_rows(folder_name: str, style: str, out_dir: Path, rows: list[dict[str, str]]) -> list[dict]:
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        aq = (row.get("动作序号") or "1").strip()
        groups.setdefault(aq, []).append(row)
    segments: list[dict] = []
    for action_seq in sorted(groups.keys(), key=lambda x: int(x) if x.isdigit() else 0):
        group = groups[action_seq]
        first = group[0]
        segments.append({
            "action_seq": action_seq,
            "style": style,
            "folder_name": folder_name,
            "start_sec": first.get("动作起始秒", ""),
            "end_sec": first.get("动作结束秒", ""),
            "frame_count": len(group),
            "output_dir": str(out_dir),
        })
    return segments


def load_sequence_segments_and_captures(stem: str) -> tuple[list[dict], list[dict]]:
    segments: list[dict] = []
    captures: list[dict] = []
    for style, folder_name, out_dir in list_sequence_data_sources(stem):
        rows = read_sequence_csv_rows(out_dir / "labels.csv")
        if not rows:
            continue
        style = (rows[0].get("动作类型") or style).strip() or style
        segs = _segments_from_rows(folder_name, style, out_dir, rows)
        segments.extend(segs)
        groups: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            aq = (row.get("动作序号") or "1").strip()
            groups.setdefault(aq, []).append(row)
        for action_seq, group_rows in groups.items():
            captures.extend(sequence_rows_to_captures(folder_name, action_seq, group_rows))
    segments.sort(key=lambda s: (
        s.get("style", ""),
        int(s["action_seq"]) if str(s["action_seq"]).isdigit() else 0,
    ))
    captures.sort(key=lambda c: (
        int(c["seq"]) if str(c["seq"]).isdigit() else 0,
    ))
    return segments, captures


def find_sequence_image_folder(stem: str, image_name: str, style: str = "") -> Path | None:
    if style in SEQUENCE_STYLE_CHOICES:
        out_dir = sequence_style_output_dir(stem, style)
        if (out_dir / image_name).is_file():
            return out_dir
    for _, _, out_dir in list_sequence_data_sources(stem):
        if (out_dir / image_name).is_file():
            return out_dir
        rows = read_sequence_csv_rows(out_dir / "labels.csv")
        if any((r.get("图片文件名") or "").strip() == image_name for r in rows):
            return out_dir
    return None


def find_legacy_segment_dir_by_seq(stem: str, action_seq: int) -> Path | None:
    for seq, _, seg_dir in list_sequence_segment_dirs(stem):
        if seq == action_seq:
            return seg_dir
    return None


def get_video_fps(video_path: Path) -> float:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 30.0
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        return fps if fps > 0 else 30.0
    finally:
        cap.release()


def get_video_frame_count(video_path: Path) -> int:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0
    try:
        return int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        cap.release()


def timestamp_to_frame(timestamp_sec: float, fps: float) -> int:
    fps = fps if fps > 0 else 30.0
    return max(0, int(round(timestamp_sec * fps)))


def frame_to_timestamp(frame: int, fps: float) -> float:
    fps = fps if fps > 0 else 30.0
    return frame / fps


def uniform_frame_indices(start_frame: int, end_frame: int, count: int) -> list[int]:
    if count <= 0:
        return []
    if start_frame > end_frame:
        start_frame, end_frame = end_frame, start_frame
    if count == 1:
        return [start_frame]
    span = end_frame - start_frame
    indices: list[int] = []
    seen: set[int] = set()
    for i in range(count):
        idx = start_frame + round(i * span / (count - 1))
        if idx not in seen:
            seen.add(idx)
            indices.append(idx)
    return indices


def segment_frame_bounds(
    start_sec: float,
    end_sec: float,
    fps: float,
    max_frame: int,
) -> tuple[int, int]:
    if start_sec > end_sec:
        start_sec, end_sec = end_sec, start_sec
    start_frame = min(timestamp_to_frame(start_sec, fps), max_frame)
    end_frame = min(timestamp_to_frame(end_sec, fps), max_frame)
    if start_frame > end_frame:
        start_frame, end_frame = end_frame, start_frame
    return start_frame, end_frame


def max_sample_count_for_segment(start_frame: int, end_frame: int) -> int:
    return max(1, end_frame - start_frame + 1)


def normalize_sequence_row(row: dict[str, str]) -> dict[str, str]:
    source = (row.get("来源视频") or row.get("MP4文件名称") or "").strip()
    timestamp = (row.get("时间戳秒") or row.get("正式最高点时间秒") or "").strip()
    return {
        "序号": (row.get("序号") or "").strip(),
        "图片文件名": (row.get("图片文件名") or "").strip(),
        "来源视频": source,
        "动作序号": (row.get("动作序号") or "").strip(),
        "段内序号": (row.get("段内序号") or "").strip(),
        "时间戳秒": timestamp,
        "帧号": (row.get("帧号") or "").strip(),
        "动作起始秒": (row.get("动作起始秒") or "").strip(),
        "动作结束秒": (row.get("动作结束秒") or "").strip(),
        "动作类型": (row.get("动作类型") or "").strip(),
    }


def read_sequence_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        return [normalize_sequence_row(dict(row)) for row in reader]


def capture_frame_at_index(video_path: Path, frame_idx: int, out_path: Path) -> None:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path.name}")
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_idx))
        ret, frame = cap.read()
        if not ret or frame is None:
            raise RuntimeError(f"无法读取第 {frame_idx} 帧")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise RuntimeError("保存图片失败")
    finally:
        cap.release()


def next_record_index(action_code: str, user_id: str, folder: Path) -> int:
    pattern = re.compile(
        rf"^{re.escape(action_code)}_{re.escape(user_id)}_r(\d+)\.mp4$",
        re.I,
    )
    max_idx = 0
    for name in list_videos(folder):
        m = pattern.match(name)
        if m:
            max_idx = max(max_idx, int(m.group(1)))
    return max_idx + 1


# ── 页面路由 ────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/offline")
def offline_page():
    return render_template("offline.html")


# ── 动作数据标注 API ─────────────────────────────────────────────────────────

@app.get("/api/label/config")
def label_get_config():
    folder = label_cfg["folder"]
    csv_path = label_cfg["csv_path"]
    videos = list_videos(Path(folder)) if folder and Path(folder).is_dir() else []
    done = annotated_names(Path(csv_path)) if csv_path else set()
    pending = [v for v in videos if v not in done]
    return jsonify({
        "folder": folder,
        "csv_path": csv_path,
        "action_code": label_cfg["action_code"],
        "user_id": label_cfg["user_id"],
        "trunk_cm": label_cfg["trunk_cm"],
        "video_count": len(videos),
        "pending_count": len(pending),
        "pending": pending,
        "all_done_message": ALL_DONE_MSG,
    })


@app.post("/api/label/config")
def label_set_config():
    data = request.get_json(force=True) or {}
    if "folder" in data:
        folder = data["folder"].strip()
        if folder and not Path(folder).is_dir():
            return jsonify({"ok": False, "error": "视频文件夹不存在"}), 400
        label_cfg["folder"] = folder
    if "csv_path" in data:
        csv_path = data["csv_path"].strip() or str(APP_DIR / "labels.csv")
        ensure_csv(Path(csv_path), LABEL_CSV_HEADERS)
        label_cfg["csv_path"] = csv_path
    if "action_code" in data:
        label_cfg["action_code"] = normalize_action_code(data["action_code"])
    if "user_id" in data:
        label_cfg["user_id"] = normalize_user_id(data["user_id"])
    if "trunk_cm" in data:
        label_cfg["trunk_cm"] = str(data["trunk_cm"]).strip()
    return jsonify({"ok": True})


@app.post("/api/label/browse/folder")
def label_browse_folder():
    try:
        path = run_osascript(
            'POSIX path of (choose folder with prompt "选择包含视频的文件夹")'
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    label_cfg["folder"] = path
    return jsonify({"ok": True, "path": path})


@app.post("/api/label/browse/csv")
def label_browse_csv():
    try:
        path = run_osascript(
            'POSIX path of (choose file with prompt "选择 CSV 文件" of type {"csv"})'
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    ensure_csv(Path(path), LABEL_CSV_HEADERS)
    label_cfg["csv_path"] = path
    return jsonify({"ok": True, "path": path})


@app.post("/api/label/rename")
def label_rename_videos():
    folder = Path(label_cfg["folder"])
    if not folder.is_dir():
        return jsonify({"ok": False, "error": "请先设置视频文件夹"}), 400
    data = request.get_json(force=True) or {}
    user_id = normalize_user_id(data.get("user_id") or label_cfg["user_id"])
    action_code = normalize_action_code(data.get("action_code") or label_cfg["action_code"])
    if not user_id:
        return jsonify({"ok": False, "error": "请填写用户编号"}), 400

    start = next_record_index(action_code, user_id, folder)
    videos = [folder / n for n in list_videos(folder)]
    to_rename = [p for p in videos if not STANDARD_NAME.match(p.name)]
    renamed = []

    temp_moves: list[tuple[Path, Path]] = []
    for i, src in enumerate(to_rename):
        target = folder / f"{action_code}_{user_id}_r{start + i:03d}{src.suffix.lower()}"
        if target.exists():
            return jsonify({"ok": False, "error": f"目标已存在: {target.name}"}), 400
        tmp = folder / f"__tmp_{i:04d}.mp4"
        shutil.move(str(src), str(tmp))
        temp_moves.append((tmp, target))

    for tmp, target in temp_moves:
        shutil.move(str(tmp), str(target))
        renamed.append({"from": tmp.name, "to": target.name})

    return jsonify({"ok": True, "renamed": len(renamed), "files": renamed})


@app.get("/api/label/video/<path:filename>")
def label_serve_video(filename: str):
    folder = Path(label_cfg["folder"])
    path = (folder / filename).resolve()
    if not str(path).startswith(str(folder.resolve())):
        return "Forbidden", 403
    if not path.is_file():
        return "Not found", 404
    return send_file(path, conditional=True)


@app.get("/api/label/video/<path:filename>/meta")
def label_video_meta(filename: str):
    csv_path = Path(label_cfg["csv_path"])
    existing = label_row_for_video(csv_path, filename)
    seq = (existing or {}).get("序号") or str(next_csv_seq(read_csv_rows(csv_path, LABEL_CSV_HEADERS)))
    defaults = {
        "序号": seq,
        "MP4文件名称": filename,
        "用户编号": label_cfg["user_id"],
        "躯干长cm": label_cfg["trunk_cm"],
        "正式最高点时间秒": "",
        "试探次数": "0",
        "是否有效": "是",
    }
    if existing:
        for k in LABEL_CSV_HEADERS:
            if existing.get(k):
                defaults[k] = existing[k]
    defaults["用户编号"] = label_cfg["user_id"]
    return jsonify({"ok": True, "row": defaults})


@app.post("/api/label/save")
def label_save_row():
    csv_path = Path(label_cfg["csv_path"])
    ensure_csv(csv_path, LABEL_CSV_HEADERS)
    data = request.get_json(force=True) or {}
    row = {h: str(data.get(h, "")).strip() for h in LABEL_CSV_HEADERS}

    if not row.get("正式最高点时间秒"):
        return jsonify({"ok": False, "error": "请填写正式最高点时间秒"}), 400
    if not row.get("MP4文件名称"):
        return jsonify({"ok": False, "error": "缺少 MP4 文件名称"}), 400

    rows: list[dict[str, str]] = []
    fieldnames = LABEL_CSV_HEADERS[:]
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                seen = set()
                fieldnames = []
                for name in reader.fieldnames:
                    normalized = LEGACY_FIELD_MAP.get(name, name)
                    if normalized not in seen:
                        fieldnames.append(normalized)
                        seen.add(normalized)
                for name in LABEL_CSV_HEADERS:
                    if name not in seen:
                        fieldnames.append(name)
                        seen.add(name)
            for r in reader:
                fixed = dict(r)
                for old_key, new_key in LEGACY_FIELD_MAP.items():
                    if old_key in fixed and not fixed.get(new_key):
                        fixed[new_key] = fixed.get(old_key, "")
                    fixed.pop(old_key, None)
                rows.append(fixed)

    target_name = row["MP4文件名称"]
    target_seq = row.get("序号", "")
    updated = False
    for r in rows:
        if (r.get("序号") or "").strip() in ("示例", "示例行", "example"):
            continue
        if r.get("MP4文件名称") == target_name or r.get("序号") == target_seq:
            for k in LABEL_CSV_HEADERS:
                r[k] = row.get(k, "")
            updated = True
            break

    if not updated:
        new_row = {h: "" for h in fieldnames}
        new_row.update(row)
        rows.append(new_row)

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    done = annotated_names(csv_path)
    pending = [v for v in list_videos(Path(label_cfg["folder"])) if v not in done]
    return jsonify({"ok": True, "pending": pending})


# ── 视频截帧 API ─────────────────────────────────────────────────────────────

@app.get("/api/capture/config")
def capture_get_config():
    folder = capture_cfg["folder"]
    videos = list_videos(Path(folder)) if folder and Path(folder).is_dir() else []
    done = capture_done_names(videos) if capture_cfg["output_root"] else set()
    pending = pending_videos(videos, done)
    return jsonify({
        "folder": folder,
        "output_root": capture_cfg["output_root"],
        "video_count": len(videos),
        "pending_count": len(pending),
        "pending": pending,
        "videos": videos,
        "all_done_message": ALL_DONE_MSG,
    })


@app.post("/api/capture/config")
def capture_set_config():
    data = request.get_json(force=True) or {}
    if "folder" in data:
        folder = data["folder"].strip()
        if folder and not Path(folder).is_dir():
            return jsonify({"ok": False, "error": "视频文件夹不存在"}), 400
        capture_cfg["folder"] = folder
    if "output_root" in data:
        output_root = data["output_root"].strip()
        if output_root:
            out_path = Path(output_root)
            out_path.mkdir(parents=True, exist_ok=True)
            read_done_stems(out_path)
        capture_cfg["output_root"] = output_root
    return jsonify({"ok": True})


@app.post("/api/capture/browse/folder")
def capture_browse_folder():
    try:
        path = run_osascript(
            'POSIX path of (choose folder with prompt "选择包含视频的文件夹")'
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    capture_cfg["folder"] = path
    return jsonify({"ok": True, "path": path})


@app.post("/api/capture/browse/output")
def capture_browse_output():
    try:
        path = run_osascript(
            'POSIX path of (choose folder with prompt "选择截帧输出根目录")'
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    Path(path).mkdir(parents=True, exist_ok=True)
    capture_cfg["output_root"] = path
    return jsonify({"ok": True, "path": path})


@app.get("/api/capture/video/<path:filename>")
def capture_serve_video(filename: str):
    folder = Path(capture_cfg["folder"])
    path = (folder / filename).resolve()
    if not str(path).startswith(str(folder.resolve())):
        return "Forbidden", 403
    if not path.is_file():
        return "Not found", 404
    return send_file(path, conditional=True)


@app.get("/api/capture/output/<path:stem>/<path:image_name>")
def capture_serve_output_image(stem: str, image_name: str):
    out_dir = capture_output_dir(stem).resolve()
    path = (out_dir / image_name).resolve()
    if not str(path).startswith(str(out_dir)) or not path.is_file():
        return "Not found", 404
    return send_file(path)


@app.get("/api/capture/video/<path:filename>/captures")
def capture_list_captures(filename: str):
    if not capture_cfg["output_root"]:
        return jsonify({"ok": False, "error": "请先设置输出目录"}), 400
    stem = video_stem(filename)
    rows = read_csv_rows(capture_csv_path(stem), CAPTURE_CSV_HEADERS)
    captures = []
    for row in rows:
        img = (row.get("图片文件名") or "").strip()
        if not img:
            continue
        captures.append({
            "seq": row.get("序号", ""),
            "image_name": img,
            "image_url": f"/api/capture/output/{stem}/{img}",
            "timestamp": row.get("时间戳秒", ""),
        })
    captures.sort(key=lambda c: int(c["seq"]) if str(c["seq"]).isdigit() else 0)
    return jsonify({
        "ok": True,
        "output_dir": str(capture_output_dir(stem)),
        "captures": captures,
    })


@app.get("/api/capture/video/<path:filename>/meta")
def capture_video_meta(filename: str):
    stem = video_stem(filename)
    return jsonify({
        "ok": True,
        "stem": stem,
        "output_dir": str(capture_output_dir(stem)) if capture_cfg["output_root"] else "",
    })


@app.post("/api/capture/capture")
def capture_at_pause():
    if not capture_cfg["folder"] or not capture_cfg["output_root"]:
        return jsonify({"ok": False, "error": "请先设置视频文件夹和输出目录"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    try:
        timestamp_sec = float(data.get("timestamp_sec", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "时间戳无效"}), 400

    folder = Path(capture_cfg["folder"])
    video_path = (folder / filename).resolve()
    if not str(video_path).startswith(str(folder.resolve())) or not video_path.is_file():
        return jsonify({"ok": False, "error": "视频不存在"}), 400

    stem = video_stem(filename)
    seq = next_image_seq(stem)
    image_name = f"{stem}_{seq:03d}.jpg"
    out_path = capture_output_dir(stem) / image_name

    image_data = (data.get("image_data") or "").strip()
    try:
        if image_data:
            save_jpeg_bytes(out_path, decode_canvas_jpeg(image_data))
        else:
            capture_frame(video_path, timestamp_sec, out_path)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    timestamp = f"{timestamp_sec:.3f}"
    csv_path = capture_csv_path(stem)
    rows = read_csv_rows(csv_path, CAPTURE_CSV_HEADERS)
    rows.append({
        "序号": str(seq),
        "视频文件名称": filename,
        "图片文件名": image_name,
        "时间戳秒": timestamp,
    })
    write_csv_rows(csv_path, CAPTURE_CSV_HEADERS, rows)

    return jsonify({
        "ok": True,
        "seq": str(seq),
        "image_name": image_name,
        "image_url": f"/api/capture/output/{stem}/{image_name}",
        "timestamp": timestamp,
        "output_dir": str(capture_output_dir(stem)),
    })


@app.post("/api/capture/delete")
def capture_delete():
    if not capture_cfg["output_root"]:
        return jsonify({"ok": False, "error": "请先设置输出目录"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    image_name = (data.get("image_name") or "").strip()
    if not filename or not image_name:
        return jsonify({"ok": False, "error": "缺少视频或图片文件名"}), 400

    stem = video_stem(filename)
    out_dir = capture_output_dir(stem).resolve()
    img_path = (out_dir / image_name).resolve()
    if not str(img_path).startswith(str(out_dir)):
        return jsonify({"ok": False, "error": "非法路径"}), 403

    if img_path.is_file():
        img_path.unlink()

    csv_path = capture_csv_path(stem)
    rows = [
        r for r in read_csv_rows(csv_path, CAPTURE_CSV_HEADERS)
        if (r.get("图片文件名") or "").strip() != image_name
    ]
    write_csv_rows(csv_path, CAPTURE_CSV_HEADERS, rows)

    return jsonify({"ok": True, "output_dir": str(out_dir)})


@app.post("/api/capture/mark-done")
def capture_mark_done():
    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "缺少视频文件名"}), 400
    if not capture_cfg["output_root"]:
        return jsonify({"ok": False, "error": "请先设置输出目录"}), 400
    mark_capture_video_done(filename)
    folder = Path(capture_cfg["folder"])
    videos = list_videos(folder) if folder.is_dir() else []
    done = capture_done_names(videos)
    pending = pending_videos(videos, done)
    return jsonify({
        "ok": True,
        "pending": pending,
        "pending_count": len(pending),
        "all_done": len(videos) > 0 and len(pending) == 0,
        "all_done_message": ALL_DONE_MSG,
    })


# ── 动作序列抽帧 API ─────────────────────────────────────────────────────────

@app.get("/api/sequence/config")
def sequence_get_config():
    folder = sequence_cfg["folder"]
    videos = list_videos(Path(folder)) if folder and Path(folder).is_dir() else []
    done = sequence_done_names(videos) if sequence_cfg["output_root"] else set()
    pending = pending_videos(videos, done)
    return jsonify({
        "folder": folder,
        "output_root": sequence_cfg["output_root"],
        "sample_count": sequence_cfg["sample_count"],
        "video_count": len(videos),
        "pending_count": len(pending),
        "pending": pending,
        "videos": videos,
        "done_videos": sorted(done),
        "all_done_message": ALL_DONE_MSG,
    })


@app.post("/api/sequence/config")
def sequence_set_config():
    data = request.get_json(force=True) or {}
    if "folder" in data:
        folder = data["folder"].strip()
        if folder and not Path(folder).is_dir():
            return jsonify({"ok": False, "error": "视频文件夹不存在"}), 400
        sequence_cfg["folder"] = folder
    if "output_root" in data:
        output_root = data["output_root"].strip()
        if output_root:
            out_path = Path(output_root)
            out_path.mkdir(parents=True, exist_ok=True)
            read_done_stems(out_path)
        sequence_cfg["output_root"] = output_root
    if "sample_count" in data:
        try:
            count = int(data["sample_count"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "抽帧数量无效"}), 400
        if count < 1:
            return jsonify({"ok": False, "error": "抽帧数量至少为 1"}), 400
        sequence_cfg["sample_count"] = count
    return jsonify({"ok": True})


@app.post("/api/sequence/mark-done")
def sequence_mark_done():
    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "缺少视频文件名"}), 400
    if not sequence_cfg["output_root"]:
        return jsonify({"ok": False, "error": "请先设置输出目录"}), 400
    mark_sequence_video_done(filename)
    folder = Path(sequence_cfg["folder"])
    videos = list_videos(folder) if folder.is_dir() else []
    done = sequence_done_names(videos)
    pending = pending_videos(videos, done)
    return jsonify({
        "ok": True,
        "pending": pending,
        "pending_count": len(pending),
        "all_done": len(videos) > 0 and len(pending) == 0,
        "all_done_message": ALL_DONE_MSG,
    })


@app.post("/api/sequence/browse/folder")
def sequence_browse_folder():
    try:
        path = run_osascript(
            'POSIX path of (choose folder with prompt "选择包含视频的文件夹")'
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    sequence_cfg["folder"] = path
    return jsonify({"ok": True, "path": path})


@app.post("/api/sequence/browse/output")
def sequence_browse_output():
    try:
        path = run_osascript(
            'POSIX path of (choose folder with prompt "选择序列抽帧输出根目录")'
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    Path(path).mkdir(parents=True, exist_ok=True)
    sequence_cfg["output_root"] = path
    return jsonify({"ok": True, "path": path})


@app.get("/api/sequence/video/<path:filename>")
def sequence_serve_video(filename: str):
    folder = Path(sequence_cfg["folder"])
    filename = unquote(filename)
    path = (folder / filename).resolve()
    if not str(path).startswith(str(folder.resolve())):
        return "Forbidden", 403
    if not path.is_file():
        return "Not found", 404
    return send_file(path, conditional=True)


@app.get("/api/sequence/output/<path:folder>/<path:image_name>")
def sequence_serve_output_image(folder: str, image_name: str):
    root = Path(sequence_cfg["output_root"]).resolve()
    folder = unquote(folder)
    image_name = unquote(image_name)
    out_dir = (root / folder).resolve()
    path = (out_dir / image_name).resolve()
    if not str(out_dir).startswith(str(root)) or not path.is_file():
        return "Not found", 404
    return send_file(path)


@app.get("/api/sequence/output/<path:stem>/<path:segment>/<path:image_name>")
def sequence_serve_output_image_legacy(stem: str, segment: str, image_name: str):
    root = Path(sequence_cfg["output_root"]).resolve()
    out_dir = (root / unquote(stem) / unquote(segment)).resolve()
    path = (out_dir / unquote(image_name)).resolve()
    if not str(out_dir).startswith(str(root)) or not path.is_file():
        return "Not found", 404
    return send_file(path)


@app.get("/api/sequence/video/<path:filename>/captures")
def sequence_list_captures(filename: str):
    if not sequence_cfg["output_root"]:
        return jsonify({"ok": False, "error": "请先设置输出目录"}), 400
    stem = video_stem(filename)
    segments, captures = load_sequence_segments_and_captures(stem)
    style_dirs = [
        str(sequence_style_output_dir(stem, s))
        for s in SEQUENCE_STYLE_CHOICES
        if sequence_style_output_dir(stem, s).is_dir()
    ]
    output_dir = style_dirs[0] if len(style_dirs) == 1 else str(Path(sequence_cfg["output_root"]))
    return jsonify({
        "ok": True,
        "output_dir": output_dir,
        "output_dirs": style_dirs,
        "segments": segments,
        "captures": captures,
    })


@app.get("/api/sequence/video/<path:filename>/meta")
def sequence_video_meta(filename: str):
    stem = video_stem(filename)
    folder = Path(sequence_cfg["folder"])
    video_path = (folder / filename).resolve()
    fps = get_video_fps(video_path) if video_path.is_file() else 30.0
    frame_count = get_video_frame_count(video_path) if video_path.is_file() else 0
    return jsonify({
        "ok": True,
        "stem": stem,
        "fps": fps,
        "frame_count": frame_count,
        "sample_count": sequence_cfg["sample_count"],
        "output_dir": str(Path(sequence_cfg["output_root"])) if sequence_cfg["output_root"] else "",
    })


@app.post("/api/sequence/segment-frame-budget")
def sequence_segment_frame_budget():
    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    try:
        start_sec = float(data.get("start_sec", 0))
        end_sec = float(data.get("end_sec", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "起止时间无效"}), 400

    folder = Path(sequence_cfg["folder"])
    if not filename or not folder.is_dir():
        return jsonify({"ok": False, "error": "请先选择视频"}), 400

    video_path = (folder / filename).resolve()
    if not str(video_path).startswith(str(folder.resolve())) or not video_path.is_file():
        return jsonify({"ok": False, "error": "视频不存在"}), 400

    fps = get_video_fps(video_path)
    total_frames = get_video_frame_count(video_path)
    max_frame = max(total_frames - 1, 0)
    start_frame, end_frame = segment_frame_bounds(start_sec, end_sec, fps, max_frame)
    max_samples = max_sample_count_for_segment(start_frame, end_frame)
    duration_sec = max(0.0, end_frame - start_frame) / fps if fps > 0 else 0.0

    return jsonify({
        "ok": True,
        "fps": fps,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_span": max_samples,
        "max_sample_count": max_samples,
        "duration_sec": round(duration_sec, 3),
    })


@app.post("/api/sequence/extract-segment")
def sequence_extract_segment():
    if not sequence_cfg["folder"] or not sequence_cfg["output_root"]:
        return jsonify({"ok": False, "error": "请先设置视频文件夹和输出目录"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    style = (data.get("style") or "自然").strip()
    try:
        start_sec = float(data.get("start_sec", 0))
        end_sec = float(data.get("end_sec", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "起止时间无效"}), 400

    sample_count = sequence_cfg["sample_count"]
    if "sample_count" in data:
        try:
            sample_count = int(data["sample_count"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "抽帧数量无效"}), 400
        if sample_count < 1:
            return jsonify({"ok": False, "error": "抽帧数量至少为 1"}), 400

    if style not in SEQUENCE_STYLE_CHOICES:
        return jsonify({"ok": False, "error": "请选择动作类型：刻意 或 自然"}), 400
    if start_sec > end_sec:
        start_sec, end_sec = end_sec, start_sec
    if end_sec - start_sec < 1e-6:
        return jsonify({"ok": False, "error": "起始点与结束点不能相同"}), 400

    folder = Path(sequence_cfg["folder"])
    video_path = (folder / filename).resolve()
    if not str(video_path).startswith(str(folder.resolve())) or not video_path.is_file():
        return jsonify({"ok": False, "error": "视频不存在"}), 400

    fps = get_video_fps(video_path)
    total_frames = get_video_frame_count(video_path)
    max_frame = max(total_frames - 1, 0)

    start_frame, end_frame = segment_frame_bounds(start_sec, end_sec, fps, max_frame)
    max_allowed = max_sample_count_for_segment(start_frame, end_frame)
    if sample_count > max_allowed:
        return jsonify({
            "ok": False,
            "error": (
                f"本段共 {max_allowed} 帧（{frame_to_timestamp(start_frame, fps):.3f}s"
                f" → {frame_to_timestamp(end_frame, fps):.3f}s），"
                f"最多只能抽取 {max_allowed} 帧"
            ),
            "max_sample_count": max_allowed,
            "frame_span": max_allowed,
        }), 400

    frame_indices = uniform_frame_indices(start_frame, end_frame, sample_count)
    if not frame_indices:
        return jsonify({"ok": False, "error": "未能生成抽帧列表"}), 400

    stem = video_stem(filename)
    out_dir = sequence_style_output_dir(stem, style)
    out_dir.mkdir(parents=True, exist_ok=True)
    folder_name = sequence_style_folder_name(stem, style)
    action_seq = next_sequence_action_seq_for_style(stem, style)
    global_seq_start = next_global_image_seq(stem, style)

    new_rows = build_sequence_segment_rows(
        filename=filename,
        action_seq=action_seq,
        start_sec=frame_to_timestamp(start_frame, fps),
        end_sec=frame_to_timestamp(end_frame, fps),
        style=style,
        frame_indices=frame_indices,
        fps=fps,
        stem=stem,
        global_seq_start=global_seq_start,
    )

    for row in new_rows:
        frame_idx = int(row["帧号"])
        out_path = out_dir / row["图片文件名"]
        try:
            capture_frame_at_index(video_path, frame_idx, out_path)
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    existing_rows = read_sequence_csv_rows(out_dir / "labels.csv")
    write_csv_rows(out_dir / "labels.csv", SEQUENCE_CSV_HEADERS, existing_rows + new_rows)

    segments, _ = load_sequence_segments_and_captures(stem)
    captures = sequence_rows_to_captures(folder_name, str(action_seq), new_rows)
    return jsonify({
        "ok": True,
        "action_seq": str(action_seq),
        "style": style,
        "folder_name": folder_name,
        "frame_count": len(new_rows),
        "frame_indices": frame_indices,
        "output_dir": str(out_dir),
        "captures": captures,
        "segments": segments,
    })


@app.post("/api/sequence/delete-segment")
def sequence_delete_segment():
    if not sequence_cfg["output_root"]:
        return jsonify({"ok": False, "error": "请先设置输出目录"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    action_seq = (data.get("action_seq") or "").strip()
    style = (data.get("style") or "").strip()
    if not filename or not action_seq.isdigit():
        return jsonify({"ok": False, "error": "缺少视频或动作序号"}), 400

    stem = video_stem(filename)
    deleted_count = 0

    if style in SEQUENCE_STYLE_CHOICES:
        out_dir = sequence_style_output_dir(stem, style).resolve()
        csv_path = out_dir / "labels.csv"
        rows = read_sequence_csv_rows(csv_path)
        to_delete = [r for r in rows if (r.get("动作序号") or "").strip() == action_seq]
        for row in to_delete:
            img_path = out_dir / (row.get("图片文件名") or "")
            if img_path.is_file():
                img_path.unlink()
                deleted_count += 1
        remaining = [r for r in rows if (r.get("动作序号") or "").strip() != action_seq]
        if remaining:
            write_csv_rows(csv_path, SEQUENCE_CSV_HEADERS, remaining)
        elif csv_path.exists():
            csv_path.unlink()
        if out_dir.is_dir() and not any(out_dir.iterdir()):
            out_dir.rmdir()
    else:
        seg_dir = find_legacy_segment_dir_by_seq(stem, int(action_seq))
        if seg_dir is None:
            return jsonify({"ok": False, "error": "动作段不存在"}), 404
        deleted_count = sum(1 for p in seg_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg")
        shutil.rmtree(seg_dir)

    segments, captures = load_sequence_segments_and_captures(stem)
    return jsonify({
        "ok": True,
        "deleted_count": deleted_count,
        "output_dir": str(Path(sequence_cfg["output_root"])),
        "segments": segments,
        "captures": captures,
    })


@app.post("/api/sequence/delete-capture")
def sequence_delete_capture():
    if not sequence_cfg["output_root"]:
        return jsonify({"ok": False, "error": "请先设置输出目录"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    image_name = (data.get("image_name") or "").strip()
    action_seq = (data.get("action_seq") or "").strip()
    if not filename or not image_name:
        return jsonify({"ok": False, "error": "缺少视频或图片文件名"}), 400

    stem = video_stem(filename)
    style = (data.get("style") or "").strip()
    out_dir = find_sequence_image_folder(stem, image_name, style)
    if out_dir is None:
        return jsonify({"ok": False, "error": "未找到对应输出目录"}), 404
    out_dir = out_dir.resolve()
    root = Path(sequence_cfg["output_root"]).resolve()
    if not str(out_dir).startswith(str(root)):
        return jsonify({"ok": False, "error": "非法路径"}), 403

    img_path = (out_dir / image_name).resolve()
    if str(img_path).startswith(str(out_dir)) and img_path.is_file():
        img_path.unlink()

    csv_path = out_dir / "labels.csv"
    rows = [
        r for r in read_sequence_csv_rows(csv_path)
        if (r.get("图片文件名") or "").strip() != image_name
    ]
    if rows:
        write_csv_rows(csv_path, SEQUENCE_CSV_HEADERS, rows)
    elif csv_path.exists():
        csv_path.unlink()
    if out_dir.is_dir() and not any(out_dir.iterdir()):
        out_dir.rmdir()

    segments, captures = load_sequence_segments_and_captures(stem)
    return jsonify({
        "ok": True,
        "output_dir": str(out_dir),
        "segments": segments,
        "captures": captures,
    })


def _offline_takes_payload() -> list[dict]:
    folder = Path(offline_cfg["folder"]) if offline_cfg["folder"] else None
    output = Path(offline_cfg["output"]) if offline_cfg["output"] else None
    if not folder or not folder.is_dir():
        return []
    takes = offline_cut.load_takes(folder)
    progress = offline_cut.read_progress(output) if output else {"done": [], "marks": {}}
    done = {str(x) for x in progress.get("done", [])}
    marks = progress.get("marks") or {}
    cam = offline_cfg.get("cam") or ""
    payload = []
    for item in takes:
        row = dict(item)
        row["done"] = str(item["take"]) in done
        row["marks"] = marks.get(str(item["take"]), {})
        row["review_cam"] = cam or item.get("review_cam") or "c90"
        payload.append(row)
    return payload


def _set_offline_cam_folder(raw: str) -> dict:
    root, cam_dir, cam = offline_cut.resolve_cam_session(Path(raw))
    offline_cfg["folder"] = str(cam_dir)
    offline_cfg["output"] = str(root)
    offline_cfg["root"] = str(root)
    offline_cfg["cam"] = cam
    csv_path = offline_cut.ensure_labels_csv(root)
    return {
        "root": str(root),
        "folder": str(cam_dir),
        "output": str(root),
        "cam": cam,
        "csv_path": str(csv_path),
        "lookup": str(root / "对照表.csv"),
    }


@app.get("/api/offline/config")
def offline_get_config():
    return jsonify({
        "folder": offline_cfg["folder"],
        "output": offline_cfg["output"],
        "root": offline_cfg.get("root", ""),
        "cam": offline_cfg.get("cam", ""),
        "user": offline_cfg["user"],
        "takes": _offline_takes_payload(),
    })


@app.post("/api/offline/config")
def offline_set_config():
    data = request.get_json(force=True) or {}
    if data.get("folder"):
        try:
            _set_offline_cam_folder(data["folder"])
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
    if isinstance(data.get("user"), dict):
        offline_cfg["user"].update({k: str(v) for k, v in data["user"].items()})
    try:
        takes = _offline_takes_payload()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "folder": offline_cfg["folder"],
        "output": offline_cfg["output"],
        "root": offline_cfg.get("root", ""),
        "cam": offline_cfg.get("cam", ""),
        "user": offline_cfg["user"],
        "takes": takes,
    })


@app.post("/api/offline/browse/folder")
def offline_browse_folder():
    try:
        path = run_osascript(
            'POSIX path of (choose folder with prompt "选择机位文件夹，例如 S004/现场原视频/c90")'
        ).rstrip("/")
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        info = _set_offline_cam_folder(path)
        n = len([t for t in offline_cut.load_takes(Path(info["folder"])) if t["keep"]])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    hint = (
        f"预览机位 {info['cam']}（只标这一机，导出时四机同步切）；"
        f"只读对照表 {info['lookup']}；"
        f"新建标注表 {info['csv_path']}；有效 {n} 条"
    )
    return jsonify({
        "path": info["folder"],
        "output": info["output"],
        "csv_path": info["csv_path"],
        "cam": info["cam"],
        "hint": hint,
    })


@app.post("/api/offline/browse/output")
def offline_browse_output():
    try:
        path = run_osascript(
            'POSIX path of (choose folder with prompt "选择切分输出目录")'
        ).rstrip("/")
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    offline_cfg["output"] = path
    return jsonify({"path": path})


@app.post("/api/offline/marks")
def offline_save_marks():
    data = request.get_json(force=True) or {}
    output = Path(offline_cfg["output"]) if offline_cfg["output"] else None
    if not output:
        return jsonify({"error": "还没选输出目录"}), 400
    progress = offline_cut.read_progress(output)
    take = str(data.get("take") or "")
    progress.setdefault("marks", {})
    progress["marks"][take] = {
        "starts": data.get("starts") or [],
        "peaks": data.get("peaks") or [],
    }
    offline_cut.write_progress(output, progress)
    return jsonify({"ok": True})


@app.get("/api/offline/video/<int:take>/<cam>")
def offline_serve_video(take: int, cam: str):
    folder = Path(offline_cfg["folder"])
    try:
        takes = {item["take"]: item for item in offline_cut.load_takes(folder)}
        info = takes[take]
        filename = info["files"][cam]
        path = offline_cut.resolve_video(folder, filename)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 404
    return send_file(path)


@app.get("/api/offline/video/<int:take>/<cam>/meta")
def offline_video_meta(take: int, cam: str):
    folder = Path(offline_cfg["folder"])
    try:
        takes = {item["take"]: item for item in offline_cut.load_takes(folder)}
        info = takes[take]
        filename = info["files"][cam]
        path = offline_cut.resolve_video(folder, filename)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 404
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    cap.release()
    duration = frames / fps if fps else 0
    return jsonify({"duration": duration, "fps": fps, "frames": frames})


@app.get("/api/offline/jobs")
def offline_jobs_status():
    return jsonify({"jobs": _offline_jobs_payload()})


@app.post("/api/offline/export")
def offline_export():
    data = request.get_json(force=True) or {}
    folder = Path(offline_cfg["folder"])
    output = Path(offline_cfg["output"])
    if not folder.is_dir():
        return jsonify({"error": "还没选机位文件夹，例如 S004/现场原视频/c90"}), 400
    if not offline_cfg["output"]:
        return jsonify({"error": "还没选机位文件夹"}), 400
    try:
        takes = {item["take"]: item for item in offline_cut.load_takes(folder)}
        info = takes[int(data["take"])]
        user = dict(offline_cfg["user"])
        if isinstance(data.get("user"), dict):
            user.update({k: str(v) for k, v in data["user"].items()})
        starts = [float(x) for x in (data.get("starts") or [])]
        peaks = [float(x) for x in (data.get("peaks") or [])]
        duration = float(data.get("duration") or 0)
        offline_cut.build_segments(info["kind"], starts, peaks, duration)
        if info["kind"] == "dynamic" and len(starts) != int(info["rep_count"]):
            raise RuntimeError(f"这条应标 {info['rep_count']} 个起始点，现在 {len(starts)} 个")
        if info["kind"] == "static" and len(peaks) != 1:
            raise RuntimeError("静态动作只需 1 个峰值")
        if info["kind"] == "dynamic" and len(peaks) != int(info["rep_count"]):
            raise RuntimeError(f"这条应标 {info['rep_count']} 个峰值，现在 {len(peaks)} 个")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    take_no = int(data["take"])
    with offline_job_lock:
        busy = [
            job for job in offline_jobs.values()
            if job["take"] == take_no and job["status"] in {"queued", "running"}
        ]
        if busy:
            return jsonify({"error": f"t{take_no:02d} 正在后台剪切，不用再点导出"}), 409
        job_id = uuid.uuid4().hex[:8]
        job = {
            "id": job_id,
            "take": take_no,
            "status": "queued",
            "error": "",
            "result": {},
            "args": {
                "folder": folder,
                "output": output,
                "take_info": info,
                "starts": starts,
                "peaks": peaks,
                "duration": duration,
                "user": user,
                "cam": offline_cfg.get("cam") or data.get("cam") or "",
            },
        }
        offline_jobs[job_id] = job
    _ensure_offline_worker()
    offline_export_queue.put(job_id)
    return jsonify({
        "ok": True,
        "queued": True,
        "job_id": job_id,
        "take": take_no,
    })


def open_browser(port: int) -> None:
    url = f"http://127.0.0.1:{port}"
    try:
        webbrowser.open(url)
    except Exception:
        print(f"请手动打开: {url}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"\n  视频标注工具（Web 版）")
    print(f"  浏览器访问: http://127.0.0.1:{port}")
    print(f"  S004 请打开「线下切分标注」")
    print(f"  标注进度保存在 .annotator-done.json，不再生成空白 .annotated 文件\n")
    open_browser(port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
