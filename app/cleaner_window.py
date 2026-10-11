"""资料清理窗口：按类别扫描本机的图片 / 文档 / PDF / 音视频等，确认后删除。

安全设计（面向用户个人资料，比卸载残留清理更保守）：
- 默认只扫描用户常用目录（桌面/文档/下载/图片/视频），系统与程序目录默认跳过；
- 扫描结果默认**一项都不勾选**，由用户勾选后才可删除；
- 删除默认进回收站（可还原），"永久删除"需要额外一次确认；
- 删除动作在工作线程执行，逐块回收站删除，失败逐个重试，全部记录到报告。

交互设计：
- 列表与预览面板之间是可拖动的分隔条；图片按面板实际大小生成并随缩放重新生成；
- 单击选中行 -> 右侧立即显示图片预览（GDI+ 解码，零依赖）；
- 双击行 -> 用系统默认程序打开该文件（看大图 / Office 文档）；
- 点击列标题可按 类别 / 大小 / 修改时间 / 创建时间 / 路径 排序；
- 顶部「返回卸载主界面」按钮（或关闭窗口）即可回到主窗口。
"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import filefinder, imgprev, reporter, winutil
from .gui import FONT, FONT_BOLD, _raise_window

MAX_RESULTS = 10000   # 扫描结果上限（与列表显示上限一致）
DELETE_CHUNK = 200    # 回收站批量删除的块大小
PREVIEW_W, PREVIEW_H = 360, 280   # 面板尺寸未知时的兜底值
PREVIEW_PANE_W = 430  # 预览面板初始宽度（可拖动分隔条调整）


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class FileCleanerWindow(tk.Toplevel):
    def __init__(self, master, show: bool = True):
        super().__init__(master)
        self.title("资料清理（图片 / 文档 / PDF / 音视频 / 压缩包）")
        self.geometry("1420x780")
        self.option_add("*Font", FONT)

        self.found: list = []                 # 全部扫描结果（FoundFile）
        self.path2file: dict = {}
        self.iid2file: dict = {}
        self.checked_paths: set = set()
        self.scanning = False
        self.deleting = False
        self.cancel_event = threading.Event()
        self._q: queue.Queue = queue.Queue()
        self._n = 0
        self._sort_col = "mtime"
        self._sort_desc = True
        self._preview_seq = 0
        self._preview_img = None              # 持有引用防止被 GC
        self._preview_file = None
        self._debounce_job = None
        self._resize_job = None
        self._pv_size = (0, 0)                # 上次生成预览时的面板尺寸

        self._build_ui()
        self._load_default_folders()
        self._update_counts()
        if show:
            _raise_window(self)
        self.protocol("WM_DELETE_WINDOW", self._back_to_main)
        self.after(150, self._pump)

    # ------------------------------------------------------------- UI 构建

    def _build_ui(self):
        # 0) 顶部：返回主界面
        hdr = ttk.Frame(self, padding=(8, 6, 8, 0))
        hdr.pack(fill="x")
        ttk.Button(hdr, text="← 返回卸载主界面",
                   command=self._back_to_main).pack(side="left")
        ttk.Label(hdr, foreground="#888888",
                  text="资料清理与卸载清理相互独立；关闭本窗口即可回到主界面")\
            .pack(side="left", padx=10)

        # 1) 类别选择
        top = ttk.LabelFrame(self, text="1. 选择要查找的文件类别", padding=(8, 4))
        top.pack(fill="x", padx=8, pady=(6, 4))
        self.cat_vars: dict = {}
        for cat, text in filefinder.CATEGORY_TEXT.items():
            var = tk.BooleanVar(value=cat in filefinder.DEFAULT_CATEGORIES)
            self.cat_vars[cat] = var
            ttk.Checkbutton(top, text=text, variable=var).pack(side="left", padx=(0, 12))

        # 2) 扫描范围
        mid = ttk.LabelFrame(self, text="2. 选择扫描范围（默认用户常用目录）", padding=(8, 4))
        mid.pack(fill="x", padx=8, pady=4)
        self.folder_frame = ttk.Frame(mid)
        self.folder_frame.pack(side="left", fill="x", expand=True)
        self.folder_vars: list = []          # [(路径, BooleanVar)]
        opts = ttk.Frame(mid)
        opts.pack(side="right")
        ttk.Button(opts, text="添加文件夹…", command=self._add_folder).pack(anchor="e")
        self.include_sys_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="包含系统与程序目录（很慢，不推荐）",
                        variable=self.include_sys_var).pack(anchor="e")

        # 3) 扫描控制
        ctrl = ttk.Frame(self, padding=(8, 2))
        ctrl.pack(fill="x")
        self.btn_scan = ttk.Button(ctrl, text="开始扫描", command=self.start_scan)
        self.btn_scan.pack(side="left")
        self.btn_cancel_scan = ttk.Button(ctrl, text="取消扫描", command=self.cancel_scan,
                                          state="disabled")
        self.btn_cancel_scan.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(ctrl, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.status = ttk.Label(ctrl, text="选择类别和范围后点击「开始扫描」", width=60,
                                anchor="w")
        self.status.pack(side="left", padx=(0, 8))

        # 操作提示（独立一行，避免与列表布局互相干扰）
        ttk.Label(self, foreground="#888888", padding=(10, 0),
                  text="单击选中行可在右侧预览图片；双击用系统程序打开；点击列标题排序"
                       "（再次点击切换升/降序）；列表与预览之间的分隔条可左右拖动")\
            .pack(fill="x")

        # 4) 结果区：左列表 + 右预览，中间可拖动分隔条
        main = ttk.Frame(self, padding=(8, 2))
        main.pack(fill="both", expand=True)
        paned = ttk.Panedwindow(main, orient="horizontal")
        paned.pack(fill="both", expand=True)

        treef = ttk.Frame(paned)
        paned.add(treef, weight=1)
        cols = ("sel", "kind", "size", "mtime", "ctime", "path")
        self.tree = ttk.Treeview(treef, columns=cols, show="headings")
        for col, text, w, a in (("sel", "删除", 46, "center"),
                                ("kind", "类别", 60, "center"),
                                ("size", "大小", 90, "e"),
                                ("mtime", "修改时间", 125, "center"),
                                ("ctime", "创建时间", 125, "center"),
                                ("path", "文件路径", 520, "w")):
            self.tree.heading(col, text=text,
                              command=(lambda c=col: self._sort_by(c)) if col != "sel" else "")
            self.tree.column(col, width=w, anchor=a, stretch=(col == "path"))
        vsb = ttk.Scrollbar(treef, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_row)

        preview = ttk.LabelFrame(paned, text="预览", padding=6)
        paned.add(preview, weight=0)
        preview.configure(width=PREVIEW_PANE_W)
        preview.pack_propagate(False)   # 面板宽度固定（拖动分隔条调整），不随图片变化
        self.preview_label = tk.Label(
            preview, text="单击列表中的图片行\n在此预览", bg="#f2f2f2", fg="#777777",
            justify="center", anchor="center", relief="sunken", bd=1)
        self.preview_label.pack(fill="both", expand=True)
        self.preview_label.bind("<Configure>", self._on_preview_resize)
        self.preview_name = tk.Label(preview, text="", wraplength=PREVIEW_PANE_W - 30,
                                     justify="left", font=FONT_BOLD, anchor="w")
        self.preview_name.pack(fill="x", pady=(6, 0))
        self.preview_info = tk.Label(preview, text="", wraplength=PREVIEW_PANE_W - 30,
                                     justify="left", fg="#555555", anchor="w")
        self.preview_info.pack(fill="x")
        pbtns = ttk.Frame(preview)
        pbtns.pack(fill="x", pady=(6, 0))
        ttk.Button(pbtns, text="用系统程序打开", command=self._open_with_system).pack(
            side="left", padx=(0, 4))
        ttk.Button(pbtns, text="打开所在位置", command=self._open_location).pack(side="left")

        # 5) 底部操作
        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="全选", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(bottom, text="全不选",
                   command=lambda: self._set_all(False)).pack(side="left", padx=4)
        ttk.Button(bottom, text="打开所在位置",
                   command=self._open_location).pack(side="left", padx=(12, 0))
        self.mode_var = tk.StringVar(value="recycle")
        ttk.Radiobutton(bottom, text="删除到回收站（推荐，可还原）", variable=self.mode_var,
                        value="recycle").pack(side="left", padx=(16, 4))
        ttk.Radiobutton(bottom, text="永久删除", variable=self.mode_var,
                        value="permanent").pack(side="left")
        self.count_label = ttk.Label(bottom, text="已选 0 项")
        self.count_label.pack(side="left", padx=16)
        self.btn_delete = ttk.Button(bottom, text="删除选中项", command=self.delete_selected,
                                     state="disabled")
        self.btn_delete.pack(side="right")

    def _load_default_folders(self):
        for path, label in filefinder.user_folders():
            var = tk.BooleanVar(value=True)
            self.folder_vars.append((path, var))
            ttk.Checkbutton(self.folder_frame, text=f"{label}（{path}）",
                            variable=var).pack(side="left", padx=(0, 10))

    def _add_folder(self):
        path = filedialog.askdirectory(parent=self, title="选择要扫描的文件夹")
        if not path or not os.path.isdir(path):
            return
        path = os.path.normpath(path)
        for p, _v in self.folder_vars:
            if winutil.normalize_path(p) == winutil.normalize_path(path):
                return
        var = tk.BooleanVar(value=True)
        self.folder_vars.append((path, var))
        ttk.Checkbutton(self.folder_frame, text=path, variable=var).pack(
            side="left", padx=(0, 10))

    # ------------------------------------------------------------- 返回

    def _back_to_main(self):
        """关闭本窗口，回到卸载主界面。"""
        self.cancel_event.set()  # 取消扫描/中止删除批次
        master = self.master
        try:
            self.destroy()
        except tk.TclError:
            pass
        try:
            master.lift()
            master.focus_force()
        except (tk.TclError, AttributeError):
            pass

    # ------------------------------------------------------------- 扫描

    def start_scan(self):
        if self.scanning or self.deleting:
            return
        cats = {c for c, var in self.cat_vars.items() if var.get()}
        roots = [p for p, var in self.folder_vars if var.get()]
        if not cats:
            messagebox.showinfo("提示", "请至少选择一个文件类别。", parent=self)
            return
        if not roots:
            messagebox.showinfo("提示", "请至少选择一个扫描目录。", parent=self)
            return
        self.cancel_event = threading.Event()
        self.found.clear()
        self.path2file.clear()
        self.iid2file.clear()
        self.checked_paths.clear()
        self._n = 0
        self._rebuild_tree(self.found)
        self.scanning = True
        self.btn_scan.config(state="disabled")
        self.btn_cancel_scan.config(state="normal")
        self.btn_delete.config(state="disabled")
        self.progress.start(12)
        self.status.config(text="正在扫描…")

        def work():
            try:
                stats = filefinder.scan(
                    roots, cats,
                    progress=lambda d, f, s: self._q.put(("progress", d, f, s)),
                    cancel_check=self.cancel_event.is_set,
                    include_system_dirs=self.include_sys_var.get(),
                    max_results=MAX_RESULTS,
                    sink=lambda batch: self._q.put(("found", batch)))
                self._q.put(("done", stats))
            except filefinder.ScanCancelled:
                self._q.put(("cancelled",))
            except Exception as e:  # noqa: BLE001
                self._q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def cancel_scan(self):
        self.cancel_event.set()
        self.status.config(text="正在取消扫描…")

    # ------------------------------------------------------------- 结果泵

    def _pump(self):
        """主线程定时任务：处理扫描/删除/预览的结果队列（批量刷新，不阻塞）。"""
        try:
            while True:
                item = self._q.get_nowait()
                try:
                    self._handle_item(item)
                except tk.TclError:
                    return  # 窗口已销毁，停止泵
                except Exception:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()  # 单条异常不影响后续处理
        except queue.Empty:
            pass
        try:
            self.after(150, self._pump)
        except tk.TclError:
            pass

    def _handle_item(self, item):
        kind = item[0]
        if kind == "found":
            for f in item[1]:
                self.path2file[f.path] = f
                self.found.append(f)
                self._insert_row(f)
            self._update_counts()
        elif kind == "progress":
            _d, found, dirs = item[1], item[2], item[3]
            self.status.config(
                text=f"正在扫描：{_d}（已找到 {found} 个，已检查 {dirs} 个目录）")
        elif kind == "done":
            stats = item[1]
            note = "（结果过多已截断，建议缩小扫描范围）" if stats["truncated"] else ""
            self.status.config(
                text=f"扫描完成：找到 {stats['found']} 个文件，共 "
                     f"{winutil.human_size(stats['total_size'])}{note}；"
                     f"勾选后点击「删除选中项」")
            self._finish_scan()
        elif kind == "cancelled":
            self.status.config(text="扫描已取消。")
            self._finish_scan()
        elif kind == "error":
            self.status.config(text=f"扫描出错：{item[1]}")
            self._finish_scan()
        elif kind == "deleted":
            self._on_deleted(item[1], item[2], item[3])
        elif kind == "preview":
            self._apply_preview(item[1], item[2], item[3])

    def _finish_scan(self):
        self.scanning = False
        self.progress.stop()
        self.btn_scan.config(state="normal")
        self.btn_cancel_scan.config(state="disabled")
        self._update_counts()

    # ------------------------------------------------------------- 预览

    def _on_select_row(self, _event=None):
        sel = self.tree.selection()
        f = self.iid2file.get(sel[0]) if sel else None
        self._preview_file = f
        if self._debounce_job:
            try:
                self.after_cancel(self._debounce_job)
            except tk.TclError:
                pass
        self._debounce_job = self.after(120, self._refresh_preview)

    def _refresh_preview(self):
        """按当前选中文件与当前面板尺寸刷新预览。"""
        f = self._preview_file
        if f is None:
            self._set_preview_text("单击列表中的图片行\n在此预览")
            self.preview_name.config(text="")
            self.preview_info.config(text="")
            return
        self.preview_name.config(text=os.path.basename(f.path))
        self.preview_info.config(
            text=f"类别：{f.category_text}　大小：{f.size_text}\n"
                 f"修改：{f.mtime_text}　创建：{f.ctime_text}")
        if f.ext not in imgprev.PREVIEWABLE_EXTS:
            self._set_preview_text("该类别不支持预览\n（仅图片文件）")
            return
        if not imgprev.available():
            self._set_preview_text("预览功能不可用")
            return
        self._set_preview_text("正在生成预览…")
        # 按面板实际大小生成（未完成布局时用兜底值）
        pw, ph = self.preview_label.winfo_width(), self.preview_label.winfo_height()
        max_w = max(120, min(pw - 10 if pw > 60 else PREVIEW_W, 900))
        max_h = max(120, min(ph - 10 if ph > 60 else PREVIEW_H, 700))
        self._pv_size = (max_w, max_h)
        seq = self._preview_seq + 1
        self._preview_seq = seq
        threading.Thread(target=self._preview_worker, args=(seq, f.path, max_w, max_h),
                         daemon=True).start()

    def _preview_worker(self, seq, path, max_w, max_h):
        try:
            data = imgprev.decode_thumbnail(path, max_w, max_h)
        except Exception:  # noqa: BLE001
            data = None
        try:
            self._q.put(("preview", seq, path, data))
        except tk.TclError:
            pass

    def _apply_preview(self, seq, path, data):
        if seq != self._preview_seq:
            return  # 已有更新的预览请求
        if not self._preview_file or self._preview_file.path != path:
            return  # 当前选中的已不是这张图
        if not data:
            self._set_preview_text("无法生成预览\n（可双击用系统程序打开）")
            return
        try:
            # 显式绑定到本窗口的解释器，避免默认根窗口问题
            self._preview_img = tk.PhotoImage(master=self, data=data, format="ppm")
            self.preview_label.config(image=self._preview_img, text="",
                                      width=0, height=0, bg="#ffffff")
        except tk.TclError:
            self._set_preview_text("预览显示失败\n（可双击用系统程序打开）")

    def _set_preview_text(self, text):
        self._preview_img = None
        self.preview_label.config(image="", text=text, bg="#f2f2f2", width=0, height=0)

    def _on_preview_resize(self, event):
        """面板尺寸变化（拖动分隔条/缩放窗口）后，按新尺寸重新生成预览图。"""
        if event.width < 60 or event.height < 60:
            return
        if self._pv_size and abs(event.width - self._pv_size[0]) < 24 \
                and abs(event.height - self._pv_size[1]) < 24:
            return
        self._pv_size = (event.width, event.height)
        if self._preview_file is None \
                or self._preview_file.ext not in imgprev.PREVIEWABLE_EXTS:
            return
        if self._resize_job:
            try:
                self.after_cancel(self._resize_job)
            except tk.TclError:
                pass
        self._resize_job = self.after(300, self._refresh_preview)

    def _open_with_system(self):
        f = self._preview_file
        if not f:
            messagebox.showinfo("提示", "请先在列表中选择一个文件。", parent=self)
            return
        try:
            os.startfile(f.path)
        except OSError as e:
            messagebox.showerror("打开失败", f"无法打开：\n{f.path}\n\n{e}", parent=self)

    # ------------------------------------------------------------- 列表

    def _insert_row(self, f):
        if self._n >= MAX_RESULTS:
            return
        iid = f"f{self._n}"
        self._n += 1
        self.iid2file[iid] = f
        self.tree.insert("", "end", iid=iid, values=(
            "☑" if f.path in self.checked_paths else "☐", f.category_text,
            f.size_text, f.mtime_text, f.ctime_text, f.path))

    def _rebuild_tree(self, items):
        self.tree.delete(*self.tree.get_children())
        self.iid2file.clear()
        self._n = 0
        for f in items:
            self._insert_row(f)
        self._update_counts()

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = col in ("size", "mtime", "ctime")  # 数值/时间列默认降序
        keys = {
            "kind": lambda f: f.category_text,
            "size": lambda f: f.size,
            "mtime": lambda f: f.mtime,
            "ctime": lambda f: f.ctime,
            "path": lambda f: f.path.lower(),
        }
        self._rebuild_tree(sorted(self.found, key=keys[col], reverse=self._sort_desc))

    def _toggle(self, iid):
        f = self.iid2file.get(iid)
        if not f:
            return
        if f.path in self.checked_paths:
            self.checked_paths.discard(f.path)
            self.tree.set(iid, "sel", "☐")
        else:
            self.checked_paths.add(f.path)
            self.tree.set(iid, "sel", "☑")
        self._update_counts()

    def _on_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) == "#1":
            self._toggle(self.tree.identify_row(event.y))

    def _on_double(self, event):
        iid = self.tree.identify_row(event.y)
        f = self.iid2file.get(iid)
        if f:
            try:
                os.startfile(f.path)  # 用系统默认程序打开（看图/Office…）
            except OSError as e:
                messagebox.showerror("打开失败", f"无法打开：\n{f.path}\n\n{e}",
                                     parent=self)
        return "break"

    def _on_space(self, _event):
        for iid in self.tree.selection():
            self._toggle(iid)
        return "break"

    def _set_all(self, checked: bool):
        if checked:
            self.checked_paths = {f.path for f in self.found}
        else:
            self.checked_paths.clear()
        for iid, f in self.iid2file.items():
            self.tree.set(iid, "sel", "☑" if f.path in self.checked_paths else "☐")
        self._update_counts()

    def _open_location(self):
        f = self._preview_file
        if f:
            winutil.open_in_explorer(f.path)
            return
        sel = self.tree.selection()
        f = self.iid2file.get(sel[0]) if sel else None
        if f:
            winutil.open_in_explorer(f.path)

    def _update_counts(self):
        files = [self.path2file[p] for p in self.checked_paths if p in self.path2file]
        total = sum(f.size for f in files)
        self.count_label.config(
            text=f"已选 {len(files)} 项，约 {winutil.human_size(total)}")
        state = "normal" if files and not self.scanning and not self.deleting else "disabled"
        self.btn_delete.config(state=state)

    # ------------------------------------------------------------- 删除

    def delete_selected(self):
        if self.scanning or self.deleting:
            return
        files = [self.path2file[p] for p in self.checked_paths if p in self.path2file]
        if not files:
            messagebox.showinfo("提示", "请先勾选要删除的文件。", parent=self)
            return
        mode = self.mode_var.get()
        total = sum(f.size for f in files)
        mode_text = "移动到回收站（可还原）" if mode == "recycle" else "永久删除（不可恢复！）"
        msg = (f"确认删除 {len(files)} 个文件，共约 {winutil.human_size(total)}？\n\n"
               f"删除模式：{mode_text}\n\n"
               "建议先逐项核对路径（可双击行打开文件、点「打开所在位置」看目录）。")
        if not messagebox.askyesno("删除确认", msg, icon="warning", parent=self):
            return
        if mode == "permanent" and not messagebox.askyesno(
                "再次确认",
                "永久删除的文件无法从回收站还原！\n\n确定要永久删除吗？",
                icon="warning", parent=self):
            return
        self.deleting = True
        self.btn_delete.config(state="disabled")
        self.btn_scan.config(state="disabled")
        self.progress.start(12)
        self.status.config(text=f"正在删除 {len(files)} 个文件…")

        def work():
            ok: list = []
            errors: list = []
            if mode == "recycle":
                for chunk in _chunks([f.path for f in files], DELETE_CHUNK):
                    if self.cancel_event.is_set():
                        break
                    okc, msg2 = winutil.send_to_recycle_bin(chunk)
                    if okc:
                        ok.extend(chunk)
                    else:  # 整块失败时逐个重试，尽量多删
                        for p in chunk:
                            ok1, m1 = winutil.send_to_recycle_bin([p])
                            if ok1:
                                ok.append(p)
                            else:
                                errors.append((p, m1))
            else:
                for f in files:
                    try:
                        os.remove(f.path)
                        ok.append(f.path)
                    except OSError as e:
                        errors.append((f.path, str(e)))
            self._q.put(("deleted", ok, errors, mode))

        threading.Thread(target=work, daemon=True).start()

    def _on_deleted(self, ok_paths, errors, mode):
        self.deleting = False
        self.progress.stop()
        deleted_files = [self.path2file[p] for p in ok_paths if p in self.path2file]
        total = sum(f.size for f in deleted_files)
        # 从列表移除已删除项
        gone = set(ok_paths)
        self.found = [f for f in self.found if f.path not in gone]
        for p in ok_paths:
            self.checked_paths.discard(p)
            self.path2file.pop(p, None)
        self._rebuild_tree(self.found)
        # 生成报告
        report_path = self._write_report(mode, deleted_files, errors, total)
        self.btn_scan.config(state="normal")
        self._update_counts()
        summary = (f"删除完成：成功 {len(ok_paths)} 项，约 {winutil.human_size(total)}"
                   f"{'（清空回收站后生效）' if mode == 'recycle' and ok_paths else ''}。\n"
                   f"失败 {len(errors)} 项。")
        if report_path:
            summary += f"\n报告：{report_path}"
        self.status.config(text=summary.replace("\n", "  "))
        messagebox.showinfo("删除完成", summary, parent=self)

    def _write_report(self, mode, deleted_files, errors, total) -> str:
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(reporter.reports_root(), f"文件清理报告_{ts}.md")
            with open(path, "w", encoding="utf-8-sig") as fp:
                fp.write("# CleanSweep 文件清理报告\n\n")
                fp.write(f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                fp.write(f"- 模式：{'回收站（可还原）' if mode == 'recycle' else '永久删除'}\n")
                fp.write(f"- 成功：{len(deleted_files)} 项，约 {winutil.human_size(total)}"
                         "（回收站模式需清空回收站后生效）\n")
                fp.write(f"- 失败：{len(errors)} 项\n\n## 已删除\n")
                for f in deleted_files[:2000]:
                    fp.write(f"- `{f.path}`（{f.category_text}，{f.size_text}）\n")
                if len(deleted_files) > 2000:
                    fp.write(f"- …共 {len(deleted_files)} 项，其余略\n")
                if errors:
                    fp.write("\n## 失败\n")
                    for p, m in errors[:500]:
                        fp.write(f"- `{p}` — {m}\n")
            return path
        except OSError:
            return ""
