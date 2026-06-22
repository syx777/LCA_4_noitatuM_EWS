# django__django-13417

## 问题背景

`QuerySet.ordered` 属性检查 queryset 是否有序。当 model 设置了 `Meta.ordering` 时，普通查询返回 `ordered=True` 是正确的。但对于含 `annotate()` 的 GROUP BY 查询，Django 会忽略 `Meta.ordering`（因为它会破坏 GROUP BY 的语义），所以实际执行的 SQL 不含 `ORDER BY`，但 `ordered` 属性仍然返回 `True`，与实际行为不符。

修复：在 `ordered` 的 `elif` 分支中加入 `not self.query.group_by` 条件，当查询有 GROUP BY 时（无论是 annotate 设置的 `True` 还是 values().annotate() 设置的字段列表），`ordered` 返回 `False`。

## Golden Patch 语义分析

```python
# 修复前
elif self.query.default_ordering and self.query.get_meta().ordering:
    return True

# 修复后
elif (
    self.query.default_ordering and
    self.query.get_meta().ordering and
    not self.query.group_by        # 新增：有 GROUP BY 时不视为有序
):
    return True
```

`self.query.group_by` 可能的值：
- `None`：无 GROUP BY → `not None = True`，保持有序判断
- `True`：`annotate()` 触发的全字段 GROUP BY → `not True = False`，无序
- `['field', ...]`：`values().annotate()` 触发的显式 GROUP BY → `not ['field'] = False`，无序

## 调用链分析

```
Tag.objects.annotate(num_notes=Count('pk'))
  → query.group_by = True  (set by annotation with aggregate)

qs.ordered
  → self.query.extra_order_by or self.query.order_by → False (无显式排序)
  → elif: default_ordering=True, get_meta().ordering=['name'], not group_by=not True=False
  → returns False  ← 修复后正确

Tag.objects.values('name').annotate(num_notes=Count('pk'))
  → query.group_by = ['name']  (set by values())

qs.ordered
  → elif: default_ordering=True, get_meta().ordering=['name'], not group_by=not ['name']=False
  → returns False  ← 修复后正确
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新建 | isinstance 类型检查无法匹配 group_by=True 哨兵值 |
| B | 语义浅层 | 保留 | 关键 `not` 反转，GROUP BY 时反而返回 True |
| C | 缺失 | 新建 | 用 annotation_select 替代 group_by，两者不等价 |
| D | 必须替换 | 替换 | 含 `# BUG: Removed...` 注释，人工痕迹明显 |
| E | 高质量 | 保留 | E2 参数门控，`@property` 永不传参 |

## 各组 Mutation 分析

### Group A — 新建

**最终 mutation**：
```diff
-            not self.query.group_by
+            not isinstance(self.query.group_by, (list, tuple))
```
**变异语义**：将 `not group_by`（真值检查）改为 `not isinstance(group_by, (list, tuple))`（类型检查）。当 `group_by=True`（`annotate()` 触发的全字段 GROUP BY 哨兵值）时：`isinstance(True, (list, tuple))` 为 `False`，`not False` 为 `True`，`ordered` 错误地返回 `True`。`test_annotated_default_ordering` 中 `Tag.objects.annotate(num_notes=Count('pk'))` 产生 `group_by=True`，断言 `assertIs(qs.ordered, False)` 失败。开发者可能认为 GROUP BY 只有以字段列表形式存在，忽略了 Django 内部用 `True` 作为"全字段 GROUP BY"哨兵值的约定。

---

### Group B — 保留

**原 mutation**：
```diff
-            not self.query.group_by
+            self.query.group_by
```
**分类**：🟡 语义浅层（保留）
**理由**：修改位置处于修复的核心逻辑节点。去掉 `not` 后：有 GROUP BY（`group_by` 为真）时 `ordered` 返回 `True`（错误），没有 GROUP BY 时 `ordered` 返回 `False`（错误）。保留原因：这是对修复逻辑的直接反转，处于关键位置，精确模拟了条件方向理解错误。

---

### Group C — 新建

**最终 mutation**：
```diff
-            not self.query.group_by
+            not self.query.annotation_select
```
**变异语义**：用 `annotation_select`（注释选择字典）替代 `group_by`。`annotation_select` 包含所有出现在 SELECT 中的 annotation（如 `{'num_notes': ...}`）。对于 `Tag.objects.annotate(num_notes=Count('pk'))`：`annotation_select` 非空，`not annotation_select = False`，`ordered = False`（F2P 测试通过）。但对于有注释但无聚合的查询（如 `.annotate(x=Value(1))`），annotation_select 非空但没有 GROUP BY，`ordered` 错误返回 `False`。开发者可能认为"有 annotation 就不有序"而混淆了 annotation_select 与 group_by 的关系。

---

### Group D — 替换（原：🔴 必须替换，含 BUG 注释）

**最终 mutation**：
```diff
         elif (
             self.query.default_ordering and
-            self.query.get_meta().ordering and
-            # A default ordering doesn't affect GROUP BY queries.
-            not self.query.group_by
+            self.query.get_meta().ordering
         ):
```
**变异语义**：删除 `not self.query.group_by` 条件，恢复原始 buggy 行为。有 GROUP BY 且有 Meta.ordering 的查询仍然报告 `ordered=True`，F2P 测试 `assertIs(qs.ordered, False)` 失败。这是对原始 bug 的精确还原，代码看起来简洁合理（两个布尔条件），开发者可能认为 GROUP BY 不影响有序性判断。

---

### Group E — 保留

**原 mutation**：
```diff
-    def ordered(self):
+    def ordered(self, _check_group_by=False):
     ...
-            not self.query.group_by
+            not (_check_group_by and self.query.group_by)
```
**分类**：🟢 保留
**理由**：E2 策略。`ordered` 是 `@property`，访问时不传参数，`_check_group_by` 永远为 `False`，`not (False and group_by) = not False = True`，group_by 检查永远不生效。GROUP BY 查询仍然 `ordered=True`，F2P 测试失败。代码看起来像是提供了可选的严格模式，实则默认关闭使修复失效。

## 新设计 Mutation 说明

### Group A（A1 — 类型判断错误）
Django 的 `group_by` 使用 `True` 作为哨兵值表示"按所有 GROUP BY 字段分组"（由 `annotate()` 设置），并用列表表示显式字段列表（由 `values().annotate()` 设置）。`isinstance(True, (list, tuple))` 为 `False`，导致哨兵值路径（`group_by=True`）的 GROUP BY 无法被正确检测，仅能检测显式列表形式。

### Group C（C1 — 属性混淆）
`annotation_select` 和 `group_by` 都与 annotation 有关，但含义不同：`annotation_select` 是 annotation 输出字典，`group_by` 是 SQL GROUP BY 标志。使用 `annotation_select` 检查会错误地将所有含 annotation 的查询视为无序，即使没有实际的 GROUP BY。
