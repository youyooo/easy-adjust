"""数值快调的原生插件偏好；配置变更只通知运行态，不强制保存用户偏好。"""

import math
from types import SimpleNamespace

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator


_BUTTON_DEFAULTS = (
    ('QUARTER', '−1', 'DIVIDE', 4.0),
    ('HALF', '−½', 'DIVIDE', 2.0),
    ('ONE_HALF', '+½', 'MULTIPLY', 1.5),
    ('DOUBLE', '+1', 'MULTIPLY', 2.0),
)
_BUTTON_INDEX = {identifier: index for index, (identifier, *_rest) in enumerate(_BUTTON_DEFAULTS, 1)}
_OPERATION_ITEMS = (
    ('ADD', 'Add', 'Add the specified value'),
    ('SUBTRACT', 'Subtract', 'Subtract the specified value'),
    ('MULTIPLY', 'Multiply', 'Multiply by the specified value'),
    ('DIVIDE', 'Divide', 'Divide by the specified nonzero value'),
)
_SYMBOLS = {'ADD': '+', 'SUBTRACT': '−', 'MULTIPLY': '×', 'DIVIDE': '÷'}
_BASE_LADDER_COUNT = 6
_MAX_LADDER_COUNT = 12
_DEFAULT_VALUES = dict(hover_enabled=True, ladder_enabled=True, hover_delay=.5,
                       ladder_count=_BASE_LADDER_COUNT,
                       ladder_steps=(100.0, 10.0, 1.0, .1, .01, .001),
                       ladder_extra_steps=(50.0, 5.0, .5, .05, .005, .0005),
                       drag_pixels=12.0, ui_scale=1.0)
for _index, (_identifier, _label, _operation, _value) in enumerate(_BUTTON_DEFAULTS, 1):
    _DEFAULT_VALUES.update({f'button{_index}_label': _label,
                            f'button{_index}_operation': _operation,
                            f'button{_index}_value': _value})


class _Defaults(SimpleNamespace):
    """直接 register 的 factory 环境可读默认值，不能误写成持久偏好。"""

    def __setattr__(self, _name, _value):
        raise AttributeError('Default preferences are read-only. Enable Easy Adjust in Blender Preferences first')

    def __delattr__(self, _name):
        raise AttributeError('Default preferences are read-only')


_DEFAULTS = _Defaults(**_DEFAULT_VALUES)
revision = 0
on_change = None
_suspend_updates = 0
_registered_classes = []
_operation_cache_key = None
_operation_cache = ()


def _native_preferences(context=None):
    try:
        current = context if context is not None else bpy.context
        addon = current.preferences.addons.get(__package__)
        return addon.preferences if addon is not None else None
    except (AttributeError, ReferenceError, KeyError):
        # 注册/卸载的受限上下文或 factory 直接注册没有 AddonPreferences 实例。
        return None


def get(context=None):
    """返回本插件的原生偏好；无注册条目时返回不可写的默认配置。"""
    native = _native_preferences(context)
    return native if native is not None else _DEFAULTS


def _notify_change(context=None):
    global revision, _operation_cache_key
    revision += 1
    _operation_cache_key = None
    if callable(on_change):
        on_change(context)


def _preference_update(_self, context):
    if not _suspend_updates:
        _notify_change(context)


def operation_value(identifier, context=None):
    """稳定按钮 ID 对应的运算和参数；这里只验证配置，不执行数值表达式。"""
    try:
        index = _BUTTON_INDEX[identifier]
    except (KeyError, TypeError) as error:
        raise ValueError('Unknown Easy Adjust button') from error
    prefs = get(context)
    operation = getattr(prefs, f'button{index}_operation')
    value = getattr(prefs, f'button{index}_value')
    if operation not in _SYMBOLS:
        raise ValueError('Invalid button operation')
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError('The button value must be a finite number')
    if operation == 'DIVIDE' and value == 0:
        raise ValueError('The divisor must not be zero')
    return operation, float(value)


def operations(context=None):
    """四个按钮保留历史 ID；标签仅用于显示，不参与确定运算。"""
    global _operation_cache_key, _operation_cache
    prefs = get(context)
    values = tuple((identifier, getattr(prefs, f'button{index}_label'),
                    getattr(prefs, f'button{index}_operation'),
                    getattr(prefs, f'button{index}_value'))
                   for index, (identifier, *_rest) in enumerate(_BUTTON_DEFAULTS, 1))
    if values != _operation_cache_key:
        entries = []
        for identifier, label, operation, value in values:
            symbol = _SYMBOLS.get(operation, '?')
            generated = f'{symbol}{value:g}'
            _, default_label, default_operation, default_value = _BUTTON_DEFAULTS[_BUTTON_INDEX[identifier] - 1]
            # 历史名称只与原默认倍率配对；改参数后自动显示真实算式，自定义名称优先。
            automatic = not label.strip() or (label == default_label
                        and (operation, value) != (default_operation, default_value))
            description = ('The divisor must not be zero' if operation == 'DIVIDE' and value == 0
                           else f'Current value {symbol} {value:g}')
            entries.append((identifier, generated if automatic else label.strip(), description))
        # 保留动态标签字符串的 Python 引用，供原生 UI 和 Enum 回调安全读取。
        _operation_cache_key, _operation_cache = values, tuple(entries)
    return _operation_cache


def levels(context=None):
    """步长按数值降序去重；拒绝把异常或非正配置带入拖动运算。"""
    prefs = get(context)
    count = prefs.ladder_count
    steps = tuple(prefs.ladder_steps)
    if count > _BASE_LADDER_COUNT:
        steps += tuple(prefs.ladder_extra_steps)
    return tuple(sorted({float(value) for value in steps[:count]
                         if isinstance(value, (int, float)) and not isinstance(value, bool)
                         and math.isfinite(value) and value > 0}, reverse=True))


