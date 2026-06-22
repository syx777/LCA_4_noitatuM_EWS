# django__django-14122 Mutation 分析

## 问题背景

`Meta.ordering` 字段不应被加入 `GROUP BY` 子句。历史 commit `0ddb4ebf` 虽然在使用 `Meta.ordering` 时移除了 `ORDER BY`，但仍把 `Meta.ordering` 字段填进了 `GROUP BY`，导致聚合结果错误。

F2P 测试 `tests/ordering/tests.py::OrderingTests::test_default_ordering_does_not_affect_group_by`：

```python
def test_default_ordering_does_not_affect_group_by(self):
    Article.objects.exclude(headline='Article 4').update(author=self.author_1)
    Article.objects.filter(headline='Article 4').update(author=self.author_2)
    articles = Article.objects.values('author').annotate(count=Count('author'))
    self.assertCountEqual(articles, [
        {'author': self.author_1.pk, 'count': 3},
        {'author': self.author_2.pk, 'count': 1},
    ])
```

`Article.Meta.ordering = ('-pub_date', 'headline')`。若 ordering 字段进入 GROUP BY，则按 (author, pub_date, headline) 分组，每行 count 都退化为 1，断言失败。

## Golden Patch 语义分析

```python
-        for expr, (sql, params, is_ref) in order_by:
-            if not is_ref:
-                expressions.extend(expr.get_group_by_cols())
+        if not self._meta_ordering:
+            for expr, (sql, params, is_ref) in order_by:
+                if not is_ref:
+                    expressions.extend(expr.get_group_by_cols())
```

即：当 `self._meta_ordering` 为真（ordering 来自 `Meta.ordering` 而非显式 `order_by`）时，跳过把 order_by 列加入 GROUP BY。

## 调用链分析

- `SQLCompiler.__init__` (line 41)：`self._meta_ordering = None` 初始化。
- `pre_sql_setup` (line 49)：先 `order_by = self.get_order_by()`，再 `group_by = self.get_group_by(...)`，二者顺序依赖。
- `get_order_by` (line 271)：仅当 ordering 取自 `self.query.get_meta().ordering` 时设置 `self._meta_ordering = ordering`（line 288）。
- `get_group_by` (line 63)：在 line 128 读取 `self._meta_ordering` 作为守卫。
- `as_sql` (line 599)：`if self._meta_ordering: order_by = None`，仅抑制 ORDER BY 输出，不影响 F2P 断言（F2P 只校验聚合分组，不校验排序）。

结论：F2P 的成败完全由 line 128 守卫表达式的真值决定，而该真值又由 `_meta_ordering` 这一产生(line 288)→消费(line 128)的状态链驱动。任何对守卫本身的真值反转都等价于输入变异（golden 回退），因此替换必须落在状态传播链上以制造正交失败模式。

## 替换决策总览

| 组 | 类别 | 决策 | 原因 |
|----|------|------|------|
| A | 🔴 MUST REPLACE | 替换为 C2 (D1) | 与 B/D/E 完全相同的守卫反转，是 golden 的直接回退 |
| B | 🔴 MUST REPLACE | 替换为 C4 (B3) | 与 A/D/E 完全相同的守卫反转，golden 直接回退 |
| D | 🔴 MUST REPLACE | 替换为 C3 (D1) | 守卫反转 + 字面 `# BUG: Inverted condition` 注释，非自然伪影 |
| E | 🔴 MUST REPLACE | 替换为 C1 (D3) | 与 A/B/D 完全相同的守卫反转，golden 直接回退 |

4 个输入变异 diff 逐字节相同（仅 D 多一条 "BUG" 注释），全部属于直接冗余/不自然伪影，故 4 个全部替换。

## 各组 Mutation 分析

