"""本机资料文件搜索：按类别（图片/文档/PDF/文本/音视频/压缩包）查找用户文件。

只负责"找"（全程只读）；删除由 GUI 在用户逐项确认后执行，默认进回收站。
默认只扫描用户常用目录（桌面/文档/下载/图片/视频），跳过系统、程序与
应用数据目录；重解析点（junction/符号链接）永不深入。
"""
from __future__ import annotations

import os
import stat
import time
import winreg
from dataclasses import dataclass

from . import winutil

# 类别定义：扩展名一律小写、带点
CATEGORY_DEFS: dict[str, set[str]] = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif",
              ".tif", ".tiff", ".svg"},
    "document": {".doc", ".docx", ".docm", ".ppt", ".pptx", ".pptm", ".xls",
                 ".xlsx", ".xlsm", ".wps", ".et", ".dps", ".csv"},
    "pdf": {".pdf"},
    "text": {".txt", ".md", ".rtf"},
    "audiovideo": {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
                   ".m4v", ".mp3", ".wav", ".m4a", ".flac", ".aac"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
}
CATEGORY_TEXT = {"image": "图片", "document": "文档", "pdf": "PDF",
                 "text": "文本", "audiovideo": "音视频", "archive": "压缩包"}
# GUI 默认勾选的类别（图片 + Word/PPT/Excel 等文档 + PDF）
DEFAULT_CATEGORIES = ("image", "document", "pdf")

# 无论是否勾选"包含系统目录"，这些目录名永远跳过
_ALWAYS_SKIP = {"$recycle.bin", "system volume information", "recycler", "recovery"}
# 默认（未勾选"包含系统目录"时）跳过的系统/程序/应用数据目录名
_SYSTEM_DIR_NAMES = {
    "windows", "windows.old", "program files", "program files (x86)",
    "programdata", "appdata", "windowsapps", "packages", "node_modules",
    ".git", "__pycache__", "nvidia", "amd", "intel", "drivers", "driverstore",
    "onedrivetemp", "perflogs",
}


@dataclass
class FoundFile:
    path: str
    size: int
    mtime: float
    ctime: float      # 创建时间（Windows 上 st_ctime 即创建时间）
    category: str
    ext: str

    @property
    def category_text(self) -> str:
        return CATEGORY_TEXT.get(self.category, self.category)

    @property
    def size_text(self) -> str:
        return winutil.human_size(self.size)

    @property
    def mtime_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.mtime))

    @property
    def ctime_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.ctime))


class ScanCancelled(RuntimeError):
    """扫描被用户取消。"""


def user_folders() -> list[tuple[str, str]]:
    """用户已知文件夹 [(路径, 显示名)]：桌面/文档/下载/图片/视频。

    从注册表读取（目录被重定向到 OneDrive 或其他盘时也能拿到），
    读不到时回退到用户主目录下的标准名称。
    """
    wanted = [("Desktop", "桌面"), ("Personal", "文档"),
              ("{374DE290-123F-4565-9164-39C4925E467B}", "下载"),
              ("My Pictures", "图片"), ("My Video", "视频")]
    out: list[tuple[str, str]] = []
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            for value_name, label in wanted:
                try:
                    path, _typ = winreg.QueryValueEx(k, value_name)
                except OSError:
                    continue
                path = winutil.expand_env(str(path)).strip()
                if path and os.path.isdir(path):
                    out.append((os.path.normpath(path), label))
    except OSError:
        pass
    if not out:
        home = os.path.expanduser("~")
        for name, label in (("Desktop", "桌面"), ("Documents", "文档"),
                            ("Downloads", "下载"), ("Pictures", "图片"),
                            ("Videos", "视频")):
            p = os.path.join(home, name)
            if os.path.isdir(p):
                out.append((p, label))
    # 去重
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for p, label in out:
        np = winutil.normalize_path(p)
        if np not in seen:
            seen.add(np)
            uniq.append((p, label))
    return uniq


def _prune_dirs(dirpath: str, dirnames: list, include_system: bool) -> None:
    """原地剪枝 os.walk 的子目录列表。"""
    keep = []
    for d in dirnames:
        low = d.lower()
        if low in _ALWAYS_SKIP:
            continue
        if not include_system and low in _SYSTEM_DIR_NAMES:
            continue
        if winutil.is_reparse_point(os.path.join(dirpath, d)):
            continue  # junction/符号链接永不深入
        keep.append(d)
    dirnames[:] = keep


def scan(roots, categories, progress=None, cancel_check=None,
         include_system_dirs: bool = False, max_results: int = 10000,
         sink=None, sink_batch: int = 200) -> dict:
    """扫描指定目录下的指定类别文件（全程只读）。

    categories: 类别键集合（见 CATEGORY_DEFS），至少一个。
    progress(current_dir, found, scanned_dirs): 周期回调（约 0.5 秒一次）。
    cancel_check(): 返回 True 时抛出 ScanCancelled。
    sink(list[FoundFile]): 结果分批回调（GUI 流式显示用）。
    max_results: 结果上限，超过即提前结束并在统计中标记 truncated。

    返回统计：{"roots", "scanned_dirs", "scanned_files", "found",
              "total_size", "truncated"}
    """
    ext_map: dict[str, str] = {}
    for cat in categories:
        for ext in CATEGORY_DEFS.get(cat, ()):
            ext_map[ext] = cat
    if not ext_map:
        raise ValueError("未选择任何文件类别")
    roots = [r for r in roots if r and os.path.isdir(r)]
    if not roots:
        raise ValueError("没有可扫描的目录")

    found = 0
    scanned_files = 0
    scanned_dirs = 0
    total_size = 0
    truncated = False
    batch: list[FoundFile] = []
    last_progress = 0.0

    def flush():
        nonlocal batch
        if sink is not None and batch:
            sink(batch)
            batch = []

    for root in roots:
        if cancel_check is not None and cancel_check():
            raise ScanCancelled()
        for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                    onerror=lambda _e: None):
            if cancel_check is not None and cancel_check():
                raise ScanCancelled()
            scanned_dirs += 1
            _prune_dirs(dirpath, dirnames, include_system_dirs)
            if progress is not None:
                now = time.time()
                if now - last_progress >= 0.5:
                    last_progress = now
                    try:
                        progress(dirpath, found, scanned_dirs)
                    except Exception:  # noqa: BLE001
                        pass
            for name in filenames:
                scanned_files += 1
                ext = os.path.splitext(name)[1].lower()
                cat = ext_map.get(ext)
                if cat is None:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    continue
                found += 1
                total_size += st.st_size
                batch.append(FoundFile(full, st.st_size, st.st_mtime,
                                       st.st_ctime, cat, ext))
                if len(batch) >= sink_batch:
                    flush()
                if found >= max_results:
                    truncated = True
                    flush()
                    return {"roots": roots, "scanned_dirs": scanned_dirs,
                            "scanned_files": scanned_files, "found": found,
                            "total_size": total_size, "truncated": truncated}
        flush()
    return {"roots": roots, "scanned_dirs": scanned_dirs,
            "scanned_files": scanned_files, "found": found,
            "total_size": total_size, "truncated": truncated}
