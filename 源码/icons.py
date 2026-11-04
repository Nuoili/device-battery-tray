# -*- coding: utf-8 -*-
"""托盘图标与程序图标绘制。"""
from PIL import Image, ImageDraw, ImageFont

GREEN = (34, 197, 94)
YELLOW = (234, 179, 8)
RED = (239, 68, 68)
GRAY = (148, 163, 184)
SHELL = (226, 232, 240)
OUTLINE = (15, 23, 42)

# 面板里电池填充用的低饱和度版本：色块更"沉"，不抢白色边框
FILL_GREEN = (78, 168, 118)
FILL_YELLOW = (206, 172, 76)
FILL_RED = (196, 96, 96)
FILL_GRAY = (120, 132, 148)

# 充电闪电：绿色电量条上只有纯白能拉开对比
BOLT_COLOR = (255, 255, 255, 255)


FONT_CANDIDATES = [
    r'C:\Windows\Fonts\seguisb.ttf',
    r'C:\Windows\Fonts\segoeuib.ttf',
    r'C:\Windows\Fonts\arialbd.ttf',
]

# 面板正文用的常规字重（比 semibold 细）
FONT_REGULAR_CANDIDATES = [
    r'C:\Windows\Fonts\segoeui.ttf',
    r'C:\Windows\Fonts\arial.ttf',
]

# 面板里要显示中文设备名，必须用带中文的字体
CN_FONT_CANDIDATES = [
    r'C:\Windows\Fonts\msyhbd.ttc',
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\msyhbd.ttf',
    r'C:\Windows\Fonts\simhei.ttf',
]
CN_FONT_REGULAR = [
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\msyhl.ttc',
    r'C:\Windows\Fonts\simhei.ttf',
]

# 电量分级：<25 红 / 25~70 黄 / ≥70 绿
WARN_LEVEL = 25
GOOD_LEVEL = 70


def _font(size):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def regular_font(size):
    """常规字重（细一些），面板里的数字用这个。"""
    for p in FONT_REGULAR_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return _font(size)


def cn_font(size, bold=True):
    """中文友好字体，用于面板文字。"""
    for p in (CN_FONT_CANDIDATES if bold else CN_FONT_REGULAR):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return _font(size)


def level_color(level):
    """<25 红，25~69 黄，>=70 绿。用于圆点、百分比与托盘图标。"""
    if level is None:
        return GRAY
    if level >= GOOD_LEVEL:
        return GREEN
    if level >= WARN_LEVEL:
        return YELLOW
    return RED


def level_fill_color(level):
    """面板电池填充色：同色相，饱和度更低，避免色块"太满"。"""
    if level is None:
        return FILL_GRAY
    if level >= GOOD_LEVEL:
        return FILL_GREEN
    if level >= WARN_LEVEL:
        return FILL_YELLOW
    return FILL_RED


def battery_glyph(level, cells=5):
    """用方块字符模拟电池，如 ■■■□□。用于原生菜单文本。"""
    if level is None:
        return '□' * cells
    n = int(round(max(0.0, min(100.0, level)) / 100.0 * cells))
    n = max(0, min(cells, n))
    if n == 0 and level > 0:
        n = 1
    return '■' * n + '□' * (cells - n)


def draw_bolt(d, cx, cy, h, color, scale=1.0, ratio=0.70):
    """画一个小闪电，h 是闪电本身的高度，宽 = h * ratio。

    充电时电池条是绿色，闪电用纯白才有对比；尺寸由调用方按"能不能塞进绿条"算好。
    1x 面板上闪电只有几像素宽，直接画多边形全是锯齿；这里放大 4 倍画完再缩回来。
    """
    bh = h
    bw = bh * ratio
    pts = [(0.30, -0.50), (-0.46, 0.06), (-0.06, 0.06),
           (-0.30, 0.50), (0.46, -0.06), (0.06, -0.06)]
    abs_pts = [(cx + px * bw, cy + py * bh) for px, py in pts]

    target = getattr(d, '_image', None)
    if target is not None and getattr(target, 'mode', '') == 'RGBA':
        m = max(2.0 * scale, 1.0)
        ox = int(cx - bw / 2.0 - m)
        oy = int(cy - bh / 2.0 - m)
        tw = int(round(bw + 2 * m))
        th = int(round(bh + 2 * m))
        ss = 4
        tmp = Image.new('RGBA', (tw * ss, th * ss), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).polygon(
            [((px - ox) * ss, (py - oy) * ss) for px, py in abs_pts], fill=color)
        target.alpha_composite(tmp.resize((tw, th), Image.LANCZOS), (ox, oy))
    else:
        d.polygon(abs_pts, fill=color)


