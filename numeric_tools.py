"""悬停原生数值输入框时提供比例运算；目标仅通过公开 RNA 解析。"""

import ast
import itertools
import math
import re
import struct
from sys import float_info
from types import SimpleNamespace

import bpy
from idprop.types import IDPropertyArray, IDPropertyGroup
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, StringProperty
from bpy.types import Operator
from . import preferences as _preferences


OPERATIONS = (
    ('QUARTER', '−1', 'Multiply the current component by 0.25'),
    ('HALF', '−½', 'Multiply the current component by 0.5'),
    ('ONE_HALF', '+½', 'Multiply the current component by 1.5'),
    ('DOUBLE', '+1', 'Multiply the current component by 2'),
)
_targets = {}
_serial = itertools.count(1)
_registered = False
_EMPTY_COMPONENTS = (('CHOOSE', 'Choose a component', 'Select the component to adjust'),)


def _read(target):
    pointer, name = target['pointer'], target['name']
    value = pointer[name] if target['custom'] else getattr(pointer, name)
    if target['array'] and target['index'] < 0:
        raise ValueError('Select a numeric component to adjust')
    return value[target['index']] if target['array'] else value


def _custom_path(name):
    return '["' + bpy.utils.escape_identifier(name) + '"]'


def _path_segments(path):
    """将相对 RNA 路径转成固定属性/下标，绝不执行其中的文本。"""
    root = '_cm_hover_root'
    current = ast.parse(root + ('' if path.startswith('[') else '.') + path, mode='eval').body
    segments = []
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        if len(segments) >= 64:
            raise ValueError('The hovered property path is too deep')
        if isinstance(current, ast.Subscript):
            key = current.slice
            if (not isinstance(key, ast.Constant) or isinstance(key.value, bool)
                    or not isinstance(key.value, (str, int))
                    or isinstance(key.value, int) and key.value < 0):
                raise ValueError('The hovered property path contains an invalid index')
            segments.append(('key', key.value))
        else:
            segments.append(('attr', current.attr))
        current = current.value
    if not isinstance(current, ast.Name) or current.id != root:
        raise ValueError('The hovered property path is invalid')
    return tuple(reversed(segments))


def _rna_path(segments):
    # Blender RNA 的字符串下标要求双引号，不能用 ast.unparse 的单引号表达式。
    path = ''
    for kind, value in segments:
        if kind == 'attr':
            path += ('.' if path else '') + value
        else:
            path += _custom_path(value) if isinstance(value, str) else '[' + str(value) + ']'
    return path


def _stored_snapshot(value, depth=0):
    """匿名 PG 没有可读的稳定身份，保守记录公开 IDProperty 存储内容，不创建永久标记。"""
    if depth > 32:
        raise ValueError('The collection item is too deeply nested to validate safely')
    if value is None or isinstance(value, (bool, int, str)):
        return (type(value).__name__, value)
    if isinstance(value, float):
        return ('float', value.hex())
    if isinstance(value, bpy.types.ID):
        return ('ID', value.as_pointer())
    if isinstance(value, (IDPropertyGroup, bpy.types.PropertyGroup, dict)):
        return ('group', tuple((name, _stored_snapshot(child, depth + 1))
                               for name, child in sorted(value.items())))
    if isinstance(value, (IDPropertyArray, tuple, list)):
        return ('array', tuple(_stored_snapshot(child, depth + 1) for child in value))
    raise ValueError('The collection item data type cannot be validated safely')


def _collection_snapshot(collection):
    members = tuple(collection)
    identities = tuple(tuple((name, ('ID', value.as_pointer()) if isinstance(value, bpy.types.ID) else value)
                             for name, value in sorted(member.items())
                             if isinstance(value, bpy.types.ID) or isinstance(value, str) and value)
                       for member in members)
    addresses = tuple(member.as_pointer() for member in members)
    if all(identities) and len(set(identities)) == len(identities):
        # 唯一字符串/ID 引用随真实条目移动；普通数值更新不影响业务机位的身份。
        return ('identity', tuple(zip(addresses, identities)))
    return ('payload', tuple((address, _stored_snapshot(member))
                              for address, member in zip(addresses, members)))