class NQA_Preferences(AddonPreferences):
    bl_idname = __package__

    hover_enabled: BoolProperty(name='Hover Buttons', default=True, update=_preference_update)
    ladder_enabled: BoolProperty(name='Middle Mouse Ladder', default=True, update=_preference_update)
    hover_delay: FloatProperty(name='Hover Delay (Seconds)', description='Delay before showing the hover buttons',
                               default=.5, min=0.0, soft_max=3.0, subtype='TIME_ABSOLUTE',
                               update=_preference_update)
    ladder_count: IntProperty(name='Step Count', description='Reduce the count to hide later step settings',
                              default=_BASE_LADDER_COUNT, min=1, max=_MAX_LADDER_COUNT,
                              update=_preference_update)
    # 保留旧版六项 RNA 向量尺寸，升级后原有的步长设置可以直接读取。
    ladder_steps: FloatVectorProperty(name='Adjustment Steps', size=_BASE_LADDER_COUNT,
                                      default=_DEFAULT_VALUES['ladder_steps'],
                                      min=1.1754943508222875e-38, precision=6,
                                      update=_preference_update)
    ladder_extra_steps: FloatVectorProperty(name='Additional Steps', size=_MAX_LADDER_COUNT - _BASE_LADDER_COUNT,
                                            default=_DEFAULT_VALUES['ladder_extra_steps'],
                                            min=1.1754943508222875e-38, precision=6,
                                            update=_preference_update)
    drag_pixels: FloatProperty(name='Pixels per Step', description='Horizontal drag distance in UI pixels per step',
                               default=12.0, min=1.0, soft_max=100.0,
                               update=_preference_update)
    ui_scale: FloatProperty(name='Overlay Scale', default=1.0, min=.25, max=4.0,
                            update=_preference_update)

    def draw(self, _context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, 'hover_enabled')
        row.prop(self, 'ladder_enabled')
        row = layout.row()
        row.prop(self, 'hover_delay')
        row.prop(self, 'ui_scale')

        buttons = layout.box()
        buttons.label(text='Hover Buttons')
        buttons.label(text='Leave labels blank for formulas. Default labels follow the settings.')
        for index in range(1, 5):
            row = buttons.row(align=True)
            row.prop(self, f'button{index}_label', text=f'Button {index}')
            row.prop(self, f'button{index}_operation', text='')
            field = row.row(align=True)
            field.alert = (getattr(self, f'button{index}_operation') == 'DIVIDE'
                           and getattr(self, f'button{index}_value') == 0)
            field.prop(self, f'button{index}_value', text='')

        steps = layout.box()
        steps.label(text='Middle Mouse Steps')
        steps.prop(self, 'drag_pixels')
        steps.prop(self, 'ladder_count')
        for index in range(min(self.ladder_count, _BASE_LADDER_COUNT)):
            steps.prop(self, 'ladder_steps', index=index, text=f'Step {index + 1}')
        for index in range(self.ladder_count - _BASE_LADDER_COUNT):
            steps.prop(self, 'ladder_extra_steps', index=index, text=f'Step {_BASE_LADDER_COUNT + index + 1}')
        steps.label(text='Steps are sorted from largest to smallest. Duplicates are merged.')
        layout.operator(NQA_OT_restore_defaults.bl_idname, icon='LOOP_BACK')


# −1 / −½ 是旧版比例按钮的名称，仍分别执行 ÷4 / ÷2；用户可自行修改名称。
for _index, (_identifier, _label, _operation, _value) in enumerate(_BUTTON_DEFAULTS, 1):
    NQA_Preferences.__annotations__[f'button{_index}_label'] = StringProperty(
        name=f'Button {_index} Label', default=_label, update=_preference_update)
    NQA_Preferences.__annotations__[f'button{_index}_operation'] = EnumProperty(
        name=f'Button {_index} Operation', items=_OPERATION_ITEMS, default=_operation,
        update=_preference_update)
    NQA_Preferences.__annotations__[f'button{_index}_value'] = FloatProperty(
        name=f'Button {_index} Value', default=_value, precision=6, update=_preference_update)


class NQA_OT_restore_defaults(Operator):
    bl_idname = 'numeric_quick_adjust.restore_defaults'
    bl_label = 'Restore Defaults'
    bl_description = 'Reset the button, hover and middle mouse ladder settings'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _suspend_updates
        prefs = _native_preferences(context)
        if prefs is None:
            self.report({'WARNING'}, 'Enable Easy Adjust in Blender Preferences first')
            return {'CANCELLED'}
        _suspend_updates += 1
        try:
            for name, value in _DEFAULT_VALUES.items():
                setattr(prefs, name, value)
        finally:
            _suspend_updates -= 1
            if not _suspend_updates:
                _notify_change(context)
        return {'FINISHED'}


def register():
    if _registered_classes:
        return
    try:
        for cls in (NQA_Preferences, NQA_OT_restore_defaults):
            bpy.utils.register_class(cls)
            _registered_classes.append(cls)
    except Exception:
        for cls in reversed(_registered_classes):
            bpy.utils.unregister_class(cls)
        _registered_classes.clear()
        raise


def unregister():
    global on_change, _operation_cache_key, _operation_cache
    on_change = None
    for cls in reversed(_registered_classes):
        bpy.utils.unregister_class(cls)
    _registered_classes.clear()
    _operation_cache_key, _operation_cache = None, ()
