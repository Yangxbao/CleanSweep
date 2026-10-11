"""CleanSweep 图形界面（tkinter / ttk，无第三方依赖）。

防卡死设计：
- 卸载流程的全部耗时操作（备份/还原点/卸载/扫描/删除/报告）都在工作线程；
- 日志走线程安全缓冲，主线程每 0.3 秒批量刷新一次，工作线程绝不因日志阻塞；
- 等待官方卸载程序有硬超时与「强制结束卸载进程并继续」按钮；
- 残留扫描支持随时取消；任何时刻主线程只做界面刷新与模态对话框。
"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from . import backup, cleaner, leftovers, reporter, scanner, uninstaller, winutil
from .matcher import WEAK

FONT = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
FONT_MONO = ("Consolas", 9)

COLUMNS = ("name", "publisher", "location", "size", "date", "risk")
HEADINGS = {
    "name": "软件名称", "publisher": "发布者", "location": "安装位置",
    "size": "大小", "date": "安装日期", "risk": "残留风险",
}
WIDTHS = {"name": 250, "publisher": 140, "location": 280, "size": 90, "date": 95, "risk": 70}
ANCHORS = {"name": "w", "publisher": "w", "location": "w",
           "size": "e", "date": "center", "risk": "center"}

RISK_TAG = {"高": "risk_high", "中": "risk_mid", "低": "risk_low"}


def _sort_key(col: str):
    if col == "name":
        return lambda a: a.name.lower()
    if col == "publisher":
        return lambda a: (a.publisher or "").lower()
    if col == "location":
        return lambda a: (a.install_location or "").lower()
    if col == "size":
        return lambda a: (a.size_bytes_effective if a.size_bytes_effective is not None else -1)
    if col == "date":
        return lambda a: a.install_date or "00000000"
    if col == "risk":
        return lambda a: {"高": 0, "中": 1, "低": 2}.get(a.risk_level, 3)
    return lambda a: a.name.lower()


def _raise_window(win):
    """把窗口提到最前并短暂置顶——避免被流氓卸载器的常驻窗口挡住。"""
    try:
        win.deiconify()
        win.lift()
        win.focus_force()
        win.attributes("-topmost", True)
        win.after(600, lambda: _safe_topmost_off(win))
    except tk.TclError:
        pass


def _safe_topmost_off(win):
    try:
        win.attributes("-topmost", False)
    except tk.TclError:
        pass


# ================================================================ 后台任务桥

class AsyncRunner:
    """工作线程 -> 主线程的回调桥。所有控件更新都经主线程执行。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        root.after(80, self._pump)

    def submit(self, fn, on_done=None, on_error=None) -> None:
        def work():
            try:
                res = fn()
                self.q.put(("cb", on_done, res, None))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.q.put(("cb", on_error, None, e))
        threading.Thread(target=work, daemon=True).start()

    def call_in_main(self, fn, *args):
        """工作线程调用：在主线程执行 fn(*args) 并等待其返回。

        仅用于模态决策（对话框）等少量场景；高频日志走 UninstallFlow 的
        缓冲批量刷新，绝不走这里。
        """
        ev = threading.Event()
        box: dict = {}
        self.q.put(("call", fn, args, ev, box))
        ev.wait()
        if "err" in box:
            raise box["err"]
        return box.get("res")

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "cb":
                    _, cb, res, err = item
                    if cb is None:
                        continue
                    try:
                        cb(err) if err is not None else cb(res)
                    except tk.TclError:
                        pass
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
                else:
                    _, fn, args, ev, box = item
                    try:
                        box["res"] = fn(*args)
                    except Exception as e:  # noqa: BLE001
                        box["err"] = e
                    finally:
                        ev.set()
        except queue.Empty:
            pass
        except RuntimeError:
            return
        try:
            self.root.after(80, self._pump)
        except tk.TclError:
            pass


# ================================================================ 主窗口

