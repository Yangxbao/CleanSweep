# CleanSweep v1.1.1

## 简体中文

### 🎨 资料清理界面优化

- **预览面板紧贴列表**：修复列表与预览面板之间出现大块空白的问题——列表现在真正撑满剩余宽度；两者之间改为**可拖动的分隔条**，拖到多宽图片就显示多大；
- **图片随窗口自适应**：拖动分隔条或缩放窗口后，预览图会**按面板实际大小自动重新生成**（原来按固定尺寸生成，缩放后会被裁剪或看不全）；
- **新增「← 返回卸载主界面」按钮**：窗口左上角，点一下即可回到软件列表页（关闭窗口等效）。

### 下载与校验

- **CleanSweep.exe**：Windows 10/11 x64，单文件免安装，双击运行（UAC 管理员权限）
- SHA256：`30de3ed19cef5a6b3c03b0c30ce783dbde888cd6344102b4467695bc132d0f5f`

```bat
certutil -hashfile CleanSweep.exe SHA256
```

---

## English

### 🎨 File-cleaner UI improvements

- **Preview panel now hugs the list**: fixed the large blank gap between the results list and the preview panel — the list now really fills the remaining width; the two panes are separated by a **draggable sash**, so the preview can be made as wide as you drag it;
- **Zoom-adaptive preview**: after dragging the sash or resizing the window, the preview image is **regenerated at the panel's actual size** (it used to be generated at a fixed size and got clipped);
- **New "← 返回卸载主界面" (back to uninstaller) button** at the top-left of the window — one click returns to the main app list (same as closing the window).

### Download & verification

- **CleanSweep.exe**: Windows 10/11 x64, single portable file, runs with admin elevation (UAC)
- SHA256: `30de3ed19cef5a6b3c03b0c30ce783dbde888cd6344102b4467695bc132d0f5f`

```bat
certutil -hashfile CleanSweep.exe SHA256
```
