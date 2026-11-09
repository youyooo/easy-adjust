"""独立数值快调：主题悬停按钮与中键精度调值条。"""

import bpy
from . import preferences, numeric_tools, numeric_hover, numeric_ladder, ui_theme


bl_info = {
    'name': 'Easy Adjust',
    'author': 'youyooo',
    'version': (1, 1, 2),
    'blender': (4, 2, 7),
    'location': 'Numeric fields and Preferences > Add-ons > Easy Adjust',
    'description': 'Theme-aware numeric hover buttons and a middle mouse value ladder',
    'category': 'Interface',
}
_enabled = False


def _configuration_changed(context=None):
    # 设置变化先安全撤销未确认的预览，再重新捕获目标，避免改错字段或突然跳值。
    numeric_ladder.abort_all()
    numeric_hover._clear(None)
    numeric_hover._drop_observers()
    numeric_tools._clear_targets()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def register():
    global _enabled
    if _enabled:
        return
    preferences.register()
    preferences.on_change = _configuration_changed
    try:
        # 加载工程时先回退未确认拖动，再清理其使用的数值目标。
        numeric_ladder.register()
        numeric_tools.register()
        numeric_hover.register()
    except Exception:
        numeric_hover.unregister()
        numeric_ladder.unregister()
        numeric_tools.unregister()
        preferences.on_change = None
        preferences.unregister()
        raise
    _enabled = True


def unregister():
    global _enabled
    if not _enabled:
        return
    preferences.on_change = None
    numeric_hover.unregister()
    numeric_ladder.unregister()
    numeric_tools.unregister()
    ui_theme.reset()
    _enabled = False
    preferences.unregister()
