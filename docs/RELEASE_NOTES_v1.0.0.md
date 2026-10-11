# CleanSweep v1.0.0

## 简体中文

### 🎉 首个公开版本

CleanSweep 彻底卸载助手：列出电脑上所有已安装软件，一键调用官方卸载程序，自动扫描并清理残留文件与注册表项，生成清理报告。运行时零第三方依赖，单个 exe 免安装。

### 功能亮点

- 通过注册表 Uninstall 键（HKLM/HKCU × 64/32 位视图）枚举全部已安装软件，支持按大小、安装日期、风险等级排序与实时搜索
- 先运行软件官方卸载程序，再自动扫描残留（`Program Files`、`AppData`、`ProgramData`、开始菜单、`HKLM\SOFTWARE`、`HKCU\Software` 等）
- 每个软件显示残留风险等级（高/中/低），高风险卸载前重点提醒
- 删除前逐项人工确认；文件默认进回收站，注册表删除前自动导出 `.reg` 备份，可选创建系统还原点
- 每次操作生成 Markdown 清理报告

### 🐞 重点修复：卸载流氓软件时程序卡死

在卸载不退出的"流氓"卸载器（如哔哩哔哩 PC 客户端）时，此前版本会彻底卡死。本版本做了系统性修复：

1. **日志阻塞死锁**：日志由逐条阻塞主线程改为线程安全缓冲 + 每 0.3 秒批量刷新，工作线程永不因日志阻塞；
2. **静默长等待**：等待官方卸载程序改为 60 秒硬超时，期间每 10 秒输出心跳日志；
3. **一键强杀**：新增「强制结束卸载进程并继续」按钮，超时也会自动 `taskkill /F /T`——强杀前先用 `tasklist` 精确匹配由卸载命令与安装目录推导出的进程名，系统进程/浏览器/本工具自身在保护名单中永不被杀，推导不出则跳过强杀；
4. **MSI 特殊保护**：强杀 `msiexec` 需二次确认，避免损坏 Windows Installer 数据库；
5. **启动与扫描**：`ShellExecuteEx` 移入独立线程；残留扫描支持随时取消；所有对话框强制前置显示。

完整根因分析见 [README](README.md#卸载流氓软件卡死问题根因与修复-v100-重点)。

### 下载与校验

- **CleanSweep.exe**：Windows 10/11 x64，单文件免安装，双击运行（UAC 管理员权限）
- SHA256：`169ebf7e2a02cfc98a17639fbb4354bc15158ceb1c453e51303f3ef6c894bd66`

```bat
certutil -hashfile CleanSweep.exe SHA256
```

---

## English

### 🎉 First public release

CleanSweep lists every installed program, runs each program's official uninstaller, then automatically scans for and removes leftover files and registry entries, and generates a cleanup report. Zero third-party runtime dependencies; ships as a single portable exe.

### Highlights

- Enumerates all installed programs from the registry Uninstall keys (HKLM/HKCU × 64/32-bit views); sortable by size, install date and risk level, with live search
- Runs the official uninstaller first, then scans for leftovers (`Program Files`, `AppData`, `ProgramData`, Start Menu, `HKLM\SOFTWARE`, `HKCU\Software`…)
- Per-program residue risk level (High/Medium/Low) with a red warning before uninstalling high-risk apps
- Item-by-item confirmation before deletion; files go to the Recycle Bin by default; registry keys are exported as `.reg` backups first; optional System Restore point
- Markdown cleanup report for every operation

### 🐞 Focus fix: UI freeze when uninstalling rogue software

The previous internal builds froze completely when an uninstaller refused to exit (e.g. the Bilibili desktop client). This release fixes it systematically:

1. **Logging deadlock**: logs moved from per-line blocking dispatch to a thread-safe buffer flushed in batches every 0.3 s — worker threads never block on logging;
2. **Silent long waits**: the uninstaller wait now has a hard 60-second timeout with heartbeat logs every 10 seconds;
3. **One-click force-kill**: a new "Force-kill uninstaller and continue" button; the timeout also auto-kills via `taskkill /F /T` — but only after matching process names derived from the uninstall command and install folder against `tasklist`; system processes, browsers and CleanSweep itself are on a protected list; if no name can be derived, force-kill is skipped;
4. **MSI protection**: killing `msiexec` requires an extra confirmation to avoid corrupting the Windows Installer database;
5. **Launch & scan**: `ShellExecuteEx` runs on a dedicated thread; leftover scans are cancellable; every dialog is force-raised so nothing hides behind the rogue uninstaller's windows.

Full root-cause analysis: [README](README.en.md#the-freeze-bug-root-cause-and-fix-v100-highlight).

### Download & verification

- **CleanSweep.exe**: Windows 10/11 x64, single portable file, runs with admin elevation (UAC)
- SHA256: `169ebf7e2a02cfc98a17639fbb4354bc15158ceb1c453e51303f3ef6c894bd66`

```bat
certutil -hashfile CleanSweep.exe SHA256
```