def _collection_guards(owner, pointer_path):
    guards = []
    if owner is None or not pointer_path:
        return ()
    segments = _path_segments(pointer_path)
    for index, (kind, _value) in enumerate(segments):
        if kind != 'key':
            continue
        path = _rna_path(segments[:index])
        collection = owner.path_resolve(path)
        # 原生 ID/ShapeKey 等集合保有真实独立指针；只保护 CollectionProperty 的 PG 槽位。
        if len(collection) and isinstance(collection[0], bpy.types.PropertyGroup):
            guards.append((path, _collection_snapshot(collection)))
    return tuple(guards)


def _custom_property(pointer, name):
    """公开 context.property 不暴露自定义 PropertyRNA，按 IDProperty 实际类型读取元数据。"""
    value = pointer[name]
    storage = getattr(value, 'typecode', None)
    array = storage is not None
    if array:
        if storage not in {'i', 'f', 'd'} or not len(value):
            raise ValueError('The custom property is not a numeric vector')
        kind = 'INT' if storage == 'i' else 'FLOAT'
        candidates = tuple(value)
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('The custom property is not an integer or a float')
        kind = 'INT' if isinstance(value, int) else 'FLOAT'
        candidates = (value,)
    expected = int if kind == 'INT' else float
    if any(isinstance(current, bool) or not isinstance(current, expected) or not math.isfinite(current)
           for current in candidates):
        raise ValueError('The custom property value is invalid')
    metadata = pointer.id_properties_ui(name).as_dict()
    limit = (-(2 ** 31), 2 ** 31 - 1) if kind == 'INT' else (-float_info.max, float_info.max)
    minimum, maximum = metadata.get('min', limit[0]), metadata.get('max', limit[1])
    if (not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float))
            or not math.isfinite(minimum) or not math.isfinite(maximum) or minimum > maximum):
        raise ValueError('The custom property range is invalid')
    return SimpleNamespace(identifier=name, name=name, type=kind, is_array=array,
                           array_dimensions=(len(value), 0, 0) if array else (0, 0, 0),
                           is_readonly=pointer.is_property_readonly(_custom_path(name)),
                           is_animatable=pointer.id_data is not None,
                           hard_min=minimum, hard_max=maximum,
                           subtype=metadata.get('subtype', 'NONE'), custom_storage=storage,
                           unit={'ANGLE': 'ROTATION', 'DISTANCE': 'LENGTH', 'TIME': 'TIME'}.get(
                               metadata.get('subtype', 'NONE'), 'NONE'))


def _property_info(pointer, prop, index, allow_unselected=False, custom=None):
    """仅接受真实可编辑的数字 RNA；整向量、矩阵和颜色轮不做批量修改。"""
    if pointer is None or prop is None or prop.type not in {'FLOAT', 'INT'}:
        raise ValueError('The current button is not an integer or float field')
    name = prop.identifier
    custom = pointer.bl_rna.properties.get(name) is None if custom is None else custom
    if prop.is_readonly or pointer.is_property_readonly(_custom_path(name) if custom else name):
        raise ValueError('The numeric property is read-only')
    owner = pointer.id_data
    if owner is not None and not owner.is_editable:
        raise ValueError('The current data is not editable')
    array = prop.is_array
    value = pointer[name] if custom else getattr(pointer, name)
    if array:
        if sum(dimension > 0 for dimension in prop.array_dimensions) != 1:
            raise ValueError('Only individual numeric vector components are supported')
        if index >= len(value) or index < -1 or (index < 0 and not allow_unselected):
            raise ValueError('Hover over an individual vector component')
    elif index not in {-1, 0}:
        raise ValueError('The numeric component is no longer valid')
    target = dict(pointer=pointer, address=pointer.as_pointer(), name=name,
                  custom=custom, array=array, index=index if array else -1,
                  length=len(value) if array else 0, kind=prop.type,
                  minimum=prop.hard_min, maximum=prop.hard_max,
                  unit=getattr(prop, 'unit', 'NONE'),
                  animatable=prop.is_animatable, owner=owner,
                  custom_storage=getattr(prop, 'custom_storage', None),
                  pointer_path=(('' if isinstance(pointer, bpy.types.ID) else pointer.path_from_id())
                                if owner is not None else None))
    target['label'] = prop.name
    target['collection_guards'] = _collection_guards(owner, target['pointer_path'])
    candidates = tuple(value) if array and index < 0 else (_read(target),)
    for current in candidates:
        if isinstance(current, bool) or not isinstance(current, (int, float)) or not math.isfinite(current):
            raise ValueError('The current value is not a finite number')
    if array:
        if prop.subtype in {'COLOR', 'COLOR_GAMMA'}:
            labels = ('R', 'G', 'B', 'A')
        elif prop.subtype == 'QUATERNION':
            labels = ('W', 'X', 'Y', 'Z')
        else:
            labels = ('X', 'Y', 'Z') if len(value) <= 3 else ()
        target['components'] = _EMPTY_COMPONENTS + tuple(
            ('AXIS_' + str(axis), labels[axis] if axis < len(labels) else 'Component ' + str(axis + 1),
             'Adjust component ' + str(axis + 1) + ' only') for axis in range(len(value)))
    return target


