"""原生数字按钮的被动快捷栏；仅消费快捷栏自身的点击。"""
import bpy
import blf
import gpu
from time import monotonic
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader
from . import preferences as _preferences
from . import numeric_tools as _api
from . import ui_theme


_enabled = False
_epoch = globals().get('_epoch', 0)
_states = {}
_observers = {}
_handles = {}
_pending = {}
_actions = set()
_keymaps = []
_classes_registered = False
_PASSIVE = {'NQA_OT_numeric_hover_observer', 'CAMERA_MOTION_OT_toggle_content_source_edit'}
_DWELL_SECONDS = .5


def _ptr(value):
    try:
        return value.as_pointer() if value is not None else 0
    except ReferenceError:
        return 0


def _inside(point, rect):
    x, y = point
    left, bottom, width, height = rect
    return left <= x < left + width and bottom <= y < bottom + height


def _signature(value):
    if not isinstance(value, tuple) or len(value) != 3:
        return None
    owner, path, index = value
    return (_ptr(owner), str(path), int(index)) if _ptr(owner) else None


def _native_identity(value):
    """撤销可替换 RNA 地址；用原生 ID 名称/路径识别 LAST_ACTIVE 的同一旧字段。"""
    if not isinstance(value, tuple) or len(value) != 3:
        return None
    owner, path, index = value
    if isinstance(owner, bpy.types.ID):
        library = owner.library.filepath if owner.library else ''
        return owner.bl_rna.identifier, library, owner.name_full, path, index
    return _signature(value)


def _scope(window, area, region):
    return (_ptr(window.scene), _ptr(window.screen), _ptr(window.workspace),
            _ptr(window.view_layer), _ptr(window.view_layer.objects.active),
            _ptr(window.scene.camera), _ptr(area), _ptr(region), _ptr(area.spaces.active),
            area.x, area.y, area.width, area.height, _preferences.revision,
            ui_theme.scale(), ui_theme.font_size())


def _at(window, point):
    # Modal 的 context.region 属于启动区域，鼠标实际所在区域用窗口坐标解析。
    for area in window.screen.areas:
        for region in sorted(area.regions, key=lambda region: region.type == 'WINDOW'):
            if region.type not in {'WINDOW', 'UI', 'HEADER', 'TOOL_HEADER'} or region.width < 2:
                continue
            if _inside(point, (region.x, region.y, region.width, region.height)):
                return area, region
    return None, None


def _redraw(state):
    try:
        # 已关闭 window 的 RNA 可短暂仍可读；不能给逻辑销毁的 screen 标脏。
        if _ptr(state.get('window')) not in {_ptr(window) for window in bpy.context.window_manager.windows}:
            return
        for area in getattr(getattr(state.get('window'), 'screen', None), 'areas', ()):
            area.tag_redraw()
    except ReferenceError:
        return


def _hide(state):
    _cancel_candidate(state)
    _pending.pop(_ptr(state.get('window')), None)
    token = state.pop('token', None)
    if token is not None and state.get('source_hotzone') and state.get('native_identity'):
        state['require_origin'] = (state['native_identity'], _ptr(state.get('region')), state['source_hotzone'])
    if state.get('signature') and state.get('scope'):
        state.setdefault('blocked', {})[state['scope']] = state['signature']
    state.pop('rect', None)
    state.pop('hotzone', None)
    state.pop('corridor', None)
    state['pressed'] = None
    state.pop('action', None)
    if token is not None:
        _api._targets.pop(token, None)
    _redraw(state)


def _valid(state):
    if not state.get('token'):
        return False
    try:
        window, area, region = state['window'], state['area'], state['region']
        if _scope(window, area, region) != state['scope']:
            return False
        _api._resolve(state['token'])
        return True
    except (ReferenceError, AttributeError, RuntimeError, ValueError, TypeError, KeyError):
        return False


def _cancel_candidate(state, prune_timer=True):
    candidate = state.pop('candidate', None)
    if candidate:
        _api._targets.pop(candidate['token'], None)
    if (prune_timer and bpy.app.timers.is_registered(_dwell_tick)
            and not any(value.get('candidate') for value in _states.values())):
        bpy.app.timers.unregister(_dwell_tick)


