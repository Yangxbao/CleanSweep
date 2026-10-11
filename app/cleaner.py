r"""删除执行与安全检查。

安全边界（硬编码，不依赖调用方自觉）：
- 文件系统：只允许删除已知扫描根目录（Program Files、Program Files (x86)、
  ProgramData、LocalAppData、Roaming AppData、LocalAppData\Programs、开始菜单）
  之下的内容；路径任何一段命中受保护目录名（microsoft/windows/packages/...）一律拒绝；
  junction / 符号链接只删除链接本身，绝不进入目标。
- 注册表：只允许 HKLM\SOFTWARE、HKLM\SOFTWARE\WOW6432Node、HKCU\Software 之下的键，
  且删除前必须先成功导出 .reg 备份，失败则跳过删除。
"""
from __future__ import annotations

import os
import re
import stat
import winreg
from dataclasses import dataclass

from . import backup, scanner, winutil

FORBIDDEN_PARTS = {
    "microsoft", "windows", "windowsapps", "common files", "internet explorer",
    "windows defender", "packages", "package cache", "temp", "tmp",
    "perflogs", "$recycle.bin", "system volume information",
    "programdata", "users", "program files", "program files (x86)",
}

FORBIDDEN_KEY_PARTS = {
    "microsoft", "windows", "windows nt", "classes", "policies", "system",
    "currentversion", "ole", "session manager", "trustedinstaller",
    "safeboot", "services", "control", "virtualstore", "app events",
    "keyboard layout",
}

# 卸载条目本身（Software\...\Uninstall\<键>）不受上面受保护词限制，
# 但只允许恰好一层键名，且只会由扫描器针对本软件生成。
UNINSTALL_ENTRY_RE = re.compile(
    r"^SOFTWARE(?:\\WOW6432Node)?\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\[^\\]+$",
    re.IGNORECASE)

REG_BASES = {
    "HKLM": ["SOFTWARE", "SOFTWARE\\WOW6432Node"],
    "HKCU": ["Software"],
}


@dataclass
class DeleteResult:
    path: str
    kind: str
    ok: bool
    detail: str
    files: int = 0
    bytes_freed: int = 0


# ---------------------------------------------------------------- 安全检查

def check_fs_path(path: str, extra_exact=()) -> tuple[bool, str]:
    """文件系统路径规则检查（不含存在性检查，由调用方负责）。

    extra_exact：额外允许删除的“精确路径”集合（通常是该软件自己的安装目录、
    以及自定义盘符上的厂商父目录）。仅放行路径本身，不放行其下其他内容。
    """
    if not path or not str(path).strip():
        return False, "空路径"
    npath = winutil.normalize_path(path)
    extra = {winutil.normalize_path(p) for p in extra_exact if p}
    if npath in extra:
        parts = [p for p in npath.split(os.sep) if p]
        if len(parts) < 2:
            return False, "路径过浅，拒绝删除"
        for part in parts:
            if part.lower() in FORBIDDEN_PARTS:
                return False, f"路径包含受保护目录名「{part}」"
        return True, ""
    roots = sorted((winutil.normalize_path(r) for r in scanner.known_fs_roots()),
                   key=len, reverse=True)
    root = next((r for r in roots if npath == r or npath.startswith(r + os.sep)), None)
    if root is None:
        return False, "不在允许清理的目录范围内"
    rel = npath[len(root):].strip(os.sep)
    if not rel:
        return False, "拒绝删除根目录本身"
    for part in rel.split(os.sep):
        if part.lower() in FORBIDDEN_PARTS:
            return False, f"路径包含受保护目录名「{part}」"
    return True, ""


def extra_exact_paths_for(app) -> set[str]:
    """该软件在自定义盘符上安装时，额外允许删除的精确路径：
    安装目录本身 + 深度足够的厂商父目录（绝不放行盘符根）。"""
    out: set[str] = set()
    il = app.install_location
    if il and os.path.isabs(il):
        out.add(winutil.normalize_path(il))
        parent = os.path.dirname(il.rstrip("\\"))
        if parent:
            np = winutil.normalize_path(parent)
            if len([p for p in np.split(os.sep) if p]) >= 2:
                out.add(np)
    return out


def check_reg_path(full: str) -> tuple[bool, str]:
    """注册表路径规则检查，full 形如 "HKLM\\SOFTWARE\\Foo"。"""
    full = (full or "").strip()
    m = re.match(r"^(HKLM|HKCU)\\(.+)$", full, re.IGNORECASE)
    if not m:
        return False, "注册表路径格式不正确"
    hive = m.group(1).upper()
    sub = m.group(2).replace("/", "\\")
    bases = REG_BASES.get(hive, [])
    low = sub.lower()
    base = next((b for b in sorted(bases, key=len, reverse=True)
                 if low == b.lower() or low.startswith(b.lower() + "\\")), None)
    if base is None:
        return False, "不在允许清理的注册表范围内"
    rel = sub[len(base):].strip("\\")
    if not rel:
        return False, "拒绝删除注册表根键本身"
    if UNINSTALL_ENTRY_RE.match(sub):
        return True, ""
    for part in rel.split("\\"):
        if part.lower() in FORBIDDEN_KEY_PARTS:
            return False, f"包含受保护键名「{part}」"
    return True, ""