def _capture(pointer, prop, index=-1, allow_unselected=False, custom=None):
    """浮层显示期间保留 RNA 目标，点击时不再依赖已经变化的 UI 上下文。"""
    target = _property_info(pointer, prop, index, allow_unselected=allow_unselected, custom=custom)
    token = str(next(_serial))
    _targets[token] = target
    # 缓存仅服务于短暂浮层；不写进 blend，也不长期持有曾经悬停的所有对象。
    while len(_targets) > 128:
        del _targets[next(iter(_targets))]
    return token


def capture_hover(property_tuple):
    """接收 native context.property 的 (owner/baseRNA, 相对路径, 当前轴)，不执行路径文本。"""
    try:
        if not isinstance(property_tuple, tuple) or len(property_tuple) != 3:
            raise ValueError('The pointer is not over a numeric field')
        owner, path, index = property_tuple
        if (not isinstance(owner, bpy.types.bpy_struct) or not isinstance(path, str) or not path
                or len(path) > 4096 or isinstance(index, bool) or not isinstance(index, int)):
            raise ValueError('The hovered property context is invalid')
        segments = _path_segments(path)
        if not segments or segments[-1][0] == 'key' and not isinstance(segments[-1][1], str):
            raise ValueError('The hovered path must refer to a single RNA property')
        name, custom = segments[-1][1], segments[-1][0] == 'key'
        pointer_path = _rna_path(segments[:-1])
        pointer = owner.path_resolve(pointer_path) if pointer_path else owner
        if not isinstance(pointer, bpy.types.bpy_struct):
            raise ValueError('The hovered path does not refer to RNA data')
        prop = _custom_property(pointer, name) if custom else pointer.bl_rna.properties.get(name)
        return _capture(pointer, prop, index, custom=custom)
    except (ReferenceError, AttributeError, TypeError, RuntimeError, SyntaxError, KeyError) as error:
        raise ValueError('The hovered value is no longer valid: ' + str(error)) from error


