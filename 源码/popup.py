# -*- coding: utf-8 -*-
"""左键点托盘图标后弹出的竖列面板。

为什么自己画窗口而不是用托盘菜单：
  · pystray 的 Win32 菜单是系统原生菜单，文本不能着色，画不了彩色电池；
  · 某些第三方托盘（如 MyDockFinder）把原生菜单渲染得很难看。
所以这里用「分层窗口 + Pillow 出图」自己贴一张竖列面板，颜色、电池图形、
行距全部可控。面板不抢焦点（WS_EX_NOACTIVATE），点面板外面自动收起。
"""
import ctypes
import threading
import time
from ctypes import wintypes

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageGrab

import icons

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_SETCURSOR = 0x0020
IDC_ARROW = 32512

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0
QS_ALLINPUT = 0x04FF
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.UINT),
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', wintypes.HINSTANCE),
        ('hIcon', wintypes.HICON),
        ('hCursor', wintypes.HANDLE),
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
        ('hIconSm', wintypes.HICON),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ('BlendOp', ctypes.c_ubyte),
        ('BlendFlags', ctypes.c_ubyte),
        ('SourceConstantAlpha', ctypes.c_ubyte),
        ('AlphaFormat', ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD),
        ('biWidth', wintypes.LONG),
        ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD),
        ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD),
        ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG),
        ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD),
        ('biClrImportant', wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wintypes.DWORD * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('rcMonitor', wintypes.RECT),
        ('rcWork', wintypes.RECT),
        ('dwFlags', wintypes.DWORD),
    ]


def _setup_api():
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.ATOM

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT

    user32.SetCursor.argtypes = [wintypes.HANDLE]
    user32.SetCursor.restype = wintypes.HANDLE

    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL

    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                    ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.MsgWaitForMultipleObjects.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
        wintypes.DWORD, wintypes.DWORD]
    user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.GetDpiForSystem.restype = wintypes.UINT

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.GetDeviceCaps.restype = ctypes.c_int

    kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                      wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE


_setup_api()


# ------------------------------------------------------------------ 绘制
BG = (26, 32, 44, 250)
BORDER = (76, 88, 110, 235)
MUTED = (176, 186, 200, 255)      # 面板唯一文字色（标题 / 设备名 / 百分比 / 未连接 / 页脚）
DIVIDER = (255, 255, 255, 36)
# 正文与「未连接」统一成同一种灰：整块面板只有一种文字色，不再分主次。
# 暗色背板上 176 这一档更亮、更清楚；126 那档压在深色磨砂上会发闷。
SOFT_GRAY = MUTED
# 电池框（细线 + 正极 + 充电闪电）比文字深一档：框里还要托住彩色电量条，
# 边缘太淡会跟绿色/黄色糊在一起。文字统一成 176 后，这里保持不变。
BAT_FRAME = (126, 138, 158, 255)

# 磨砂背板参数
# 说的是"透明度"：透明度 60% → 背板不透明度 40%（alpha≈102）。
# 想要更透就调小 BACKDROP_OPACITY，更实就调大（0.8 = 透明度 20%）。
BACKDROP_OPACITY = 0.30     # 背板不透明度 → 30% 不透明（70% 透）
BACKDROP_BLUR = 26.0        # 高斯模糊半径
BACKDROP_RADIUS = 13.0      # 圆角半径
BACKDROP_TINT = (255, 255, 255)  # 压在上面的颜色（白色 = 白色磨砂）
BACKDROP_EDGE = (120, 132, 150, 70)   # 1px 细边，白底上勾勒轮廓

# 面板投影：很淡的一圈，让浮层"立"起来一点点，不要做成卡片阴影
SHADOW_PAD = 14        # 画布四周留白（随 scale 缩放）
SHADOW_ALPHA = 42      # 阴影不透明度（0-255）
SHADOW_BLUR = 7.0      # 高斯模糊半径
SHADOW_DY = 3          # 向下偏移，光从上方来的错觉