class MainWindow(tk.Tk):
    def __init__(self, smoke: bool = False):
        super().__init__()
        self.title("CleanSweep 彻底卸载助手")
        self.geometry("1180x720")
        self.minsize(920, 560)
        self.option_add("*Font", FONT)

        self.smoke = smoke
        self.apps: list = []
        self.app2iid: dict = {}
        self.sort_col = "name"
        self.sort_desc = False
        self.busy = False
        self._size_gen = 0

        self.runner = AsyncRunner(self)
        self._build_ui()
        self._update_admin_ui()

        if smoke:
            self.withdraw()
            self.after(6000, self._smoke_done)
        self.after(200, self.refresh)

    def _smoke_done(self):
        risks = [a.risk_level for a in self.apps]
        print(f"[smoke] apps={len(self.apps)} risks={ {r: risks.count(r) for r in set(risks)} }")
        try:
            from .cleaner_window import FileCleanerWindow
            w = FileCleanerWindow(self, show=False)
            self.update_idletasks()
            print(f"[smoke] file-cleaner window OK (folders={len(w.folder_vars)})")
            w.destroy()
        except Exception as e:  # noqa: BLE001
            print(f"[smoke] file-cleaner window FAILED: {e}")
        self.destroy()

    # ------------------------------------------------------------- UI 构建

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24, font=FONT)
        style.configure("Treeview.Heading", font=FONT_BOLD)

        top = ttk.Frame(self, padding=(8, 6))
        top.pack(fill="x")
        self.btn_refresh = ttk.Button(top, text="刷新列表", command=self.refresh)
        self.btn_refresh.pack(side="left")
        ttk.Label(top, text="  搜索：").pack(side="left")
        self.search_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.search_var, width=28)
        entry.pack(side="left")
        entry.bind("<KeyRelease>", lambda _e: self._populate())
        self.admin_label = ttk.Label(top, text="")
        self.admin_label.pack(side="right", padx=4)
        self.btn_elevate = ttk.Button(top, text="以管理员身份重启", command=self._try_elevate)
        self.btn_elevate.pack(side="right")

        tree_frame = ttk.Frame(self, padding=(8, 0))
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=COLUMNS, show="headings",
                                 selectmode="browse")
        for col in COLUMNS:
            self.tree.heading(col, text=HEADINGS[col],
                              command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=WIDTHS[col], anchor=ANCHORS[col],
                             stretch=(col in ("name", "location")))
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.tag_configure("risk_high", background="#fde8e8")
        self.tree.tag_configure("risk_mid", background="#fff5e0")
        self.tree.tag_configure("risk_low", background="#eef7ee")
        self.tree.tag_configure("weak", foreground="#888888")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail = ttk.LabelFrame(self, text="详情", padding=(6, 2))
        detail.pack(fill="x", padx=8, pady=(4, 0))
        self.detail = tk.Text(detail, height=7, wrap="word", font=FONT,
                              state="disabled", relief="flat", background="#fafafa")
        self.detail.pack(fill="x")

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        self.btn_uninstall = ttk.Button(bottom, text="彻底卸载选中软件",
                                        command=self.do_uninstall)
        self.btn_uninstall.pack(side="left")
        self.btn_scan = ttk.Button(bottom, text="仅扫描残留（不卸载）", command=self.do_scan_only)
        self.btn_scan.pack(side="left", padx=6)
        self.btn_files = ttk.Button(bottom, text="资料清理（图片/文档/PPT…）",
                                    command=self.open_file_cleaner)
        self.btn_files.pack(side="left", padx=6)
        ttk.Button(bottom, text="打开报告文件夹", command=self._open_reports).pack(side="left")
        self.status = ttk.Label(self, text="正在读取注册表…", padding=(8, 2), anchor="w")
        self.status.pack(fill="x")

    def _update_admin_ui(self):
        if winutil.is_admin():
            self.admin_label.config(text="管理员权限：已启用 ✓", foreground="#1b7e2c")
            self.btn_elevate.config(state="disabled")
        else:
            self.admin_label.config(text="管理员权限：未启用（部分清理不可用）",
                                    foreground="#c62828")
            self.btn_elevate.config(state="normal")

    def _try_elevate(self):
        if winutil.relaunch_as_admin():
            self.destroy()
        else:
            messagebox.showwarning("未提升", "已取消 UAC 或提升失败，继续以当前权限运行。",
                                   parent=self)

    def _open_reports(self):
        winutil.open_in_explorer(reporter.reports_root())

    # ------------------------------------------------------------- 列表

    def refresh(self):
        if self.busy:
            return
        self._size_gen += 1
        self.status.config(text="正在读取注册表…")
        self.runner.submit(scanner.enumerate_apps, self._on_loaded, self._on_error)

    def _on_error(self, err):
        self.status.config(text=f"读取失败：{err}")

    def _on_loaded(self, apps):
        self.apps = apps
        self._populate()
        n = len(apps)
        self.status.config(text=f"共 {n} 个软件 · 点击列标题排序 · 正在评估残留风险…")
        self.runner.submit(self._risk_job, self._on_risks, self._on_error)

    def _risk_job(self):
        ctx = leftovers.ScanContext()
        return [(a,) + leftovers.assess_risk(a, self.apps, ctx) for a in self.apps]

    def _on_risks(self, results):
        for app, level, reasons in results:
            app.risk_level, app.risk_reasons = level, reasons
            self._refresh_row(app)
        risks = [a.risk_level for a in self.apps]
        self.status.config(
            text=f"共 {len(self.apps)} 个软件 · 高风险 {risks.count('高')} · "
                 f"中 {risks.count('中')} · 低 {risks.count('低')} · "
                 "点击列标题排序；正在后台统计实际占用大小…")
        if self.smoke:
            return
        self.runner.submit(lambda: self._size_job(self._size_gen))

    def _size_job(self, gen):
        for app in list(self.apps):
            if gen != self._size_gen:
                return
            if app.size_bytes is None and app.install_location \
                    and os.path.isdir(app.install_location):
                app.size_bytes = scanner.dir_size(app.install_location)[0]
                self.runner.call_in_main(self._refresh_row, app)
                time.sleep(0.02)

    def _filtered(self):
        q = self.search_var.get().strip().lower()
        if not q:
            return list(self.apps)
        return [a for a in self.apps
                if q in a.name.lower() or q in (a.publisher or "").lower()]

    def _sorted(self):
        apps = self._filtered()
        key = _sort_key(self.sort_col)
        if self.sort_col == "size" and self.sort_desc:
            return sorted(apps, key=lambda a: key(a) if key(a) != -1 else float("inf"),
                          reverse=True)
        return sorted(apps, key=key, reverse=self.sort_desc)

    def _populate(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.app2iid.clear()
        for i, app in enumerate(self._sorted()):
            iid = f"a{i}"
            self.app2iid[id(app)] = iid
            tags = [RISK_TAG.get(app.risk_level, "")] if app.risk_level else []
            self.tree.insert("", "end", iid=iid, values=self._row_values(app),
                             tags=tags)
        if selected:
            for iid in selected:
                if self.tree.exists(iid):
                    self.tree.selection_set(iid)
                    break

    def _row_values(self, app):
        return (app.name, app.publisher or "—", app.install_location or "—",
                app.size_display, app.date_display or "—", app.risk_level or "…")

    def _row_tags(self, app):
        tags = [RISK_TAG.get(app.risk_level, "")] if app.risk_level else []
        return [t for t in tags if t]

    def _refresh_row(self, app):
        iid = self.app2iid.get(id(app))
        if iid and self.tree.exists(iid):
            self.tree.item(iid, values=self._row_values(app), tags=self._row_tags(app))
            if iid in self.tree.selection():
                self._fill_detail(app)

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = col in ("size", "risk", "date")  # 数值列默认从大到小
        self._populate()

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        app = next((a for a in self.apps if self.app2iid.get(id(a)) == sel[0]), None)
        if app:
            self._fill_detail(app)

    def _fill_detail(self, app):
        lines = [
            f"名称：{app.name}" + (f"    版本：{app.version}" if app.version else ""),
            f"发布者：{app.publisher or '未知'}",
            f"安装位置：{app.install_location or '未知'}",
            f"大小：{app.size_display}    安装日期：{app.date_display or '未知'}",
            f"注册表条目：{app.hive_name}\\{app.reg_subkey}" if app.reg_subkey else "",
            f"卸载命令：{app.uninstall_string or '（无，疑似残留条目）'}",
        ]
        risk = app.risk_level or "评估中…"
        lines.append(f"残留风险：{risk}")
        for r in app.risk_reasons:
            lines.append(f"    · {r}")
        if not app.uninstall_string.strip():
            lines.append("提示：该条目没有卸载程序，建议直接“仅扫描残留”并清理。")
        text = "\n".join(x for x in lines if x)
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.config(state="disabled")

    # ------------------------------------------------------------- 动作

    def _selected_app(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择一个软件。", parent=self)
            return None
        return next((a for a in self.apps if self.app2iid.get(id(a)) == sel[0]), None)

    def _set_buttons_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for btn in (self.btn_refresh, self.btn_uninstall, self.btn_scan, self.btn_elevate):
            if btn is self.btn_elevate and winutil.is_admin():
                continue
            btn.config(state=state)

    def do_uninstall(self):
        app = self._selected_app()
        if not app or self.busy:
            return
        opts = ask_uninstall_options(self, app)
        if not opts:
            return
        self.busy = True
        self._set_buttons_busy(True)
        UninstallFlow(self, app, scan_only=False,
                      restore_point=opts["restore_point"], silent=opts["silent"]).start()

    def do_scan_only(self):
        app = self._selected_app()
        if not app or self.busy:
            return
        if not messagebox.askyesno(
                "扫描残留",
                f"扫描「{app.name}」的残留文件和注册表项？\n\n"
                "不会运行卸载程序，仅列出建议清理的候选项目，确认后才会删除。",
                parent=self):
            return
        self.busy = True
        self._set_buttons_busy(True)
        UninstallFlow(self, app, scan_only=True).start()

    def open_file_cleaner(self):
        """打开资料清理窗口（独立于卸载流程，可同时使用）。"""
        from .cleaner_window import FileCleanerWindow  # 延迟导入避免循环依赖
        FileCleanerWindow(self)


# ================================================================ 对话框

def ask_uninstall_options(master: MainWindow, app):
    """卸载确认对话框。返回 {"restore_point": bool, "silent": bool} 或 None。"""
    dlg = tk.Toplevel(master)
    dlg.title("确认彻底卸载")
    dlg.transient(master)
    dlg.resizable(True, True)
    result = {}

    frm = ttk.Frame(dlg, padding=12)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=f"即将彻底卸载：{app.name}" +
              (f"（版本 {app.version}）" if app.version else ""),
              font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
    ttk.Label(frm, text=f"发布者：{app.publisher or '未知'}    "
                        f"大小：{app.size_display}    "
                        f"安装日期：{app.date_display or '未知'}").pack(anchor="w", pady=(2, 4))
    ttk.Label(frm, text=f"安装位置：{app.install_location or '未知'}",
              wraplength=560).pack(anchor="w")

    level = app.risk_level or "评估中…"
    color = {"高": "#c62828", "中": "#e65100", "低": "#1b7e2c"}.get(level, "#555555")
    risk_frm = ttk.Frame(frm)
    risk_frm.pack(anchor="w", pady=(8, 2))
    ttk.Label(risk_frm, text=f"残留风险等级：{level}",
              foreground=color, font=FONT_BOLD).pack(side="left")
    if app.risk_reasons:
        reasons = "\n".join("· " + r for r in app.risk_reasons[:6])
        ttk.Label(frm, text=reasons, foreground="#666666", wraplength=560)\
            .pack(anchor="w", padx=12)

    if level == "高":
        ttk.Label(frm, foreground="#c62828", font=FONT_BOLD, wraplength=560,
                  text="⚠ 高风险软件：预计卸载后会留下较多残留，"
                       "请务必在稍后的残留清单中逐项确认。").pack(anchor="w", pady=(4, 0))
    if not winutil.is_admin():
        ttk.Label(frm, foreground="#c62828", wraplength=560,
                  text="⚠ 当前未以管理员身份运行：将无法清理 HKLM 注册表项和 "
                       "Program Files 等系统目录，建议先点「以管理员身份重启」。")\
            .pack(anchor="w", pady=(4, 0))

    ttk.Separator(frm).pack(fill="x", pady=8)
    ttk.Label(frm, wraplength=560, justify="left", text=(
        "执行流程：\n"
        "① 备份注册表卸载条目  ② （可选）创建系统还原点\n"
        "③ 运行软件官方卸载程序（最长等待 60 秒，全程有进度日志；\n"
        "    卡住可随时点「强制结束卸载进程并继续」，超时也会自动结束）\n"
        "④ 自动扫描残留文件/文件夹与注册表项（可随时取消）\n"
        "⑤ 你逐项勾选确认后删除（默认进回收站，注册表删除前自动导出 .reg 备份）\n"
        "⑥ 生成清理报告")).pack(anchor="w")

    var_restore = tk.BooleanVar(value=True)
    var_silent = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm, variable=var_restore,
                    text="创建系统还原点（推荐；受系统 24 小时频率限制）")\
        .pack(anchor="w", pady=(8, 0))
    ttk.Checkbutton(frm, variable=var_silent,
                    text="尝试静默卸载（使用 QuietUninstallString / 已知静默参数）")\
        .pack(anchor="w")

    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=(12, 0))

    def on_start():
        result["restore_point"] = var_restore.get()
        result["silent"] = var_silent.get()
        dlg.destroy()

    def on_cancel():
        dlg.destroy()

    ttk.Button(btns, text="开始彻底卸载", command=on_start).pack(side="right")
    ttk.Button(btns, text="取消", command=on_cancel).pack(side="right", padx=6)
    dlg.protocol("WM_DELETE_WINDOW", on_cancel)
    dlg.grab_set()
    dlg.wait_window()
    return result or None


