"""Windows API 辅助函数（纯 ctypes 实现，无第三方依赖）。

包括：管理员检测与提权重启、ShellExecuteEx 启动并等待进程、
送入回收站（SHFileOperationW）、junction/符号链接检测、
命令行解析、路径规范化、字节格式化，
以及进程枚举（tasklist）与受控强杀（taskkill /F /T）。
"""
from __future__ import annotations

import csv
import ctypes
import io
import os
import subprocess
import sys
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetFileAttributesW.restype = wintypes.DWORD
_kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
_kernel32.GetProcessId.restype = wintypes.DWORD
_shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
_shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPCWSTR)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_DDEWAIT = 0x00000400

WAIT_OBJECT_0 = 0x00000000

MB_OK = 0x00000000
MB_OKCANCEL = 0x00000001
MB_YESNO = 0x00000004
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
MB_ICONQUESTION = 0x00000020
MB_SETFOREGROUND = 0x00010000
IDOK, IDCANCEL, IDYES, IDNO = 1, 2, 6, 7

# 强杀进程的绝对保护名单：这些镜像名永远不会被本工具 taskkill。
# 浏览器也在名单里——流氓卸载器常弹网页，但杀浏览器会丢用户的标签页。
PROTECTED_PROCESS_NAMES = {
    # 系统核心
    "system", "idle", "registry",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "dwm.exe", "win32k.exe", "lsaiso.exe",
    # 桌面 / 外壳
    "explorer.exe", "taskmgr.exe", "dwm.exe", "sihost.exe", "ctfmon.exe",
    "runtimebroker.exe", "searchapp.exe", "searchhost.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "applicationframehost.exe", "taskhostw.exe", "dllhost.exe",
    "fontdrvhost.exe", "audiodg.exe", "conhost.exe", "openconsole.exe",
    "windowsterminal.exe",
    # 系统服务 / 安全
    "spoolsv.exe", "msmpeng.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe", "wudfhost.exe", "wmiapsrv.exe",
    # 开发/脚本宿主（可能就是本工具自己）
    "python.exe", "pythonw.exe", "py.exe", "python3.11.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe",
    "conhost.exe",
    # 系统工具：被误杀会造成不可预期的破坏
    "rundll32.exe", "regsvr32.exe", "msiexec.exe", "tasklist.exe",
    "taskkill.exe", "reg.exe",
    # 浏览器
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
    "360se.exe", "360chrome.exe", "qqbrowser.exe", "baidubrowser.exe",
    "sogouexplorer.exe", "ucbrowser.exe", "2345explorer.exe", "maxthon.exe",
    # 本工具自身（打包后的名字）
    "cleansweep.exe",
}


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _decode(data: bytes) -> str:
    """控制台输出解码：先试 UTF-8，再试 GBK（中文 Windows）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def is_admin() -> bool:
    """当前进程是否具有管理员权限。"""
    try:
        return bool(_shell32.IsUserAnAdmin())
    except Exception:
        return False


def message_box(text: str, title: str = "CleanSweep", flags: int = MB_OK) -> int:
    """不依赖 tkinter 的系统消息框，返回被点击按钮的 ID。"""
    return int(_user32.MessageBoxW(None, text, title, flags | MB_SETFOREGROUND))


def relaunch_as_admin(extra_args: str = "") -> bool:
    """以管理员身份重新启动自身（触发 UAC）。成功发起新进程返回 True。"""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = extra_args.strip()
    else:
        base = os.path.dirname(sys.executable)
        pythonw = os.path.join(base, "pythonw.exe")
        exe = pythonw if os.path.exists(pythonw) else sys.executable
        script = os.path.abspath(sys.argv[0])
        params = (f'"{script}" ' + extra_args).strip()
    try:
        shell_execute(exe, params, verb="runas")
        return True
    except OSError:
        return False


def shell_execute(file: str, params: str = "", verb: str = "open",
                  work_dir: str | None = None, show: int = 1):
    """用 ShellExecuteExW 启动程序。返回进程句柄（可能为 None），调用方负责 close_handle。

    注意：若目标需要 UAC 提升，本调用会一直阻塞到用户响应 UAC——
    请不要在需要保持响应的线程里直接调用（uninstaller.py 用独立线程启动）。
    """
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_DDEWAIT
    sei.lpVerb = verb
    sei.lpFile = file
    sei.lpParameters = params or None
    sei.lpDirectory = work_dir
    sei.nShow = show
    if not _shell32.ShellExecuteExW(ctypes.byref(sei)):
        raise ctypes.WinError(ctypes.get_last_error())
    return sei.hProcess or None


def close_handle(handle) -> None:
    if handle:
        _kernel32.CloseHandle(handle)


def process_exited(handle) -> bool:
    """进程是否已退出（不等待、不关闭句柄）。"""
    return _kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0


def get_process_id(handle) -> int:
    """从进程句柄取 PID（失败返回 0）。"""
    try:
        return int(_kernel32.GetProcessId(handle) or 0)
    except Exception:
        return 0


def wait_process(handle, timeout_ms: int, cancel_check=None,
                 on_tick=None, tick_seconds: int = 10) -> str:
    """等待进程句柄结束，返回 "exited" / "timeout" / "cancelled"。

    cancel_check: 每 200ms 轮询一次的取消判定（返回 True 即停止等待）。
    on_tick(elapsed_seconds): 每 tick_seconds 秒回调一次，供界面输出心跳日志。
    无论结果如何都会关闭句柄。
    """
    try:
        waited = 0.0
        step = 0.2
        last_tick = 0.0
        while waited * 1000 < timeout_ms:
            if _kernel32.WaitForSingleObject(handle, int(step * 1000)) == WAIT_OBJECT_0:
                return "exited"
            waited += step
            if cancel_check is not None and cancel_check():
                return "cancelled"
            if on_tick is not None and (waited - last_tick) >= tick_seconds:
                last_tick = waited
                try:
                    on_tick(int(waited))
                except Exception:  # noqa: BLE001
                    pass
        return "timeout"
    finally:
        close_handle(handle)


def send_to_recycle_bin(paths) -> tuple[bool, str]:
    """把若干文件/文件夹送入回收站（FOF_ALLOWUNDO，可在回收站还原）。

    路径必须是绝对路径。返回 (是否成功, 说明)。
    """
    pfrom = "".join(os.path.normpath(os.path.abspath(p)) + "\0" for p in paths) + "\0"
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = 3  # FO_DELETE
    op.pFrom = pfrom
    op.pTo = None
    # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400
    result = int(_shell32.SHFileOperationW(ctypes.byref(op)))
    if result == 0 and not op.fAnyOperationsAborted:
        return True, ""
    if op.fAnyOperationsAborted:
        return False, "操作被用户中止"
    return False, f"回收站删除失败（错误码 {result}）"


def is_reparse_point(path: str) -> bool:
    """路径是否为 junction / 符号链接等重解析点（删除时不能进入其目标）。"""
    attrs = _kernel32.GetFileAttributesW(str(path))
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def cmdline_to_argv(cmdline: str) -> list[str]:
    """按 Windows 规则把命令行字符串切分为参数列表（正确处理引号）。"""
    n = ctypes.c_int(0)
    argv = _shell32.CommandLineToArgvW(cmdline, ctypes.byref(n))
    if not argv:
        return [cmdline]
    try:
        return [argv[i] for i in range(n.value)]
    finally:
        _kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def expand_env(text: str) -> str:
    """展开 %VAR% 环境变量。"""
    return os.path.expandvars(text or "")


def normalize_path(path: str) -> str:
    """用于比较的规范化路径：绝对路径、展开变量、小写。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expandvars(str(path)))))