def _candidate_valid(state):
    candidate = state.get('candidate')
    if not candidate or candidate['epoch'] != _epoch:
        return False
    try:
        if (state.get('point') != candidate['point']
                or _scope(state['window'], candidate['area'], candidate['region']) != candidate['scope']):
            return False
        _api._resolve(candidate['token'])
        return True
    except (ReferenceError, AttributeError, RuntimeError, ValueError, TypeError, KeyError):
        return False


def _set_candidate(state, token, signature, area, region, point, native):
    scope = _scope(state['window'], area, region)
    candidate = state.get('candidate')
    if (candidate and candidate['point'] == point and candidate['signature'] == signature
            and candidate['scope'] == scope and _candidate_valid(state)):
        # 原生重绘也会派发同坐标 MOUSEMOVE；鼠标没动时保留原截止时间。
        if token != candidate['token']:
            _api._targets.pop(token, None)
        return
    _cancel_candidate(state)
    # 候选阶段也需真实区域，短时 observer 从此上下文启动。
    state.update(point=point, area=area, region=region)
    state['candidate'] = dict(token=token, signature=signature, area=area, region=region,
                              point=point, native=native, epoch=_epoch,
                              scope=scope, due=monotonic() + _preferences.get().hover_delay)
    if not bpy.app.timers.is_registered(_dwell_tick):
        bpy.app.timers.register(_dwell_tick, first_interval=max(.001, _preferences.get().hover_delay))
    _begin_observer(state)


def _dwell_tick():
    if not _enabled:
        return None
    now, earliest = monotonic(), None
    live = {_ptr(window) for window in bpy.context.window_manager.windows}
    for key, state in tuple(_states.items()):
        candidate = state.get('candidate')
        if not candidate:
            continue
        if key not in live or not _candidate_valid(state):
            _cancel_candidate(state, prune_timer=False)
            continue
        if candidate['due'] > now:
            earliest = candidate['due'] if earliest is None else min(earliest, candidate['due'])
            continue
        try:
            window, area, region = state['window'], candidate['area'], candidate['region']
            current_area, current_region = _at(window, candidate['point'])
            if _ptr(current_area) != _ptr(area) or _ptr(current_region) != _ptr(region):
                _cancel_candidate(state, prune_timer=False)
                continue
            with bpy.context.temp_override(window=window, area=area, region=region):
                native = getattr(bpy.context, 'property', None)
                if _native_busy(bpy.context, window) or _signature(native) != candidate['signature']:
                    _cancel_candidate(state, prune_timer=False)
                    continue
                # 停留期不持有显示状态；到期再校验原生目标，防止 PG 重排/切场景改错值。
                state.pop('candidate', None)
                _show(state, candidate['token'], candidate['signature'], area, region,
                      candidate['point'], native)
        except (ReferenceError, AttributeError, RuntimeError, ValueError, TypeError, KeyError):
            _cancel_candidate(state, prune_timer=False)
            _api._targets.pop(candidate['token'], None)
    return max(.001, earliest - monotonic()) if earliest is not None else None


def _native_busy(context, window):
    if getattr(context, 'region_popup', None) is not None:
        return True
    for operator in getattr(window, 'modal_operators', ()):
        if operator is None:
            # unregister 同步释放 operator，DO_FREE handler 在当前循环暂保留空项。
            continue
        # modal_operators 是原生 RNA 包装，identifier 比 Python self 身份可靠。
        if operator.bl_rna.identifier not in _PASSIVE:
            return True
    return False


