"""残留扫描：文件系统目录 + 注册表键。

扫描结果只是"候选"，删除前必须经过用户勾选确认与 cleaner 的安全检查。
"""
from __future__ import annotations

import os
import winreg
from dataclasses import dataclass

from . import matcher, scanner, winutil
from .matcher import MEDIUM, STRONG, WEAK

HIVE_NAMES = {winreg.HKEY_LOCAL_MACHINE: "HKLM", winreg.HKEY_CURRENT_USER: "HKCU"}

REG_SCAN_ROOTS = [
    (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE"),
    (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node"),
    (winreg.HKEY_CURRENT_USER, "Software"),
]

_KIND_TEXT = {"dir": "文件夹", "file": "文件", "regkey": "注册表项"}


class ScanCancelled(RuntimeError):
    """扫描被用户取消（cancel_check 返回 True 时抛出）。"""


def _make_stop(cancel_check):
    def _stop():
        if cancel_check is not None and cancel_check():
            raise ScanCancelled()
    return _stop


@dataclass
class Leftover:
    path: str              # 文件系统绝对路径，或 "HKLM\SOFTWARE\..." 形式
    kind: str              # "dir" / "file" / "regkey"
    matched_by: str        # 匹配原因（给人看）
    strength: str          # STRONG / MEDIUM / WEAK
    warning: str = ""      # 风险提示（如可能包含其他软件）
    default_checked: bool = False

    @property
    def kind_display(self) -> str:
        return _KIND_TEXT.get(self.kind, self.kind)


class ScanContext:
    """一次扫描会话的缓存：根目录列表与注册表子键列表只读一遍。"""

    def __init__(self):
        self._fs: dict = {}
        self._reg: dict = {}

    def fs_children(self, root: str) -> list[tuple[str, str, bool]]:
        if root not in self._fs:
            try:
                with os.scandir(root) as it:
                    self._fs[root] = [(e.name, e.path, e.is_dir()) for e in it]
            except OSError:
                self._fs[root] = []
        return self._fs[root]

    def reg_children(self, hive, base: str) -> list[str]:
        key = (hive, base)
        if key not in self._reg:
            names: list[str] = []
            try:
                with winreg.OpenKey(hive, base, 0,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                    i = 0
                    while True:
                        try:
                            names.append(winreg.EnumKey(k, i))
                            i += 1
                        except OSError:
                            break
            except OSError:
                pass
            self._reg[key] = names
        return self._reg[key]


def _is_shared_with_others(path: str, other_apps) -> bool:
    """候选目录是否是其他已安装软件的安装目录（或其上级）。"""
    np = winutil.normalize_path(path)
    for a in other_apps:
        if not a.install_location:
            continue
        nl = winutil.normalize_path(a.install_location)
        if nl == np or nl.startswith(np + os.sep):
            return True
    return False


def _is_shared_reg(subpath: str, other_apps) -> bool:
    """候选注册表键是否包含其他软件的卸载条目。"""
    p = subpath.lower()
    for a in other_apps:
        sk = (a.reg_subkey or "").lower()
        if sk and (sk == p or sk.startswith(p + "\\")):
            return True
    return False


def _dedupe(items: list[Leftover]) -> list[Leftover]:
    best: dict[str, Leftover] = {}
    for x in items:
        key = x.path.lower() if x.kind == "regkey" else winutil.normalize_path(x.path)
        old = best.get(key)
        if old is None or matcher.STRENGTH_RANK[x.strength] < matcher.STRENGTH_RANK[old.strength]:
            best[key] = x
    out = sorted(best.values(),
                 key=lambda x: (matcher.STRENGTH_RANK[x.strength], x.path.lower()))
    return out


# ---------------------------------------------------------------- 文件系统

def scan_filesystem(app, after_uninstall: bool, ctx: ScanContext, other_apps,
                    cancel_check=None) -> list[Leftover]:
    """扫描文件系统残留。

    after_uninstall=True（卸载后）：额外包含安装目录本体、安装类目录、开始菜单快捷方式。
    after_uninstall=False（软件仍安装时）：只看用户数据目录，避免把正在使用的程序目录当残留。
    cancel_check: 周期性调用的取消判定，返回 True 时抛出 ScanCancelled。
    """
    _stop = _make_stop(cancel_check)
    _stop()
    results: list[Leftover] = []

    def add(path, kind, strength, reason, warning="", default_checked=None):
        if default_checked is None:
            default_checked = strength != WEAK and not warning
        results.append(Leftover(str(path), kind, reason, strength, warning, default_checked))

    # 用户数据目录（两种模式都扫）
    for root in scanner.expand_roots(scanner.FS_ROOTS_USER):
        _stop()
        for i, (name, path, is_dir) in enumerate(ctx.fs_children(root)):
            if i % 256 == 0:
                _stop()
            m = matcher.match_candidate(name, app)
            if not m:
                continue
            strength, reason = m
            warn = "⚠ 该目录可能同时包含其他软件的数据" if is_dir and _is_shared_with_others(path, other_apps) else ""
            add(path, "dir" if is_dir else "file", strength, reason, warn)

    if after_uninstall:
        # 原安装目录本体
        il = app.install_location
        if il and os.path.isdir(il):
            warn = "⚠ 该目录可能同时包含其他软件" if _is_shared_with_others(il, other_apps) else ""
            add(il, "dir", STRONG, "原安装目录在卸载后仍然存在", warn)
            # 上级厂商目录（如 ...\Tencent\Meeting 里的 Tencent）
            parent = os.path.dirname(il.rstrip("\\"))
            pnorm = winutil.normalize_path(parent)
            install_roots = [winutil.normalize_path(r)
                             for r in scanner.expand_roots(scanner.FS_ROOTS_INSTALL)]
            if any(pnorm.startswith(r + os.sep) for r in install_roots):
                m = matcher.match_candidate(os.path.basename(parent), app)
                if m and m[0] in (STRONG, MEDIUM):
                    add(parent, "dir", MEDIUM, f"上级目录：{m[1]}",
                        "⚠ 可能同时包含其他软件", default_checked=False)
        # 安装类目录里的其它匹配文件夹
        for root in scanner.expand_roots(scanner.FS_ROOTS_INSTALL):
            _stop()
            for i, (name, path, is_dir) in enumerate(ctx.fs_children(root)):
                if i % 256 == 0:
                    _stop()
                if not is_dir:
                    continue
                if il and winutil.normalize_path(path) == winutil.normalize_path(il):
                    continue
                m = matcher.match_candidate(name, app)
                if not m:
                    continue
                strength, reason = m
                warn = "⚠ 可能同时包含其他软件" if _is_shared_with_others(path, other_apps) else ""
                add(path, "dir", strength, reason, warn)
        # 开始菜单快捷方式
        for root in scanner.expand_roots(scanner.FS_ROOTS_STARTMENU):
            _stop()
            for i, (name, path, is_dir) in enumerate(ctx.fs_children(root)):
                if i % 256 == 0:
                    _stop()
                if is_dir or not name.lower().endswith(".lnk"):
                    continue
                m = matcher.match_candidate(name[:-4], app)
                if not m or m[0] == WEAK:
                    continue
                add(path, "file", m[0], "开始菜单快捷方式：" + m[1])

    return _dedupe(results)


# ---------------------------------------------------------------- 注册表

def scan_registry(app, ctx: ScanContext, other_apps,
                  include_uninstall_entry: bool = True,
                  cancel_check=None) -> list[Leftover]:
    """扫描注册表残留（限定在 HKLM\SOFTWARE、WOW6432Node、HKCU\Software 下）。"""
    _stop = _make_stop(cancel_check)
    _stop()
    results: list[Leftover] = []
    for hive, base in REG_SCAN_ROOTS:
        _stop()
        children = ctx.reg_children(hive, base)
        for i, name in enumerate(children):
            if i % 64 == 0:
                _stop()
            m = matcher.match_candidate(name, app)
            if not m:
                continue
            strength, reason = m
            sub = base + "\\" + name
            warn = "⚠ 可能包含其他软件的配置" if _is_shared_reg(sub, other_apps) else ""
            results.append(Leftover(f"{HIVE_NAMES[hive]}\\{sub}", "regkey", reason,
                                    strength, warn, strength != WEAK and not warn))
            # 二级展开：厂商键下与软件名精确匹配的产品键
            if strength in (STRONG, MEDIUM):
                for j, child in enumerate(ctx.reg_children(hive, sub)[:300]):
                    if j % 64 == 0:
                        _stop()
                    m2 = matcher.match_candidate(child, app)
                    if m2 and m2[0] == STRONG:
                        sub2 = sub + "\\" + child
                        warn2 = "⚠ 可能包含其他软件的配置" if _is_shared_reg(sub2, other_apps) else ""
                        results.append(Leftover(f"{HIVE_NAMES[hive]}\\{sub2}", "regkey",
                                                f"{reason}；子键{m2[1]}", STRONG, warn2))
    # 卸载条目本身未删干净的残留（软件仍安装时必然存在，评估风险时应排除）
    if include_uninstall_entry and app.reg_subkey:
        try:
            winreg.OpenKey(scanner.HIVES[app.hive_name], app.reg_subkey, 0,
                           winreg.KEY_READ | winreg.KEY_WOW64_64KEY).Close()
            results.append(Leftover(f"{app.hive_name}\\{app.reg_subkey}", "regkey",
                                    "软件的注册表卸载条目仍未删除", STRONG, "", True))
        except OSError:
            pass
    return _dedupe(results)


# ---------------------------------------------------------------- 风险评估

def assess_risk(app, all_apps, ctx: ScanContext) -> tuple[str, list[str]]:
    """估算“残留风险等级”（高/中/低）及原因。

    评分（启发式，仅作参考）：
      无卸载程序 +3；用户数据疑似残留每处 +2（最多 3 处）；
      注册表疑似残留每处 +1（最多 3 处）；自定义卸载器 +1；发布者未知 +1；MSI -1。
      >=5 高；2~4 中；其余 低。
    """
    others = [a for a in all_apps if a is not app]
    fs_hits = [x for x in scan_filesystem(app, False, ctx, others)
               if x.strength in (STRONG, MEDIUM)]
    reg_hits = [x for x in scan_registry(app, ctx, others, include_uninstall_entry=False)
                if x.strength in (STRONG, MEDIUM)]

    score = 0
    reasons: list[str] = []
    if not (app.uninstall_string or "").strip():
        score += 3
        reasons.append("没有卸载程序，该条目本身就是残留")
    for x in fs_hits[:3]:
        score += 2
        reasons.append(f"检测到疑似残留数据：{x.path}")
    for x in reg_hits[:3]:
        score += 1
        reasons.append(f"检测到疑似残留注册表项：{x.path}")
    if app.is_msi:
        score = max(0, score - 1)
    else:
        score += 1
        reasons.append("使用自定义卸载程序（非 Windows Installer），官方卸载通常清理不彻底")
    if not (app.publisher or "").strip():
        score += 1

    level = "高" if score >= 5 else "中" if score >= 2 else "低"
    if not reasons:
        reasons.append("未发现明显残留迹象")
    return level, reasons