# ---------------------------------------------------------------- 文件删除

def safe_rmtree(path: str) -> int:
    """递归删除目录，返回删除的文件数。

    junction / 符号链接只删除链接本身，绝不进入其目标目录。
    """
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or \
            (getattr(st, "st_file_attributes", 0) & winutil.FILE_ATTRIBUTE_REPARSE_POINT):
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.remove(path)
        return 0
    count = 0
    with os.scandir(path) as it:
        for e in it:
            try:
                est = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if getattr(est, "st_file_attributes", 0) & winutil.FILE_ATTRIBUTE_REPARSE_POINT:
                # 链接：只删链接本身
                if e.is_dir(follow_symlinks=False):
                    os.rmdir(e.path)
                else:
                    os.remove(e.path)
            elif e.is_dir(follow_symlinks=False):
                count += safe_rmtree(e.path)
            else:
                os.remove(e.path)
                count += 1
    os.rmdir(path)
    return count


def delete_fs(items, mode: str, log=None, extra_exact=()) -> list[DeleteResult]:
    """删除文件/文件夹候选。mode: "recycle"（回收站）或 "permanent"。

    按路径长度升序处理：父目录删除后，其内部的候选自动标记为“已随上级目录删除”。
    """
    results: list[DeleteResult] = []
    for item in sorted(items, key=lambda x: len(winutil.normalize_path(x.path))):
        path = item.path
        ok, why = check_fs_path(path, extra_exact)
        if not ok:
            results.append(DeleteResult(path, item.kind, False, "安全检查未通过：" + why))
            if log:
                log(f"✗ {path} — 安全检查未通过（{why}）")
            continue
        if not os.path.exists(path):
            results.append(DeleteResult(path, item.kind, True, "已随上级目录一并删除"))
            if log:
                log(f"✓ {path} — 已随上级目录一并删除")
            continue
        files, nbytes = 0, 0
        if os.path.isdir(path):
            try:
                nbytes, files = scanner.dir_size(path)
            except OSError:
                pass
        if mode == "recycle":
            ok2, msg = winutil.send_to_recycle_bin([path])
            detail = "已移入回收站（可还原）" if ok2 else msg
        else:
            try:
                if os.path.isdir(path):
                    n = safe_rmtree(path)
                    files = files or n
                    detail = f"已永久删除（{n} 个文件）"
                else:
                    os.remove(path)
                    detail = "已永久删除"
                ok2 = True
            except OSError as e:
                ok2, detail = False, f"删除失败：{e}"
        results.append(DeleteResult(path, item.kind, ok2, detail,
                                    files=files, bytes_freed=nbytes if ok2 else 0))
        if log:
            log(f"{'✓' if ok2 else '✗'} {path} — {detail}")
    return results


# ---------------------------------------------------------------- 注册表删除

def reg_delete_tree(hive, subpath: str) -> None:
    """删除整个注册表键树（自底向上）。键不存在时抛 FileNotFoundError。"""
    view = winreg.KEY_WOW64_64KEY

    def collect(path: str, acc: list) -> None:
        try:
            k = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
        except FileNotFoundError:
            return
        with k:
            i = 0
            while True:
                try:
                    child = winreg.EnumKey(k, i)
                    i += 1
                except OSError:
                    break
                child_full = path + "\\" + child
                acc.append(child_full)
                collect(child_full, acc)

    children: list[str] = []
    collect(subpath, children)
    for p in reversed(children):
        winreg.DeleteKeyEx(hive, p, view, 0)
    winreg.DeleteKeyEx(hive, subpath, view, 0)


def delete_registry(items, backup_dir: str, log=None) -> list[DeleteResult]:
    """删除注册表键候选：先导出 .reg 备份，备份失败则跳过该键。"""
    results: list[DeleteResult] = []
    for item in items:
        path = item.path
        ok, why = check_reg_path(path)
        if not ok:
            results.append(DeleteResult(path, item.kind, False, "安全检查未通过：" + why))
            if log:
                log(f"✗ {path} — 安全检查未通过（{why}）")
            continue
        hive_name, _, sub = path.partition("\\")
        dest = os.path.join(backup_dir, backup.safe_filename(sub) + ".reg")
        ok_exp, msg = backup.export_registry_key(path, dest)
        if not ok_exp:
            results.append(DeleteResult(path, item.kind, False,
                                        f"已跳过：注册表备份失败（{msg}）"))
            if log:
                log(f"✗ {path} — 已跳过：备份失败（{msg}）")
            continue
        try:
            reg_delete_tree(scanner.HIVES[hive_name.upper()], sub)
            results.append(DeleteResult(path, item.kind, True,
                                        f"已删除（备份：{os.path.basename(dest)}）"))
            if log:
                log(f"✓ {path} — 已删除（备份：{os.path.basename(dest)}）")
        except FileNotFoundError:
            results.append(DeleteResult(path, item.kind, True, "已随上级键一并删除"))
            if log:
                log(f"✓ {path} — 已随上级键一并删除")
        except OSError as e:
            results.append(DeleteResult(path, item.kind, False,
                                        f"删除失败：{e}（备份已保存：{os.path.basename(dest)}）"))
            if log:
                log(f"✗ {path} — 删除失败：{e}（备份已保存）")
    return results