def _draw():
    context = bpy.context
    region = context.region
    if region is None or not _enabled or _native_busy(context, context.window):
        return
    state = _states.get(_ptr(context.window))
    if not state or not state.get('rect') or not _valid(state):
        return
    left, bottom, width, height = state['rect']
    if not (region.x < left + width and region.x + region.width > left
            and region.y < bottom + height and region.y + region.height > bottom):
        return
    style = ui_theme.palette(context)
    scale = style['scale']
    local_rect = (left - region.x, bottom - region.y, width, height)
    previous_blend = gpu.state.blend_get()
    gpu.state.blend_set('ALPHA')
    try:
        ui_theme.draw_card(local_rect, style)
        choices = _preferences.operations(context)
        count = len(choices)
        for index, (_operation, label, _description) in enumerate(choices):
            x, y = left - region.x + index * width / count, bottom - region.y
            step = width / count
            hovered = _inside(state.get('point', (-1, -1)), (left + index * step, bottom, step, height))
            pressed = state.get('pressed') == index
            inset = max(.75, scale)
            corners = (index == 0, index == count - 1, index == count - 1, index == 0)
            ui_theme.draw_item((x + inset, y + inset, step - inset * 2, height - inset * 2),
                               style, selected=pressed, hovered=hovered, corners=corners)
            if index:
                ui_theme.draw_separator(x, y + height * .25, height * .5, style)
            ui_theme.draw_text(label, (x, y, step, height), style, selected=pressed)
    finally:
        gpu.state.blend_set(previous_blend)


def _ensure_draws(window, rect):
    left, bottom, width, height = rect
    for area in window.screen.areas:
        cls = type(area.spaces.active)
        if not hasattr(cls, 'draw_handler_add'):
            continue
        for region in area.regions:
            if region.type not in {'WINDOW', 'UI', 'HEADER', 'TOOL_HEADER'} or region.width < 2:
                continue
            if not (region.x < left + width and region.x + region.width > left
                    and region.y < bottom + height and region.y + region.height > bottom):
                continue
            key = (cls, region.type)
            if key in _handles:
                continue
            try:
                _handles[key] = cls.draw_handler_add(_draw, (), region.type, 'POST_PIXEL')
            except (ValueError, TypeError, RuntimeError):
                # 某些 Space 不提供 UI region；其真实 WINDOW/HEADER 仍可绘制。
                continue


def _toolbar_rect(bounds, point, scale):
    """仅跨来源 Area 内的 UI/WINDOW 区域，避免相邻 Space 裁剪按钮。"""
    area_x, area_y, area_width, area_height = bounds
    # 四个按钮只保留文字与原生按钮边距，不使用固定宽度撑满浮条。
    blf.size(0, ui_theme.font_size(bpy.context))
    cell = max(34 * scale, max(blf.dimensions(0, label)[0] for _op, label, _desc in _preferences.operations()) + 16 * scale)
    width, height = min(4 * cell, max(1, area_width - 8)), 26 * scale
    x, y = point
    left = max(area_x + 4, min(x - width / 2, area_x + area_width - width - 4))
    # 原生 tooltip 默认位于字段下方；快捷栏优先放上方，保留原生提示设置。
    bottom = y + 12 * scale
    if bottom + height > area_y + area_height - 4:
        bottom = max(area_y + 4, y - height - 12 * scale)
    return left, bottom, width, height


def _show(state, token, signature, area, region, point, native):
    scale = ui_theme.scale(bpy.context)
    rect = _toolbar_rect((area.x, area.y, area.width, area.height), point, scale)
    left, bottom, width, height = rect
    x, y = point
    nearest_edge = bottom if bottom >= y else bottom + height
    state.update(token=token, signature=signature, native=native,
                 native_identity=_native_identity(native), area=area, region=region,
                 scope=_scope(state['window'], area, region), rect=rect,
                 hotzone=(x - 85 * scale, y - 9 * scale, 170 * scale, 18 * scale),
                 corridor=(left, min(y, nearest_edge) - 2 * scale,
                           width, abs(nearest_edge - y) + 4 * scale), pressed=None)
    state['source_hotzone'] = state['hotzone']
    state.pop('require_origin', None)
    _ensure_draws(state['window'], rect)
    _redraw(state)
    _begin_observer(state)


def _begin_observer(state):
    if (bpy.app.background or _ptr(state['window']) in _observers
            or not (_valid(state) or _candidate_valid(state))):
        return
    with bpy.context.temp_override(window=state['window'], area=state['area'], region=state['region']):
        bpy.ops.nqa.numeric_hover_observer('INVOKE_DEFAULT')


