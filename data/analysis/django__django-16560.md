# django__django-16560

## 问题背景

Django 约束（`BaseConstraint` 及子类 `CheckConstraint`/`UniqueConstraint`/`ExclusionConstraint`）的 `validate()` 抛 `ValidationError` 时，只能自定义 `violation_error_message`，不能自定义 `code`。文档建议抛 ValidationError 时提供描述性 code，因此应允许约束也定制 code。Golden patch 给 `BaseConstraint` 加 `violation_error_code` 类属性与 `__init__` 参数，在 `validate()` 抛错时传 `code=self.violation_error_code`，并贯穿到 deconstruct/__eq__/__repr__ 及各子类构造器。

## Golden Patch 语义分析

`BaseConstraint`（constraints.py）：
```python
class BaseConstraint:
    violation_error_code = None        # 新增类属性
    violation_error_message = None
    def __init__(self, *args, name=None, violation_error_code=None, violation_error_message=None):
        ...
        self.name = name
        if violation_error_code is not None:
            self.violation_error_code = violation_error_code   # 实例覆盖
        ...
    def deconstruct(self):
        ...
        if self.violation_error_code is not None:
            kwargs["violation_error_code"] = self.violation_error_code   # 序列化
        return (path, (), kwargs)
```
子类 `CheckConstraint`/`UniqueConstraint`/`ExclusionConstraint` 的 `__init__` 透传 `violation_error_code` 给 super；`__eq__` 增加 `self.violation_error_code == other.violation_error_code` 比较；`__repr__` 增加 code 段；`validate()` 抛错时 `raise ValidationError(msg, code=self.violation_error_code)`。

核心语义：**`violation_error_code` 必须像 `violation_error_message` 一样：可经构造器设置、被存为实例状态、参与 deconstruct/eq/repr、并在 validate 抛错时作为 ValidationError 的 code 传出**。多处协同——任一环节（类属性默认、__init__ 存储、deconstruct 序列化、__eq__ 比较、validate 传 code）缺失都会破坏对应功能。

F2P 测试覆盖 `BaseConstraintTests`（custom code、deconstruction）、`CheckConstraintTests`（eq、repr、validate_custom_error）、`UniqueConstraintTests`（eq、repr、validate）等。

## 调用链分析

构造约束时 `violation_error_code` 经子类 `__init__` 透传到 `BaseConstraint.__init__`，`if violation_error_code is not None: self.violation_error_code = ...` 存为实例属性（否则用类属性 None）。`deconstruct()` 在 code 非 None 时写入 kwargs（迁移序列化）。`__eq__` 比较两约束的 code。`validate()` 抛 `ValidationError(msg, code=self.violation_error_code)`。各测试分别命中：custom_code（__init__ 存储）、deconstruction（序列化）、eq（比较）、repr（展示）、validate_custom_error（抛错传 code）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 类属性默认 `None`→`""`，身份/真值语义边界改变 |
| B | 🔴 必须替换 | 替换 | 原 B 与 C 等价（注释/删除存储）；改为 `is None` 条件反转 |
| C | 🔴 必须替换 | 替换 | 原 C 删除 __init__ 存储；改为 deconstruct 省略 code 序列化 |
| D | 🔴 必须替换 | 替换 | 原 D 与 C 等价（`pass` 占位）；改为 CheckConstraint __eq__ 漏比较 code |
| E | 🟢 高质量 | 保留（重做）| __init__ code 存储藏到默认关闭开关后 |

原 B、C、D 都围绕 `__init__` 里 `if violation_error_code is not None: self.violation_error_code = ...`（注释/删除/`pass`），机制重复。保留 A，重做 B、C、D、E 分布到 __init__/deconstruct/__eq__ 等不同环节。

## 各组 Mutation 分析

