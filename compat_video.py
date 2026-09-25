# -*- coding: utf-8 -*-
"""把 iPhone 的 HEVC/.mov 转成 Windows 浏览器和 OpenCV 都能打开的 H.264 MP4。

用的是项目已有的 imageio-ffmpeg，不另装编码器。
已经是 H.264 MP4 的文件跳过。H.264 的 .mov 只改封装，不重编码。
HEVC 会重编码。成功后原文件挪到同目录的 iphone原片，列表里不再出现。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}
SKIP_DIRS = {"iphone原片", "作废", "处理后视频"}
HEVC_CODECS = {"hevc", "h265", "hev1", "hvc1", "dvhe", "dvh1"}
H264_CODECS = {"h264", "avc", "avc1"}


def get_ffmpeg() -> str:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError(
            "缺少 imageio-ffmpeg，请先执行：pip install -r requirements.txt"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def pick_folder() -> str:
    from pick_path import pick_folder as choose

    return choose("选择 iPhone 视频所在文件夹")


def video_codec(path: Path) -> str:
    result = subprocess.run(
        [get_ffmpeg(), "-hide_banner", "-i", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    text = (result.stderr or "") + (result.stdout or "")
    for line in text.splitlines():
        if "Video:" not in line:
            continue
        part = line.split("Video:", 1)[1].strip()
        token = part.split()[0] if part else ""
        return token.lower().strip(",")
    return ""


def plan_action(codec: str, suffix: str) -> str:
    """返回 skip、remux 或 transcode。"""
    codec = (codec or "").lower()
    suffix = suffix.lower()
    if codec in HEVC_CODECS or codec == "prores":
        return "transcode"
    if codec in H264_CODECS and suffix == ".mp4":
        return "skip"
    if codec in H264_CODECS and suffix in {".mov", ".m4v", ".qt"}:
        return "remux"
    if suffix in {".mov", ".m4v", ".qt"}:
        return "transcode"
    return "skip"


def iter_videos(root: Path):
    if root.is_file():
        if root.suffix.lower() in VIDEO_EXTS and not root.name.startswith("."):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".")
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() in VIDEO_EXTS:
                yield path


def scan_folder(folder: str) -> list[dict]:
    from pick_path import clean_user_path

    root = Path(clean_user_path(folder)).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"找不到文件夹：{folder}")
    items = []
    for path in iter_videos(root):
        codec = video_codec(path)
        action = plan_action(codec, path.suffix)
        items.append(
            {
                "path": str(path),
                "name": path.name,
                "codec": codec or "未知",
                "action": action,
            }
        )
    return items


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "ffmpeg 失败").strip()
        raise RuntimeError(err[-800:])


def _remux(src: Path, dst: Path) -> None:
    _run_ffmpeg(
        [
            get_ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dst),
        ]
    )


def _transcode(src: Path, dst: Path) -> None:
    # 默认会按拍摄方向把画面转正，Windows 播放器不用再认旋转标记。
    _run_ffmpeg(
        [
            get_ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-tag:v",
            "avc1",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(dst),
        ]
    )


def convert_file(file_path: str) -> dict:
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"找不到文件：{file_path}")
    codec = video_codec(path)
    action = plan_action(codec, path.suffix)
    if action == "skip":
        return {"ok": True, "action": "skip", "name": path.name, "codec": codec}
    desired = path.with_suffix(".mp4")
    if desired.exists() and desired.resolve() != path.resolve():
        raise FileExistsError(f"已有 {desired.name}，没有覆盖")
    tmp = path.parent / f".~compat-{path.stem}.mp4"
    if tmp.exists():
        tmp.unlink()
    try:
        if action == "remux":
            try:
                _remux(path, tmp)
            except RuntimeError:
                action = "transcode"
                if tmp.exists():
                    tmp.unlink()
                _transcode(path, tmp)
        else:
            _transcode(path, tmp)
        if not tmp.is_file() or tmp.stat().st_size < 1000:
            raise RuntimeError("转换结果是空的")
        archive = path.parent / "iphone原片"
        archive.mkdir(exist_ok=True)
        archived = archive / path.name
        if archived.exists():
            archived = archive / f"{path.stem}-{path.stat().st_mtime_ns}{path.suffix}"
        path.replace(archived)
        try:
            tmp.replace(desired)
        except Exception:
            archived.replace(path)
            raise
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    return {
        "ok": True,
        "action": action,
        "name": path.name,
        "output": desired.name,
        "archived": str(archived),
        "codec": codec,
    }
