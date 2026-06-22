# django__django-11734 Mutation 分析

## 问题背景

该 issue 修复的是在子查询中使用 `OuterRef` 配合 `exclude()` 时的崩溃/错误结果问题。当对一个 N-to-many 关系做 `exclude()` 时，Django 会调用 `Query.split_exclude()` 把过滤条件改写成 `NOT (pk IN (SELECT ...))` 形式的子查询。原始代码只处理了 `filter_rhs` 是 `F` 表达式的情形，没有处理 `OuterRef` 的情形，导致：

- `split_exclude` 拿到的 `OuterRef` 值未被重新包装，进入内层 `Query` 后无法正确解析为外层引用；
- `RelatedLookupMixin.get_prep_lookup` 用 `rhs_is_direct_value()` 判断时，`OuterRef`（其 `as_sql` 抛错，但不是直接值）被错误处理；
- `AutoFieldMixin.get_prep_value` 对 `OuterRef` 做了 hack。

F2P 测试 `queries.tests.ExcludeTests.test_subquery_exclude_outerref` 构造了一个 `Exists(Responsibility.objects.exclude(jobs=OuterRef('job')))` 子查询，断言删除前后 `qs.exists()` 结果由 True 变 False。

## Golden Patch 语义分析

Golden patch 三处改动：

1. `fields/__init__.py`：**删除** `AutoFieldMixin.get_prep_value` 的 OuterRef 特判（不再需要 hack）。
2. `fields/related_lookups.py`：把判断条件从 `self.rhs_is_direct_value()` 改为 `not hasattr(self.rhs, 'resolve_expression')`，更精确地排除任何可解析表达式（含 OuterRef）。
3. `sql/query.py` `split_exclude`：在 `isinstance(filter_rhs, F)` 之前**新增** `isinstance(filter_rhs, OuterRef)` 分支，把 `OuterRef` 重新包装为 `(filter_lhs, OuterRef(filter_rhs))`，使内层子查询能正确解析外层引用。

核心修复点是 `query.py` 中的新分支：

```python
if isinstance(filter_rhs, OuterRef):
    filter_expr = (filter_lhs, OuterRef(filter_rhs))
elif isinstance(filter_rhs, F):
    filter_expr = (filter_lhs, OuterRef(filter_rhs.name))
```

`OuterRef.resolve_expression` 中 `if isinstance(self.name, self.__class__): return self.name`，说明嵌套 `OuterRef(OuterRef(...))` 是被设计支持的，用于在内层子查询里把引用层级再下推一层。

## 调用链分析

`QuerySet.exclude()` → `Query.add_q()` → `build_filter(..., branch_negated/current_negated)` → 对 N-to-many 关系调用 `split_exclude(filter_expr, can_reuse, names_with_path)`。`split_exclude` 内部新建 `Query`，`add_filter(filter_expr)`，再用 `'%s__in' % trimmed_prefix` 把内层 query 作为子查询挂回。`RelatedLookupMixin.get_prep_lookup` / `RelatedIn` 是 `__in` lookup 在准备 rhs 时被触发的位置——注意该位置同时服务于普通 `F` 表达式 exclude（大量 P2P 测试），因此对它的变异极易破坏 P2P。

## 替换决策总览

| 组 | 原 strategy_code 性质 | 分类 | 决策 |
|----|----------------------|------|------|
| A | related_lookups 条件还原为 `rhs_is_direct_value()`（golden 反向） | 🔴 直接冗余 + 破坏 3 个 P2P | 替换 |
| B | query.py 条件加 `not`（单 token swap） | 🟡 但破坏 32 个 P2P，过广/易检测 | 替换 |
| D | query.py body 改为 `pass # Removed ...` 注释 | 🔴 非自然 artifact 注释 | 替换 |
| E | query.py 条件加 `and False` 死代码 | 🔴 非自然 artifact | 替换 |

说明：A 是 golden patch 的精确反向（直接冗余）；D、E 含明显人工痕迹（解释性注释、`and False`）。B 虽是单 token swap（floor(M/2)=0 本应可保留），但实测把过滤逻辑反转后内层 query 对所有非 OuterRef 路径误改写，破坏 32 个既有 P2P 测试，属"破坏 P2P 必须重设计"。故四组全部替换为经验证的 F2P-only 变异。

