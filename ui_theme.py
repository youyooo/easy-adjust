"""读取 Blender 当前主题，绘制数值快捷栏共用的轻量圆角卡片。"""

import math

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader


_shader = None
_ALL_CORNERS = (True, True, True, True)  # 左下、右下、右上、左上。


def scale(context=None):
    """系统缩放已经包含 DPI；插件倍率只乘一次，后台零值按 1 处理。"""
    context = context or bpy.context
    system = float(context.preferences.system.ui_scale)
    if not math.isfinite(system) or system <= 0:
        system = 1.0
    # 延迟导入，允许包先导入绘制模块、再注册 AddonPreferences。
    from . import preferences
    settings = preferences.get(context)
    multiplier = float(settings.ui_scale) if settings is not None else 1.0
    if not math.isfinite(multiplier):
        multiplier = 1.0
    return max(.25, min(8.0, system * max(.25, min(4.0, multiplier))))


def _rgba(value):
    color = tuple(float(component) for component in value)
    return color if len(color) == 4 else (*color[:3], 1.0)


def _mix(first, second, amount):
    return tuple(a + (b - a) * amount for a, b in zip(first, second))


def font_size(context=None):
    context = context or bpy.context
    return max(6.0, min(64.0, float(context.preferences.ui_styles[0].widget.points) * scale(context)))


def palette(context=None):
    """每次绘制读取颜色和圆角；主题 RNA 的 COLOR_GAMMA 可直接用于 UI。"""
    context = context or bpy.context
    ui = context.preferences.themes[0].user_interface
    card, item = ui.wcol_menu_back, ui.wcol_menu_item
    background = _rgba(card.inner)
    selected = _rgba(item.inner_sel)
    return {
        'card': background,
        'border': _rgba(card.outline),
        'text': _rgba(item.text),
        'text_selected': _rgba(item.text_sel),
        # 原生菜单无悬停时不重复铺色；轻混合仅用于鼠标悬停反馈。
        'hover': _mix(background, selected, .18),
        'selected': selected,
        'roundness': max(0.0, min(1.0, float(card.roundness))),
        'item_roundness': max(0.0, min(1.0, float(item.roundness))),
        'scale': scale(context),
        'font_size': font_size(context),
    }


def radius(row_height, roundness):
    # RNA roundness 是原生内部相对半径的两倍；菜单卡片按单行高度计算。
    return max(0.0, float(row_height) * max(0.0, min(1.0, roundness)) * .5)


def _path(rect, rounding, corners):
    left, bottom, width, height = rect
    rounding = min(max(0.0, rounding), width * .5, height * .5)
    if rounding < .01:
        return ((left, bottom), (left + width, bottom),
                (left + width, bottom + height), (left, bottom + height))
    # 小圆角使用 6 段已足够平滑；大倍率补充少量顶点，不依赖内部 Widget API。
    steps = max(6, min(16, math.ceil(rounding * 1.2)))
    result = []
    for enabled, cx, cy, angle, square in (
        (corners[0], left + rounding, bottom + rounding, math.pi,
         (left, bottom)),
        (corners[1], left + width - rounding, bottom + rounding, math.pi * 1.5,
         (left + width, bottom)),
        (corners[2], left + width - rounding, bottom + height - rounding, 0.0,
         (left + width, bottom + height)),
        (corners[3], left + rounding, bottom + height - rounding, math.pi * .5,
         (left, bottom + height)),
    ):
        if not enabled:
            result.append(square)
            continue
        for index in range(steps + 1):
            theta = angle + math.pi * .5 * index / steps
            result.append((cx + math.cos(theta) * rounding, cy + math.sin(theta) * rounding))
    return result


def _rounded(rect, color, rounding=0.0, corners=_ALL_CORNERS):
    global _shader
    left, bottom, width, height = rect
    if width <= 0 or height <= 0 or color[3] <= 0:
        return
    if _shader is None:
        _shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    vertices = _path(rect, rounding, corners)
    _shader.bind()
    _shader.uniform_float('color', color)
    batch_for_shader(_shader, 'TRI_FAN', {'pos': vertices}).draw(_shader)


def draw_card(rect, style, row_height=None):
    """一个整体外框；多行阶梯沿用单行圆角，不把整张卡片画成胶囊。"""
    left, bottom, width, height = rect
    rounding = radius(row_height if row_height is not None else height, style['roundness'])
    _rounded(rect, style['border'], rounding)
    inset = min(max(.75, style['scale']), width * .25, height * .25)
    _rounded((left + inset, bottom + inset, width - inset * 2, height - inset * 2),
             style['card'], max(0.0, rounding - inset))


def draw_item(rect, style, *, selected=False, hovered=False, corners=_ALL_CORNERS):
    if not selected and not hovered:
        return
    color = style['selected'] if selected else style['hover']
    _rounded(rect, color, radius(rect[3], style['item_roundness']), corners)


def draw_separator(x, bottom, height, style):
    if height <= 0:
        return
    color = (*style['border'][:3], style['border'][3] * .65)
    _rounded((x, bottom, max(.75, style['scale'] * .65), height), color)


def _fit_text(text, available):
    """仅截断显示文字；二分限制测量次数，不改用户保存的完整标签。"""
    if available <= 0:
        return ''
    if blf.dimensions(0, text)[0] <= available:
        return text
    suffix = '…'
    if blf.dimensions(0, suffix)[0] > available:
        return ''
    lower, upper = 0, len(text)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if blf.dimensions(0, text[:middle] + suffix)[0] <= available:
            lower = middle
        else:
            upper = middle - 1
    return text[:lower] + suffix


def draw_text(text, rect, style, *, selected=False, align='CENTER', padding=0.0):
    left, bottom, width, height = rect
    blf.size(0, style['font_size'])
    text = _fit_text(text, width - 2 * max(padding, 4 * style['scale']))
    if not text:
        return
    text_width, text_height = blf.dimensions(0, text)
    x = left + padding if align == 'LEFT' else left + (width - text_width) * .5
    blf.position(0, x, bottom + (height - text_height) * .5, 0)
    blf.color(0, *(style['text_selected'] if selected else style['text']))
    blf.draw(0, text)


def reset():
    """在移除 draw handlers 后释放缓存的 GPU 包装。"""
    global _shader
    _shader = None