### 组 A（替换）
- 原 diff：`if not self._meta_ordering:` → `if self._meta_ordering:`
- 分类：🔴 direct redundancy（golden 回退）
- 理由：直接将守卫反转回有 bug 的行为，与 B/D/E 重复。
- 最终 diff（C2，strategy_code D1）：
```diff
@@ -286,7 +286,7 @@ class SQLCompiler:
             ordering = self.query.order_by
         elif self.query.get_meta().ordering:
             ordering = self.query.get_meta().ordering
-            self._meta_ordering = ordering
+            self._meta_ordering = self.query.order_by
         else:
             ordering = []
```
- 变异语义：把状态绑定到错误来源。`Meta.ordering` 路径下 `self.query.order_by` 为空列表（falsy），守卫永远不触发，GROUP BY 被污染。任何显式 `order_by` 的查询都正常，只有隐式 Meta.ordering 聚合被破坏。
- 验证：F2P FAILED（rc=1）；全模块 27 测试仅此 1 个失败。

### 组 B（替换）
- 原 diff：同上守卫反转。
- 分类：🔴 direct redundancy。
- 最终 diff（C4，strategy_code B3）：
```diff
@@ -125,7 +125,7 @@ class SQLCompiler:
             cols = expr.get_group_by_cols()
             for col in cols:
                 expressions.append(col)
-        if not self._meta_ordering:
+        if self.query.group_by is not True:
             for expr, (sql, params, is_ref) in order_by:
```
- 变异语义：用 group_by 形状谓词替换 meta-ordering 守卫。`.values().annotate()` 时 `group_by` 是列表（不为 True），分支仍执行并污染 GROUP BY。条件看似合理且对多数聚合正确，仅掩盖 Meta.ordering 专属缺陷。
- 验证：F2P FAILED（rc=1）；全模块仅此 1 个失败。

### 组 D（替换）
- 原 diff：守卫反转 + `# BUG: Inverted condition` 注释。
- 分类：🔴 不自然伪影（字面 "BUG" 注释）。
- 最终 diff（C3，strategy_code D1）：
```diff
@@ -54,6 +54,7 @@ class SQLCompiler:
         """
         self.setup_query()
         order_by = self.get_order_by()
+        self._meta_ordering = None
         self.where, self.having = self.query.where.split_having()
         extra_select = self.get_extra_select(order_by, self.select)
         self.has_extra_select = bool(extra_select)
```
- 变异语义：在 `get_order_by()` 之后、`get_group_by()` 之前把 `_meta_ordering` 重置为 None，抹掉刚记录的状态。形似一句防御性 re-init；对无 Meta.ordering 的查询无害，却重新打开 GROUP BY 污染。
- 验证：F2P FAILED（rc=1）；全模块仅此 1 个失败。

### 组 E（替换）
- 原 diff：同守卫反转。
- 分类：🔴 direct redundancy。
- 最终 diff（C1，strategy_code D3）：
```diff
@@ -286,7 +286,6 @@ class SQLCompiler:
             ordering = self.query.order_by
         elif self.query.get_meta().ordering:
             ordering = self.query.get_meta().ordering
-            self._meta_ordering = ordering
         else:
             ordering = []
```
- 变异语义：删除 `self._meta_ordering = ordering` 赋值，切断 produce→consume 的跨方法握手。flag 停留在 `__init__` 的 None，守卫永不触发。对任何显式排序查询无语法/行为改变，只破坏跨方法状态依赖。
- 验证：F2P FAILED（rc=1）；全模块仅此 1 个失败。

## 新设计 Mutation 说明

4 个替换覆盖 4 种正交机制，均作用于 `_meta_ordering` 状态链而非守卫真值的简单反转：

1. C1（删除赋值，D3）：切断顺序依赖中的生产端。
2. C2（错误来源变量，D1）：状态初始化/绑定到错误对象。
3. C3（多余重置，D1）：在消费前重置状态。
4. C4（条件替换，B3）：用看似合理的 group_by 形状谓词偷换语义守卫。

四者都能通过常规显式 `order_by` / 非聚合查询（全部 26 个 P2P 测试通过），仅在 Meta.ordering + 聚合的 F2P 场景失败，且都比单 token 守卫反转更隐蔽、更贴近真实开发者错误。

## 验证总结

- 基线（golden + test_patch，无变异）：`ordering.tests` 27 测试全过（OK）。
- 4 个最终变异：均 apply 干净、py_compile 通过、F2P FAILED（rc=1）。
- 每个变异跑全模块确认仅 F2P 1 个失败，未破坏任何 P2P。