def draw_battery_bar(d, x, y, w, h, level, scale=1.0, shell=(255, 255, 255),
                     charging=False):
    """画一个 macOS 菜单栏风格的电池：细圆角描边 + 右侧小凸块 + 内缩彩条。

    苹果那套的三个特征：描边很细（不画实心壳）、腔体会透出背景（不铺深色底）、
    电量条缩在框内一小圈，圆角比外框略小。
    charging=True 时在腔体正中叠一个闪电，颜色同 shell（= 电池框色）。
    返回电池实际占用的宽度。
    """
    s = scale
    sw = max(1, int(round(1.4 * s)))        # 细描边：苹果的电池框就是一根细线
    # 充电中一律用绿色：电量再低也说明正在回血，红色配闪电会读成"快没电还在充"
    color = level_fill_color(100) if charging else level_fill_color(level)
    cap_w = max(2.0, 2.3 * s)               # 正极小凸块
    body_l = x
    body_r = x + w - cap_w
    body_t = y
    body_b = y + h
    hh = body_b - body_t
    r = max(2.0, hh * 0.27)                 # 圆角按高度取比例，缩放到任何尺寸都协调
    cap_r = max(1.0, 1.1 * s)

    # 正极：贴在右侧的小圆角块（垂直居中，高度约 42%）
    cap_top = body_t + hh * 0.29
    cap_bot = body_t + hh * 0.71
    d.rounded_rectangle((body_r, cap_top, x + w, cap_bot), radius=cap_r, fill=shell)

    # 外框：只描边，不填底，腔体透出后面的磨砂背板
    d.rounded_rectangle((body_l + sw / 2.0, body_t + sw / 2.0,
                         body_r - sw / 2.0, body_b - sw / 2.0),
                        radius=r, outline=shell, width=sw)

    # 电量条：四边留同样的空隙（gap 从"描边内沿"量起），内圆角跟着 gap 走，
    # 这样左端和上下的空隙看起来才一样宽。
    gap = max(2.0, 2.2 * s)
    edge = sw + gap
    inner_l = body_l + edge
    inner_r = body_r - edge
    inner_t = body_t + edge
    inner_b = body_b - edge

    bar_w = 0.0
    if level is not None and inner_r > inner_l and inner_b > inner_t:
        frac = max(0.0, min(1.0, level / 100.0))
        fill_r = inner_l + (inner_r - inner_l) * frac
        bar_w = fill_r - inner_l
        if bar_w >= 1:
            d.rounded_rectangle(
                (inner_l, inner_t, fill_r, inner_b),
                radius=max(1.0, gap * 0.9), fill=color)

    # 充电中：纯白闪电整块落在绿条里面，四边（尤其上下）留一丝空隙。
    # 绿条宽度跟着电量走，电量低时闪电等比缩小；缩到认不出形状就不画了，
    # 否则只会留下一根白噪点，比没有更难看。
    if charging and bar_w > 0:
        full_h = inner_b - inner_t
        pad_v = max(0.7, 0.9 * s)                    # 上下留的这一丝
        bh = full_h - 2 * pad_v
        bw = bh * 0.70
        room = bar_w - 2 * max(0.7, 0.9 * s)         # 左右也留一丝
        if bw > room > 0:
            k = room / bw
            bh *= k
            bw *= k
        if bh >= full_h * 0.45:
            draw_bolt(d, inner_l + bar_w / 2.0, (inner_t + inner_b) / 2.0,
                      bh, BOLT_COLOR, s, ratio=0.70)

    return w


def render_icon(level, size=64):
    """画一个横向电池图标，中间叠百分比数字。

    用不透明底 + 彩色填充，保证在任何托盘实现（含第三方）里都不会渲染成灰块。
    level=None 表示未知。
    """
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0

    def px(*vals):
        return tuple(v * s for v in vals)

    color = level_color(level)
    dark = (30, 41, 59)

    # 电池底盘（不透明，深色）——几乎铺满画布，托盘里才够大
    d.rounded_rectangle(px(1, 11, 51, 53), radius=7 * s, fill=dark)
    # 正极
    d.rounded_rectangle(px(52, 24, 60, 40), radius=2.5 * s, fill=dark)
    # 外壳描边
    d.rounded_rectangle(px(1, 11, 51, 53), radius=7 * s, outline=SHELL,
                        width=max(2, int(3.0 * s)))

    if level is not None:
        inner_l, inner_r = 6, 46
        frac = max(0.0, min(1.0, level / 100.0))
        fill_r = inner_l + (inner_r - inner_l) * frac
        if fill_r - inner_l >= 1:
            d.rounded_rectangle(px(inner_l, 16, max(inner_l + 1, fill_r), 48),
                                radius=4 * s, fill=color)

    # 数字（白字 + 极细深色描边：黄底上也能看清，缩到 16px 不会糊）
    text = '?' if level is None else str(int(round(level)))
    fsize = 32 if len(text) <= 2 else 26
    f = _font(int(fsize * s))
    d.text(px(26, 32), text, font=f, fill=(255, 255, 255), anchor='mm',
           stroke_width=max(1, int(round(1.1 * s))), stroke_fill=(12, 18, 30))
    return img


def render_app_icon(size=256):
    """程序/exe 用的图标：蓝绿渐变电池 + 闪电。"""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0

    def px(*vals):
        return tuple(v * s for v in vals)

    # 圆角底板
    d.rounded_rectangle(px(2, 2, 62, 62), radius=14 * s, fill=(15, 23, 42))
    # 电池
    d.rounded_rectangle(px(9, 20, 49, 44), radius=5 * s, outline=(226, 232, 240), width=max(2, int(2.5 * s)))
    d.rounded_rectangle(px(50, 28, 55, 36), radius=2 * s, fill=(226, 232, 240))
    # 电量条
    d.rounded_rectangle(px(13, 24, 45, 40), radius=3 * s, fill=(34, 197, 94))
    # 闪电
    bolt = [(33, 24), (24, 33), (30, 33), (27, 40), (37, 30), (31, 30)]
    d.polygon([(x * s, y * s) for x, y in bolt], fill=(15, 23, 42))
    return img


if __name__ == '__main__':
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else '.'
    for lv in (95, 57, 18, None):
        name = 'unknown' if lv is None else str(lv)
        render_icon(lv).save(f'{out}/preview_{name}.png')
    render_app_icon().save(f'{out}/app_icon.png')
    print('ok')
