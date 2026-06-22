# django__django-16801

## 问题背景

`ImageField` 即使未设置 `width_field`/`height_field` 也会给模型注册一个 `post_init` 信号处理器，造成模型初始化性能损耗（30-40%）。该信号处理器在无维度字段时是 noop（直接 return），完全没必要注册。Golden patch 在 `contribute_to_class` 中给注册信号的条件加上 `(self.width_field or self.height_field)`——仅当确有维度字段时才连接 `post_init`；同时简化 `update_dimension_fields` 里冗余的 `has_dimension_fields` 判断（因为现在只有有维度字段才会被连接调用）。

## Golden Patch 语义分析

```python
def contribute_to_class(self, cls, name, **kwargs):
    super().contribute_to_class(cls, name, **kwargs)
    # Only run post-initialization dimension update on non-abstract models
    # with width_field/height_field.
    if not cls._meta.abstract and (self.width_field or self.height_field):
        signals.post_init.connect(self.update_dimension_fields, sender=cls)
```
核心语义：**只有"非抽象模型 且 设置了 width_field 或 height_field"时才连接 `post_init` 信号**。关键是新增的 `and (self.width_field or self.height_field)` 子条件——无维度字段的 ImageField 不再注册信号处理器。`width_field`/`height_field` 在 `__init__` 中恒被赋值（默认 None），故用真值判断（None→假）而非 `hasattr`。

F2P 测试 `ImageFieldNoDimensionsTests.test_post_init_not_connected`：对无维度字段的 PersonModel，断言其 id 不在 `signals.post_init.receivers` 的 sender 列表中（即未连接信号）。

## 调用链分析

模型类定义时 `ImageField.contribute_to_class(cls, name)` 被调用 → 判断 `not cls._meta.abstract and (self.width_field or self.height_field)` → 真则 `signals.post_init.connect(...)`。无维度字段时该条件为假 → 不连接。F2P 检查 `post_init.receivers` 里是否含该 model 的 sender。条件中维度守卫被删/反转/改 hasattr/改 or、或藏到开关后，都会让无维度模型错误地连接信号。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 新增 | 新增 | `self.width_field` 换成 `hasattr(self, "width_field")`，恒 True |
| B | 🟢 高质量 | 保留 | 维度守卫加 `not` 反转 |
| C | 🟢 高质量 | 保留 | 删除整个维度守卫子条件（还原 bug） |
| D | ➕ 新增 | 新增 | `and`→`or`，非抽象即短路连接 |
| E | 🟢 高质量 | 保留 | 维度守卫藏到 `skip_dimensionless_signal` 开关后 |

原始仅 B/C/E 三组，缺 A、D。保留 B/C/E，补充 A（hasattr 恒真）、D（and→or）。

## 各组 Mutation 分析

### Group A — 新增（A2 接口契约：hasattr 恒真）
```diff
-        if not cls._meta.abstract and (self.width_field or self.height_field):
+        if not cls._meta.abstract and (hasattr(self, "width_field") or hasattr(self, "height_field")):
```
**变异语义**：把 `self.width_field or self.height_field`（真值判断）换成 `hasattr(self, "width_field") or hasattr(self, "height_field")`。这两个属性在 `__init__` 里恒被赋值（即使为 None），故 `hasattr` 恒为 True → 条件永真 → 无维度模型也连接信号。模拟"用 hasattr 检查存在性、却忽略了属性恒存在（只是值为 None）"。F2P 失败。新增为 A。

### Group B — 保留（B3 条件反转）
```diff
-        if not cls._meta.abstract and (self.width_field or self.height_field):
+        if not cls._meta.abstract and not (self.width_field or self.height_field):
```
**变异语义**：维度守卫加 `not`。语义颠倒——只有"无维度字段"时才连接信号（与修复意图相反）。无维度模型连接、有维度模型反而不连。F2P 断言无维度模型未连接，实际连接了 → 失败。保留。

### Group C — 保留（B2 删除守卫）
```diff
-        if not cls._meta.abstract and (self.width_field or self.height_field):
+        if not cls._meta.abstract:
```
**变异语义**：删除 `and (self.width_field or self.height_field)` 整个子条件，退回原始 bug——所有非抽象模型都连接 `post_init` 信号，无维度模型也连。这正是修复前的行为。F2P 失败。保留。

### Group D — 新增（B3 逻辑运算符：and→or）
```diff
-        if not cls._meta.abstract and (self.width_field or self.height_field):
+        if not cls._meta.abstract or (self.width_field or self.height_field):
```
**变异语义**：把连接 `not abstract` 与维度守卫的 `and` 改成 `or`。对非抽象模型 `not abstract` 为真 → 整个 `or` 短路为真 → 无条件连接信号，维度守卫形同虚设。无维度模型也连。模拟"逻辑运算符 and/or 写错使守卫失效"。比 C（删守卫）隐蔽——守卫还在、只是被 or 短路。F2P 失败。新增为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-        if not cls._meta.abstract and (self.width_field or self.height_field):
+        if not cls._meta.abstract and ((self.width_field or self.height_field) if getattr(self, "skip_dimensionless_signal", False) else True):
```
**变异语义**：维度守卫藏到 `skip_dimensionless_signal` 开关后（默认 False → 走 `else` 的 `True`）。默认对所有非抽象模型连接信号（守卫被 True 短路），无维度模型也连。只有显式开启才跳过。模拟"把'跳过无维度信号'优化做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原始仅 B/C/E 三组，缺 A、D。本次保留 B（守卫反转）、C（删守卫还原 bug）、E（skip_dimensionless_signal 默认关闭开关），补充 A（hasattr 恒真使守卫失效）、D（and→or 短路连接）。五组覆盖"hasattr 恒真 / 守卫反转 / 删守卫 / 逻辑运算符错 / 默认关闭开关"五个角度——全部令无维度 ImageField 错误地连接 post_init 信号。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