def human_size(num) -> str:
    """字节数 -> 人类可读大小。"""
    if num is None:
        return "未知"
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def open_in_explorer(path: str) -> None:
    target = path if os.path.isdir(path) else os.path.dirname(path)
    if target:
        os.startfile(target)


# ================================================================ 进程管理

def is_protected_process(image_name: str) -> bool:
    """镜像名是否在强杀保护名单（系统进程/浏览器/本工具自身）。"""
    return (image_name or "").strip().lower() in PROTECTED_PROCESS_NAMES


def list_running_processes() -> list[dict]:
    """tasklist 枚举当前进程：[{"name": 小写镜像名, "pid": int, "mem": str}]。"""
    try:
        proc = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                              capture_output=True, timeout=30,
                              creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = _decode(proc.stdout or b"")
    rows: list[dict] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        pid_text = row[1].strip()
        if not pid_text.isdigit():
            continue
        rows.append({"name": row[0].strip().strip('"').lower(),
                     "pid": int(pid_text),
                     "mem": row[-1].strip() if row else ""})
    return rows


def find_processes(image_names) -> list[dict]:
    """在运行中的进程里查找给定镜像名（已自动排除保护名单与自身）。"""
    wanted = {n.strip().lower() for n in image_names if n}
    wanted = {n for n in wanted if n and not is_protected_process(n)}
    if not wanted:
        return []
    return [p for p in list_running_processes()
            if p["name"] in wanted and p["pid"] != os.getpid()]


def kill_process_tree(pid: int) -> tuple[bool, str]:
    """taskkill /F /T /PID <pid>：强制结束该进程及其子进程。"""
    try:
        proc = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                              capture_output=True, timeout=30,
                              creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if proc.returncode == 0:
        return True, ""
    msg = _decode(proc.stderr or proc.stdout or b"").strip()
    return False, msg or f"taskkill 返回码 {proc.returncode}"


def kill_processes(image_names, log=None) -> list[dict]:
    """按镜像名查找并强杀（taskkill /F /T /PID，逐个进程精确执行）。

    安全约束：
    - 名单先经保护名单过滤，系统进程/浏览器/本工具自身永远不会被杀；
    - 只杀“当前正在运行且镜像名精确匹配”的进程；
    - 名单过滤后为空则什么都不做（调用方应提示用户手动处理）。
    返回被成功结束的进程记录列表。
    """
    matched = find_processes(image_names)
    killed: list[dict] = []
    for p in matched:
        ok, msg = kill_process_tree(p["pid"])
        if log:
            log(f"  {'✓' if ok else '✗'} taskkill /F /T {p['name']} (PID {p['pid']})"
                + ("" if ok else f" — {msg}"))
        if ok:
            killed.append(p)
    return killed
