# django__django-15957

## 问题背景

`Prefetch()` 不支持切片 queryset（`Post.objects.all()[:3]`），抛 `AssertionError: Cannot filter a query once a slice has been taken`。因为 prefetch 内部要对 queryset 再 `.filter(field__in=instances)`，而已切片的 query 不允许再 filter。Golden patch 新增 `_filter_prefetch_queryset`：当 queryset 已切片时，用窗口函数 `RowNumber()` 按关系字段分区、按原排序编号，把切片的 `low_mark/high_mark` 转成对行号的 `GreaterThan/LessThanOrEqual` 谓词，再 `clear_limits()` 去掉切片，最后 filter。前向 FK 与反向 M2M 的 `get_prefetch_queryset` 都改用该 helper。

## Golden Patch 语义分析

```python
def _filter_prefetch_queryset(queryset, field_name, instances):
    predicate = Q(**{f"{field_name}__in": instances})
    if queryset.query.is_sliced:
        low_mark, high_mark = queryset.query.low_mark, queryset.query.high_mark
        order_by = [expr for expr, _ in queryset.query.get_compiler(
            using=queryset._db or DEFAULT_DB_ALIAS).get_order_by()]
        window = Window(RowNumber(), partition_by=field_name, order_by=order_by)
        predicate &= GreaterThan(window, low_mark)
        if high_mark is not None:
            predicate &= LessThanOrEqual(window, high_mark)
        queryset.query.clear_limits()
    return queryset.filter(predicate)
```
核心语义：**用窗口行号模拟切片**。每个 partition（同一关系对象的相关行）内按原 order_by 编号，切片 `[low:high]` 等价于 `row_number > low AND row_number <= high`（行号从 1 开始：`low_mark` 是 0-based 起点，故用 `>` 严格大于；`high_mark` 是终点，用 `<=`）。`clear_limits()` 必须调用以移除原切片（否则 filter 仍报错）。边界精度（`>` vs `>=`、`<=` vs `<`）和 low/high 的对应关系是正确性关键。

F2P 测试 `PrefetchLimitTests`（4 个）：对 M2M 前向/反向、FK 反向、reverse ordering 的切片 prefetch，断言 `to_attr` 的切片结果等于手动 `list(...)[1:]` 等。

## 调用链分析

`prefetch_related(Prefetch("rel", qs[1:], to_attr=...))` → 相关 descriptor 的 `get_prefetch_queryset(instances, queryset)` → `_filter_prefetch_queryset(queryset, field_name, instances)`。helper 检测 `is_sliced`，取 `low_mark/high_mark` 与 `order_by`，构造 `Window(RowNumber(), partition_by, order_by)` 窗口，用行号谓词替代切片，`clear_limits()` 清切片，`filter(predicate)`。下界谓词 `GreaterThan(window, low_mark)`、上界 `LessThanOrEqual(window, high_mark)`——任一边界算子错、low/high 错配、或漏 `clear_limits` 都会导致切片结果错误或 filter 报错。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 下界 `GreaterThan`→`GreaterThanOrEqual`，多包含 low_mark 行 |
| B | ➕ 补充 | 新增 | 原缺 B 组；上界 `LessThanOrEqual`→`LessThan`，少包含 high_mark 行 |
| C | ➕ 补充 | 新增 | 原缺 C 组；low_mark/high_mark 对调，切片窗口完全错位 |
| D | ➕ 补充 | 新增 | 原缺 D 组；漏调 `clear_limits()`，filter 仍报切片错误 |
| E | 🟢 高质量 | 保留 | 下界 `GreaterThan`→`LessThanOrEqual`，谓词方向完全反转 |

原实例只有 A、E 两组（都改下界算子），缺 B、C、D。补充 B（上界 off-by-one）、C（low/high 对调）、D（漏 clear_limits），保留 A、E。

## 各组 Mutation 分析

