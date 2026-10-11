"""清理报告：记录每一步做了什么、删了什么、备份在哪里。"""
from __future__ import annotations

import datetime
import os
import re


def reports_root() -> str:
    home = os.path.expanduser("~")
    for candidate in (os.path.join(home, "Documents"), home):
        path = os.path.join(candidate, "CleanSweepReports")
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    return os.path.join(home, "CleanSweepReports")


def prepare_operation_dir(app_name: str) -> str:
    """创建本次操作的目录：<报告根>/<时间>_<软件名>/。"""
    safe = re.sub(r'[\\/:*?"<>|]+', "_", app_name)[:50] or "app"
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(reports_root(), f"{ts}_{safe}")
    os.makedirs(os.path.join(path, "registry-backup"), exist_ok=True)
    return path


class CleanupReport:
    def __init__(self, app):
        self.app = app
        self.created = datetime.datetime.now()
        self.op_dir = ""
        self.backup_dir = ""
        self.restore_point: tuple | None = None      # (ok|None, msg)
        self.backups: list[tuple] = []               # (reg_path, ok, info)
        self.uninstall_result = None                 # UninstallResult
        self.deleted_fs: list = []                   # DeleteResult
        self.deleted_reg: list = []                  # DeleteResult
        self.errors: list[str] = []

    def set_dirs(self, op_dir: str, backup_dir: str) -> None:
        self.op_dir, self.backup_dir = op_dir, backup_dir

    def record_restore_point(self, ok, msg: str) -> None:
        self.restore_point = (ok, msg)

    def record_backup(self, reg_path: str, ok: bool, info: str) -> None:
        self.backups.append((reg_path, ok, info))

    def record_uninstall(self, result) -> None:
        self.uninstall_result = result

    def add_fs(self, results) -> None:
        self.deleted_fs.extend(results)

    def add_reg(self, results) -> None:
        self.deleted_reg.extend(results)

    def add_error(self, text: str) -> None:
        self.errors.append(text)

    # ------------------------------------------------------------- 渲染

    def to_markdown(self) -> str:
        a = self.app
        lines = [
            "# CleanSweep 清理报告",
            "",
            f"- 时间：{self.created.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 软件：**{a.name}**" + (f" {a.version}" if a.version else ""),
            f"- 发布者：{a.publisher or '未知'}",
            f"- 安装位置：{a.install_location or '未知'}",
            f"- 残留风险等级：{a.risk_level or '未评估'}",
        ]
        for r in a.risk_reasons:
            lines.append(f"  - {r}")
        lines.append("")

        lines += ["## 执行步骤", ""]
        if self.restore_point is not None:
            ok, msg = self.restore_point
            mark = "✓" if ok else ("—" if ok is None else "✗")
            lines.append(f"- {mark} 系统还原点：{msg}")
        if self.backups:
            lines.append(f"- 注册表备份：{sum(1 for _, ok, _ in self.backups if ok)}"
                         f"/{len(self.backups)} 个键导出成功，目录 `{self.backup_dir}`")
        u = self.uninstall_result
        if u is not None:
            if u.error:
                lines.append(f"- ✗ 官方卸载程序：{u.error}")
            else:
                removed = {True: "是", False: "否", None: "未知"}[u.entry_removed]
                lines.append(f"- 官方卸载程序：`{u.command_display}`；"
                             f"等待状态 {u.wait_status}；注册表条目已移除：{removed}")
        lines.append("")

        lines += [f"## 删除的文件/文件夹（{sum(1 for x in self.deleted_fs if x.ok)}"
                  f"/{len(self.deleted_fs)} 成功）", ""]
        if self.deleted_fs:
            for x in self.deleted_fs:
                mark = "✓" if x.ok else "✗"
                extra = f"（{x.files} 个文件" + \
                        (f"，约 {x.bytes_freed / 1048576:.1f} MB" if x.bytes_freed else "") + "）" \
                        if x.files or x.bytes_freed else ""
                lines.append(f"- {mark} `{x.path}` — {x.detail}{extra}")
        else:
            lines.append("- （无）")
        lines.append("")

        lines += [f"## 删除的注册表项（{sum(1 for x in self.deleted_reg if x.ok)}"
                  f"/{len(self.deleted_reg)} 成功）", ""]
        if self.deleted_reg:
            for x in self.deleted_reg:
                mark = "✓" if x.ok else "✗"
                lines.append(f"- {mark} `{x.path}` — {x.detail}")
        else:
            lines.append("- （无）")
        lines.append("")

        if self.errors:
            lines += ["## 其他错误", ""]
            lines += [f"- {e}" for e in self.errors]
            lines.append("")

        fs_ok = [x for x in self.deleted_fs if x.ok]
        reg_ok = [x for x in self.deleted_reg if x.ok]
        freed = sum(x.bytes_freed for x in fs_ok)
        lines += ["## 汇总", "",
                  f"- 文件/文件夹：{len(fs_ok)} 项",
                  f"- 注册表项：{len(reg_ok)} 项",
                  f"- 释放空间：约 {freed / 1048576:.1f} MB"
                  "（回收站模式需清空回收站后生效）",
                  "",
                  "> 恢复方法：文件可在回收站还原；注册表可双击备份目录中的 .reg 文件导入。"]
        return "\n".join(lines)

    def save(self, op_dir: str | None = None) -> str:
        directory = op_dir or self.op_dir or prepare_operation_dir(self.app.name)
        self.op_dir = directory
        path = os.path.join(directory, "清理报告.md")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(self.to_markdown())
        return path
