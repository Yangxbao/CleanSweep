# CleanSweep v1.1.0

## 简体中文

### 🆕 新功能：资料文件清理

主窗口新增 **「资料清理（图片/文档/PPT…）」** 按钮，打开独立窗口：

- **按类别一键查找本机资料**：图片（jpg/png/gif/bmp/webp/heic…）、文档（Word/PPT/Excel/WPS/CSV…）、PDF、文本、音视频、压缩包，任意组合；
- **扫描范围**：默认桌面/文档/下载/图片/视频（自动识别重定向位置），可添加任意文件夹；系统与程序目录默认跳过，扫描全程只读、可随时取消；
- **图片预览**：单击列表中的图片行，右侧立即显示缩略图（GDI+ 解码，零第三方依赖），支持 JPG/PNG/GIF/BMP/TIFF/WebP/ICO；双击行用系统默认程序打开（看大图/Office），另有「打开所在位置」按钮；
- **创建时间排序**：列表含类别/大小/修改时间/创建时间/路径五列，点击列标题排序（再点切换升降序）；
- **安全删除**：默认一项都不勾选，逐项确认后删除；默认进回收站（可还原），永久删除需二次确认；删除在工作线程分块执行，失败自动逐个重试，生成 Markdown 清理报告。

### 🐞 修复

- **双击/勾选定位错行**：列表事件处理把鼠标横向坐标误传给行定位函数，导致双击打开的路径、勾选的行与看到的不一致（资料清理列表与卸载残留确认对话框同款问题，均已修复）；
- 预览解码链路修复并新增端到端回归测试（真实窗口选中图片 → 断言预览控件渲染成功）。

### 下载与校验

- **CleanSweep.exe**：Windows 10/11 x64，单文件免安装，双击运行（UAC 管理员权限）
- SHA256：`24f3f1ed4db37a004a53edd18c6d0993cef2ae133bd75e45c62f4b7852fb6ef8`

```bat
certutil -hashfile CleanSweep.exe SHA256
```

---

## English

### 🆕 New: personal file cleaner

A new **"资料清理" (file cleaner)** button in the main window opens a dedicated window:

- **One-click search by category**: images (jpg/png/gif/bmp/webp/heic…), documents (Word/PPT/Excel/WPS/CSV…), PDF, text, audio/video, archives — any combination;
- **Scope**: Desktop/Documents/Downloads/Pictures/Videos by default (redirect-aware), custom folders welcome; system & program directories are skipped by default; scanning is read-only and cancellable;
- **Image preview**: single-click a row to see the thumbnail instantly in the side panel (decoded via GDI+, zero third-party deps; JPG/PNG/GIF/BMP/TIFF/WebP/ICO); double-click opens the file with its default system program;
- **Creation-time sorting**: category/size/modification time/creation time/path columns — click a header to sort, click again to flip;
- **Safe deletion**: nothing is checked by default; recycle-bin-first with an extra confirmation for permanent deletion; chunked worker-thread deletion with per-file retry; Markdown cleanup report.

### 🐞 Fixes

- **Wrong-row clicks**: list event handlers passed the mouse X coordinate to the row-lookup function, so double-clicks and checkbox toggles acted on a different row than the one you saw (both in the file cleaner and the leftover-confirmation dialog) — fixed;
- The preview decode pipeline was repaired and is now covered by an end-to-end regression test (real window, real selection, asserts the preview widget renders).

### Download & verification

- **CleanSweep.exe**: Windows 10/11 x64, single portable file, runs with admin elevation (UAC)
- SHA256: `24f3f1ed4db37a004a53edd18c6d0993cef2ae133bd75e45c62f4b7852fb6ef8`

```bat
certutil -hashfile CleanSweep.exe SHA256
```
