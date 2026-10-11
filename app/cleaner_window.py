"""资料清理窗口：按类别扫描本机的图片 / 文档 / PDF / 音视频等，确认后删除。

安全设计（面向用户个人资料，比卸载残留清理更保守）：
- 默认只扫描用户常用目录（桌面/文档/下载/图片/视频），系统与程序目录默认跳过；
- 扫描结果默认**一项都不勾选**，由用户勾选后才可删除；
- 删除默认进回收站（可还原），"永久删除"需要额外一次确认；
- 删除动作在工作线程执行，逐块回收站删除，失败逐个重试，全部记录到报告。

交互设计：
- 单击选中行 -> 右侧立即显示图片预览（GDI+ 解码，零依赖）；
- 双击行 -> 用系统默认程序打开该文件（看大图 / Office 文档）；
- 点击列标题可按 类别 / 大小 / 修改时间 / 创建时间 / 路径 排序。
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
PREVIEW_W, PREVIEW_H = 360, 280


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

        self._build_ui()
        self._load_default_folders()
        self._update_counts()
        if show:
            _raise_window(self)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._pump)

    # ------------------------------------------------------------- UI 构建

    def _build_ui(self):
        # 1) 类别选择
        top = ttk.LabelFrame(self, text="1. 选择要查找的文件类别", padding=(8, 4))
        top.pack(fill="x", padx=8, pady=(8, 4))
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

        # 4) 结果区（左：列表，右：预览）
        main = ttk.Frame(self, padding=(8, 2))
        main.pack(fill="both", expand=True)

        treef = ttk.Frame(main)
        treef.pack(side="left", fill="both", expand=True)
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
        ttk.Label(treef, foreground="#888888",
                  text="单击选中行可在右侧预览图片；双击用系统程序打开；点击列标题排序"
                       "（再次点击切换升/降序）").pack(anchor="w", pady=(3, 0))

        preview = ttk.LabelFrame(main, text="预览", padding=6)
        preview.pack(side="left", fill="y", padx=(8, 0))
        preview.configure(width=PREVIEW_W + 28)
        preview.pack_propagate(False)
        self.preview_label = tk.Label(
            preview, text="单击列表中的图片行\n在此预览", bg="#f2f2f2", fg="#777777",
            justify="center", anchor="center", relief="sunken", bd=1)
        self.preview_label.pack(fill="x")
        self.preview_name = tk.Label(preview, text="", wraplength=PREVIEW_W - 8,
                                     justify="left", font=FONT_BOLD, anchor="w")
        self.preview_name.pack(fill="x", pady=(6, 0))
        self.preview_info = tk.Label(preview, text="", wraplength=PREVIEW_W - 8,
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
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(150, self._pump)
        except tk.TclError:
            pass

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
        self._preview_seq += 1
        seq = self._preview_seq
        if self._debounce_job:
            try:
                self.after_cancel(self._debounce_job)
            except tk.TclError:
                pass
        self._debounce_job = self.after(120, lambda: self._request_preview(seq, f))

    def _request_preview(self, seq, f):
        if seq != self._preview_seq:
            return
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
        threading.Thread(target=self._preview_worker, args=(seq, f.path),
                         daemon=True).start()

    def _preview_worker(self, seq, path):
        try:
            data = imgprev.decode_thumbnail(path, PREVIEW_W, PREVIEW_H)
        except Exception:  # noqa: BLE001
            data = None
        try:
            self._q.put(("preview", seq, path, data))
        except tk.TclError:
            pass

    def _apply_preview(self, seq, path, data):
        if seq != self._preview_seq:
            return  # 已有更新的选择
        if not self._preview_file or self._preview_file.path != path:
            return  # 当前选中的已不是这张图
        if not data:
            self._set_preview_text("无法生成预览\n（可双击用系统程序打开）")
            return
        try:
            self._preview_img = tk.PhotoImage(data=data, format="ppm")
            self.preview_label.config(image=self._preview_img, text="",
                                      width=0, height=0, bg="#ffffff")
        except tk.TclError:
            self._set_preview_text("预览显示失败\n（可双击用系统程序打开）")

    def _set_preview_text(self, text):
        self._preview_img = None
        self.preview_label.config(image="", text=text, bg="#f2f2f2", width=0, height=0)

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

    def _on_close(self):
        self.cancel_event.set()  # 取消扫描/中止删除批次
        self.destroy()