### Group A — 保留（C1 值：默认 None→""）
```diff
-    violation_error_code = None
+    violation_error_code = ""
     violation_error_message = None
```
**变异语义**：类属性默认从 `None` 改成 `""`。`deconstruct` 的 `if self.violation_error_code is not None` 对未设 code 的约束（现默认 `""`）变为真 → 把空字符串 code 错误地写进 kwargs，破坏 deconstruction 断言（期望无 code key 或特定值）。`""` 与 `None` 在身份/真值判断处行为不同，多个测试受影响。模拟"默认哨兵值用错（空串当无值）"。保留。

### Group B — 替换（B3 条件反转：is None）
**原**：注释掉 `if ...: self.violation_error_code = ...`（与 C 等价）。
**最终 mutation**：
```diff
-        if violation_error_code is not None:
+        if violation_error_code is None:
             self.violation_error_code = violation_error_code
```
**变异语义**：`__init__` 存储条件反转。只有传入 `violation_error_code is None` 时才赋值（赋的还是 None，无意义）；显式传了非 None code 时反而**不**存储 → 实例 code 保持类属性 None。`test_custom_violation_code_message`（断言 `c.violation_error_code == "custom_code"`）失败。模拟"`is not None`/`is None` 守卫写反"。比注释/删除保留了 if 结构。

### Group C — 替换（D1 状态：deconstruct 省略 code）
**原**：删除 `__init__` 的存储行。
**最终 mutation**：
```diff
-        if self.violation_error_code is not None:
-            kwargs["violation_error_code"] = self.violation_error_code
         return (path, (), kwargs)
```
**变异语义**：`deconstruct` 不再把 `violation_error_code` 写入 kwargs。code 能正常设置/比较，但迁移序列化时丢失——`test_deconstruction`（断言 kwargs 含 `violation_error_code`）失败。模拟"加了属性、忘了在 deconstruct 里序列化它"。这是与 __init__ 存储不同的环节。

### Group D — 替换（A1 接口契约：__eq__ 漏比较）
**原**：`if ...: pass`（与 C 等价）。
**最终 mutation**：
```diff
                 self.name == other.name
                 and self.check == other.check
-                and self.violation_error_code == other.violation_error_code
                 and self.violation_error_message == other.violation_error_message
```
**变异语义**：`CheckConstraint.__eq__` 删去对 `violation_error_code` 的比较。两个仅 code 不同的 CheckConstraint 会被判为相等。`CheckConstraintTests.test_eq`（断言 code 不同的约束 `assertNotEqual`）失败。模拟"加了字段、忘了纳入相等比较"。是 __eq__ 这一环节，与 B（__init__）、C（deconstruct）不同。

### Group E — 重做（E2 隐式→显式开关）
**原**：与 C 等价。
**最终 mutation**：
```diff
-        if violation_error_code is not None:
+        if violation_error_code is not None and getattr(self, "_allow_custom_code", True) is None:
             self.violation_error_code = violation_error_code
```
**变异语义**：在存储条件后追加 `getattr(self, "_allow_custom_code", True) is None`——该 getattr 默认返回 `True`，`True is None` 为假 → 整个条件恒假 → 永不存储 code。措辞看似一个"允许自定义 code"的开关检查，实则因 `is None` 比较恒为假而禁用存储。只有把 `_allow_custom_code` 显式设为 None 才生效（反直觉）。模拟"把行为 gate 在一个写错的开关判断后、默认禁用"。重做为 E。

## 新设计 Mutation 说明

原 B、C、D 都围绕 `BaseConstraint.__init__` 的 `if violation_error_code is not None: self.violation_error_code = ...`（分别为注释、删除、`pass`），机制重复。本次保留 A（类属性默认 `None`→`""`），重做 B（`__init__` 条件 `is not None`→`is None` 反转）、C（`deconstruct` 省略 code 序列化）、D（`CheckConstraint.__eq__` 漏比较 code）、E（`__init__` 存储藏到写错的默认禁用开关后）。五组分布到 BaseConstraint 类属性/__init__/deconstruct 与 CheckConstraint.__eq__ 多个环节，覆盖"默认值 / 存储条件反转 / 序列化缺失 / 相等比较缺失 / 默认关闭开关"五个角度。全部实测（Python 3.11/Django 5.0，三个约束测试类全量）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