def _schedule_hover(state, point):
    # 真实坐标位移重新计时；同坐标事件仍读原生目标但不延后截止时间。
    if point != state.get('point'):
        _cancel_candidate(state)
    state['point'] = point
    key = _ptr(state['window'])
    _pending[key] = (_epoch, point)
    if not bpy.app.timers.is_registered(_hover_tick):
        bpy.app.timers.register(_hover_tick, first_interval=0.0)


def _hover_tick():
    requests = dict(_pending)
    _pending.clear()
    if not _enabled or not _preferences.get().hover_enabled:
        return None
    for key, (epoch, point) in requests.items():
        state = _states.get(key)
        if state is None or epoch != _epoch or state.get('action'):
            continue
        try:
            window = state['window']
            area, region = _at(window, point)
            if area is None:
                _hide(state)
                continue
            with bpy.context.temp_override(window=window, area=area, region=region):
                if _native_busy(bpy.context, window):
                    _hide(state)
                    continue
                if state.get('token') and (_inside(point, state['rect']) or _inside(point, state['corridor'])):
                    if not _valid(state):
                        _hide(state)
                    continue
                value = getattr(bpy.context, 'property', None)
                signature = _signature(value)
                scope = _scope(window, area, region)
                origin = state.get('require_origin')
                if origin and _native_identity(value) == origin[0]:
                    # Undo/切机位可能使旧 scope 改变，但 LAST_ACTIVE 仍在。
                    # 仅回到原框的已知热点可重开，不能在空白凭同旧 tuple 重锚。
                    if _ptr(region) != origin[1] or not _inside(point, origin[2]):
                        continue
                    state.setdefault('blocked', {}).pop(scope, None)
                elif signature is not None:
                    state.pop('require_origin', None)
                if state.get('token'):
                    if signature == state['signature'] and scope == state['scope'] and _valid(state):
                        continue
                    _hide(state)
                blocked = state.setdefault('blocked', {})
                if signature is None:
                    _cancel_candidate(state)
                    blocked.pop(scope, None)
                    state.pop('require_origin', None)
                    continue
                if blocked.get(scope) == signature:
                    continue
                # 原生 getter 会回退 LAST_ACTIVE；超出固定热点后，同一旧字段禁止跟随鼠标重开。
                token = _api.capture_hover(value)
                _set_candidate(state, token, signature, area, region, point, value)
        except (ReferenceError, AttributeError, RuntimeError, ValueError, TypeError, KeyError):
            _hide(state)
    return None


def _perform(state, token, operation, window, area, region, scope, epoch):
    try:
        if not _enabled or epoch != _epoch or _scope(window, area, region) != scope:
            _hide(state)
            return None
        with bpy.context.temp_override(window=window, area=area, region=region):
            if _native_busy(bpy.context, window):
                _hide(state)
                return None
            # 独立 timer 入口执行带 UNDO 的原操作器，不在 observer.modal 内写数据。
            # Python timer 的操作器调用需公开 undo 参数 True，才进入原生撤销栈。
            result = bpy.ops.nqa.numeric_quick('EXEC_DEFAULT', True, target_token=token, operation=operation)
        if result == {'FINISHED'} and _scope(window, area, region) == scope and state.get('token') == token:
            # 匿名 PG 的快照会因本次写入失效；重新捕获而非长期复用旧 token。
            refreshed = _api.capture_hover(state['native'])
            _api._targets.pop(token, None)
            state['token'] = refreshed
        if _valid(state):
            _begin_observer(state)
            _redraw(state)
        else:
            _hide(state)
        if result != {'FINISHED'}:
            print('Hover operation did not change the target', operation, flush=True)
    except (ReferenceError, AttributeError, RuntimeError, ValueError, TypeError, KeyError) as error:
        _hide(state)
        print('Hover operation was not applied', error, flush=True)
    finally:
        state.pop('action', None)
    return None


def _queue_action(state, args):
    state['action'] = True
    def perform():
        _actions.discard(perform)
        return _perform(state, *args)
    _actions.add(perform)
    bpy.app.timers.register(perform, first_interval=0.0)