## 各组 Mutation 分析

所有替换均落在真正的 bug 点 `split_exclude`（`query.py`），保证只在 OuterRef 子查询路径触发、对 F/直接值 exclude 路径无影响（实测整模块仅 F2P 失败）。

### A 组（原：golden 反向；新：C1 条件操作数错误）
- 原 diff：`not hasattr(self.rhs, 'resolve_expression')` → `self.rhs_is_direct_value()`，即 golden patch 的精确反向。分类 🔴。理由：直接冗余，且改 related_lookups 共享路径破坏 3 个 P2P。
- 最终 diff：`if isinstance(filter_rhs, OuterRef):` → `if isinstance(filter_lhs, OuterRef):`
- 变异语义：守卫条件检查了错误的操作数 `filter_lhs`（field-path 字符串），OuterRef 分支永不触发，body 行原样保留，外观合理。

### B 组（原：`not` 反转；新：C2 包装错误变量）
- 原 diff：`if isinstance(filter_rhs, OuterRef):` → `if not isinstance(...):`。分类 🟡→实测破坏 32 P2P。
- 最终 diff：`OuterRef(filter_rhs)` → `OuterRef(filter_lhs)`
- 变异语义：把 lhs（字段路径）包进 OuterRef，引用了错误的列；SQL 仍能构建，仅 OuterRef 子查询结果错误。

### D 组（原：`pass` 注释 artifact；新：C3 lhs/rhs 元组位置交换）
- 原 diff：body 改为 `pass  # Removed filter_expr initialization`。分类 🔴 人工注释痕迹。
- 最终 diff：`(filter_lhs, OuterRef(filter_rhs))` → `(filter_rhs, OuterRef(filter_rhs))`
- 变异语义：重建的 filter 元组里把 rhs 误当作 lhs，模拟真实 copy-paste 失误；仅 OuterRef 路径到达此行。

### E 组（原：`and False` artifact；新：D1 类型混淆）
- 原 diff：`isinstance(...) and False` 死代码。分类 🔴。
- 最终 diff：`OuterRef(filter_rhs)` → `OuterRef(filter_rhs.name)`
- 变异语义：把下面 F 分支的写法 `OuterRef(filter_rhs.name)` 误用到 OuterRef 上，混淆 OuterRef 与 F 两种表达式类型；只有 rhs 为 OuterRef 时才出错。

## 新设计 Mutation 说明（正交性）

四个替换均位于 `split_exclude` 新增分支，但失败模式正交：

1. **A (C1)**：条件层——检查错误操作数，分支不进入。
2. **B (C2)**：body 层——包装了错误的变量（lhs 而非 rhs）。
3. **D (C3)**：body 层——元组两元素位置错乱。
4. **E (D1)**：body 层——表达式类型混淆（OuterRef 当 F 处理）。

均为现实开发者易犯错误，普通 F/直接值 exclude 测试全绿，仅 OuterRef 子查询 exclude 失败。

## 验证结果（真实运行）

环境：`cp -r repo /tmp/swemut_11734`，依次 `patch -p1` 应用 golden patch 与 test patch，`git commit` 得 POST-PATCH HEAD。

- **Baseline**（仅 golden+test，无变异）：`queries.tests.ExcludeTests.test_subquery_exclude_outerref` PASS（rc=0），整模块 282 tests OK（skipped=3, expected failures=2）。
- **A (C1)**：apply OK，py_compile OK，F2P FAILED(errors=1)，整模块仅 F2P 失败。
- **B (C2)**：apply OK，py_compile OK，F2P FAILED(failures=1)，整模块仅 F2P 失败。
- **D (C3)**：apply OK，py_compile OK，F2P FAILED(errors=1)，整模块仅 F2P 失败。
- **E (D1)**：apply OK，py_compile OK，F2P FAILED(errors=1)，整模块仅 F2P 失败。

被淘汰候选记录：原 B（`not` 反转）破坏 32 个 P2P；related_lookups 上的 `resolve` typo 候选破坏 3 个 P2P——均不满足 F2P-only，已弃用。
