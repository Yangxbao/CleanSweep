"""删除前的备份：注册表键导出（.reg，可双击还原）与系统还原点。"""
from __future__ import annotations

import datetime
import os
import re
import subprocess

from . import winutil

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _decode(data: bytes) -> str:
    """子进程输出解码：先试 UTF-8，再试 GBK（中文 Windows 控制台）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def safe_filename(text: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_")
    return (name[:80] or "key")


def export_registry_key(reg_path: str, dest_file: str) -> tuple[bool, str]:
    """reg.exe 导出单个键（含全部子键）为 .reg 文件。"""
    try:
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
    except OSError as e:
        return False, f"无法创建备份目录：{e}"
    try:
        proc = subprocess.run(["reg", "export", reg_path, dest_file, "/y"],
                              capture_output=True, timeout=60,
                              creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if proc.returncode == 0 and os.path.isfile(dest_file):
        return True, ""
    err = _decode(proc.stderr or proc.stdout or b"").strip()
    return False, err or f"reg export 返回码 {proc.returncode}"


def export_many(reg_paths, backup_dir: str, log=None) -> dict:
    """批量导出。返回 {reg_path: (备份文件或None, 是否成功, 说明)}。"""
    out = {}
    for rp in reg_paths:
        tail = rp.split("\\", 1)[1] if "\\" in rp else rp
        dest = os.path.join(backup_dir, safe_filename(tail) + ".reg")
        ok, msg = export_registry_key(rp, dest)
        out[rp] = (dest if ok else None, ok, msg)
        if log:
            log(f"{'✓' if ok else '✗'} 备份 {rp}" + ("" if ok else f"（{msg}）"))
    return out


def _wmi_dt_to_seconds(text: str) -> float:
    try:
        return datetime.datetime.strptime(text[:14], "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return 0.0


def create_restore_point(description: str) -> tuple[bool, str]:
    """创建系统还原点（需要管理员权限；系统还原被禁用时返回失败原因）。

    注意：Windows 默认限制 24 小时内只能创建一个还原点
    （SystemRestorePointCreationFrequency），窗口期内调用会成功但不会新建，
    此时会如实报告最近一次还原点的时间。
    """
    if not winutil.is_admin():
        return False, "需要管理员权限才能创建系统还原点（已改为仅备份注册表）"
    desc = re.sub(r"['\"`]", "", description)[:120]
    ps = (
        "$ErrorActionPreference='Stop';"
        f"Checkpoint-Computer -Description '{desc}' -RestorePointType 'MODIFY_SETTINGS';"
        "$p = Get-ComputerRestorePoint | Sort-Object -Property SequenceNumber | "
        "Select-Object -Last 1;"
        "Write-Output ('LATEST=' + $p.CreationTime)"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, timeout=300,
            creationflags=_CREATE_NO_WINDOW)
    except FileNotFoundError:
        return False, "未找到 PowerShell"
    except subprocess.TimeoutExpired:
        return False, "创建还原点超时"
    out = _decode(proc.stdout or b"")
    if proc.returncode != 0:
        err = _decode(proc.stderr or proc.stdout or b"").strip()
        if "服务" in err or "service" in err.lower() or "disabled" in err.lower():
            return False, ("系统还原功能可能未启用"
                           "（可在「控制面板→系统→系统保护」中为系统盘开启）：" + err[:200])
        return False, (err[:300] or f"返回码 {proc.returncode}")
    m = re.search(r"LATEST=(\d{14})", out)
    if m:
        latest = _wmi_dt_to_seconds(m.group(1))
        if latest and datetime.datetime.now().timestamp() - latest < 15 * 60:
            return True, "已创建系统还原点"
        return True, ("已请求创建还原点；最近一次还原点时间为 "
                      + m.group(1) + "（受系统 24 小时频率限制，本次可能未新建）")
    return True, out.strip()[:200] or "命令执行完成"
