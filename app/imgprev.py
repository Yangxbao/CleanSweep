"""图片缩略图解码（基于 Windows GDI+，纯 ctypes，零第三方依赖）。

decode_thumbnail(path, max_w, max_h) 返回 PPM(P6) 二进制数据，
可直接用于 tk.PhotoImage(data=..., format="ppm")；失败返回 None。
标准 GDI+ 解码器支持 bmp/gif/jpeg/png/tiff；webp/ico 取决于系统安装的
编解码器；heic/svg 不支持（返回 None，由界面提示用系统程序打开）。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

PREVIEWABLE_EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".dib",
                    ".tif", ".tiff", ".webp", ".ico"}

PixelFormat32bppARGB = 0x26200A
InterpolationModeHighQualityBicubic = 7
ImageLockModeRead = 1

_gdiplus = None
_token = None
_init_error = ""


class _Guid(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


# PNG 编码器 CLSID {557CF406-1A04-11D3-9A73-0000F81EF32E}（仅测试用）
PNG_ENCODER_CLSID = _Guid(0x557CF406, 0x1A04, 0x11D3,
                          (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00,
                                               0xF8, 0x1E, 0xF3, 0x2E))


def _startup():
    """初始化 GDI+（进程级一次）。返回 gdiplus 句柄或 None。"""
    global _gdiplus, _token, _init_error
    if _gdiplus is not None:
        return _gdiplus
    if _init_error:
        return None
    try:
        gp = ctypes.WinDLL("gdiplus")

        class StartupInput(ctypes.Structure):
            _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                        ("DebugEventCallback", ctypes.c_void_p),
                        ("SuppressBackgroundThread", wintypes.BOOL),
                        ("SuppressExternalCodecs", wintypes.BOOL)]

        si = StartupInput(1, None, False, False)
        token = ctypes.c_size_t(0)
        if gp.GdiplusStartup(ctypes.byref(token), ctypes.byref(si), None) != 0:
            _init_error = "GdiplusStartup failed"
            return None
        _gdiplus, _token = gp, token
        _set_argtypes(gp)
        return gp
    except Exception as e:  # noqa: BLE001
        _init_error = str(e)
        return None


def available() -> bool:
    return _startup() is not None


def _set_argtypes(gp):
    P = ctypes.POINTER(ctypes.c_void_p)
    gp.GdipCreateBitmapFromFile.argtypes = [wintypes.LPCWSTR, P]
    gp.GdipGetImageWidth.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    gp.GdipGetImageWidth.restype = ctypes.c_int
    gp.GdipGetImageHeight.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    gp.GdipGetImageHeight.restype = ctypes.c_int
    gp.GdipCreateBitmapFromScan0.argtypes = [ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int,
                                             ctypes.c_void_p, P]
    gp.GdipGetImageGraphicsContext.argtypes = [ctypes.c_void_p, P]
    gp.GdipDrawImageRectI.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int]
    gp.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gp.GdipBitmapLockBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_uint, ctypes.c_int, ctypes.c_void_p]
    gp.GdipBitmapUnlockBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gp.GdipCreateSolidFill.argtypes = [ctypes.c_uint32, P]
    gp.GdipFillRectangleI.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int]
    gp.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(_Guid), ctypes.c_void_p]
    for name in ("GdipDeleteGraphics", "GdipDeleteBrush", "GdipDisposeImage"):
        getattr(gp, name).argtypes = [ctypes.c_void_p]


class _BitmapData(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_int), ("Height", ctypes.c_int),
                ("Stride", ctypes.c_int), ("PixelFormat", ctypes.c_int),
                ("Scan0", ctypes.c_void_p), ("Reserved", ctypes.c_void_p)]


def decode_thumbnail(path, max_w: int = 360, max_h: int = 280):
    """把图片解码为等比缩放的 PPM(P6) 缩略图数据。失败返回 None。"""
    gp = _startup()
    if gp is None or not path or not os.path.isfile(path):
        return None
    bmp = ctypes.c_void_p()
    if gp.GdipCreateBitmapFromFile(str(path), ctypes.byref(bmp)) != 0 or not bmp.value:
        return None
    try:
        wv, hv = ctypes.c_uint(0), ctypes.c_uint(0)
        if gp.GdipGetImageWidth(bmp, ctypes.byref(wv)) != 0 \
                or gp.GdipGetImageHeight(bmp, ctypes.byref(hv)) != 0:
            return None
        w, h = wv.value, hv.value
        if w <= 0 or h <= 0:
            return None
        scale = min(max_w / w, max_h / h, 1.0)
        tw, th = max(1, int(w * scale)), max(1, int(h * scale))
        dst = ctypes.c_void_p()
        if gp.GdipCreateBitmapFromScan0(tw, th, 0, PixelFormat32bppARGB,
                                        None, ctypes.byref(dst)) != 0 or not dst.value:
            return None
        try:
            g = ctypes.c_void_p()
            if gp.GdipGetImageGraphicsContext(dst, ctypes.byref(g)) != 0 or not g.value:
                return None
            try:
                brush = ctypes.c_void_p()
                # 先铺白底，透明 PNG/GIF 合成后不透黑
                if gp.GdipCreateSolidFill(0xFFFFFFFF, ctypes.byref(brush)) == 0 and brush.value:
                    gp.GdipFillRectangleI(g, brush, 0, 0, tw, th)
                    gp.GdipDeleteBrush(brush)
                gp.GdipSetInterpolationMode(g, InterpolationModeHighQualityBicubic)
                if gp.GdipDrawImageRectI(g, bmp, 0, 0, tw, th) != 0:
                    return None
            finally:
                gp.GdipDeleteGraphics(g)

            bd = _BitmapData()
            if gp.GdipBitmapLockBits(dst, None, ImageLockModeRead,
                                     PixelFormat32bppARGB, ctypes.byref(bd)) != 0 \
                    or not bd.Scan0:
                return None
            try:
                width, height, stride = bd.Width, bd.Height, bd.Stride
                row_len = width * 4
                buf = (ctypes.c_ubyte * (abs(stride) * height)).from_address(bd.Scan0)
                out = [b"P6\n%d %d\n255\n" % (width, height)]
                rows = range(height) if stride >= 0 else range(height - 1, -1, -1)
                for y in rows:
                    row = buf[y * abs(stride): y * abs(stride) + row_len]
                    rgb = bytearray(row_len // 4 * 3)
                    rgb[0::3] = bytes(row[2::4])  # BGR A -> RGB
                    rgb[1::3] = bytes(row[1::4])
                    rgb[2::3] = bytes(row[0::4])
                    out.append(bytes(rgb))
                return b"".join(out)
            finally:
                gp.GdipBitmapUnlockBits(dst, ctypes.byref(bd))
        finally:
            gp.GdipDisposeImage(dst)
    finally:
        gp.GdipDisposeImage(bmp)


def selftest_make_png(path, w: int = 7, h: int = 5) -> bool:
    """（仅测试用）用 GDI+ 生成一张纯色 PNG。"""
    gp = _startup()
    if gp is None:
        return False
    bmp = ctypes.c_void_p()
    if gp.GdipCreateBitmapFromScan0(w, h, 0, PixelFormat32bppARGB,
                                    None, ctypes.byref(bmp)) != 0 or not bmp.value:
        return False
    try:
        g = ctypes.c_void_p()
        if gp.GdipGetImageGraphicsContext(bmp, ctypes.byref(g)) != 0 or not g.value:
            return False
        try:
            brush = ctypes.c_void_p()
            if gp.GdipCreateSolidFill(0xFF3366CC, ctypes.byref(brush)) == 0 and brush.value:
                gp.GdipFillRectangleI(g, brush, 0, 0, w, h)
                gp.GdipDeleteBrush(brush)
        finally:
            gp.GdipDeleteGraphics(g)
        return gp.GdipSaveImageToFile(bmp, str(path),
                                      ctypes.byref(PNG_ENCODER_CLSID), None) == 0
    finally:
        gp.GdipDisposeImage(bmp)
