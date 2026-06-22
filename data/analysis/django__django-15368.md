# django__django-15368

## 问题背景

`bulk_update()` 无法处理普通 `F('...')` 表达式：把 `o.c8 = F('name')` 后 `bulk_update` 会把字段值写成字符串 `'F(name)'` 而非解析成列名。根因在 `bulk_update` 用 `isinstance(attr, Expression)` 判断是否为表达式，但 `F` 并不是 `Expression` 的子类，因此被当作普通值包进 `Value(...)`，导致其 `repr` 被当字面量写入 SQL。Golden patch 改用 `hasattr(attr, 'resolve_expression')`（鸭子类型判定），并移除不再需要的 `Expression` 导入。

## Golden Patch 语义分析

```python
attr = getattr(obj, field.attname)
if not hasattr(attr, 'resolve_expression'):
    attr = Value(attr, output_field=field)
when_statements.append(When(pk=obj.pk, then=attr))
```
核心语义：**用"是否具备 `resolve_expression` 方法"来判定一个属性值是否为查询表达式**，而非用 `isinstance(Expression)`。`F` 有 `resolve_expression` 但不继承 `Expression`，所以鸭子类型判定才能正确放行 `F('name')`，使其在 SQL 中解析为列引用而非被包成 `Value`。同时 golden patch 把 `from ...expressions import ... Expression ...` 里的 `Expression` 移除（因不再使用）。

F2P 测试 `BulkUpdateTests.test_f_expression`：把 10 个 Note 的 `misc` 设为 `F('note')`，`bulk_update(['misc'])` 后断言 `Note.objects.filter(misc='test_note')` 命中全部——即 `F('note')` 被正确解析为 `note` 列的值，而非字面量。

## 调用链分析

`bulk_update(objs, fields)` 内层循环对每个对象的字段值 `attr` 判定：若非表达式则 `Value(attr, output_field=field)` 包装，否则原样作为 `When(then=attr)`。`Case(*when_statements)` 拼成 SQL。若判定错误地把 `F('note')` 包进 `Value`，写入的是 `F('note')` 的字符串表示而非列引用。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A 组 |
| B | 🟢 高质量 | 保留 | 去掉 `not`，逻辑反转——只对真表达式做 Value 包装、对普通值不包装 |
| C | 🔴 必须替换 | 替换 | 原 C=E（`isinstance(attr, Expression)`），但 golden 已移除 Expression 导入→NameError 崩溃；改为 `isinstance(attr, Value)` |
| D | ➕ 补充 | 新增 | 原缺 D 组 |
| E | 🔴 必须替换 | 替换 | 与 C 字节级重复且 NameError |

原仅 B/C/E 且 C=E 重复并触发 NameError。补 A、D，重做 C。

## 各组 Mutation 分析

### Group A — 补充（A1 接口契约：属性名 typo）
```diff
-                    if not hasattr(attr, 'resolve_expression'):
+                    if not hasattr(attr, 'resolve_expressions'):
```
**变异语义**：把鸭子类型探测的方法名拼成复数 `resolve_expressions`（多了个 s）。任何表达式都没有该方法，故 `not hasattr(...)` 恒真，连 `F('note')` 也被包进 `Value`，写成字面量，F2P 失败。模拟"方法名记错/手误加复数"。

### Group B — 保留（B3 逻辑反转）
```diff
-                    if not hasattr(attr, 'resolve_expression'):
+                    if hasattr(attr, 'resolve_expression'):
```
**变异语义**：去掉 `not`，逻辑彻底反转——表达式被包进 `Value`（破坏其解析），普通值反而不被包装。`F('note')` 有 `resolve_expression` → 被包成 `Value(F('note'))` → 写成字面量，F2P 失败。保留。

### Group C — 替换（C1 类型/数据形状：过窄的 isinstance）
**原**：`isinstance(attr, Expression)`，但 `Expression` 已被 golden 从 import 移除 → NameError。
**最终 mutation**：
```diff
-                    if not hasattr(attr, 'resolve_expression'):
+                    if not isinstance(attr, Value):
```
**变异语义**：用 `isinstance(attr, Value)` 替代鸭子类型判定。`Value` 仍在导入中（不会 NameError），但判定过窄——`F('note')` 不是 `Value` 实例，故 `not isinstance(...)` 为真 → 被包进 `Value`，解析丢失。模拟"用具体类型检查替代鸭子类型"的数据形状误判，且避开了原 C/E 的 NameError 崩溃。

### Group D — 补充（D1 状态：无条件包装）
```diff
-                    attr = getattr(obj, field.attname)
-                    if not hasattr(attr, 'resolve_expression'):
-                        attr = Value(attr, output_field=field)
+                    attr = getattr(obj, field.attname)
+                    attr = Value(attr, output_field=field)
```
**变异语义**：删掉守卫，对**所有**属性值无条件 `Value(...)` 包装。表达式 `F('note')` 也被包成 `Value`，写成字面量。模拟"简化代码、去掉看似多余的条件判断"的过度重构。

### Group E — 替换（E1 测试期望：附加错误条件）
**原**：与 C 重复（NameError）。
**最终 mutation**：
```diff
-                    if not hasattr(attr, 'resolve_expression'):
+                    if not hasattr(attr, 'resolve_expression') or not hasattr(attr, 'output_field'):
```
**变异语义**：给判定追加 `or not hasattr(attr, 'output_field')`。`F('note')` 有 `resolve_expression` 但**没有** `output_field` 属性，故 `or` 后半为真 → 整个条件为真 → 被包进 `Value`，解析丢失。看似"更严格地确认这是个完整表达式"，实则把缺 `output_field` 的合法表达式（如裸 `F`）误判为普通值。

## 新设计 Mutation 说明

原 C/E 用 `isinstance(attr, Expression)`，但 golden patch 已删除 `Expression` 导入，二者都会 NameError 崩溃且互相重复。本次补齐缺失的 A（方法名 typo）、D（无条件包装），并把 C 改为 `isinstance(attr, Value)`（仍导入、过窄判定）、E 改为附加 `output_field` 条件。五组覆盖"方法名 typo / 逻辑反转 / 过窄 isinstance / 去守卫 / 附加错误条件"五个角度，全部避开 NameError。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