def _resolve(token):
    target = _targets.get(token)
    if target is None:
        raise ValueError('This numeric operation has expired. Hover over the field again')
    owner, name = target['owner'], target['name']
    if owner is not None:
        for path, snapshot in target.get('collection_guards', ()):
            if _collection_snapshot(owner.path_resolve(path)) != snapshot:
                raise ValueError('The owning list has changed. Hover over the field again')
        # CollectionProperty 扩容后旧 PG 的 as_pointer 仍可能返回旧地址，甚至悬空。
        # 先从稳定 ID 重新解析，地址改变就拒绝；禁止先读取旧 PG 的任何 RNA 属性。
        pointer = owner.path_resolve(target['pointer_path']) if target['pointer_path'] else owner
    else:
        pointer = target['pointer']
    if (pointer.as_pointer() != target['address']
            or pointer.is_property_readonly(_custom_path(name) if target['custom'] else name)):
        raise ValueError('The original property has changed or become read-only. Hover over the field again')
    target = dict(target, pointer=pointer)
    owner = pointer.id_data
    if owner is not None and not owner.is_editable:
        raise ValueError('The original data is no longer editable')
    if target['custom']:
        prop = _custom_property(pointer, name)
        if (prop.type != target['kind'] or prop.is_array != target['array']
                or target.get('custom_storage') is not None and prop.custom_storage != target['custom_storage']):
            raise ValueError('The original custom property type or value has changed')
        current_target = _property_info(pointer, prop, target['index'], allow_unselected=True, custom=True)
        if current_target['length'] != target['length']:
            raise ValueError('The original vector size has changed')
        target = current_target
    else:
        prop = pointer.bl_rna.properties.get(name)
        if prop is None or prop.type != target['kind'] or prop.is_array != target['array']:
            raise ValueError('The original numeric property type has changed')
        current_target = _property_info(pointer, prop, target['index'], allow_unselected=True)
        if current_target['length'] != target['length']:
            raise ValueError('The original vector size has changed')
        target = current_target
    return target


def _write(target, value):
    pointer, name = target['pointer'], target['name']
    if target['array']:
        values = pointer[name] if target['custom'] else getattr(pointer, name)
        values[target['index']] = value
    elif target['custom']:
        pointer[name] = value
    else:
        setattr(pointer, name, value)
    # PropertyGroup 的运行时数字也储存在所属 ID 内，显式标脏使原生撤销记录可靠。
    if target['owner'] is not None:
        target['owner'].update_tag()


def operations(context=None):
    return _preferences.operations(context)


def _scaled_value(target, operation, context=None):
    """按用户配置运算；加减使用输入框显示单位，乘除保持比例。"""
    kind, amount = _preferences.operation_value(operation, context)
    current = _read(target)
    if kind in {'ADD', 'SUBTRACT'}:
        units = _display_units(target, context if context is not None else bpy.context)
        delta = amount * units['raw_per_display']
        value = current + delta if kind == 'ADD' else current - delta
    elif kind == 'DIVIDE':
        value = current / amount
    else:
        value = current * amount
    return _bounded_value(target, value)


def _bounded_value(target, value):
    if not math.isfinite(value):
        raise ValueError('The result is outside the valid numeric range')
    value = min(target['maximum'], max(target['minimum'], value))
    if target['kind'] == 'INT':
        # 使用普通四舍五入（半数远离零），避免 Python 偶数舍入使整数运算难以预期。
        value = int(math.copysign(math.floor(abs(value) + 0.5), value))
        value = min(int(target['maximum']), max(int(target['minimum']), value))
    return value


def _owner_curves(owner):
    animation = getattr(owner, 'animation_data', None)
    action = animation.action if animation else None
    if action is None:
        return ()
    if getattr(action, 'is_action_layered', False):
        handle = getattr(animation, 'action_slot_handle', 0)
        return tuple(curve for layer in action.layers for strip in layer.strips
                     for bag in getattr(strip, 'channelbags', ()) if bag.slot_handle == handle
                     for curve in bag.fcurves)
    return tuple(action.fcurves)


def _key_path(target):
    pointer, name = target['pointer'], target['name']
    if target['custom']:
        return target['pointer_path'] + _custom_path(name)
    return pointer.path_from_id(name)


