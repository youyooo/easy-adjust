"""原生数值框的中键分档调值；预览不写帧，确认只提交一次撤销。"""

import math

import bpy
import blf
import gpu
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader

from . import numeric_hover as _hover
from . import numeric_tools as _api
from . import preferences as _preferences
from . import ui_theme


_enabled = False
_epoch = globals().get('_epoch', 0)
_states = {}
_operators = {}
_handles = {}
_keymaps = []
_registered = False
_ERRORS = (ReferenceError, AttributeError, RuntimeError, ValueError, TypeError, KeyError)
_ROW_HEIGHT = 24


def _scope(window, area, region):
    units = window.scene.unit_settings
    return _hover._scope(window, area, region) + (
        window.scene.frame_current_final, region.x, region.y, region.width, region.height,
        units.system, units.scale_length, units.length_unit, units.system_rotation,
        getattr(units, 'time_unit', ''), getattr(units, 'mass_unit', ''),
    )


def _valid(state):
    try:
        return (_enabled and state['epoch'] == _epoch
                and _scope(state['window'], state['area'], state['region']) == state['scope'])
    except _ERRORS:
        return False


def _levels(units):
    """整数也可按显示单位调节，但每档必须至少对应一个原生整数。"""
    steps = list(_preferences.levels())
    minimum = units.get('minimum_display_step', 0.0)
    if minimum:
        steps = [step for step in steps if step >= minimum - 1e-12]
        if not steps or not math.isclose(steps[-1], minimum):
            steps.append(minimum)
    return tuple(steps)


def _row(state, point):
    if state.get('dragging'):
        return state['level']
    left, bottom, width, _height = state['rect']
    if not left <= point[0] <= left + width:
        return None
    y = point[1] - bottom - state['footer']
    if not 0 <= y < len(state['levels']) * state['row_height']:
        return None
    return len(state['levels']) - 1 - int(y / state['row_height'])


def _draw():
    context = bpy.context
    state = _states.get(_hover._ptr(context.window))
    region = context.region
    if not state or region is None or not _valid(state):
        return
    left, bottom, width, height = state['rect']
    if not (region.x < left + width and region.x + region.width > left
            and region.y < bottom + height and region.y + region.height > bottom):
        return
    left -= region.x
    bottom -= region.y
    style = ui_theme.palette(context)
    scale = state['row_height'] / _ROW_HEIGHT
    style['font_size'] *= scale / style['scale']
    style['scale'] = scale
    previous_blend = gpu.state.blend_get()
    gpu.state.blend_set('ALPHA')
    try:
        ui_theme.draw_card((left, bottom, width, height), style, state['row_height'])
        unit = state['units'].get('label', '')
        indices = (state['level'],) if state['dragging'] else range(len(state['levels']))
        for index in indices:
            step = state['levels'][index]
            y = bottom if state['dragging'] else bottom + (len(state['levels']) - 1 - index) * state['row_height']
            active = index == state['level']
            inset = max(.75, scale)
            ui_theme.draw_item((left + inset, y + inset, width - inset * 2, state['row_height'] - inset * 2),
                               style, selected=active)
            ui_theme.draw_text(f'{step:g} {unit}'.rstrip(), (left, y, width, state['row_height']), style,
                               selected=active, padding=6 * scale)
    finally:
        gpu.state.blend_set(previous_blend)


def _ensure_draws(state):
    left, bottom, width, height = state['rect']
    for area in state['window'].screen.areas:
        cls = type(area.spaces.active)
        if not hasattr(cls, 'draw_handler_add'):
            continue
        for region in area.regions:
            if region.type not in {'WINDOW', 'UI', 'HEADER', 'TOOL_HEADER'} or region.width < 2:
                continue
            if not (region.x < left + width and region.x + region.width > left
                    and region.y < bottom + height and region.y + region.height > bottom):
                continue
            key = cls, region.type
            if key not in _handles:
                try:
                    _handles[key] = cls.draw_handler_add(_draw, (), region.type, 'POST_PIXEL')
                except (ValueError, TypeError, RuntimeError):
                    continue  # 该 Space 没有对应 region，不伪造绘制区域。


def _remove_draws():
    for (cls, region), handle in tuple(_handles.items()):
        try:
            cls.draw_handler_remove(handle, region)
        except (ValueError, RuntimeError, ReferenceError):
            continue
    _handles.clear()


def _discard_closed(key):
    """窗口销毁后只读独立会话，不再访问已经释放的 Operator/Area RNA。"""
    state = _states.pop(key, None)
    _operators.pop(key, None)
    if state:
        session = state['session']
        try:
            _api.cancel_drag(session)
        except _ERRORS as error:
            print('Value adjustment cleanup for a closed window failed', error, flush=True)
        _api._targets.pop(session.get('token'), None)
    if not _states:
        _remove_draws()