def ask_leftovers_dialog(master, fs_items, reg_items, backup_dir):
    """残留候选确认对话框。返回 (fs_sel, reg_sel, mode) 或 None。"""
    dlg = tk.Toplevel(master)
    dlg.title("确认要删除的残留项目")
    dlg.geometry("980x600")
    dlg.transient(master)
    _raise_window(dlg)

    top = ttk.Frame(dlg, padding=(10, 8))
    top.pack(fill="x")
    ttk.Label(top, wraplength=940, text=(
        "以下为扫描到的残留候选。请逐项确认：取消勾选的项目将被保留。"
        "删除注册表键前会自动导出 .reg 备份；文件默认送入回收站（可还原）。")).pack(anchor="w")

    mid = ttk.Frame(dlg)
    mid.pack(fill="both", expand=True, padx=10)
    cols = ("sel", "kind", "path", "match", "hint")
    tree = ttk.Treeview(mid, columns=cols, show="headings")
    for col, text, w, a in (("sel", "删除", 50, "center"),
                            ("kind", "类型", 70, "center"),
                            ("path", "位置", 400, "w"),
                            ("match", "匹配依据", 230, "w"),
                            ("hint", "提示", 190, "w")):
        tree.heading(col, text=text)
        tree.column(col, width=w, anchor=a, stretch=(col in ("path", "match")))
    vsb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="left", fill="y")
    tree.tag_configure("weak", foreground="#999999")
    tree.tag_configure("warn", foreground="#c62828")

    rows: list[dict] = []  # {"iid", "item": Leftover, "checked": bool}

    def insert_item(item: leftovers.Leftover):
        tags = []
        if item.strength == WEAK:
            tags.append("weak")
        if item.warning:
            tags.append("warn")
        values = ("☑" if item.default_checked else "☐", item.kind_display,
                  item.path, item.matched_by, item.warning)
        iid = tree.insert("", "end", values=values, tags=tags)
        rows.append({"iid": iid, "item": item, "checked": item.default_checked})

    for x in fs_items:
        insert_item(x)
    for x in reg_items:
        insert_item(x)

    def toggle(iid):
        row = next((r for r in rows if r["iid"] == iid), None)
        if not row:
            return
        row["checked"] = not row["checked"]
        tree.set(iid, "sel", "☑" if row["checked"] else "☐")
        update_count()

    def on_click(event):
        region = tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        if tree.identify_column(event.x) == "#1":  # “删除”列
            toggle(tree.identify_row(event.x))

    def on_double(event):
        toggle(tree.identify_row(event.x) or "")

    def on_space(_event):
        for iid in tree.selection():
            toggle(iid)
        return "break"

    tree.bind("<Button-1>", on_click)
    tree.bind("<Double-1>", on_double)
    tree.bind("<space>", on_space)

    def set_all(checked: bool):
        for r in rows:
            r["checked"] = checked
            tree.set(r["iid"], "sel", "☑" if checked else "☐")
        update_count()

    def selected_items():
        return [r["item"] for r in rows if r["checked"]]

    bottom = ttk.Frame(dlg, padding=(10, 4))
    bottom.pack(fill="x")
    ttk.Button(bottom, text="全选", command=lambda: set_all(True)).pack(side="left")
    ttk.Button(bottom, text="全不选", command=lambda: set_all(False)).pack(side="left", padx=4)
    mode_var = tk.StringVar(value="recycle")
    ttk.Radiobutton(bottom, text="删除到回收站（推荐，可还原）", variable=mode_var,
                    value="recycle").pack(side="left", padx=(16, 4))
    ttk.Radiobutton(bottom, text="永久删除", variable=mode_var,
                    value="permanent").pack(side="left")
    count_label = ttk.Label(bottom, text="")
    count_label.pack(side="left", padx=16)

    def update_count():
        n = len(selected_items())
        count_label.config(text=f"已选 {n} / {len(rows)} 项")
        state = "normal" if n else "disabled"
        btn_del.config(state=state)

    btns = ttk.Frame(dlg, padding=(10, 8))
    btns.pack(fill="x")

    def on_delete():
        fs_sel = [x for x in selected_items() if x.kind != "regkey"]
        reg_sel = [x for x in selected_items() if x.kind == "regkey"]
        mode = mode_var.get()
        msg = (f"确认删除 {len(fs_sel)} 个文件/文件夹 和 {len(reg_sel)} 个注册表项？\n\n"
               f"删除模式：{'移动到回收站（可还原）' if mode == 'recycle' else '永久删除（不可恢复！）'}\n"
               f"注册表备份目录：\n{backup_dir}\n\n"
               "该确认是删除前最后一道防线。")
        if not messagebox.askyesno("最后确认", msg, icon="warning", parent=dlg):
            return
        dlg.result = (fs_sel, reg_sel, mode)
        dlg.destroy()

    btn_del = ttk.Button(btns, text="删除选中项", command=on_delete, state="disabled")
    btn_del.pack(side="right")
    ttk.Button(btns, text="取消（不删除任何内容）",
               command=dlg.destroy).pack(side="right", padx=6)

    dlg.result = None
    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
    update_count()
    dlg.grab_set()
    dlg.wait_window()
    return dlg.result