def _autokey(target, context):
    """与原生数字输入一致：自动关键帧只更新已动画化的当前分量，跳过驱动。"""
    scene = context.scene
    if scene is None or not scene.tool_settings.use_keyframe_insert_auto or not target['animatable']:
        return
    owner = target['owner']
    if owner is None:
        return
    path, index = _key_path(target), max(0, target['index'])
    animation = getattr(owner, 'animation_data', None)
    if animation and any(curve.data_path == path and curve.array_index == index
                         for curve in animation.drivers):
        return
    curve = next((curve for curve in _owner_curves(owner)
                  if curve.data_path == path and curve.array_index == index), None)
    if curve is None:
        return
    if curve.lock:
        raise ValueError('The animation channel is locked. No automatic keyframe was inserted')
    prefs, tools = context.preferences.edit, scene.tool_settings
    options = {'INSERTKEY_AVAILABLE'}
    if prefs.use_auto_keyframe_insert_needed:
        options.add('INSERTKEY_NEEDED')
    if tools.auto_keying_mode == 'REPLACE_KEYS':
        options.add('INSERTKEY_REPLACE')
    if tools.use_keyframe_cycle_aware:
        options.add('INSERTKEY_CYCLE_AWARE')
    if prefs.use_visual_keying:
        options.add('INSERTKEY_VISUAL')
    previous_frames = {float(point.co.x) for point in curve.keyframe_points}
    # Python API 原生处理 Action Slot/NLA 映射，但会禁用用户默认插值设置。
    # 仅为真正新增的点补上偏好；替换已有点时保留用户编辑过的插值/手柄。
    inserted = target['pointer'].keyframe_insert(
        data_path=(_custom_path(target['name'])
                   if target['custom'] else target['name']),
        index=target['index'], frame=scene.frame_current_final,
        options=options, keytype=tools.keyframe_type,
        group=curve.group.name if curve.group else '')
    if inserted:
        for point in curve.keyframe_points:
            if float(point.co.x) not in previous_frames:
                point.interpolation = prefs.keyframe_new_interpolation_type
                point.handle_left_type = point.handle_right_type = prefs.keyframe_new_handle_type
        curve.update()


_LENGTH_UNITS = {
    'KILOMETERS': ('km', 1000.0), 'METERS': ('m', 1.0), 'CENTIMETERS': ('cm', .01),
    'MILLIMETERS': ('mm', .001), 'MICROMETERS': ('µm', .000001),
    'MILES': ('mi', 1609.344), 'FEET': ('ft', .3048), 'INCHES': ('in', .0254),
    'THOU': ('thou', .0000254),
}
_PREFERRED_UNITS = {
    'MASS': ('mass_unit', {'TONNES': 't', 'KILOGRAMS': 'kg', 'GRAMS': 'g',
                          'MILLIGRAMS': 'mg', 'POUNDS': 'lb', 'OUNCES': 'oz'}),
    'TIME': ('time_unit', {'DAYS': 'd', 'HOURS': 'h', 'MINUTES': 'min', 'SECONDS': 's',
                          'MILLISECONDS': 'ms', 'MICROSECONDS': 'µs'}),
    'TEMPERATURE': ('temperature_unit', {'KELVIN': 'K', 'CELSIUS': '°C', 'FAHRENHEIT': '°F'}),
}


def drag_units(token, context):
    """返回显示步长到 RNA 原值的系数；当前值自适应单位在拖动开始时固定。"""
    return _display_units(_resolve(token), context)