class NQA_OT_numeric_ladder(bpy.types.Operator):
    bl_idname = 'nqa.numeric_ladder'
    bl_label = 'Middle Mouse Value Ladder'
    bl_description = 'Hold MMB to select a step, then drag past either edge to adjust. Release to confirm or Esc/RMB to cancel'
    bl_options = {'INTERNAL', 'UNDO'}

    def invoke(self, context, event):
        live = {_hover._ptr(window) for window in context.window_manager.windows}
        for key in set(_operators) - live:
            _discard_closed(key)
        if (not _enabled or not _preferences.get(context).ladder_enabled or _operators or event.type != 'MIDDLEMOUSE'
                or event.value != 'PRESS' or context.window is None):
            return {'PASS_THROUGH'}
        window = context.window
        point = event.mouse_x, event.mouse_y
        area, region = _hover._at(window, point)
        if area is None:
            return {'PASS_THROUGH'}
        token = None
        try:
            with context.temp_override(window=window, area=area, region=region):
                if _hover._native_busy(bpy.context, window):
                    return {'PASS_THROUGH'}
                native = getattr(bpy.context, 'property', None)
                previous = _hover._states.get(_hover._ptr(window), {})
                origin = previous.get('require_origin')
                if (origin and _hover._native_identity(native) == origin[0]
                        and (_hover._ptr(region) != origin[1] or not _hover._inside(point, origin[2]))):
                    return {'PASS_THROUGH'}
                token = _api.capture_hover(native)
                units = _api.drag_units(token, bpy.context)
                original = _api._read(_api._resolve(token))
                session = _api.begin_drag(token, bpy.context)
                scope = _scope(window, area, region)
        except _ERRORS:
            if token is not None:
                _api._targets.pop(token, None)
            return {'PASS_THROUGH'}
        scale = ui_theme.scale(context)
        levels = _levels(units)
        level = min(range(len(levels)), key=lambda index: abs(math.log10(levels[index])))
        # 高倍率或编辑区较矮时让所有档位都可选，拖动灵敏度仍使用原界面倍率。
        row_height = min(_ROW_HEIGHT * scale, max(1, area.height - 8) / len(levels))
        layout_scale = row_height / _ROW_HEIGHT
        # 只保留档位数字与单位，不让标题、说明或固定宽度撑大弹窗。
        blf.size(0, ui_theme.font_size(context) * layout_scale / scale)
        unit = units.get('label', '')
        width = max(blf.dimensions(0, f'{step:g} {unit}'.rstrip())[0] for step in levels) + 12 * layout_scale
        width = min(width, max(1, area.width - 8))
        height = row_height * len(levels)
        left = max(area.x + 4, min(point[0] - width / 2, area.x + area.width - width - 4))
        # 紧贴编辑区边缘打开时，起点也必须落在阶梯左右边界内。
        left = min(max(left, point[0] - width), point[0])
        bottom = max(area.y + 4, min(point[1] - (len(levels) - level - .5) * row_height,
                                   area.y + area.height - height - 4))
        self._window_key = _hover._ptr(window)
        self._epoch = _epoch
        self._timer = context.window_manager.event_timer_add(.1, window=window)
        self._session = session
        self._delta = 0.0
        self._original = original
        state = dict(window=window, area=area, region=region, scope=scope, epoch=_epoch,
                     session=session, levels=levels, units=units, actual=original,
                     rect=(left, bottom, width, height), row_height=row_height, footer=0,
                     level=level, scale=scale, dragging=False)
        _states[self._window_key] = state
        _operators[self._window_key] = self
        if previous:
            _hover._hide(previous)
        _ensure_draws(state)
        _hover._redraw(state)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _finish(self, context, rollback):
        state = _states.pop(self._window_key, None)
        if rollback and self._session is not None:
            try:
                restored = _api.cancel_drag(self._session, context)
                if not restored and self._session.get('warning'):
                    self.report({'WARNING'}, 'The target changed. Its current value was preserved. ' + self._session['warning'])
            except _ERRORS as error:
                self.report({'WARNING'}, 'Adjustment stopped because the original target changed. ' + str(error))
        if self._session is not None:
            _api._targets.pop(self._session.get('token'), None)
        self._session = None
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if _operators.get(self._window_key) is self:
            _operators.pop(self._window_key, None)
        if not _states:
            _remove_draws()
        if state:
            _hover._redraw(state)

    def modal(self, context, event):
        state = _states.get(self._window_key)
        if not state or not _valid(state):
            self._finish(context, rollback=True)
            return {'CANCELLED'}
        if event.type == 'WINDOW_DEACTIVATE' or (
                event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS'):
            self._finish(context, rollback=True)
            return {'CANCELLED'}
        try:
            with context.temp_override(window=state['window'], area=state['area'], region=state['region']):
                if event.type == 'MIDDLEMOUSE' and event.value == 'RELEASE':
                    changed = _api.commit_drag(self._session, bpy.context)
                    warning = self._session.get('warning')
                    self._finish(context, rollback=False)
                    if warning:
                        self.report({'WARNING'}, warning)
                    return {'FINISHED'} if changed else {'CANCELLED'}
                if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
                    point = event.mouse_x, event.mouse_y
                    if not state['dragging']:
                        left, bottom, width, _height = state['rect']
                        right = left + width
                        if left <= point[0] <= right:
                            level = _row(state, point)
                            if level is not None and level != state['level']:
                                state['level'] = level
                                _hover._redraw(state)
                            return {'RUNNING_MODAL'}
                        # 从越过的边缘计量，选档时的横向位移不计入数值变化。
                        self._anchor_x = left if point[0] < left else right
                        # 收起其余档位，后续拖动始终使用同一档位和锚点。
                        bottom += (len(state['levels']) - 1 - state['level']) * state['row_height']
                        layout_scale = state['row_height'] / _ROW_HEIGHT
                        blf.size(0, ui_theme.font_size(context) * layout_scale / state['scale'])
                        text = f"{state['levels'][state['level']]:g} {state['units'].get('label', '')}".rstrip()
                        width = min(width, blf.dimensions(0, text)[0] + 12 * layout_scale)
                        state['rect'] = left, bottom, width, state['row_height']
                        state['dragging'] = True
                    distance = point[0] - self._anchor_x
                    ticks = distance / (_preferences.get(context).drag_pixels * state['scale'])
                    if state['units']['kind'] == 'INT':
                        ticks = math.trunc(ticks)
                    requested = ticks * state['levels'][state['level']] * state['units']['raw_per_display']
                    state['actual'] = _api.preview_drag(self._session, requested)
                    self._delta = state['actual'] - self._original
                    _hover._redraw(state)
                elif event.type == 'TIMER':
                    # 只在这次中键操作期间复验目标，不创建永久后台轮询。
                    _api.preview_drag(self._session, self._delta)
        except _ERRORS as error:
            self.report({'WARNING'}, 'Adjustment stopped. ' + str(error))
            self._finish(context, rollback=True)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self._finish(context, rollback=True)


@persistent
def abort_all(_unused=None):
    """保存、加载、停用前取消未确认预览；注销原生类型不会替我们调用 cancel。"""
    live = {_hover._ptr(window) for window in bpy.context.window_manager.windows}
    for key, operator in tuple(_operators.items()):
        if key in live:
            try:
                operator._finish(bpy.context, rollback=True)
            except _ERRORS as error:
                print('Middle mouse adjustment cleanup failed', error, flush=True)
        else:
            _discard_closed(key)
    _operators.clear()
    _states.clear()
    _remove_draws()


def register():
    global _enabled, _epoch, _registered
    if _enabled:
        return
    _enabled = True
    _epoch += 1
    if bpy.app.background:
        return
    bpy.utils.register_class(NQA_OT_numeric_ladder)
    _registered = True
    # 侧栏/属性编辑器会先处理 View2D 的中键平移；数值入口必须排在平移前。
    # 非数值框返回 PASS_THROUGH，原生中键导航继续执行。
    for name in ('View2D Buttons List', 'View2D', 'Window'):
        keymap = bpy.context.window_manager.keyconfigs.addon.keymaps.new(name=name, space_type='EMPTY')
        key = keymap.keymap_items.new(NQA_OT_numeric_ladder.bl_idname, type='MIDDLEMOUSE',
                                     value='PRESS', head=True)
        _keymaps.append((keymap, key))
    for handlers in (bpy.app.handlers.load_pre, bpy.app.handlers.save_pre, bpy.app.handlers.undo_pre):
        if abort_all not in handlers:
            handlers.append(abort_all)


def unregister():
    global _enabled, _epoch, _registered
    _enabled = False
    _epoch += 1
    abort_all()
    for keymap, key in _keymaps:
        keymap.keymap_items.remove(key)
    _keymaps.clear()
    for handlers in (bpy.app.handlers.load_pre, bpy.app.handlers.save_pre, bpy.app.handlers.undo_pre):
        if abort_all in handlers:
            handlers.remove(abort_all)
    if _registered:
        bpy.utils.unregister_class(NQA_OT_numeric_ladder)
        _registered = False