def show_report_window(master, markdown_text: str, folder: str):
    dlg = tk.Toplevel(master)
    dlg.title("清理报告")
    dlg.geometry("860x620")
    text = tk.Text(dlg, wrap="word", font=FONT_MONO)
    ysb = ttk.Scrollbar(dlg, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=ysb.set)
    text.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
    ysb.pack(side="left", fill="y", pady=10)
    text.insert("1.0", markdown_text)
    text.config(state="disabled")
    btns = ttk.Frame(dlg)
    btns.pack(fill="x", pady=(0, 10))
    ttk.Button(btns, text="打开报告所在文件夹",
               command=lambda: winutil.open_in_explorer(folder)).pack(side="right", padx=10)
    ttk.Button(btns, text="关闭", command=dlg.destroy).pack(side="right")
    _raise_window(dlg)


# ================================================================ 卸载流程

class FlowCancelled(RuntimeError):
    """用户取消了整个流程。"""


class UninstallFlow:
    """“备份→（还原点）→官方卸载→扫描→确认→删除→报告”的完整流程。

    线程模型：_worker 在工作线程执行全部耗时步骤；
    日志写入线程安全缓冲，主线程定时器每 0.3 秒批量刷新；
    只有模态决策（询问对话框、候选清单）通过 call_in_main 回到主线程。
    """

    LOG_FLUSH_MS = 300  # 日志批量刷新间隔

    def __init__(self, gui: MainWindow, app, scan_only=False,
                 restore_point=False, silent=False):
        self.gui = gui
        self.app = app
        self.scan_only = scan_only
        self.restore_point = restore_point
        self.silent = silent
        self.wait_event = threading.Event()      # 「卸载完成，继续下一步」
        self.cancel_event = threading.Event()    # 「取消整个流程」
        self.force_event = threading.Event()     # 「强制结束卸载进程并继续」
        self.finished = False
        self._log_buffer: list[str] = []
        self._log_lock = threading.Lock()
        self._phase = ""
        self._phase_shown = ""

    # ------------------------------------------------------------- 日志

    def log(self, text: str):
        """线程安全追加日志（非阻塞）。主线程定时器批量刷新到界面。"""
        with self._log_lock:
            self._log_buffer.append(time.strftime("[%H:%M:%S] ") + text)

    def set_phase(self, text: str):
        """更新流程窗口顶部的阶段指示（线程安全，随日志批量刷新）。"""
        with self._log_lock:
            self._phase = text

    def _flush_logs(self):
        """主线程定时任务：一次性写入缓冲中的全部日志。"""
        with self._log_lock:
            lines = self._log_buffer
            self._log_buffer = []
            phase = self._phase
        try:
            if lines:
                self.log_text.config(state="normal")
                self.log_text.insert("end", "\n".join(lines) + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
            if phase and phase != self._phase_shown:
                self._phase_shown = phase
                self.phase_label.config(text=phase)
        except tk.TclError:
            return  # 窗口已销毁，停止刷新
        try:
            self.after(self.LOG_FLUSH_MS, self._flush_logs)
        except tk.TclError:
            pass

    # ------------------------------------------------------------- 启动

    def start(self):
        win = tk.Toplevel(self.gui)
        self.win = win
        win.title(f"{'扫描残留' if self.scan_only else '彻底卸载'}：{self.app.name}")
        win.geometry("860x560")
        top = ttk.Frame(win, padding=(8, 6))
        top.pack(fill="x")
        self.phase_label = ttk.Label(top, text="准备中…", font=FONT_BOLD)
        self.phase_label.pack(anchor="w")
        logf = ttk.Frame(win, padding=(8, 0))
        logf.pack(fill="both", expand=True)
        self.log_text = tk.Text(logf, wrap="word", font=FONT_MONO, state="disabled")
        ysb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ysb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")
        self.progress = ttk.Progressbar(win, mode="indeterminate")
        self.progress.pack(fill="x", padx=8)
        self.progress.start(12)
        btns = ttk.Frame(win, padding=8)
        btns.pack(fill="x")
        self.btn_continue = ttk.Button(btns, text="卸载完成，继续下一步 →",
                                       command=self.wait_event.set)
        self.btn_continue.pack(side="right")
        self.btn_force = ttk.Button(btns, text="强制结束卸载进程并继续",
                                    command=self._on_force,
                                    state="disabled" if self.scan_only else "normal")
        self.btn_force.pack(side="right", padx=6)
        self.btn_cancel = ttk.Button(btns, text="取消整个流程", command=self._on_cancel)
        self.btn_cancel.pack(side="right", padx=6)
        self.btn_close = ttk.Button(btns, text="关闭", state="disabled",
                                    command=win.destroy)
        self.btn_close.pack(side="right", padx=6)
        win.protocol("WM_DELETE_WINDOW", self._on_close)
        _raise_window(win)
        self.log(f"目标：{self.app.name}（{self.app.install_location or '位置未知'}）")
        if self.scan_only:
            self.log("模式：仅扫描残留（不运行卸载程序）")
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        except Exception:  # noqa: BLE001
            pass
        self._flush_logs()  # 启动批量刷新定时器
        self.gui.runner.submit(self._worker)

    # ------------------------------------------------------------- 按钮

    def _on_continue(self):
        self.wait_event.set()

    def _on_cancel(self):
        self.cancel_event.set()
        self.log("已请求取消：将在安全位置停止（已删除的内容不受影响）。")

    def _on_force(self):
        if self.scan_only or self.finished:
            return
        if self.app.is_msi:
            if not messagebox.askyesno(
                    "强制结束 MSI 卸载？",
                    "强杀 msiexec.exe 可能损坏 Windows Installer 数据库，\n"
                    "导致之后的安装/卸载异常，通常需要重启才能恢复。\n\n确定要强制结束吗？",
                    icon="warning", parent=self.win):
                return
        self.force_event.set()
        self.log("已请求强制结束卸载相关进程…")
        try:
            self.btn_force.config(state="disabled")
        except tk.TclError:
            pass

    def _on_close(self):
        if self.finished:
            self.win.destroy()
        else:
            self._on_cancel()

    # ------------------------------------------------------------- 工作线程

    def _worker(self):
        report = reporter.CleanupReport(self.app)
        try:
            self._run(report)
        except leftovers.ScanCancelled:
            self.log("已取消：扫描中断，本次未删除任何内容。")
        except FlowCancelled:
            self.log("已取消：后续步骤不再执行（已删除的内容不受影响）。")
        except Exception:  # noqa: BLE001
            err = traceback.format_exc()
            self.log("发生异常：\n" + err)
            report.add_error(err.splitlines()[-1] if err else "未知异常")
            try:
                self._save_report(report)
                self.gui.runner.call_in_main(
                    lambda: messagebox.showerror("错误", "流程出现异常，详见日志窗口。",
                                                 parent=self.win))
            except tk.TclError:
                pass
        finally:
            self._finish()

    def _run(self, report):
        app = self.app
        self.set_phase("准备中…")
        op_dir = reporter.prepare_operation_dir(app.name)
        backup_dir = os.path.join(op_dir, "registry-backup")
        report.set_dirs(op_dir, backup_dir)
        self.log(f"操作目录：{op_dir}")
        others = [a for a in self.gui.apps if a is not app]

        if self.scan_only:
            self.log("步骤 1/3：跳过备份与卸载（仅扫描模式）")
        elif not app.uninstall_string.strip():
            self.log("该条目没有卸载程序（疑似残留条目），直接进入残留扫描。")
        else:
            # 1 备份卸载条目
            self.set_phase("备份注册表卸载条目…")
            self.log("步骤 1/5：备份注册表卸载条目…")
            if app.reg_subkey:
                ok, msg = backup.export_registry_key(
                    f"{app.hive_name}\\{app.reg_subkey}",
                    os.path.join(backup_dir, "uninstall-entry.reg"))
                report.record_backup(f"{app.hive_name}\\{app.reg_subkey}", ok,
                                     "uninstall-entry.reg" if ok else msg)
                self.log("  ✓ 已备份 → registry-backup\\uninstall-entry.reg" if ok
                         else f"  ✗ 备份失败：{msg}（流程继续）")
            else:
                self.log("  无注册表卸载条目，跳过")

            # 2 还原点
            self.set_phase("创建系统还原点…")
            self.log("步骤 2/5：系统还原点…")
            if self.restore_point:
                self.log("  创建中（可能需要几十秒，请稍候）…")
                ok, msg = backup.create_restore_point(f"CleanSweep 卸载 {app.name} 前")
                report.record_restore_point(ok, msg)
                self.log(("  ✓ " if ok else "  ✗ ") + msg)
            else:
                report.record_restore_point(None, "未启用")
                self.log("  未勾选，跳过")

            # 3 官方卸载程序
            self.set_phase("运行官方卸载程序…")
            self.log("步骤 3/5：运行软件官方卸载程序…")
            result = uninstaller.run(app, self.silent, progress=self.log,
                                     wait_event=self.wait_event,
                                     cancel_event=self.cancel_event,
                                     force_event=self.force_event,
                                     other_apps=others)
            report.record_uninstall(result)
            if result.error:
                self.log("  ✗ " + result.error)
                self.log("流程终止：未执行任何删除。")
                return
            if self.cancel_event.is_set() and not result.user_continued:
                raise FlowCancelled()
            if result.forced:
                self.log("  已强制结束进程：" + "、".join(result.force_killed)
                         if result.force_killed else "  已尝试强制结束（无匹配进程）")
            if result.entry_removed is False and not result.user_continued:
                cont = self.gui.runner.call_in_main(self._ask_unfinished)
                if not cont:
                    self.log("用户选择终止流程，未删除任何内容。")
                    return
            self.log("  等待文件句柄释放…")
            time.sleep(1.0)

        # 4 扫描残留（后台线程，可取消）
        fs_items, reg_items = self._scan(others)

        self.log(f"  候选：文件/文件夹 {len(fs_items)} 项，注册表 {len(reg_items)} 项")
        if not fs_items and not reg_items:
            self.log("未发现任何残留候选，无需清理。")
            self._save_report(report)
            return

        # 5 用户确认 + 删除
        self.set_phase("等待确认残留清单…")
        sel = self.gui.runner.call_in_main(self._ask_candidates, fs_items, reg_items,
                                           backup_dir)
        if not sel:
            self.log("已取消或未选择任何项目，未删除任何内容。")
            self._save_report(report)
            return
        fs_sel, reg_sel, mode = sel
        mode_text = "回收站（可还原）" if mode == "recycle" else "永久删除"
        self.set_phase(f"删除残留（{mode_text}）…")
        self.log(f"步骤 5/5：删除选中的残留（模式：{mode_text}）…")
        if reg_sel:
            self.log(f"  先备份 {len(reg_sel)} 个注册表键 → registry-backup\\")
            for rp, (_dest, ok, msg) in backup.export_many(
                    [x.path for x in reg_sel], backup_dir, log=self.log).items():
                report.record_backup(rp, ok, msg)
        extra = cleaner.extra_exact_paths_for(app)  # 自定义盘符上的安装目录
        report.add_fs(cleaner.delete_fs(fs_sel, mode, log=self.log, extra_exact=extra))
        report.add_reg(cleaner.delete_registry(reg_sel, backup_dir, log=self.log))
        n_err = sum(1 for x in report.deleted_fs + report.deleted_reg if not x.ok)
        if n_err:
            report.add_error(f"{n_err} 项删除失败/被跳过（详见报告），"
                             "常见原因：权限不足或文件被占用。")
        self.log("清理完成。")
        self._save_report(report)

    # ------------------------------------------------------------- 扫描

    def _scan(self, others):
        """残留扫描（在工作线程执行），支持随时取消。"""
        self._check_cancel()
        self.set_phase("正在扫描残留（文件系统）…")
        self.log("正在扫描文件系统残留…（可随时点「取消整个流程」）")
        ctx = leftovers.ScanContext()
        fs_items = leftovers.scan_filesystem(self.app,
                                             after_uninstall=not self.scan_only,
                                             ctx=ctx, other_apps=others,
                                             cancel_check=self.cancel_event.is_set)
        self._check_cancel()
        self.set_phase("正在扫描残留（注册表）…")
        self.log("正在扫描注册表残留…")
        reg_items = leftovers.scan_registry(self.app, ctx=ctx, other_apps=others,
                                            cancel_check=self.cancel_event.is_set)
        self._check_cancel()
        self.set_phase("扫描完成")
        return fs_items, reg_items

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise FlowCancelled()

    # ------------------------------------------------------------- 主线程回调

    def _ask_unfinished(self):
        _raise_window(self.win)
        return messagebox.askyesno(
            "卸载似乎未完成",
            "卸载程序已退出，但注册表中仍存在该软件的卸载条目。\n"
            "可能尚未完成卸载，或卸载程序没有清理干净。\n\n是否继续扫描残留？",
            parent=self.win)

    def _ask_candidates(self, fs_items, reg_items, backup_dir):
        return ask_leftovers_dialog(self.win, fs_items, reg_items, backup_dir)

    def _save_report(self, report):
        path = report.save()
        self.log("报告已生成：" + path)
        try:
            self.gui.runner.call_in_main(
                lambda: show_report_window(self.win, report.to_markdown(), report.op_dir))
        except tk.TclError:
            pass

    def _finish(self):
        self.finished = True

        def done():
            try:
                self.progress.stop()
                for btn in (self.btn_continue, self.btn_force, self.btn_cancel):
                    btn.config(state="disabled")
                self.btn_close.config(state="normal")
                self.set_phase("已结束")
                self.gui.busy = False
                self.gui._set_buttons_busy(False)
                self.gui.after(600, self.gui.refresh)
            except tk.TclError:
                pass

        try:
            self.gui.runner.call_in_main(done)
        except Exception:  # noqa: BLE001
            pass


def run(smoke: bool = False) -> None:
    app = MainWindow(smoke=smoke)
    if smoke:
        app.withdraw()
    app.mainloop()