class _TextLayer:
    """文字专用图层：2 倍画布上渲染 + 1px 同色描边，缩回原尺寸后约等于 +1/2 px。

    这就是"非常克制的加粗"——直接换 Bold 字体太重，直接 1px 描边又会把中文
    笔画的间隙填满发糊；超采样之后只厚一点点，边缘也不毛。
    """

    SS = 2          # 超采样倍数（越大加粗越轻；SS=3 约 +1/3 px，SS=2 约 +1/2 px）

    def __init__(self, size):
        self.buf = Image.new('RGBA', (size[0] * self.SS, size[1] * self.SS),
                             (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.buf)

    def text(self, xy, text, font, fill, anchor='la', grow=1):
        try:
            big = font.font_variant(size=max(1, int(round(font.size * self.SS))))
        except Exception:
            self.d.text((xy[0] * self.SS, xy[1] * self.SS), text, font=font,
                        fill=fill, anchor=anchor)
            return
        self.d.text((xy[0] * self.SS, xy[1] * self.SS), text, font=big, fill=fill,
                    anchor=anchor, stroke_width=grow, stroke_fill=fill)

    def flush(self, img):
        img.alpha_composite(self.buf.resize(img.size, Image.LANCZOS))


def _fit(draw, text, font, max_w):
    """把文本裁剪到 max_w 内，超出用省略号。返回 (文本, 宽度)。"""
    if max_w <= 0:
        return '', 0
    w = draw.textlength(text, font=font)
    if w <= max_w:
        return text, w
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid] + '…', font=font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    out = text[:lo] + '…'
    return out, draw.textlength(out, font=font)


def render_panel(rows, scale=1.0, width_dip=340, updated_text='', footer='',
                 backdrop=False):
    """把设备列表画成一张竖向面板内容层。

    backdrop=False 时只画文字/电池等内容，背景透明，留给磨砂背板去填。
    """
    s = scale
    pad = 15 * s
    head_h = 40 * s
    row_h = 47 * s
    n = max(1, min(7, len(rows)))
    W = int(round(width_dip * s))
    H = int(round(head_h + n * row_h + (20 * s if footer else 6 * s)))

    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tl = _TextLayer((W, H))         # 文字都画这层，最后整体缩回
    if backdrop:
        d.rounded_rectangle((1, 1, W - 2, H - 2), radius=13 * s, fill=BG,
                            outline=BORDER, width=max(1, int(round(1.0 * s))))

    f_head = icons.cn_font(int(round(11.5 * s)), bold=False)
    tl.text((pad, head_h * 0.5), '外接设备电量', font=f_head, fill=SOFT_GRAY,
            anchor='lm')
    if updated_text:
        tl.text((W - pad, head_h * 0.5), updated_text, font=f_head,
                fill=SOFT_GRAY, anchor='rm')
    d.line([(pad, head_h - 1 * s), (W - pad, head_h - 1 * s)], fill=DIVIDER,
           width=max(1, int(round(1.0 * s))))

    f_name = icons.cn_font(int(round(13.0 * s)), bold=False)
    f_pct = f_name                                  # 百分比跟设备名同一套字，视觉上齐平

    if not rows:
        tl.text((pad, head_h + row_h * 0.5), '未发现可读取电量的设备',
                font=f_name, fill=SOFT_GRAY, anchor='lm')
        tl.flush(img)
        return img

    # 右侧预留固定宽度，让所有行的电池与百分比竖向对齐
    bat_w = 44 * s
    bat_h = 18 * s
    pct_zone = d.textlength('≈100%', font=f_pct) + 2 * s
    bat_right = W - pad - pct_zone - 13 * s
    name_x = pad + 12 * s
    max_name_w = bat_right - bat_w - 13 * s - name_x

    for i, r in enumerate(rows[:7]):
        cy = head_h + i * row_h + row_h / 2.0
        lv = r.get('level')
        online = r.get('online') is not False    # 只有明确断开才算，缺字段（部分读取器不填）视为在线
        color = icons.level_color(lv)

        # 状态圆点
        dot_r = 3.2 * s
        dot_cx = pad + dot_r
        if online:
            d.ellipse((dot_cx - dot_r, cy - dot_r, dot_cx + dot_r, cy + dot_r), fill=color)
        else:
            d.ellipse((dot_cx - dot_r, cy - dot_r, dot_cx + dot_r, cy + dot_r),
                      outline=MUTED, width=max(1, int(round(1.2 * s))))

        name = r.get('name') or '未知设备'

        if online:
            pct_text = '—' if lv is None else ('≈' if r.get('estimated') else '') + f'{int(round(lv))}%'
            shown, _ = _fit(d, name, f_name, max_name_w)
            tl.text((name_x, cy), shown, font=f_name, fill=SOFT_GRAY, anchor='lm')

            bx = bat_right - bat_w
            icons.draw_battery_bar(d, bx, cy - bat_h / 2.0, bat_w, bat_h, lv, s,
                                   shell=BAT_FRAME,
                                   charging=bool(r.get('charging')))

            tl.text((W - pad, cy), pct_text, font=f_pct,
                    fill=SOFT_GRAY, anchor='rm')
        else:
            # 断开时手里只剩缓存电量，画出来会被当成实时值——整块电池撤掉，
            # 位置写"未连接"，名字也不再挂后缀（省得一行说两遍）。
            state = '未连接'
            state_w = d.textlength(state, font=f_pct)
            shown, _ = _fit(d, name, f_name, (W - pad - state_w - 13 * s) - name_x)
            tl.text((name_x, cy), shown, font=f_name, fill=MUTED, anchor='lm')
            tl.text((W - pad, cy), state, font=f_pct, fill=MUTED, anchor='rm')

    if footer:
        f_foot = icons.cn_font(int(round(10.5 * s)), bold=False)
        tl.text((W / 2.0, H - 12 * s), footer, font=f_foot, fill=SOFT_GRAY,
                anchor='mm')
    tl.flush(img)
    return img