def _display_units(target, context):
    scene = context.scene
    settings = scene.unit_settings if scene is not None else None
    system = settings.system if settings is not None else 'NONE'
    unit = target['unit']
    label, raw_per_display = '', 1.0
    if unit == 'ROTATION':
        radians = settings is not None and settings.system_rotation == 'RADIANS'
        label, raw_per_display = ('rad', 1.0) if radians else ('°', math.pi / 180)
    elif unit == 'CAMERA':
        label = 'mm'  # 镜头焦距的原值就是毫米，不受 Scene.scale_length 影响。
    elif unit == 'TIME':
        # 原生 button_is_unit 禁用 PROP_UNIT_TIME 换算，帧数输入框仍显示原值。
        pass
    elif unit != 'NONE' and settings is not None:
        power = {'LENGTH': 1, 'VELOCITY': 1, 'ACCELERATION': 1, 'AREA': 2,
                 'POWER': 2, 'VOLUME': 3, 'MASS': 3}.get(unit, 0)
        scale = settings.scale_length ** power if system != 'NONE' else 1.0
        preferred = None
        if unit in {'LENGTH', 'AREA', 'VOLUME'} and system != 'NONE':
            length = _LENGTH_UNITS.get(settings.length_unit)
            if length:
                exponent = {'LENGTH': 1, 'AREA': 2, 'VOLUME': 3}[unit]
                label = length[0] + ({1: '', 2: '²', 3: '³'}[exponent])
                raw_per_display = length[1] ** exponent / scale
                preferred = label
        if preferred is None and (system != 'NONE' or unit in {'TIME', 'TIME_ABSOLUTE'}):
            category = 'TIME' if unit == 'TIME_ABSOLUTE' else unit
            selector = _PREFERRED_UNITS.get(category)
            label = selector[1].get(getattr(settings, selector[0]), '') if selector else ''
            if not label:
                formatted = bpy.utils.units.to_string(system, unit, abs(_read(target)) * scale,
                                                       precision=6, split_unit=False)
                match = re.fullmatch(r'\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*(.*?)\s*', formatted)
                label = match.group(1) if match else ''
            if label:
                # 仅解析 Blender 自己的单位标签和固定数字，不接受用户表达式或 RNA 路径文本。
                delta = (bpy.utils.units.to_value(system, unit, '1' + label)
                         - bpy.utils.units.to_value(system, unit, '0' + label))
                raw_per_display = delta / scale
    if not math.isfinite(raw_per_display) or raw_per_display <= 0:
        raise ValueError('The current scene unit conversion is invalid')
    integer = target['kind'] == 'INT'
    return dict(label=label, raw_per_display=raw_per_display, kind=target['kind'],
                integer_step=1 if integer else 0, minimum_raw_step=1 if integer else 0,
                minimum_display_step=1 / raw_per_display if integer else 0)


def display_delta_to_raw(token, context, delta_display):
    if isinstance(delta_display, bool) or not isinstance(delta_display, (int, float)) or not math.isfinite(delta_display):
        raise ValueError('The drag distance is not a valid number')
    return delta_display * drag_units(token, context)['raw_per_display']


def _animation_signature(target):
    owner = target['owner']
    animation = getattr(owner, 'animation_data', None) if owner is not None else None
    if animation is None:
        return None, False
    path, index = _key_path(target), max(0, target['index'])
    action = animation.action
    curve = next((curve for curve in _owner_curves(owner)
                  if curve.data_path == path and curve.array_index == index), None)
    drivers = tuple(curve.as_pointer() for curve in animation.drivers
                    if curve.data_path == path and curve.array_index == index)
    signature = (action.as_pointer() if action else 0, getattr(animation, 'action_slot_handle', 0),
                 curve.as_pointer() if curve else 0, curve.lock if curve else False, drivers,
                 tuple(tuple(point.co) for point in curve.keyframe_points) if curve else ())
    return signature, bool(curve and not curve.lock and not drivers)


def _unit_settings_signature(scene):
    if scene is None:
        return None
    settings = scene.unit_settings
    return (settings.system, settings.system_rotation, settings.scale_length, settings.length_unit,
            settings.mass_unit, settings.time_unit, settings.temperature_unit,
            scene.render.fps, scene.render.fps_base)


def begin_drag(token, context):
    """快照当前目标与已有自动 K 通道；预览和取消均不修改动画。"""
    target = _resolve(token)
    original = _read(target)
    animation, keyable = _animation_signature(target)
    scene = context.scene
    return dict(token=token, original=original, expected=original, valid=True, done=False,
                scene=scene, frame=scene.frame_current_final if scene else None,
                units=drag_units(token, context), unit_settings=_unit_settings_signature(scene),
                animation=animation, autokey=bool(scene and scene.tool_settings.use_keyframe_insert_auto and keyable),
                warning='')


def _drag_target(session, context=None, *, for_cancel=False):
    if session.get('done') or not session.get('valid') and not for_cancel:
        raise ValueError('This numeric drag session has ended')
    scene = session['scene']
    if ((context is not None and context.scene != scene)
            or scene is not None and scene.frame_current_final != session['frame']
            or _unit_settings_signature(scene) != session['unit_settings']):
        raise ValueError('The scene, current frame, or display units have changed')
    target = _resolve(session['token'])
    if _read(target) != session['expected'] or _animation_signature(target)[0] != session['animation']:
        raise ValueError('The original value or animation channel was changed externally')
    return target


