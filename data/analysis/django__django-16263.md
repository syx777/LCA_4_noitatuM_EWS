# django__django-16263

## 问题背景

`Book.objects.annotate(Count('chapters')).count()` 生成的 SQL 包含了 `Count('chapters')`，尽管 `count()` 并不使用它。这拖慢了带复杂 annotation 的 count 查询。Django 应当剥离 count 查询中未被过滤/其它 annotation/排序引用的 annotation。Golden patch 在聚合查询构造时分析哪些 annotation 真正被聚合引用，把无关 annotation 从内/外层查询的 annotation_mask 中剔除（用 `set_annotation_mask`），并新增 `get_refs()` 收集表达式引用、`refs_expression`/`solve_lookup_type` 支持 `summarize` 等多文件改动。

## Golden Patch 语义分析

核心在 `Query.get_aggregation`（query.py）：
```python
existing_annotations = {alias: ann for alias, ann in self.annotation_select.items()
                        if alias not in added_aggregate_names}
has_existing_aggregation = any(
    getattr(ann, "contains_aggregate", True) or getattr(ann, "contains_over_clause", True)
    for ann in existing_annotations.values()
) or any(self.where.split_having_qualify()[1:])
...
if (... or has_existing_aggregation or ...):   # 决定是否用子查询
    ...
    # GROUP BY 分支：构造 inner_query 后
    annotation_mask = set()
    for name in added_aggregate_names:
        annotation_mask.add(name)
        annotation_mask |= inner_query.annotations[name].get_refs()
    inner_query.set_annotation_mask(annotation_mask)   # 只保留被引用的
else:
    # 无 GROUP BY 分支：内联替换 + mask
    ...
    self.set_annotation_mask(added_aggregate_names)
```
配套：`BaseExpression.get_refs()`（聚合所有子表达式的引用）、`Ref.get_refs()` 返回 `{self.refs}`、`WhereNode.get_refs()`、`refs_expression` 返回引用名、`solve_lookup_type(summarize)` 在 summarize 时把 annotation 包成 `Ref`。

核心语义：**count()/aggregate() 只需保留被聚合真正引用的 annotation，其余应通过 annotation_mask 剔除以避免无谓的 SELECT 列与可能的子查询包裹**。`get_refs()` 沿表达式树收集 `Ref` 名构成"被引用集合"，`set_annotation_mask` 据此收窄。是否需要子查询由 `has_existing_aggregation`（存在聚合/窗口 annotation 或 HAVING/QUALIFY 过滤）决定。

F2P 测试 `AggregateAnnotationPruningTests`：`test_unused_aliased_aggregate_pruned`（alias 的 Count 不出现、无子查询）、`test_non_aggregate_annotation_pruned`（Lower 注解不出现、无子查询）、`test_unreferenced_aggregate_annotation_pruned`（未引用的 Count 不出现、但需子查询）、`test_referenced_aggregate_annotation_kept`（被 Avg 引用的 Count 保留、需子查询）。

## 调用链分析

`QuerySet.count()` → `Query.get_aggregation(using, ["__count"])`。该方法据 `has_existing_aggregation` 等决定是否包子查询；GROUP BY 分支构造 `inner_query` 后用 `annotation_mask`（= added_aggregate_names ∪ 各自 `get_refs()`）调 `inner_query.set_annotation_mask` 剔除无关 annotation；无 GROUP BY 分支内联替换 existing annotations 并 `self.set_annotation_mask(added_aggregate_names)`。`get_refs()` 决定哪些 annotation 被视作"被引用"。mask 调用缺失、refs 收集错、子查询决策错都会让无关 annotation 残留在 SQL（F2P 的 `assertNotIn`/`select count` 断言失败）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 子查询决策去掉 `has_existing_aggregation` 项，需子查询时不包 |
| B | 🟢 高质量 | 保留 | 删除 GROUP-BY 分支的 `inner_query.set_annotation_mask`，无关 annotation 残留 |
| C | ➕ 补充 | 新增 | `Ref.get_refs()` 返回空集，被引用 annotation 不进 mask 被误剔 |
| D | ➕ 补充 | 新增 | 删除无-GROUP-BY 分支的 `self.set_annotation_mask`，annotation 不剔除 |
| E | 🟢 高质量 | 保留 | mask 调用藏到默认关闭开关后 |

