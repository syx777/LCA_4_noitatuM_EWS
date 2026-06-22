# django__django-16032

## 问题背景

`Model.objects.filter(field__in=subquery)` 中，当子查询在 `annotate()` 之后又用了 `alias()` 时，`__in` 没有清除子查询 RHS 的选择字段，导致子查询 `SELECT` 出多列，报 `sub-select returns 10 columns - expected 1`。根因：`RelatedIn.get_prep_lookup` 判断"子查询是否已有显式选择字段"用的是 `Query.has_select_fields` 属性，而该属性原是个 `@property`，会把 `alias()` 设置的 `annotation_select_mask` 也算作"有选择字段"，于是误判子查询已选好字段、不去 `set_values([target_field])` 收窄为单列。Golden patch 把 `has_select_fields` 从计算属性改为普通类属性（默认 `False`），并只在 `set_values()` 里显式置 `True`；同时把 RHS 收窄从 `add_fields([target], True)` 改为 `set_values([target])`。

## Golden Patch 语义分析

`query.py`：
```python
class Query(BaseExpression):
    ...
    has_select_fields = False          # 新增类属性，替代原 @property
    # 删除原 @property has_select_fields（基于 select/annotation_select_mask/extra_select_mask 计算）

def set_values(self, fields):
    ...
    self.clear_select_fields()
    self.has_select_fields = True       # 只有显式 values()/set_values 才标记
```
`related_lookups.py`：`self.rhs.set_values([target_field])`（原 `add_fields([target_field], True)`）。

核心语义：**`has_select_fields` 必须只反映"用户是否通过 `values()`/`set_values()` 显式选了字段"，而非把 `alias()`/`annotate()` 产生的 annotation mask 也算进去**。改成默认 `False` 的实例状态后，`alias()` 不再误置它为真，于是 `RelatedIn` 的守卫 `not getattr(self.rhs, "has_select_fields", True)` 成立，正确调用 `set_values([target_field])` 把子查询收窄为单列。

F2P 测试 `NonAggregateAnnotationTestCase.test_annotation_and_alias_filter_in_subquery` 与 `_related_in_subquery`：子查询 `annotate(...).alias(...)` 后用于 `filter(pk__in=...)`/`filter(book__in=...)`，断言结果正确（子查询被收窄为单列、不报多列错误）。

## 调用链分析

`filter(field__in=subquery)` → `RelatedIn.get_prep_lookup`：当 `not getattr(self.rhs, "has_select_fields", True)`（子查询没有显式选字段）且 lhs 非主键时，调 `self.rhs.set_values([target_field])` 把 RHS 子查询收窄为单列。`has_select_fields` 由 `Query` 提供。在 `Query.add_filter`（query.py:1238）里也用 `value.has_select_fields` 校验关系兼容性。`alias()` 会填 `annotation_select_mask`；若 `has_select_fields` 仍是基于 mask 的计算属性，`alias()` 后它变真 → 守卫失效 → 不收窄 → 多列子查询。修复让它成为仅由 `set_values` 翻转的实例状态。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 类属性默认 `False`→`True`，守卫恒不触发，子查询不收窄 |
| B | ➕ 补充 | 新增 | 还原旧 `@property`（select/annotation_mask/extra 三者），alias 使其为真 |
| C | ➕ 补充 | 新增 | `@property` 只查 `select or extra_select_mask`（漏 annotation_mask 也漏 select 单独项的形态差异） |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 字节相同；改为 `@property` 漏 `self.select`（只查 annotation/extra） |
| E | ➕ 补充 | 新增 | `@property` 藏到默认关闭开关后，否则回退旧计算逻辑 |

原实例只有 A、D 两组且字节完全相同（类属性 `False`→`True`）。保留 A，补充 B、C、E，重做 D。五组全部围绕 `has_select_fields` 的定义形态分化（这是修复的唯一杠杆点）。

## 各组 Mutation 分析

### Group A — 保留（D1 状态：默认值反转）
```diff
     select_related = False
-    has_select_fields = False
+    has_select_fields = True
```
**变异语义**：把新增的类属性默认值从 `False` 改成 `True`。所有 Query 实例一开始就"声称有选择字段"，`RelatedIn` 守卫 `not getattr(rhs, "has_select_fields", True)` 恒为 False → 永不调 `set_values` 收窄 → 子查询保持多列，还原原 bug。保留。

### Group B — 补充（D1 状态：还原旧计算属性）
```diff
-    has_select_fields = False
...
+    @property
+    def has_select_fields(self):
+        return bool(
+            self.select or self.annotation_select_mask or self.extra_select_mask
+        )
```
**变异语义**：删除类属性、还原 golden 之前的 `@property`。`alias()` 填充 `annotation_select_mask` → 属性返回真 → 守卫失效 → 子查询不收窄。这正是原始 bug 的根因实现。模拟"把修复回退成旧的计算属性"。

### Group C — 补充（C1 数据形状：漏 annotation_select_mask）
```diff
+    @property
+    def has_select_fields(self):
+        return bool(self.select or self.extra_select_mask)
```
**变异语义**：计算属性只看 `select` 和 `extra_select_mask`，漏掉 `annotation_select_mask`。对 `.values()`（填 select）能正确判真，但 `alias()`/`annotate()`（填 annotation mask）判不出——看似"修了一部分"，实则对 alias 子查询仍误判。与 B（全量计算）不同：C 是"部分计算属性"，对纯 alias 场景失效。模拟"列举判断项时漏了一项"。

### Group D — 替换（C1 数据形状：漏 self.select）
**原**：与 A 字节相同（类属性 `False`→`True`）。
**最终 mutation**：
```diff
+    @property
+    def has_select_fields(self):
+        return bool(self.annotation_select_mask or self.extra_select_mask)
```
**变异语义**：计算属性只看 annotation/extra mask，漏掉 `self.select`。与 C 互补——C 漏 annotation、D 漏 select。`alias()` 子查询会因 annotation_mask 被判真 → 守卫失效 → 不收窄。模拟"漏了 select 维度的判断"。与 A（类属性）、B（全量属性）、C（漏 annotation）三者机制各异。

### Group E — 补充（E2 隐式→显式开关）
```diff
+    @property
+    def has_select_fields(self):
+        return self._has_select_fields if getattr(self, "_strict_select_fields", False) else bool(
+            self.select or self.annotation_select_mask or self.extra_select_mask
+        )
```
**变异语义**：把"严格语义"（仅由 set_values 翻转的 `_has_select_fields`）藏到开关 `_strict_select_fields` 后，默认 `False` → 走 else 的旧计算逻辑（含 annotation_mask，即 bug 行为）。只有显式开启才用修复后的严格状态。模拟"把修复做成可配置、默认却保留旧计算"。默认分支不引用 `_has_select_fields`，故无属性缺失问题。

## 新设计 Mutation 说明

原实例只有 A、D 两组且字节完全相同（`has_select_fields = False`→`True`）。本次保留 A（类属性默认反转），补充 B（还原全量计算属性）、C（计算属性漏 annotation_select_mask）、E（计算属性藏到默认关闭开关后回退旧逻辑），把与 A 重复的 D 重做为"计算属性漏 self.select"（与 C 互补）。修复的唯一杠杆是 `Query.has_select_fields` 的定义形态，五组分别为"默认值反转 / 全量旧属性 / 漏 annotation / 漏 select / 默认关闭开关"，覆盖该杠杆的五种不同失效方式。全部实测：golden 通过、五个变异均令两个 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