def _after_own_write(target):
    owner = target['owner']
    pointer = owner.path_resolve(target['pointer_path']) if owner is not None and target['pointer_path'] else owner or target['pointer']
    if pointer.as_pointer() != target['address']:
        raise ValueError('The numeric item became invalid during the write')
    segments = _path_segments(target['pointer_path']) if target['pointer_path'] else ()
    for path, before in target.get('collection_guards', ()):
        collection = owner.path_resolve(path)
        after = _collection_snapshot(collection)
        if before == after:
            continue
        if before[0] != 'payload' or after[0] != 'payload' or len(before[1]) != len(after[1]):
            raise ValueError('The owning list changed during the write')
        selector = segments[len(_path_segments(path))][1]
        selected = selector if isinstance(selector, int) else collection.find(selector)
        if selected < 0 or selected >= len(before[1]):
            raise ValueError('The original numeric item is no longer valid')
        expected = list(before[1])
        # 匿名 PG 仅允许本次当前条目的写入，其余条目顺序/数据仍须与快照完全相同。
        expected[selected] = after[1][selected]
        if tuple(expected) != after[1]:
            raise ValueError('The owning list was reordered during the write')
    prop = _custom_property(pointer, target['name']) if target['custom'] else pointer.bl_rna.properties.get(target['name'])
    token = _capture(pointer, prop, target['index'], custom=target['custom'])
    try:
        fresh = _resolve(token)
        if (fresh['kind'], fresh['array'], fresh['length']) != (target['kind'], target['array'], target['length']):
            raise ValueError('The numeric type changed during the write')
    except Exception:
        _targets.pop(token, None)
        raise
    return token


def _preview_write(session, target, value):
    scene = session['scene']
    automatic = scene.tool_settings.use_keyframe_insert_auto if scene is not None else False
    if scene is not None:
        scene.tool_settings.use_keyframe_insert_auto = False
    try:
        _write(target, value)
    finally:
        if scene is not None:
            scene.tool_settings.use_keyframe_insert_auto = automatic
    token = _after_own_write(target)
    try:
        current = _read(_resolve(token))
        if value != session['expected'] and current == session['expected']:
            raise ValueError('The value did not change. Check property locks and controllers')
    except Exception:
        _targets.pop(token, None)
        raise
    _targets.pop(session['token'], None)
    session['token'], session['expected'] = token, current
    return current


def preview_drag(session, delta_raw):
    try:
        target = _drag_target(session)
        if isinstance(delta_raw, bool) or not isinstance(delta_raw, (int, float)) or not math.isfinite(delta_raw):
            raise ValueError('The drag distance is not a valid number')
        value = _bounded_value(target, session['original'] + delta_raw)
        if target['kind'] == 'FLOAT' and (not target['custom'] or target['custom_storage'] == 'f'):
            # RNA PROP_FLOAT 在 setter 前转成 C float；IDProperty 的 scalar/d 数组仍是 double。
            # 小于实际精度的位移只积累，不写同值、误报锁定或给旧值新增 K 帧。
            try:
                value = struct.unpack('=f', struct.pack('=f', value))[0]
            except OverflowError as error:
                raise ValueError('The result is outside the native floating-point range') from error
            if not math.isfinite(value) or not target['minimum'] <= value <= target['maximum']:
                raise ValueError('The result is outside the valid property range')
        return session['expected'] if value == session['expected'] else _preview_write(session, target, value)
    except (ReferenceError, AttributeError, TypeError, ValueError, RuntimeError, KeyError) as error:
        session['valid'] = False
        raise ValueError(str(error)) from error


def commit_drag(session, context):
    target = _drag_target(session, context)
    changed = session['expected'] != session['original']
    session['done'] = True
    if changed and session['autokey']:
        try:
            _autokey(target, context)
        except (ReferenceError, AttributeError, TypeError, ValueError, RuntimeError) as error:
            # 预览值已经写入，K 失败仍须保留改值与原生 Undo，不伪报整体取消。
            session['warning'] = 'The value was changed. ' + str(error)
    return changed