class NQA_OT_numeric_hover_capture(bpy.types.Operator):
    bl_idname = 'nqa.numeric_hover_capture'
    bl_label = 'Capture Hovered Value'
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        if _enabled and _preferences.get(context).hover_enabled and context.window is not None:
            live = {_ptr(window) for window in context.window_manager.windows}
            for key in set(_states) - live:
                _hide(_states.pop(key))
                _observers.pop(key, None)
                _pending.pop(key, None)
            state = _states.setdefault(_ptr(context.window), {'window': context.window, 'blocked': {}, 'pressed': None})
            _schedule_hover(state, (event.mouse_x, event.mouse_y))
        # 原生按钮先处理鼠标，timer 下一轮只读 property，不抢焦点。
        return {'FINISHED', 'PASS_THROUGH'}


class NQA_OT_numeric_hover_observer(bpy.types.Operator):
    bl_idname = 'nqa.numeric_hover_observer'
    bl_label = 'Observe Hovered Value'
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        state = _states.get(_ptr(context.window))
        if not _enabled or not state or not (_valid(state) or _candidate_valid(state)):
            return {'CANCELLED'}
        self._epoch = _epoch
        self._window = context.window
        self._window_key = _ptr(context.window)
        self._accepted_press = False
        self._timer = context.window_manager.event_timer_add(.2, window=self._window)
        _observers[self._window_key] = self
        _states.setdefault(self._window_key, {'window': self._window, 'blocked': {}, 'pressed': None})
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        key = self._window_key
        if _observers.get(key) is self:
            _observers.pop(key, None)

    def modal(self, context, event):
        state = _states.get(self._window_key)
        accepted = getattr(self, '_accepted_press', False)
        paired_release = accepted and event.type == 'LEFTMOUSE' and event.value == 'RELEASE'
        if paired_release:
            self._accepted_press = False
            index = state.get('pressed') if state else None
            if (_enabled and self._epoch == _epoch and state and index is not None
                    and _valid(state) and not _native_busy(context, self._window)
                    and _inside((event.mouse_x, event.mouse_y), state['rect'])):
                current = min(3, int((event.mouse_x - state['rect'][0]) / (state['rect'][2] / 4)))
                if current == index:
                    args = (state['token'], _api.operations(context)[index][0], self._window,
                            state['area'], state['region'], state['scope'], _epoch)
                    state['pressed'] = None
                    _queue_action(state, args)
                    self._finish(context)
                    return {'FINISHED'}
            if state:
                _hide(state)
            self._finish(context)
            # 已消费的 PRESS 必须配对消费 RELEASE，即使外部切场景使目标失效。
            return {'FINISHED'}
        if not _enabled or self._epoch != _epoch:
            if accepted and event.type != 'WINDOW_DEACTIVATE':
                return {'PASS_THROUGH'}
            self._finish(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        if state is None:
            if accepted and event.type != 'WINDOW_DEACTIVATE':
                return {'PASS_THROUGH'}
            self._finish(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        point = (event.mouse_x, event.mouse_y)
        if _native_busy(context, self._window):
            _hide(state)
            if accepted and event.type != 'WINDOW_DEACTIVATE':
                return {'PASS_THROUGH'}
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}
        if state.get('candidate'):
            if not _candidate_valid(state):
                _cancel_candidate(state)
                self._finish(context)
                return {'FINISHED', 'PASS_THROUGH'}
            if event.type == 'MOUSEMOVE':
                _schedule_hover(state, point)
                return {'PASS_THROUGH'}
            if event.type in {'TIMER', 'INBETWEEN_MOUSEMOVE'}:
                return {'PASS_THROUGH'}
            # 候选期必须看见原生编辑的 PRESS；中键交独立原生 keymap 入口。
            _hide(state)
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}
        if state.get('token') and not _valid(state):
            _hide(state)
        if not state.get('token'):
            if accepted and event.type != 'WINDOW_DEACTIVATE':
                return {'PASS_THROUGH'}
            if event.type not in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'TIMER'}:
                _hide(state)
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE':
            if point == state.get('point'):
                # 同坐标重绘不改变悬停状态，也无需再次重绘；事件仍交给原生 UI。
                return {'PASS_THROUGH'}
            state['point'] = point
            if state.get('token') and not any(_inside(point, state[name]) for name in ('rect', 'hotzone', 'corridor')):
                _hide(state)
                if accepted:
                    return {'PASS_THROUGH'}
                self._finish(context)
                _schedule_hover(state, point)
                return {'FINISHED', 'PASS_THROUGH'}
            _schedule_hover(state, point)
            if state.get('token'):
                _redraw(state)
            return {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if state.get('token') and _inside(point, state['rect']):
                state['pressed'] = min(3, int((point[0] - state['rect'][0]) / (state['rect'][2] / 4)))
                self._accepted_press = True
                return {'RUNNING_MODAL'}
            _hide(state)
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}
        if event.type == 'MIDDLEMOUSE' and _inside(point, state['rect']):
            # 浮条覆盖的下方可能是另一 XYZ 输入框，不把其自身中键透到底层。
            _hide(state)
            self._finish(context)
            return {'FINISHED'}
        if (event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'MIDDLEMOUSE', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}
                or (event.value == 'PRESS' and event.type not in {'TIMER', 'INBETWEEN_MOUSEMOVE'})):
            _hide(state)
            if accepted and event.type != 'WINDOW_DEACTIVATE':
                return {'PASS_THROUGH'}
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        self._finish(context)