### Group A — 保留（B3 边界：下界 off-by-one）
```diff
-from django.db.models.lookups import GreaterThan, LessThanOrEqual
+from django.db.models.lookups import GreaterThan, GreaterThanOrEqual, LessThanOrEqual
...
-        predicate &= GreaterThan(window, low_mark)
+        predicate &= GreaterThanOrEqual(window, low_mark)
```
**变异语义**：下界从严格 `>` 改成 `>=`。行号从 1 开始、`low_mark` 是 0-based 起点（切 `[1:]` 时 low_mark=1，应保留行号 ≥2 即 `>1`），改成 `>=1` 会多包含行号为 1 的那一行，切片结果多一个元素。F2P 断言 `== list[1:]` 失败。模拟经典 off-by-one。保留。

### Group B — 补充（B3 边界：上界 off-by-one）
```diff
-from django.db.models.lookups import GreaterThan, LessThanOrEqual
+from django.db.models.lookups import GreaterThan, LessThan, LessThanOrEqual
...
-            predicate &= LessThanOrEqual(window, high_mark)
+            predicate &= LessThan(window, high_mark)
```
**变异语义**：上界从 `<=` 改成 `<`，少包含行号等于 `high_mark` 的那一行。切 `[1:2]` 时 high_mark=2，应保留行号 `<=2`，改成 `<2` 会丢掉行号 2 的元素，切片结果少一个。与 A（下界）对称的另一端 off-by-one。`test_m2m_reverse`（用 `[1:2]` 含上界）失败。

### Group C — 补充（C1 类型/数据形状：low/high 对调）
```diff
-        predicate &= GreaterThan(window, low_mark)
-        if high_mark is not None:
-            predicate &= LessThanOrEqual(window, high_mark)
+        predicate &= GreaterThan(window, high_mark)
+        if low_mark is not None:
+            predicate &= LessThanOrEqual(window, low_mark)
```
**变异语义**：把 `low_mark` 与 `high_mark` 在两个谓词里对调——下界用 high_mark、上界用 low_mark，且判空也换成 `low_mark is not None`。窗口变成 `row > high AND row <= low`，对正常 `low<high` 恒为空集（或在 high_mark=None 时行为异常）。切片结果完全错误。模拟"两个边界变量写反"。

### Group D — 补充（D1 状态：漏 clear_limits）
```diff
             predicate &= LessThanOrEqual(window, high_mark)
-        queryset.query.clear_limits()
     return queryset.filter(predicate)
```
**变异语义**：删除 `queryset.query.clear_limits()`。窗口谓词加上了，但原切片 limit 没清除，最后 `queryset.filter(predicate)` 在仍带切片的 query 上 filter，触发原始的 `Cannot filter a query once a slice has been taken` 断言错误。修复"加了窗口替代逻辑、却忘了移除旧切片"这一关键收尾步骤。模拟"状态清理遗漏"。

### Group E — 保留（B3 逻辑反转：下界算子方向反转）
```diff
-        predicate &= GreaterThan(window, low_mark)
+        predicate &= LessThanOrEqual(window, low_mark)
```
**变异语义**：下界谓词从 `GreaterThan(window, low_mark)` 反转成 `LessThanOrEqual(window, low_mark)`。本该"行号 > low"变成"行号 <= low"，与上界 `<= high` 组合后选出的是切片**之前**的行（行号 ≤ low_mark），结果与期望完全相反。比 A 的 off-by-one 更激进的方向反转。保留。

## 新设计 Mutation 说明

原实例只有 A、E 两组（都作用于下界算子：A 改 `>=`、E 改 `<=`），缺 B、C、D。本次保留 A（下界 off-by-one）、E（下界方向反转），补充 B（上界 `<=`→`<` off-by-one，对称的另一端）、C（low_mark/high_mark 两边界变量对调）、D（漏调 `clear_limits()` 使 filter 报切片错）。五组覆盖"下界 off-by-one / 上界 off-by-one / 边界变量对调 / 漏清切片 / 下界方向反转"五个角度，分别命中窗口谓词的下界、上界、变量绑定、状态清理、算子方向五个独立位置。全部实测：golden 通过、五个变异均令 F2P（`PrefetchLimitTests` 四个测试）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