def _premul_bgra(img):
    """PIL RGBA → UpdateLayeredWindow 需要的预乘 BGRA 字节串。"""
    r, g, b, a = img.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge('RGBA', (r, g, b, a)).tobytes('raw', 'BGRA')


# ------------------------------------------------------------------ 磨砂背板
def _rounded_mask(w, h, radius, ss=4):
    """4 倍超采样画圆角蒙版再缩回来，边缘没有锯齿。"""
    m = Image.new('L', (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1),
        radius=max(1.0, radius * ss), fill=255)
    return m.resize((w, h), Image.LANCZOS)


def grab_backdrop(bbox, size, scale=1.0, blur=None):
    """抓屏幕上一块区域，高斯模糊 —— 这就是磨砂玻璃的"底"。"""
    w, h = size
    if blur is None:
        blur = BACKDROP_BLUR
    try:
        bg = ImageGrab.grab(bbox=bbox, all_screens=True).convert('RGB')
        if bg.size != (w, h):
            bg = bg.resize((w, h), Image.LANCZOS)
    except Exception:
        bg = Image.new('RGB', (w, h), (52, 58, 72))
    return bg.filter(ImageFilter.GaussianBlur(blur * scale))


def compose_panel(content, backdrop, scale=1.0, opacity=None,
                  radius=None, tint=None):
    """模糊底 + 纯净半透明白色 + 抗锯齿圆角 + 细边 + 内容。"""
    if opacity is None:
        opacity = BACKDROP_OPACITY
    if radius is None:
        radius = BACKDROP_RADIUS
    if tint is None:
        tint = BACKDROP_TINT
    w, h = content.size
    if backdrop.size != (w, h):
        backdrop = backdrop.resize((w, h), Image.LANCZOS)
    base = backdrop.convert('RGBA')

    # 纯净白色压一层：60% 透明度 = 白 tint alpha 102
    layer = Image.new('RGBA', (w, h), tuple(tint) + (int(round(255 * opacity)),))
    base = Image.alpha_composite(base, layer)

    # 圆角：用蒙版裁，边缘抗锯齿
    mask = _rounded_mask(w, h, radius * scale)
    base.putalpha(mask)

    # 1px 细边：白底上不靠它就没有轮廓
    ss = 4
    edge = Image.new('RGBA', (w * ss, h * ss), (0, 0, 0, 0))
    de = ImageDraw.Draw(edge)
    de.rounded_rectangle((1, 1, w * ss - 2, h * ss - 2),
                         radius=max(1.0, radius * scale * ss),
                         outline=BACKDROP_EDGE,
                         width=max(1, int(round(1.0 * scale * ss))))
    base = Image.alpha_composite(base, edge.resize((w, h), Image.LANCZOS))
    panel = Image.alpha_composite(base, content)
    return _with_shadow(panel, mask, scale)


