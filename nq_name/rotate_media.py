# -*- coding: utf-8 -*-
"""相册/QuickTime 式旋转：只改显示方向元数据（-c copy），可另存为新文件。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def get_ffmpeg() -> str:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError(
            "缺少 imageio-ffmpeg，请在本目录执行: pip3 install imageio-ffmpeg"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def probe_display_rotation(path: Path) -> float:
    """读取显示旋转角（逆时针，度），与 ffmpeg displaymatrix 一致。"""
    ffmpeg = get_ffmpeg()
    result = _run([ffmpeg, "-hide_banner", "-i", str(path)])
    text = (result.stderr or "") + (result.stdout or "")
    matches = re.findall(
        r"displaymatrix:\s*rotation of\s*([+-]?\d+(?:\.\d+)?)\s*degrees",
        text,
        flags=re.IGNORECASE,
    )
    if matches:
        return float(matches[0])
    tag = re.findall(r"rotate\s*:\s*([+-]?\d+(?:\.\d+)?)", text, flags=re.IGNORECASE)
    if tag:
        return float(tag[0])
    return 0.0


def normalize_rotation_deg(deg: float) -> int:
    """规范到 {0, 90, 180, -90}。"""
    x = ((float(deg) + 180.0) % 360.0) - 180.0
    candidates = [0, 90, 180, -90, -180]
    best = min(candidates, key=lambda c: abs(c - x))
    if best in (-180, 180):
        return 180
    return int(best)


def css_degrees_from_ccw(ccw: float) -> int:
    """ffmpeg 逆时针角 → CSS 顺时针角。"""
    return int((-normalize_rotation_deg(ccw)) % 360)


def export_with_display_rotation(
    src: Path,
    dst: Path,
    degrees_ccw: float,
) -> int:
    """
    将 src 流拷贝另存为 dst，并设置显示旋转（逆时针度）。
    不重编码视频轨/音轨，速度接近复制文件。
    """
    src = src.resolve()
    dst = Path(dst)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    if dst.exists() and dst.resolve() == src.resolve():
        # 同路径：先写临时再替换
        tmp = src.parent / f".~orient-save-{src.stem}{src.suffix}"
        written = export_with_display_rotation(src, tmp, degrees_ccw)
        bak = src.parent / f".~backup-{src.name}"
        if bak.exists():
            bak.unlink()
        src.rename(bak)
        try:
            tmp.rename(src)
        except Exception:
            if src.exists():
                src.unlink(missing_ok=True)
            bak.rename(src)
            raise
        finally:
            if bak.exists():
                bak.unlink(missing_ok=True)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return written

    target = normalize_rotation_deg(degrees_ccw)
    ffmpeg = get_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(f"目标已存在: {dst.name}")

    tmp = dst.parent / f".~orient-export-{dst.stem}{dst.suffix}"
    if tmp.exists():
        tmp.unlink()

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-display_rotation",
        str(target),
        "-i",
        str(src),
        "-c",
        "copy",
        "-map_metadata",
        "0",
        str(tmp),
    ]
    result = _run(cmd)
    if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1000:
        err = (result.stderr or result.stdout or "另存旋转文件失败").strip()
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(err)

    tmp.rename(dst)
    return target
