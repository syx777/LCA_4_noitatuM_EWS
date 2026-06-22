# django__django-17084

## 问题背景

Django 4.2 起对 Window 函数做聚合会报错（`psycopg2.errors.GroupingError: aggregate function calls cannot contain window function calls`）。当对一个含 Window 的注解再做 `Sum(...)` 聚合时，Django 没把窗口注解的引用放进子查询包装，导致聚合直接套在窗口函数上。Golden patch 在 `get_aggregation` 里新增 `refs_window` 标志：检测聚合是否引用了含 `contains_over_clause` 的注解，若有则把它纳入"需要子查询包装"的条件。

## Golden Patch 语义分析

```python
refs_window = False
...
refs_window |= any(
    getattr(self.annotations[ref], "contains_over_clause", True)
    for ref in aggregate.get_refs()
)
...
if (... or refs_subquery or refs_window or qualify or ...):
    # use subquery (AggregateQuery)
```
核心语义：**用 `refs_window |= any(getattr(annotation, "contains_over_clause", True) for ref in ...)` 检测聚合是否引用了窗口注解；为真则强制走子查询包装**。关键点：`any`（任一引用是窗口即真）、属性名 `contains_over_clause`、默认值 `True`（保守地认为是窗口）、`|=` 累积（多个聚合 OR 累积）、把 `refs_window` 并入子查询判定条件。

F2P 测试 `AggregateAnnotationPruningTests.test_referenced_window_requires_wrapping`：对含 `Window(Avg("pages"))` 的注解做 `Sum`，断言 SQL 含 2 层 `select`（子查询包装）且结果正确。

## 调用链分析

`get_aggregation(using, aggregate_exprs)` 遍历聚合表达式，对每个聚合 `aggregate.get_refs()` 取其引用的注解；`refs_window |= any(getattr(self.annotations[ref], "contains_over_clause", True) ...)`。最终 `if (... or refs_window or ...)` 决定是否用 `AggregateQuery` 子查询包装。`any`→其它、属性名错、默认值改、`|=`→`=`、或门控开关，都会让窗口引用漏检、不包装子查询 → GroupingError 或结果错。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `getattr(...)` 前加 `not`，判定反转 |
| B | 🟢 高质量 | 重做 | `getattr(...)` 改 `... is False`，窗口注解判定恒假 |
| C | 🟢 高质量 | 保留 | 属性名 `contains_over_clause`→`contains_aggregate`（默认 False） |
| D | 🟢 高质量 | 保留 | `refs_window |= any(...)`→`= any(...)`，累积变覆盖 |
| E | 🟢 高质量 | 重做 | 检测藏到 `wrap_window_refs` 开关后 |

原始 A、E 字节完全相同（`not getattr(...)`），且原 B（`all(not ...)`）实测不破坏 F2P（`all([])` 对无引用聚合恒真、误置 refs_window）。保留 A（not 反转）、C（属性名错）、D（|=→=），重做 B（`is False` 判定）、E（开关）。

## 各组 Mutation 分析

### Group A — 保留（B3 条件反转：not getattr）
```diff
             refs_window |= any(
-                getattr(self.annotations[ref], "contains_over_clause", True)
+                not getattr(self.annotations[ref], "contains_over_clause", True)
                 for ref in aggregate.get_refs()
             )
```
**变异语义**：`getattr(...)` 前加 `not`，判定反转——窗口注解（`contains_over_clause=True`）被 `not` 成假、非窗口注解反成真。`refs_window` 对真正的窗口引用不被置真 → 不包装子查询；反而对普通引用误置真。F2P（窗口注解需包装）失败。保留。

### Group B — 重做（C1 类型：is False 判定）
**原**：与 A/E 相同（`not getattr(...)`），且另一形式 `all(not ...)` 实测不破坏（`all([])` 恒真使无引用聚合误置 refs_window=True）。
**最终 mutation**：
```diff
             refs_window |= any(
-                getattr(self.annotations[ref], "contains_over_clause", True)
+                getattr(self.annotations[ref], "contains_over_clause", True) is False
                 for ref in aggregate.get_refs()
             )
```
**变异语义**：把 `getattr(...)` 改成 `getattr(...) is False`。窗口注解 `contains_over_clause` 为 True 时 `is False` 为假 → `refs_window` 不被置真 → 窗口引用不触发子查询包装。模拟"误加 `is False` 比较、把真值判定整个反过来且收窄成严格等于 False"。与 A（`not`）形式不同——`is False` 只对严格 False 成立（None/其它真值都为假）。重做为 B。

### Group C — 保留（A1 接口契约：属性名错）
```diff
             refs_window |= any(
-                getattr(self.annotations[ref], "contains_over_clause", True)
+                getattr(self.annotations[ref], "contains_aggregate", False)
                 for ref in aggregate.get_refs()
             )
```
**变异语义**：检测的属性名 `contains_over_clause` 错写成 `contains_aggregate`（默认值也从 True 改成 False）。窗口注解的 over clause 不再被检测，`contains_aggregate` 对 Window-only 注解为假 → `refs_window` 恒假 → 不包装子查询。模拟"取错了属性名（两个 contains_* 属性混淆）"。F2P 失败。保留。

### Group D — 保留（D1 状态：|= 变 =）
```diff
-            refs_window |= any(
+            refs_window = any(
                 getattr(self.annotations[ref], "contains_over_clause", True)
                 for ref in aggregate.get_refs()
             )
```
**变异语义**：`refs_window |= any(...)`（累积 OR）改成 `refs_window = any(...)`（直接赋值）。多个聚合表达式循环时，每轮覆盖 `refs_window`——前面聚合检测到的窗口引用被后面非窗口聚合的 False 覆盖丢失。F2P 用例有 `Sum(window)` 和 `Count("id")` 两个聚合，若 Count 在后则覆盖成 False → 不包装。模拟"误把累积或赋值写成普通赋值"。保留。

### Group E — 重做（E2 隐式→显式开关）
**原**：与 A 字节相同（`not getattr(...)`）。
**最终 mutation**：
```diff
-            refs_window |= any(
+            refs_window |= getattr(self, "wrap_window_refs", False) and any(
                 getattr(self.annotations[ref], "contains_over_clause", True)
                 for ref in aggregate.get_refs()
             )
```
**变异语义**：窗口引用检测前置 `getattr(self, "wrap_window_refs", False) and` 开关（默认 False）。默认情况下 `False and any(...)` 短路为假 → `refs_window` 恒不被置真 → 窗口聚合不包装子查询，还原 GroupingError 场景。只有显式设 `wrap_window_refs=True` 才检测。模拟"把窗口引用包装做成可配置、默认却关掉"。重做为 E。

## 新设计 Mutation 说明

原始 A、E 字节完全相同（`not getattr(...)`），原 B（`all(not ...)`）实测不破坏 F2P（`all([])` 对无引用聚合恒真、误置 refs_window，反而触发包装）。本次保留 A（`not` 反转）、C（属性名 contains_aggregate）、D（`|=`→`=` 覆盖），重做 B（`is False` 严格判定使窗口判定恒假）、E（`wrap_window_refs` 默认关闭开关）。五组覆盖"not 反转 / is False 判定 / 属性名错 / 累积变覆盖 / 默认关闭开关"五个角度——全部令窗口引用漏检、不包装子查询。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
