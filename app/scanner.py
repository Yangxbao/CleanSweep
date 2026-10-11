r"""读取 Windows 注册表 Uninstall 键，枚举已安装软件。

覆盖以下位置（KEY_WOW64_64KEY 视图，兼容 32/64 位 Python）：
  HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall
  HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall
  HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall
  HKCU\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall
"""
from __future__ import annotations

import os
import re
import winreg
from dataclasses import dataclass, field

from . import matcher, winutil

HIVES = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}

_MSI_GUID_RE = re.compile(
    r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")

UNINSTALL_KEYS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
]

# 残留扫描涉及的文件系统根目录（%VAR% 写法，运行时展开）
FS_ROOTS_USER = [r"%LocalAppData%", r"%APPDATA%", r"%ProgramData%"]
FS_ROOTS_INSTALL = [r"%ProgramFiles%", r"%ProgramFiles(x86)%", r"%LocalAppData%\Programs"]
FS_ROOTS_STARTMENU = [
    r"%ProgramData%\Microsoft\Windows\Start Menu\Programs",
    r"%APPDATA%\Microsoft\Windows\Start Menu\Programs",
]


@dataclass
class InstalledApp:
    name: str
    version: str = ""
    publisher: str = ""
    install_location: str = ""
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    display_icon: str = ""
    estimated_size_kb: int | None = None
    install_date: str = ""          # 注册表原始值 YYYYMMDD
    hive_name: str = ""             # "HKLM" / "HKCU"
    reg_subkey: str = ""            # 卸载条目的完整子键路径
    is_msi: bool = False
    # 以下为计算字段
    size_bytes: int | None = None   # 实测安装目录大小（后台线程填充）
    risk_level: str = ""            # 高 / 中 / 低（后台评估填充）
    risk_reasons: list = field(default_factory=list)
    name_tokens: list = field(default_factory=list)
    install_tokens: list = field(default_factory=list)
    _name_core: str = ""

    @property
    def name_core(self) -> str:
        if not self._name_core:
            self._name_core = matcher.name_core(self.name)
        return self._name_core

    def match_tokens(self) -> list[tuple[str, int]]:
        """(特征词, 权重)。来自安装目录的组件最可信；同一词取最大权重。"""
        weights: dict[str, int] = {}
        for t in self.name_tokens:
            weights[t] = max(weights.get(t, 0), 2)
        for t in self.install_tokens:
            weights[t] = max(weights.get(t, 0), 3)
        return list(weights.items())

    @property
    def size_bytes_effective(self):
        if self.size_bytes is not None:
            return self.size_bytes
        if self.estimated_size_kb:
            return self.estimated_size_kb * 1024
        return None

    @property
    def size_display(self) -> str:
        return winutil.human_size(self.size_bytes_effective)

    @property
    def date_display(self) -> str:
        d = self.install_date
        if re.fullmatch(r"\d{8}", d or ""):
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return ""


# ---------------------------------------------------------------- 注册表枚举

def _read_value(key, name: str):
    try:
        value, _typ = winreg.QueryValueEx(key, name)
        return value
    except OSError:
        return None


def _as_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(re.sub(r"[^\d\-]", "", str(value)))
    except (TypeError, ValueError):
        return None


def _valid_date(value) -> str:
    s = str(value or "").strip()
    return s if re.fullmatch(r"\d{8}", s) else ""


def _derive_location_from_icon(icon: str) -> str:
    if not icon:
        return ""
    icon = winutil.expand_env(icon).strip().strip('"')
    m = re.match(r"^(.*\.(?:exe|ico|dll))", icon, re.IGNORECASE)
    if m:  # 去掉 "xxx.exe,0" 这类图标索引后缀
        icon = m.group(1)
    if os.path.isdir(icon):
        return icon
    parent = os.path.dirname(icon)
    return parent if os.path.isdir(parent) else ""


def _install_root_components(location: str) -> list[str]:
    """安装路径中位于标准根目录之下的组件（避免把用户名等当作特征词）。"""
    if not location:
        return []
    nloc = winutil.normalize_path(location)
    roots = sorted((winutil.normalize_path(r) for r in known_fs_roots()),
                   key=len, reverse=True)
    for root in roots:
        if nloc == root:
            return []
        if nloc.startswith(root + os.sep):
            rel = nloc[len(root):].strip(os.sep)
            return [c for c in rel.split(os.sep) if c]
    return []