原实例只有 B、E 两组（都围绕 GROUP-BY 分支的 `set_annotation_mask`）。保留 B、E，补充 A、C、D，分布到子查询决策、Ref.get_refs、无-GROUP-BY 分支等不同杠杆点。

## 各组 Mutation 分析

### Group A — 补充（B3 条件：子查询决策漏项）
```diff
             or self.is_sliced
-            or has_existing_aggregation
             or self.distinct
```
**变异语义**：从"是否需要子查询"的或条件中删去 `has_existing_aggregation`。当存在聚合 annotation（如被引用的 Count）本应包子查询时，决策漏掉该信号 → 不包子查询 → 结果错误或 SQL 结构不符。`test_referenced_aggregate_annotation_kept`/`test_unreferenced_aggregate_annotation_pruned`（断言 `select count == 2`，即需子查询）失败。模拟"复合决策条件漏了一个分支"。

### Group B — 保留（B2 删除 mask 调用）
```diff
                     annotation_mask |= inner_query.annotations[name].get_refs()
-                inner_query.set_annotation_mask(annotation_mask)
```
**变异语义**：删除 GROUP-BY 分支里对 `inner_query` 的 `set_annotation_mask`。内层查询不剔除无关 annotation，所有 annotation 残留进 SQL。`test_unreferenced_aggregate_annotation_pruned`（断言 `authors_count` 不出现）失败。保留。

### Group C — 补充（A1 接口契约：Ref.get_refs 空集）
```diff
     def get_refs(self):
-        return {self.refs}
+        return set()
```
**变异语义**：`Ref.get_refs()` 返回空集而非 `{self.refs}`。聚合表达式收集引用时，`Ref`（指向被引用 annotation）贡献为空 → `annotation_mask` 不含被引用的 annotation → 它被错误地从 mask 剔除。`test_referenced_aggregate_annotation_kept`（断言被 Avg 引用的 `authors_count` 出现两次、被保留）失败。模拟"引用收集的叶子节点返回空、漏报引用"。隐蔽——上层 `get_refs` 逻辑都对，只是 Ref 这个叶子失效。

### Group D — 补充（B2 删除无-GROUP-BY 分支 mask）
```diff
                 for name in added_aggregate_names:
                     self.annotations[name] = self.annotations[name].replace_expressions(
                         replacements
                     )
-                self.set_annotation_mask(added_aggregate_names)
```
**变异语义**：删除 `else`（无 GROUP BY）分支末尾的 `self.set_annotation_mask(added_aggregate_names)`。该分支处理"内联替换 existing annotations 后只保留聚合"的场景；缺 mask 调用则 existing annotations 不被收窄，残留进外层 SQL。与 B（GROUP-BY 分支的 inner_query mask）是**对称的另一分支**——同名方法、不同代码路径。模拟"两个分支只补了一个的 mask"。

### Group E — 保留（E2 隐式→显式开关）
```diff
                     annotation_mask |= inner_query.annotations[name].get_refs()
-                inner_query.set_annotation_mask(annotation_mask)
+                if getattr(inner_query, "prune_annotations", False):
+                    inner_query.set_annotation_mask(annotation_mask)
```
**变异语义**：把 GROUP-BY 分支的 mask 调用藏到 `inner_query.prune_annotations` 开关后，默认 `False` → 不剔除。只有显式开启才剪枝。模拟"把剪枝做成可配置、默认却关掉"。与 B（直接删）形式不同。保留。

## 新设计 Mutation 说明

原实例只有 B、E 两组，且都围绕 GROUP-BY 分支的同一行 `inner_query.set_annotation_mask`（B 删、E 注释/旁路），缺对其它杠杆点的覆盖。本次保留 B（删 GROUP-BY 分支 mask）、E（开关旁路同一 mask），补充 A（子查询决策漏 `has_existing_aggregation`）、C（`Ref.get_refs` 返回空集使引用漏报）、D（删无-GROUP-BY 分支的 `self.set_annotation_mask`）。五组分布在 `query.py` 的子查询决策、两个 mask 分支与 `expressions.py` 的 `Ref.get_refs`，覆盖"子查询决策 / GROUP-BY mask / 引用收集叶子 / 无-GROUP-BY mask / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令 F2P（`AggregateAnnotationPruningTests`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