def _with_shadow(panel, mask, scale=1.0):
    """给面板垫一层很淡的投影：向外留一圈边距，阴影超出面板就被裁不到了。"""
    w, h = panel.size
    pad = int(round(SHADOW_PAD * scale))
    if pad <= 0:
        return panel
    W, H = w + pad * 2, h + pad * 2
    dy = int(round(SHADOW_DY * scale))

    shadow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    tint_box = Image.new('RGBA', (w, h), (0, 0, 0, SHADOW_ALPHA))
    shadow.paste(tint_box, (pad, pad + dy), mask)
    # 模糊半径随边长走一点点，宽扁面板的阴影才不会糊成一条
    shadow = shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR * scale))

    canvas = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.alpha_composite(panel, (pad, pad))
    return canvas


# ------------------------------------------------------------------ 窗口
_wndproc_ref = None


class DevicePanel:
    """面板单例。所有公开方法都可以从任意线程调用。"""

    CLASS_NAME = 'DeviceBatteryTrayPanelW'

    def __init__(self):
        self._thread = None
        self._hwnd = None
        self._h_cursor = None
        self._evt = None
        self._hev = wintypes.HANDLE()
        self._lock = threading.Lock()
        self._req = None
        self._visible = False
        self._rect = (0, 0, 0, 0)
        self._shown_at = 0.0
        self._top_at = 0.0            # 上一次"确保置顶"的时间，用于节流
        self._block_until = 0.0
        self._stopping = False
        self._last_key = None
        self._bg = None
        self._bg_key = None

    # -- 外部 API ----------------------------------------------------
    def toggle(self, rows, updated_text='', footer=''):
        """点一下开、再点一下关。"""
        if time.time() < self._block_until:
            return
        self._ensure_thread()
        with self._lock:
            self._req = ('toggle', rows, updated_text, footer, None)
        if self._evt:
            kernel32.SetEvent(self._evt)

    def refresh_if_visible(self, rows, updated_text='', footer=''):
        if not self._visible:
            return
        with self._lock:
            self._req = ('refresh', rows, updated_text, footer, None)
        if self._evt:
            kernel32.SetEvent(self._evt)

    def show(self, rows, updated_text='', footer=''):
        """把面板显示出来。已经开着就刷新并重新置顶，不会像 toggle 那样收起。

        给"第二次双击程序图标"用：用户要的是看见面板，不是把它关掉。
        """
        self._ensure_thread()
        with self._lock:
            self._req = ('show', rows, updated_text, footer, None)
        if self._evt:
            kernel32.SetEvent(self._evt)

    def hide(self):
        with self._lock:
            self._req = ('hide', None, '', '', None)
        if self._evt:
            kernel32.SetEvent(self._evt)

    def stop(self):
        self._stopping = True
        with self._lock:
            self._req = ('hide', None, '', '', None)
        if self._evt:
            kernel32.SetEvent(self._evt)

    # -- 线程 --------------------------------------------------------
    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._thread_main,
                                        name='panel', daemon=True)
        self._thread.start()
        for _ in range(60):
            if self._hwnd:
                return
            time.sleep(0.01)

    def _thread_main(self):
        global _wndproc_ref
        self._evt = kernel32.CreateEventW(None, False, False, None)
        self._hev = wintypes.HANDLE(self._evt)
        hinst = kernel32.GetModuleHandleW(None)

        def wndproc(hwnd, msg, wp, lp):
            if msg == WM_SETCURSOR:
                # 面板是 WS_EX_NOACTIVATE 的纯绘制窗口，不设光标时系统会沿用
                # 上一个光标（常常停在沙漏上），鼠标一进来就变成"等待"。
                # 这里每次都硬压回箭头并吃掉消息，不让 DefWindowProc 再改。
                user32.SetCursor(wintypes.HANDLE(self._h_cursor))
                return 1
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        _wndproc_ref = WNDPROC(wndproc)

        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        self._h_cursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = _wndproc_ref
        wc.hInstance = hinst
        wc.hCursor = self._h_cursor
        wc.lpszClassName = self.CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (1410,):      # ERROR_CLASS_ALREADY_EXISTS
                return

        ex_style = (WS_EX_LAYERED | WS_EX_TOOLWINDOW
                    | WS_EX_TOPMOST | WS_EX_NOACTIVATE)
        self._hwnd = user32.CreateWindowExW(
            ex_style, self.CLASS_NAME, '设备电量', WS_POPUP,
            0, 0, 0, 0, None, None, hinst, None)
        if not self._hwnd:
            return

        msg = wintypes.MSG()
        while not self._stopping:
            r = user32.MsgWaitForMultipleObjects(
                1, ctypes.byref(self._hev), False, 80, QS_ALLINPUT)
            if r == 0:      # WAIT_OBJECT_0 → 有新请求
                self._handle_request()
            elif r == 258:  # WAIT_TIMEOUT
                self._poll_outside_click()
                self._keep_top()

            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == WM_QUIT:
                    self._stopping = True
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

    def _handle_request(self):
        with self._lock:
            req = self._req
            self._req = None
        if not req:
            return
        action, rows, updated, footer, _ = req
        if action == 'hide':
            self._hide(external=False)
        elif action == 'toggle':
            if self._visible:
                self._hide(external=False)
            else:
                self._show(rows or [], updated, footer)
        elif action in ('refresh', 'show'):
            self._show(rows or [], updated, footer)

    # -- 显示 / 隐藏 -------------------------------------------------
    def _dpi_scale(self):
        try:
            dpi = user32.GetDpiForSystem()
            if dpi:
                return max(1.0, dpi / 96.0)
        except Exception:
            pass
        try:
            dc = user32.GetDC(None)
            dpi = gdi32.GetDeviceCaps(dc, 88)
            user32.ReleaseDC(None, dc)
            return max(1.0, dpi / 96.0)
        except Exception:
            return 1.0

    def _place(self, w, h):
        """固定贴在托盘旁边（任务栏靠右的那一端），不跟着鼠标指针跑。

        为什么不去查托盘图标的精确坐标：Shell_NotifyIconGetRect 要拿 pystray 内部
        隐藏窗口的 hWnd + uID，不是公开接口，这套自绘外壳下调还可能直接崩。
        通知区永远在任务栏靠右的一端，对齐任务栏就够了。
        """
        s = self._dpi_scale()
        m = max(4, int(round(8 * s)))
        try:
            user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
            user32.FindWindowW.restype = wintypes.HWND
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            tb = wintypes.RECT()
            tray = user32.FindWindowW('Shell_TrayWnd', None)
            if tray and user32.GetWindowRect(tray, ctypes.byref(tb)) and tb.right > tb.left:
                horiz = (tb.right - tb.left) > (tb.bottom - tb.top)   # 横条任务栏
                if horiz and tb.top > sh * 0.5:          # 在屏幕下方（最常见）
                    x, y = tb.right - w - m, tb.top - h - m
                elif horiz:                              # 在屏幕上方
                    x, y = tb.right - w - m, tb.bottom + m
                elif tb.left > sw * 0.5:                 # 竖条贴右边
                    x, y = tb.left - w - m, tb.bottom - h - m
                else:                                    # 竖条贴左边
                    x, y = tb.right + m, tb.bottom - h - m
                return max(0, min(x, sw - w)), max(0, min(y, sh - h))
        except Exception:
            pass
        # 拿不到任务栏（被第三方外壳接管等）就退回工作区右下角
        wa = wintypes.RECT()
        try:
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0) and wa.right > wa.left:
                return (max(wa.left, wa.right - w - m), max(wa.top, wa.bottom - h - m))
        except Exception:
            pass
        return max(0, user32.GetSystemMetrics(0) - w - m), max(0, user32.GetSystemMetrics(1) - h - m)

    def _show(self, rows, updated, footer):
        s = self._dpi_scale()
        content = render_panel(rows, s, updated_text=updated, footer=footer)
        w, h = content.size
        x, y = self._place(w, h)

        key = (w, h)
        if self._visible and self._bg is not None and self._bg_key == key:
            # 面板正开着：沿用上次的磨砂底，否则会把自己也抓进底图
            bg = self._bg
        else:
            if self._visible:
                # 尺寸变了必须重抓底图，先藏一帧
                user32.ShowWindow(self._hwnd, SW_HIDE)
                self._visible = False
                time.sleep(0.05)
            bg = grab_backdrop((x, y, x + w, y + h), (w, h), s)
            self._bg = bg
            self._bg_key = key

        img = compose_panel(content, bg, s)
        pad = int(round(SHADOW_PAD * s))   # compose_panel 在四周留了投影用的边距
        if not self._paint(img, x - pad, y - pad):
            return
        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        self._visible = True
        self._rect = (x, y, x + w, y + h)
        self._shown_at = time.time()
        self._top_at = self._shown_at
        self._raise_top()          # 每次弹出都重新置顶，别被别的置顶窗口压住
        with self._lock:
            self._last_key = (tuple((r.get('key'), r.get('level')) for r in rows), updated)

    def _paint(self, img, x, y):
        w, h = img.size
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                     ctypes.byref(bits), None, 0)
        if not hbm:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)
            return False
        old = gdi32.SelectObject(mem_dc, hbm)
        data = _premul_bgra(img)
        ctypes.memmove(bits.value, data, len(data))

        pt_dst = wintypes.POINT(x, y)
        size = wintypes.SIZE(w, h)
        pt_src = wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        ok = user32.UpdateLayeredWindow(
            self._hwnd, screen_dc, ctypes.byref(pt_dst), ctypes.byref(size),
            mem_dc, ctypes.byref(pt_src), 0, ctypes.byref(blend), ULW_ALPHA)

        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)
        return bool(ok)

    def _raise_top(self):
        """把面板顶到最上层（topmost 组的最顶），且不抢焦点。"""
        if not self._hwnd:
            return
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _keep_top(self):
        """面板开着时，若被别的置顶窗口压住就重新顶上去。"""
        if not self._visible or not self._hwnd:
            return
        now = time.time()
        if now - self._top_at < 0.5:      # 节流，避免和别的置顶窗口来回抢
            return
        self._top_at = now
        x1, y1, x2, y2 = self._rect
        if x2 <= x1:
            return
        pt = wintypes.POINT((x1 + x2) // 2, (y1 + y2) // 2)
        at = user32.WindowFromPoint(pt)
        if int(at or 0) != int(self._hwnd or 0):
            self._raise_top()

    def _hide(self, external):
        if self._hwnd:
            user32.ShowWindow(self._hwnd, SW_HIDE)
        self._visible = False
        if external:
            # 让紧接着的托盘点击（松开鼠标）不要再把面板弹开
            self._block_until = time.time() + 0.5

    def _poll_outside_click(self):
        if not self._visible or self._stopping:
            return
        if time.time() - self._shown_at < 0.35:
            return
        down = (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000) or \
               (user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)
        if not down:
            return
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        x1, y1, x2, y2 = self._rect
        if not (x1 <= pt.x < x2 and y1 <= pt.y < y2):
            self._hide(external=True)