def fill_derived(app: InstalledApp) -> None:
    """填充特征词（matcher 用）等派生字段。"""
    app.name_tokens = matcher.distinctive_tokens(app.name, app.publisher)
    comps = _install_root_components(app.install_location)
    if app.uninstall_string and not app.is_msi:
        try:
            argv = winutil.cmdline_to_argv(winutil.expand_env(app.uninstall_string))
            comps += _install_root_components(os.path.dirname(argv[0]))
        except Exception:
            pass
    app.install_tokens = matcher.distinctive_tokens(*comps)


def _collect_view(hive_name: str, base_path: str, out: list, seen: set) -> None:
    hive = HIVES[hive_name]
    try:
        root = winreg.OpenKey(hive, base_path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except OSError:
        return
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
                i += 1
            except OSError:
                break
            full = base_path + "\\" + sub
            try:
                with winreg.OpenKey(hive, full, 0,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                    name = _read_value(k, "DisplayName")
                    if not name or not str(name).strip():
                        continue
                    name = str(name).strip()
                    if name.startswith("@") or "ms-resource:" in name:
                        continue  # 商店应用的占位条目
                    if _as_int(_read_value(k, "SystemComponent")):
                        continue  # 隐藏的系统组件/更新（与控制面板口径一致）
                    if _read_value(k, "ParentKeyName"):
                        continue  # 补丁类条目
                    uninstall_string = str(_read_value(k, "UninstallString") or "").strip()
                    quiet = str(_read_value(k, "QuietUninstallString") or "").strip()
                    is_msi = "msiexec" in uninstall_string.lower()
                    if is_msi:
                        guid = _MSI_GUID_RE.search(uninstall_string)
                        dedupe = ("msi", name.lower(),
                                  guid.group(0).lower() if guid else uninstall_string)
                    else:
                        dedupe = ("exe", name.lower(),
                                  str(_read_value(k, "DisplayVersion") or ""),
                                  uninstall_string)
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)

                    loc = winutil.expand_env(str(_read_value(k, "InstallLocation") or ""))
                    loc = loc.strip().strip('"').rstrip("\\")
                    if loc and not os.path.isabs(loc):
                        loc = ""
                    if not loc:
                        loc = _derive_location_from_icon(
                            str(_read_value(k, "DisplayIcon") or ""))
                        if not loc and uninstall_string and not is_msi:
                            try:
                                argv = winutil.cmdline_to_argv(
                                    winutil.expand_env(uninstall_string))
                                d = os.path.dirname(argv[0])
                                if os.path.isdir(d):
                                    loc = d
                            except Exception:
                                pass

                    app = InstalledApp(
                        name=name,
                        version=str(_read_value(k, "DisplayVersion") or "").strip(),
                        publisher=str(_read_value(k, "Publisher") or "").strip(),
                        install_location=loc,
                        uninstall_string=uninstall_string,
                        quiet_uninstall_string=quiet,
                        display_icon=str(_read_value(k, "DisplayIcon") or "").strip(),
                        estimated_size_kb=_as_int(_read_value(k, "EstimatedSize")),
                        install_date=_valid_date(_read_value(k, "InstallDate")),
                        hive_name=hive_name,
                        reg_subkey=full,
                        is_msi=is_msi,
                    )
                    fill_derived(app)
                    out.append(app)
            except OSError:
                continue


def enumerate_apps() -> list[InstalledApp]:
    """枚举所有已安装软件，按名称排序。"""
    apps: list[InstalledApp] = []
    seen: set = set()
    for hive_name, base in UNINSTALL_KEYS:
        _collect_view(hive_name, base, apps, seen)
    apps.sort(key=lambda a: a.name.lower())
    return apps


def entry_exists(app: InstalledApp) -> bool:
    """该软件的卸载条目是否仍存在于注册表。"""
    if not app.hive_name or not app.reg_subkey:
        return False
    try:
        winreg.OpenKey(HIVES[app.hive_name], app.reg_subkey, 0,
                       winreg.KEY_READ | winreg.KEY_WOW64_64KEY).Close()
        return True
    except OSError:
        return False


def dir_size(path: str) -> tuple[int, int]:
    """统计目录大小（字节）与文件数。不进入 junction / 符号链接。"""
    total, files = 0, 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
                if getattr(st, "st_file_attributes", 0) & winutil.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                else:
                    total += st.st_size
                    files += 1
            except OSError:
                continue
    return total, files


# ---------------------------------------------------------------- 目录工具

def expand_roots(templates: list[str]) -> list[str]:
    out = []
    for t in templates:
        p = winutil.expand_env(t)
        if p and os.path.isdir(p):
            out.append(os.path.normpath(p))
    return out


def known_fs_roots() -> list[str]:
    """所有允许扫描/清理的文件系统根目录（含开始菜单）。"""
    return expand_roots(FS_ROOTS_USER + FS_ROOTS_INSTALL + FS_ROOTS_STARTMENU)
