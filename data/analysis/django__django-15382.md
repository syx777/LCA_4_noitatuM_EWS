# django__django-15382

## 问题背景

对带空 queryset 的 `Exists` 子查询取反后再加过滤（`~Exists(MyModel.objects.none()), name='test'`），整个 WHERE 块会被错误删除，查询变成 `EmptyResultSet`。根因：`Exists.as_sql` 调用 `super().as_sql` 时，空子查询抛 `EmptyResultSet`，未被捕获就向上传播，把整个查询判空。Golden patch 用 `try/except EmptyResultSet` 包裹：当 `EmptyResultSet` 且 `self.negated`（取反）时返回空 SQL `('', ())`（`NOT EXISTS(空)` 恒真，应从 WHERE 中消失而非清空整个查询），否则重新抛出。

## Golden Patch 语义分析

```python
query = self.query.exists(using=connection.alias)
try:
    sql, params = super().as_sql(..., query=query, ...)
except EmptyResultSet:
    if self.negated:
        return '', ()
    raise
if self.negated:
    sql = 'NOT {}'.format(sql)
return sql, params
```
核心语义：**取反的空 Exists 等价于恒真条件，应贡献空 SQL（不影响 WHERE 其它部分）**；非取反的空 Exists 才应让 `EmptyResultSet` 传播（整个查询确实为空）。`self.negated` 标志区分这两种情况。

F2P 测试 `ExistsTests.test_negated_empty_exists`：`~Exists(Manager.objects.none()) & Q(pk=manager.pk)` 应返回该 manager（恒真的 NOT EXISTS 不影响 pk 过滤），而非空结果集。

## 调用链分析

`Exists.__init__(queryset, negated=False)` 存 `self.negated`；`__invert__` 翻转它。`as_sql` 调 `super().as_sql`（Subquery）编译子查询，空 queryset 触发 `EmptyResultSet`。修复后取反场景返回 `('', ())`，外层 WHERE 拼接时该条件消失。`self.negated` 的正确存储与 except 分支的正确判断共同决定行为。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | except 内 `if self.negated`→`if not self.negated`，取反场景反而 re-raise 清空查询 |
| B | 🔴 必须替换 | 替换 | 原 A=B 重复（同 invert）；改为返回错误哨兵 `'0 = 1'` |
| C | 🟢 高质量 | 保留 | 注释掉 `self.negated = negated`，AttributeError/状态丢失 |
| D | 🟢 高质量 | 保留 | 移除 try/except，EmptyResultSet 直接传播清空查询 |
| E | ➕ 补充 | 新增 | 原缺 E 组 |

原 A=B 重复（都 invert except 条件）。重做 B，补充 E。

## 各组 Mutation 分析

### Group A — 保留（B3 逻辑反转）
```diff
         except EmptyResultSet:
-            if self.negated:
+            if not self.negated:
                 return '', ()
             raise
```
**变异语义**：把 except 内的取反判断反转。取反的空 Exists 落到 `raise` 分支→`EmptyResultSet` 传播→整个查询清空（正是原 bug）；非取反的反而被吞掉返回空 SQL。F2P 的 `~Exists(none())` 场景查询被清空，失败。保留。

### Group B — 替换（B2 错误哨兵值）
**原**：与 A 重复（invert）。
**最终 mutation**：
```diff
         except EmptyResultSet:
             if self.negated:
-                return '', ()
+                return '0 = 1', ()
```
**变异语义**：取反空 Exists 时返回 `'0 = 1'`（恒假 SQL）而非空字符串。本应"恒真、从 WHERE 消失"，却变成"恒假、过滤掉所有行"，`~Exists(none()) & Q(pk=...)` 结果为空。模拟"以为要返回一个占位条件、却选了恒假表达式"的哨兵值错误。

### Group C — 保留（D1 状态初始化：丢失 negated）
```diff
     def __init__(self, queryset, negated=False, **kwargs):
-        self.negated = negated
+        # self.negated = negated
         super().__init__(queryset, **kwargs)
```
**变异语义**：注释掉 `self.negated = negated`，实例不再有 `negated` 属性（或继承默认）。`as_sql` 中 `self.negated` 读取出错/恒假，取反语义完全丢失，F2P 失败。状态初始化缺失。保留。

### Group D — 保留（D-移除 try/except）
```diff
-        try:
-            sql, params = super().as_sql(...)
-        except EmptyResultSet:
-            if self.negated:
-                return '', ()
-            raise
+        sql, params = super().as_sql(...)
```
**变异语义**：移除整个 try/except，空子查询的 `EmptyResultSet` 直接向上传播，取反场景查询被清空——直接还原原 bug。保留。

### Group E — 补充（E1 测试期望：__invert__ 不翻转）
```diff
     def __invert__(self):
         clone = self.copy()
-        clone.negated = not self.negated
+        clone.negated = self.negated
         return clone
```
**变异语义**：`__invert__`（`~` 运算符）本应翻转 `negated`，改成原样赋值后 `~Exists(...)` 不再标记为取反。于是 `as_sql` 的 except 分支 `if self.negated` 为假 → `raise` → 空子查询使整个查询清空。模拟"取反操作忘了真正翻转标志"。这是与 A/B/C/D 不同的入口（`__invert__` 而非 `as_sql`/`__init__`）。

## 新设计 Mutation 说明

原 A=B 都反转 except 内的 `self.negated` 判断，重复。本次保留 A（invert），把 B 改为"返回恒假哨兵 `'0 = 1'`"，并补充 E（`__invert__` 不翻转 negated）。五组覆盖四个不同入口/机制：A（except 条件反转）、B（错误返回值）、C（__init__ 丢失 negated）、D（移除 try/except）、E（__invert__ 不翻转）。全部实测：golden 通过、变异令 F2P（`test_negated_empty_exists`）失败、`base→golden→test_patch` 后干净应用。