@persistent
def _clear(_dummy):
    for state in tuple(_states.values()):
        _hide(state)
    _pending.clear()
    for callback in tuple(_actions):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    _actions.clear()


@persistent
def _loaded(_dummy):
    # 原生加载结束时旧 modal handler 已移除，不持有上一文件的窗口/RNA。
    _states.clear()
    _drop_observers()


def _drop_observers():
    live = {_ptr(window) for window in bpy.context.window_manager.windows}
    for key, observer in tuple(_observers.items()):
        if key not in live:
            continue  # 关闭窗口原生 cancel/free 已完成，只释放其 Python 引用。
        try:
            observer._epoch = -1  # 先失效原生仍待退出的 handler，避免配置变化后接管新候选。
            observer._finish(bpy.context)
        except (ReferenceError, RuntimeError, TypeError):
            # 已关闭的窗口由原生释放 handler/timer；不再持有其失效 RNA。
            continue
    _observers.clear()


def register():
    global _enabled, _epoch, _classes_registered
    if _enabled:
        return
    _enabled = True
    _epoch += 1
    if bpy.app.background:
        return
    bpy.utils.register_class(NQA_OT_numeric_hover_capture)
    try:
        bpy.utils.register_class(NQA_OT_numeric_hover_observer)
    except Exception:
        bpy.utils.unregister_class(NQA_OT_numeric_hover_capture)
        raise
    _classes_registered = True
    for handlers in (bpy.app.handlers.load_pre, bpy.app.handlers.save_pre, bpy.app.handlers.undo_pre):
        if _clear not in handlers:
            handlers.append(_clear)
    bpy.app.handlers.load_post.append(_loaded)
    keymap = bpy.context.window_manager.keyconfigs.addon.keymaps.new(name='Window', space_type='EMPTY')
    key = keymap.keymap_items.new(NQA_OT_numeric_hover_capture.bl_idname, type='MOUSEMOVE', value='ANY')
    _keymaps.append((keymap, key))


def unregister():
    global _enabled, _epoch, _classes_registered
    _enabled = False
    _epoch += 1
    _clear(None)
    for callback in (_hover_tick, _dwell_tick):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    for keymap, key in _keymaps:
        keymap.keymap_items.remove(key)
    _keymaps.clear()
    for (cls, region), handle in tuple(_handles.items()):
        try:
            cls.draw_handler_remove(handle, region)
        except (ValueError, RuntimeError, ReferenceError):
            continue
    _handles.clear()
    for handlers in (bpy.app.handlers.load_pre, bpy.app.handlers.save_pre, bpy.app.handlers.undo_pre):
        if _clear in handlers:
            handlers.remove(_clear)
    _states.clear()
    _drop_observers()
    if _loaded in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_loaded)
    # 原生 Operator unregister 同步清 modal handlers，但不调用 cancel；timer/引用先自清。
    if _classes_registered:
        bpy.utils.unregister_class(NQA_OT_numeric_hover_observer)
        bpy.utils.unregister_class(NQA_OT_numeric_hover_capture)
        _classes_registered = False
