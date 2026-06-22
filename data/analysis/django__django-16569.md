# django__django-16569

## 问题背景

FormSet 的 `add_fields(form, index)` 在 `index is None` 时崩溃。当 FormSet 设 `can_delete=True` 且 `can_delete_extra=False`，调 `add_fields` 且 index 为 None（如访问 `empty_form`）时，条件 `index < initial_form_count` 拿 None 与 int 比较，抛 `TypeError: '<' not supported between instances of 'NoneType' and 'int'`。Golden patch 在比较前加 `index is not None` 守卫。

## Golden Patch 语义分析

```python
if self.can_delete and (
    self.can_delete_extra or (index is not None and index < initial_form_count)
):
    form.fields[DELETION_FIELD_NAME] = BooleanField(...)
```
核心语义：**`index < initial_form_count` 比较前必须先确认 `index is not None`**，因为 `empty_form` 传入的 index 是 None。`empty_form` 是用于"再加一个空表单"的模板，不应有 DELETE 字段。短路求值保证：index 为 None 时 `index is not None` 为假 → 整个括号为假（且不触发比较）→ empty_form 不加 DELETE 字段，也不崩溃。`can_delete_extra` 为 True 时该项被 or 短路，无所谓 index。

F2P 测试 `FormsFormsetTestCase.test_disable_delete_extra_formset_forms`：can_delete=True、can_delete_extra=False 的 formset，断言前几个表单有/无 DELETE，并新增断言 `empty_form` 无 DELETE 字段（即 add_fields(index=None) 不崩溃且不加字段）。

## 调用链分析

`FormSet.empty_form` 属性构造一个空表单并调 `add_fields(form, None)`（index=None）。`add_fields` 在 can_delete 时计算是否加 DELETION 字段：`can_delete_extra or (index is not None and index < initial_form_count)`。index=None 时守卫短路为假，不加字段、不比较。守卫缺失/写错则 `None < int` TypeError，或 empty_form 错误地获得 DELETE 字段。`can_delete_extra=False` 是触发该分支的前提（否则 or 短路掉整个比较）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 删除 `index is not None` 守卫，None<int 抛 TypeError |
| B | 🔴 必须替换 | 替换 | 原 B 与 A/D/E 相同；改为 `index is None`（守卫语义反转） |
| C | ➕ 补充 | 新增 | `and`→`or`：`index is not None or index<count`，None 时仍比较 TypeError |
| D | ➕ 补充 | 新增 | `(index or 0) < count`：None→0，empty_form 错误地获得 DELETE 字段 |
| E | 🟢 高质量 | 保留（重做）| None 守卫藏到默认关闭开关后，退回裸比较 TypeError |

原 A、D、E 字节完全相同（删除 `index is not None` 守卫），只有 3 个 mutation。保留 A，重做 B、C、D、E 为不同机制。本 patch 是一行布尔条件，五组围绕该条件分化。

## 各组 Mutation 分析

### Group A — 保留（B2 删除守卫）
```diff
         if self.can_delete and (
-            self.can_delete_extra or (index is not None and index < initial_form_count)
+            self.can_delete_extra or index < initial_form_count
         ):
```
**变异语义**：删除 `index is not None and` 守卫，退回裸 `index < initial_form_count`。empty_form 的 index=None → `None < int` 抛 TypeError，还原原 bug。保留。

### Group B — 替换（B3 条件反转：is None）
**原**：与 A/D/E 相同（删守卫）。
**最终 mutation**：
```diff
-            self.can_delete_extra or (index is not None and index < initial_form_count)
+            self.can_delete_extra or (index is None and index < initial_form_count)
```
**变异语义**：守卫从 `index is not None` 反转成 `index is None`。当 index 是 None 时 `index is None` 为真，于是继续求值 `index < initial_form_count`（`None < int`）→ TypeError（短路没拦住，反而专门在 None 时触发比较）。当 index 非 None 时 `index is None` 为假 → 不加 DELETE 字段（漏加，行为也错）。守卫语义完全颠倒。比删守卫保留了 `is ... None` 结构。

### Group C — 补充（B3 逻辑运算符：and→or）
```diff
-            self.can_delete_extra or (index is not None and index < initial_form_count)
+            self.can_delete_extra or (index is not None or index < initial_form_count)
```
**变异语义**：把守卫与比较间的 `and` 改成 `or`。`index is not None or index < count`：index=None 时第一项为假，继续求 `None < count` → TypeError（or 不短路第二项，因为第一项假）。短路保护失效。模拟"and/or 写错使守卫不再保护比较"。与 B（守卫条件反转）机制不同——C 是连接运算符错。

### Group D — 补充（C1 值：None 当 0）
```diff
-            self.can_delete_extra or (index is not None and index < initial_form_count)
+            self.can_delete_extra or ((index or 0) < initial_form_count)
```
**变异语义**：用 `(index or 0)` 把 None 转成 0 再比较，避免了 TypeError，但 `0 < initial_form_count`（通常为真）→ empty_form **错误地获得** DELETE 字段。F2P 断言 `empty_form` 无 DELETE 失败（实际有了）。模拟"用 `or 0` 兜底 None、却让 empty_form 当成了 index=0 的真实表单"。不崩溃但语义错——比 A 的崩溃更隐蔽。

### Group E — 保留（E2 隐式→显式开关）
**原**：与 A 相同（删守卫）。
**最终 mutation**：
```diff
-            self.can_delete_extra or (index is not None and index < initial_form_count)
+            self.can_delete_extra or ((index is not None and index < initial_form_count) if getattr(self, "_guard_none_index", False) else index < initial_form_count)
```
**变异语义**：None 守卫只在 `_guard_none_index` 开关开启时生效，默认 `False` → 走 else 的裸 `index < initial_form_count`（原 bug）→ None 时 TypeError。只有显式开启才安全。模拟"把空值守卫做成可配置、默认却关掉"。重做为 E。

## 新设计 Mutation 说明

原 A、D、E 字节完全相同（删除 `index is not None` 守卫），实际只有"删守卫"一种机制、且仅 3 个 mutation。本次保留 A（删守卫致 TypeError），重做 B（`is None` 守卫反转）、E（`_guard_none_index` 默认关闭开关），补充 C（`and`→`or` 使守卫不短路保护）、D（`(index or 0)` 把 None 当 0 使 empty_form 错误获得 DELETE 字段）。本 patch 是单行布尔条件，五组围绕"删守卫 / 守卫反转 / 连接运算符错 / None 当 0 / 默认关闭开关"五个角度——A/B/C/E 让 None 触发 TypeError，D 不崩溃但产生错误字段。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
