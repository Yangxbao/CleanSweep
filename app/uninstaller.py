"""调用软件自带卸载程序（UninstallString）并等待其完成。

防卡死设计（针对流氓软件卸载器）：
- 等待卸载进程有硬超时（默认 60 秒），全程每 10 秒输出一次心跳日志；
- 启动卸载程序放在独立线程（ShellExecuteEx 可能在 UAC 上长时间阻塞，
  不能让它卡住流程线程）；
- 超时或用户点「强制结束」时：先用卸载命令 + 安装目录推导进程名，
  经 tasklist 精确匹配“正在运行”的进程后 taskkill /F /T 逐个结束；
- 推导不出进程名时不强杀，只提示用户手动处理；
- msiexec 例外：强杀可能损坏 Windows Installer 数据库，超时不自动强杀，
  由用户在界面二次确认后仅结束该 MSI 事务的 PID。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import scanner, winutil

_MSI_GUID_RE = re.compile(
    r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")

# 等待卸载进程的心跳日志间隔（秒）
TICK_SECONDS = 10
# 启动 ShellExecute 的最长等待（UAC 无响应等情况）
LAUNCH_TIMEOUT_SECONDS = 30


@dataclass
class UninstallResult:
    command_display: str = ""
    launched: bool = False
    wait_status: str = ""          # exited / timeout / cancelled / forced / skipped / unknown
    entry_removed: bool | None = None
    exit_code: int | None = None
    error: str = ""
    user_continued: bool = False   # 用户点了「继续」或「强制结束」，跳过“未完成”询问
    forced: bool = False           # 是否执行过强杀
    force_killed: list = field(default_factory=list)   # 被强杀的进程描述
    pid: int | None = None
    process_names: list = field(default_factory=list)  # 推导出的相关进程名


def parse_command(app, silent: bool):
    """解析卸载命令。返回 (exe, args, 显示用完整命令) 或 None。"""
    raw = ""
    if silent and app.quiet_uninstall_string.strip():
        raw = app.quiet_uninstall_string.strip()
    elif app.uninstall_string.strip():
        raw = app.uninstall_string.strip()
    if not raw:
        return None
    raw = winutil.expand_env(raw).strip()

    # MSI：MsiExec.exe /X{GUID}（/I 也改为 /X 强制卸载）
    guid = _MSI_GUID_RE.search(raw)
    if "msiexec" in raw.lower() and guid:
        args = ["/X" + guid.group(0)]
        if silent:
            args += ["/qn", "/norestart"]
        display = "MsiExec.exe " + " ".join(args)
        return "MsiExec.exe", " ".join(args), display

    argv = winutil.cmdline_to_argv(raw)
    if not argv:
        return None
    exe = argv[0]
    rest = subprocess.list2cmdline(argv[1:])
    # 勾选静默时，为常见安装器家族追加公认参数（不识别则原样运行，无副作用）
    if silent and not (silent and app.quiet_uninstall_string.strip()):
        base = os.path.basename(exe).lower()
        if re.fullmatch(r"unins\d{3}\.exe", base):          # Inno Setup
            rest = (rest + " /VERYSILENT /SUPPRESSMSGBOXES /NORESTART").strip()
        elif base in ("uninstall.exe", "uninst.exe"):        # NSIS
            rest = (rest + " /S").strip()
    display = exe + ((" " + rest) if rest else "")
    return exe, rest, display


def _install_shared_with_others(install_location: str, other_apps) -> bool:
    """安装目录是否与其他软件共用（共用时不能把目录里的 exe 都当成本软件进程）。"""
    nil = winutil.normalize_path(install_location)
    for a in other_apps:
        if not a.install_location:
            continue
        nl = winutil.normalize_path(a.install_location)
        if nl == nil or nl.startswith(nil + os.sep) or nil.startswith(nl + os.sep):
            return True
    return False


def derive_process_names(app, other_apps=()) -> set[str]:
    """推导与当前软件相关的进程镜像名（强杀候选）。

    来源：卸载命令里的 exe + 安装目录顶层的 .exe（目录与其他软件共用时跳过后者）。
    结果已排除保护名单（系统进程/浏览器/本工具自身）。
    other_apps 必须不含 app 自身。推导不出时返回空集——调用方应跳过强杀。
    """
    names: set[str] = set()
    for s in (app.uninstall_string, app.quiet_uninstall_string):
        s = (s or "").strip()
        if not s:
            continue
        try:
            argv = winutil.cmdline_to_argv(winutil.expand_env(s))
            base = os.path.basename(argv[0]).strip().lower()
            if base.endswith(".exe") and base != "msiexec.exe":
                names.add(base)
        except Exception:  # noqa: BLE001
            continue
    il = app.install_location
    if il and os.path.isdir(il) and not _install_shared_with_others(il, other_apps):
        try:
            with os.scandir(il) as it:
                for e in it:
                    try:
                        if e.is_file() and e.name.lower().endswith(".exe"):
                            names.add(e.name.lower())
                    except OSError:
                        continue
        except OSError:
            pass
    return {n for n in names if not winutil.is_protected_process(n)}


def force_kill(pid: int | None, names, progress) -> list[str]:
    """强杀当前软件的卸载相关进程：先按 PID 结束进程树，再按镜像名兜底。

    返回被成功结束的进程描述（如 "uninst.exe (PID 1234)"）。
    """
    killed: list[str] = []
    if pid:
        ok, msg = winutil.kill_process_tree(pid)
        if progress:
            progress(f"  {'✓' if ok else '·'} taskkill /F /T /PID {pid}"
                     + ("" if ok else f" — {msg}"))
        if ok:
            killed.append(f"PID {pid}")
    for p in winutil.kill_processes(names, log=progress):
        killed.append(f"{p['name']} (PID {p['pid']})")
    return killed


def _wait_popen(proc: subprocess.Popen, timeout_s: float,
                cancel_event, wait_event, force_event, progress, what: str) -> str:
    """等待 Popen 进程，带心跳日志；返回 exited / cancelled / timeout。"""
    waited = 0.0
    last_tick = 0.0
    while waited < timeout_s:
        if proc.poll() is not None:
            return "exited"
        if cancel_event.is_set() or wait_event.is_set():
            return "cancelled"
        if force_event is not None and force_event.is_set():
            return "cancelled"  # 由调用方区分是哪种事件
        if waited - last_tick >= TICK_SECONDS:
            last_tick = waited
            progress(f"仍在等待{what}完成…（{int(waited)}s/{int(timeout_s)}s，"
                     "可点「强制结束」或「继续」）")
        time.sleep(0.5)
        waited += 0.5
    return "timeout"


def _poll_entry(app, progress, wait_event, cancel_event, force_event,
                seconds: float, on_force) -> tuple[bool, bool]:
    """轮询注册表卸载条目是否被移除。返回 (entry_removed, user_continued)。"""
    progress("正在等待卸载程序清理注册表条目…")
    deadline = time.time() + seconds
    last_tick = time.time()
    while True:
        if not scanner.entry_exists(app):
            progress("✓ 卸载条目已从注册表移除")
            return True, False
        if cancel_event.is_set():
            return False, False
        if wait_event.is_set():
            progress("已收到「继续」指令，进入下一步")
            return False, True
        if force_event is not None and force_event.is_set():
            progress("收到强制结束指令")
            on_force()
            return False, True
        remaining = deadline - time.time()
        if remaining <= 0:
            return False, False
        if time.time() - last_tick >= TICK_SECONDS:
            last_tick = time.time()
            progress(f"仍在等待注册表条目被移除…（剩余 {int(remaining)}s，"
                     "可点「继续」跳过或「强制结束」）")
        time.sleep(1)


def run(app, silent: bool, progress, wait_event, cancel_event, force_event=None,
        wait_seconds: float = 60, poll_seconds: float = 120,
        other_apps=()) -> UninstallResult:
    """运行官方卸载程序：等待（带超时+心跳）→（超时自动强杀）→ 轮询注册表条目。

    progress: 日志回调（非阻塞）；wait_event: 用户点「继续」；
    cancel_event: 取消整个流程；force_event: 用户点「强制结束卸载进程并继续」。
    """
    res = UninstallResult()
    cmd = parse_command(app, silent)
    if not cmd:
        res.error = "没有可用的卸载命令"
        return res
    res.command_display = cmd[2]

    if not scanner.entry_exists(app):
        res.entry_removed = True
        res.wait_status = "skipped"
        progress("注册表中已没有该软件的卸载条目（可能此前已卸载）")
        return res

    is_msi = os.path.splitext(cmd[0].lower())[0] == "msiexec"

    names = derive_process_names(app, other_apps)
    res.process_names = sorted(names)
    if names:
        progress("已识别卸载相关进程名（仅用于超时/手动强杀）："
                 + ", ".join(res.process_names))
    else:
        progress("未能识别卸载相关进程名：如卸载程序卡住，"
                 "请手动关闭其窗口后点「卸载完成，继续」")

    user_continued = False

    def _on_force():
        res.forced = True
        progress("正在强制结束卸载相关进程…")
        res.force_killed = force_kill(res.pid, () if is_msi else names, progress)
        if not res.force_killed:
            progress("没有匹配到仍在运行的卸载进程（可能已自行退出）")

    if is_msi:
        argv = ["msiexec.exe"] + cmd[1].split()
        try:
            proc = subprocess.Popen(argv)
        except OSError as e:
            res.error = f"无法启动 Windows Installer：{e}"
            return res
        res.launched = True
        res.pid = proc.pid
        progress(f"Windows Installer 卸载进行中（PID {proc.pid}），请在弹窗中配合完成…")
        progress("提示：强杀 msiexec 可能损坏 Windows Installer 数据库，"
                 "超时后不会自动强杀；确需强杀请点「强制结束卸载进程并继续」（会再次确认）。")
        status = _wait_popen(proc, wait_seconds, cancel_event, wait_event,
                             force_event, progress, "Windows Installer 卸载")
        if status == "exited":
            res.wait_status = "exited"
            res.exit_code = proc.returncode
            if res.exit_code == 1602:
                progress("Windows Installer：用户取消了卸载")
            elif res.exit_code == 1605:
                progress("Windows Installer：该产品未安装")
            elif res.exit_code in (0, 3010):
                progress("Windows Installer 卸载完成"
                         + ("（需要重启后完全生效）" if res.exit_code == 3010 else ""))
            else:
                progress(f"Windows Installer 退出码：{res.exit_code}")
        elif status == "timeout":
            res.wait_status = "timeout"
            progress(f"等待超时（{int(wait_seconds)}s）。为避免损坏 Windows Installer "
                     "未自动强杀：可继续等待、点「继续」，或点「强制结束卸载进程并继续」。")
        else:  # cancelled（取消 / 继续 / 强制 三种事件之一）
            if force_event is not None and force_event.is_set():
                res.wait_status = "forced"
                _on_force()
                user_continued = True
            elif wait_event.is_set():
                res.wait_status = "cancelled"
                user_continued = True
                progress("已收到「继续」指令，提前结束等待")
            else:
                res.wait_status = "cancelled"
                progress("已停止等待（Windows Installer 可能仍在运行）")
    else:
        # 在独立线程里启动卸载程序：ShellExecuteEx 可能因 UAC 长时间阻塞，
        # 绝不能让它卡住流程线程。
        launch_box: dict = {}

        def _launch():
            try:
                launch_box["handle"] = winutil.shell_execute(cmd[0], cmd[1], verb="open")
            except OSError as e:
                if getattr(e, "winerror", None) in (5, 740):
                    try:
                        progress("需要管理员权限，正在请求提升（UAC）…")
                        launch_box["handle"] = winutil.shell_execute(cmd[0], cmd[1],
                                                                     verb="runas")
                    except OSError as e2:
                        launch_box["error"] = e2
                else:
                    launch_box["error"] = e

        launcher = threading.Thread(target=_launch, daemon=True)
        launcher.start()
        progress(f"正在启动卸载程序：{res.command_display}")
        progress("（若弹出 UAC 确认框，请在安全桌面上响应）")

        launch_deadline = time.time() + LAUNCH_TIMEOUT_SECONDS
        while launcher.is_alive() and time.time() < launch_deadline:
            if cancel_event.is_set():
                progress("已取消（若 UAC 确认框仍停留，请勿响应它）")
                res.wait_status = "cancelled"
                return res
            time.sleep(0.25)
        if launcher.is_alive():
            res.error = (f"启动卸载程序超时（{int(LAUNCH_TIMEOUT_SECONDS)}s），"
                         "可能停留在 UAC 确认框，请响应或取消后重试")
            return res
        if "error" in launch_box:
            res.error = f"无法启动卸载程序：{launch_box['error']}"
            return res

        res.launched = True
        handle = launch_box.get("handle")
        if handle:
            res.pid = winutil.get_process_id(handle) or None
            status = winutil.wait_process(
                handle, wait_seconds * 1000,
                cancel_check=lambda: (cancel_event.is_set() or wait_event.is_set()
                                      or (force_event is not None and force_event.is_set())),
                on_tick=lambda s: progress(
                    f"仍在等待卸载程序退出…（{s}s/{int(wait_seconds)}s，"
                    "卡住可点「强制结束卸载进程并继续」）"),
                tick_seconds=TICK_SECONDS)
            if status == "cancelled":
                if force_event is not None and force_event.is_set():
                    status = "forced"
                elif wait_event.is_set():
                    status = "continued"
        else:
            status = "unknown"
            progress("卸载程序已启动（无进程句柄），等待 20 秒…")
            end = time.time() + 20
            while time.time() < end:
                if force_event is not None and force_event.is_set():
                    status = "forced"
                    break
                if cancel_event.is_set() or wait_event.is_set():
                    status = "continued"
                    break
                time.sleep(0.5)

        if status == "exited":
            res.wait_status = "exited"
            progress("卸载程序已退出")
        elif status == "timeout":
            res.wait_status = "timeout"
            progress(f"等待超时（{int(wait_seconds)}s），自动结束卸载相关进程…")
            res.forced = True
            user_continued = True
            res.force_killed = force_kill(res.pid, names, progress)
            if not res.force_killed:
                progress("未找到匹配的运行中进程，跳过强杀"
                         "（可手动关闭卸载窗口后点「继续」）")
        elif status == "forced":
            res.wait_status = "forced"
            _on_force()
            user_continued = True
        elif status == "continued":
            res.wait_status = "cancelled"
            user_continued = True
            progress("已收到「继续」指令，提前结束等待")
        else:  # cancelled
            res.wait_status = "cancelled"
            progress("已停止等待（卸载程序可能仍在运行）")

    # 轮询注册表条目（强制结束/用户继续后只短暂确认，否则完整等待）
    if cancel_event.is_set() and not user_continued:
        res.entry_removed = False
        res.user_continued = False
        return res

    def _on_force_poll():
        _on_force()

    removed, cont = _poll_entry(
        app, progress, wait_event, cancel_event, force_event,
        seconds=12 if (res.forced or user_continued) else poll_seconds,
        on_force=_on_force_poll)
    if removed:
        res.entry_removed = True
    else:
        res.entry_removed = False
        if cont:
            user_continued = True
    if res.forced:
        user_continued = True
    res.user_continued = user_continued
    return res