def cancel_drag(session, context=None):
    try:
        # 无效 delta 会停止预览，但取消仍逐一复验真实目标、原值、作用域和动画。
        # 已提交会话及外部变更始终拒绝回退，不能仅凭旧 valid 标记决定是否安全。
        target = _drag_target(session, context, for_cancel=True)
        if session['expected'] != session['original']:
            _preview_write(session, target, session['original'])
        restored = session['expected'] == session['original']
    except (ReferenceError, AttributeError, TypeError, ValueError, RuntimeError, KeyError) as error:
        session['warning'] = str(error)
        restored = False
    session['valid'], session['done'] = False, True
    return restored


def _component_items(self, _context):
    # 动态 Enum 的字符串必须保留 Python 引用，缓存同时保存这组完整 items。
    return _targets.get(self.target_token, {}).get('components', _EMPTY_COMPONENTS)


class NQA_OT_numeric_quick(Operator):
    bl_idname = 'nqa.numeric_quick'
    bl_label = 'Quick Numeric Adjustment'
    bl_description = 'Adjust the current component, round integers and respect property limits'
    bl_options = {'UNDO', 'INTERNAL'}

    target_token: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    operation: EnumProperty(items=OPERATIONS, default='HALF', options={'HIDDEN', 'SKIP_SAVE'})
    component: EnumProperty(name='Component', items=_component_items, default=0, options={'SKIP_SAVE'})

    def invoke(self, context, _event):
        try:
            target = _resolve(self.target_token)
        except (ReferenceError, AttributeError, TypeError, ValueError, RuntimeError) as error:
            self.report({'WARNING'}, 'Numeric adjustment failed: ' + str(error))
            return {'CANCELLED'}
        if target['array'] and target['index'] < 0:
            return context.window_manager.invoke_props_dialog(self, width=260)
        return self.execute(context)

    def draw(self, _context):
        target = _targets.get(self.target_token, {})
        label = next(label for identifier, label, _description in operations(_context) if identifier == self.operation)
        self.layout.label(text=target.get('label', '') + ' · ' + label)
        self.layout.prop(self, 'component')
        self.layout.label(text='Only the selected component is adjusted')

    def execute(self, context):
        try:
            target = _resolve(self.target_token)
            if target['array'] and target['index'] < 0:
                choices = {identifier: index for index, (identifier, _label, _description)
                           in enumerate(target['components'][1:])}
                if self.component not in choices:
                    raise ValueError('Select a numeric component to adjust')
                target = dict(target, index=choices[self.component])
            old = _read(target)
            value = _scaled_value(target, self.operation, context)
            if value == old:
                return {'CANCELLED'}
            _write(target, value)
            # getter/setter 代理可因锁定或其他控制器而拒写；不能伪报成功或给旧值 K 帧。
            if _read(target) == old:
                self.report({'WARNING'}, 'The value did not change. Check property locks and controllers')
                return {'CANCELLED'}
        except (ReferenceError, AttributeError, TypeError, ValueError, RuntimeError, OverflowError) as error:
            self.report({'WARNING'}, 'Numeric adjustment failed: ' + str(error))
            return {'CANCELLED'}
        try:
            _autokey(target, context)
        except (ReferenceError, AttributeError, TypeError, ValueError, RuntimeError) as error:
            # 数值已经修改；自动 K 帧失败不能伪装成整个操作未发生。
            self.report({'WARNING'}, 'The value was changed. ' + str(error))
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        return {'FINISHED'}


@persistent
def _clear_targets(_unused=None):
    _targets.clear()


def register():
    global _registered
    if _registered:
        return
    bpy.utils.register_class(NQA_OT_numeric_quick)
    try:
        bpy.app.handlers.load_pre.append(_clear_targets)
    except Exception:
        bpy.utils.unregister_class(NQA_OT_numeric_quick)
        raise
    _registered = True


def unregister():
    global _registered
    if not _registered:
        return
    if _clear_targets in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_clear_targets)
    _clear_targets()
    bpy.utils.unregister_class(NQA_OT_numeric_quick)
    _registered = False
