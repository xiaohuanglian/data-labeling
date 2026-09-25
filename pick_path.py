# -*- coding: utf-8 -*-
"""选文件夹。macOS 用系统对话框，Windows 用系统文件夹窗口并提到最前。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile


def clean_user_path(raw: str) -> str:
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    text = text.rstrip("/\\")
    if len(text) == 2 and text[1] == ":":
        text += "\\"
    return text


def pick_folder(prompt: str) -> str:
    return _pick(prompt, kind="dir")


def pick_file(prompt: str) -> str:
    return _pick(prompt, kind="file")


def _pick(prompt: str, kind: str) -> str:
    if sys.platform == "darwin":
        return _pick_mac(prompt, kind)
    if sys.platform == "win32":
        return _pick_windows(prompt, kind)
    raise RuntimeError("没有打开文件夹窗口")


def _pick_mac(prompt: str, kind: str) -> str:
    safe = prompt.replace("\\", "\\\\").replace('"', '\\"')
    if kind == "dir":
        script = f'POSIX path of (choose folder with prompt "{safe}")'
    else:
        script = f'POSIX path of (choose file with prompt "{safe}" of type {{"csv"}})'
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        stderr = (result.stderr or "").strip()
        if (
            not stderr
            or "User canceled" in stderr
            or "用户已取消" in stderr
            or "(-128)" in stderr
        ):
            raise RuntimeError("没有选择文件夹")
        raise RuntimeError(stderr)
    return result.stdout.strip().rstrip("/")


_TK_PICK = r"""
import sys
from tkinter import Tk, filedialog
title = sys.argv[1]
kind = sys.argv[2]
root = Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
root.update()
if kind == "dir":
    path = filedialog.askdirectory(title=title, parent=root) or ""
else:
    path = filedialog.askopenfilename(
        title=title,
        parent=root,
        filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
    ) or ""
root.destroy()
sys.stdout.buffer.write(str(path).encode("utf-8"))
"""

_PS_PICK = r"""
$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class PickForeground {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr ProcessId);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  public static void Steal(IntPtr hWnd) {
    uint fore = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero);
    uint cur = GetCurrentThreadId();
    if (fore != 0 && fore != cur) AttachThreadInput(fore, cur, true);
    BringWindowToTop(hWnd);
    SetForegroundWindow(hWnd);
    if (fore != 0 && fore != cur) AttachThreadInput(fore, cur, false);
  }
}
"@
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Show()
$owner.Activate()
[PickForeground]::Steal($owner.Handle) | Out-Null
$chosen = ""
if ($env:PICK_KIND -eq "file") {
  $dialog = New-Object System.Windows.Forms.OpenFileDialog
  $dialog.Title = $env:PICK_TITLE
  $dialog.Filter = "CSV (*.csv)|*.csv|All (*.*)|*.*"
  $ok = $dialog.ShowDialog($owner)
  if ($ok -eq [System.Windows.Forms.DialogResult]::OK) { $chosen = $dialog.FileName }
} else {
  $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description = $env:PICK_TITLE
  $dialog.UseDescriptionForTitle = $true
  $dialog.RootFolder = [System.Environment+SpecialFolder]::MyComputer
  $ok = $dialog.ShowDialog($owner)
  if ($ok -eq [System.Windows.Forms.DialogResult]::OK) { $chosen = $dialog.SelectedPath }
}
$owner.Close()
if ($chosen) {
  $utf8 = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($env:PICK_OUT, $chosen, $utf8)
}
"""


def _pick_windows(prompt: str, kind: str) -> str:
    path, failed = _pick_windows_powershell(prompt, kind)
    if failed:
        tk_path = _pick_windows_tk(prompt, kind)
        if tk_path is None:
            raise RuntimeError("没有打开文件夹窗口")
        path = tk_path
    path = clean_user_path(path or "")
    if not path:
        raise RuntimeError("没有选择文件夹")
    return path


def _hidden_startupinfo():
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def _pick_windows_tk(prompt: str, kind: str) -> str | None:
    result = subprocess.run(
        [sys.executable, "-c", _TK_PICK, prompt, kind],
        check=False,
        capture_output=True,
        startupinfo=_hidden_startupinfo(),
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace")


def _pick_windows_powershell(prompt: str, kind: str) -> tuple[str, bool]:
    """返回 (路径, 是否失败)。取消选择时路径为空、失败为假。"""
    fd, out_path = tempfile.mkstemp(prefix="pick-folder-", suffix=".txt")
    os.close(fd)
    env = os.environ.copy()
    env["PICK_TITLE"] = prompt
    env["PICK_KIND"] = kind
    env["PICK_OUT"] = out_path
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", _PS_PICK],
            check=False,
            capture_output=True,
            env=env,
            stdin=subprocess.DEVNULL,
            startupinfo=_hidden_startupinfo(),
        )
    except FileNotFoundError:
        return "", True
    finally:
        text = ""
        try:
            with open(out_path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            text = ""
        try:
            os.remove(out_path)
        except OSError:
            pass
    if result.returncode != 0 and not text.strip():
        return "", True
    return text, False
