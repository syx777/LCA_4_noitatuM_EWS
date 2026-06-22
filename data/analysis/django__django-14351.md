# django__django-14351 Mutation 分析

## 问题背景

当对一个带聚合注解的子查询使用 `__in` 查找并与其它 `Q` 对象 `OR` 组合时（例如
`Q(authors__in=authors) | Q(authors__count__gt=2)`），Django 3.2 在构建 `GROUP BY`
子句时会把内层子查询的 **所有默认列**（`get_default_columns`）都加入到分组的
subquery 中，导致数据库报错 `subquery must return only one column`
（SQLite 上表现为 `sub-select returns N columns - expected 1`）。2.2.5 中可正常工作。

根因在 `django/db/models/lookups.py` 的 `In` 查找：`In` 缺少自定义的
`get_group_by_cols`，使得 RHS 子查询在进入 `GROUP BY` 时仍携带全部默认列。

## Golden Patch 语义分析

Golden 为 `In` 新增 `get_group_by_cols`：

```python
def get_group_by_cols(self, alias=None):
    cols = self.lhs.get_group_by_cols()
    if hasattr(self.rhs, 'get_group_by_cols'):
        if not getattr(self.rhs, 'has_select_fields', True):
            self.rhs.clear_select_clause()
            self.rhs.add_fields(['pk'])
        cols.extend(self.rhs.get_group_by_cols())
    return cols
```

语义要点：
1. 先取 LHS（字段列表达式）的分组列。
2. 仅当 RHS 是一个 `Query`（具备 `get_group_by_cols`）时处理 RHS。
3. 关键守卫 `if not getattr(self.rhs, 'has_select_fields', True)`：当 RHS 子查询
   **没有显式 select 字段**（即依赖默认列，正是出错场景）时，先 `clear_select_clause()`
   清空 SELECT，再 `add_fields(['pk'])` 只保留主键单列，保证子查询只返回一列。
4. 最后把 RHS 规整后的分组列并入。

`has_select_fields` 定义于 `sql/query.py:244`，为
`bool(self.select or self.annotation_select_mask or self.extra_select_mask)`。
`clear_select_clause`/`add_fields` 同在 `sql/query.py`。注意 `In.process_rhs`
中存在**字面完全相同**的三行守卫，但作用于不同流程（WHERE 子句的 RHS 处理）。

## 调用链分析

`QuerySet.filter(Q | Q)` → SQL 编译 `SQLCompiler.get_group_by` →
对每个表达式调用 `expr.get_group_by_cols()` →
`In.get_group_by_cols`（golden 新增）→ `self.rhs`（内层 `Query`）的
`clear_select_clause` / `add_fields(['pk'])` / `get_group_by_cols`。

F2P 测试 `tests/aggregation_regress/tests.py::AggregationTests.test_having_subquery_select`
构造 `Book.objects.annotate(Count('authors')).filter(Q(authors__in=authors) | Q(authors__count__gt=2))`，
正好触发上述路径。

**重要的同模块耦合**：`test_more_more`、`test_more_more_more`、`test_negated_aggregation`
等也走 `filter(id__in=<聚合子查询>)`，但它们的 RHS 子查询带显式 select
（`has_select_fields=True`），不进入守卫分支。验证证实：在 buggy 原始代码（无该方法）下，
整个 `aggregation_regress.tests` 模块**仅** F2P 失败，三个兄弟测试通过——它们是真正的 P2P。
因此设计变异时必须保证只破坏 F2P 路径而不触及这些 P2P。

## 替换决策总览

| 组 | 原类别 | 决策 | 原因 |
|----|--------|------|------|
| B | 🟡 单 token swap（`not` 翻转） | **KEEP** | 落在真实控制流边界（`has_select_fields` 守卫），只破坏 F2P，是对边界条件的真实误判 |
| D | 🔴 不自然 dead-guard（注释 "Removed pre-condition check" 删除整段守卫） | **REPLACE** | 注释痕迹+直接禁用 fix，属人工痕迹冗余；替换为 lhs/rhs 操作数混淆 |
| E | 🔴 与 B 字节完全相同的重复 diff | **REPLACE** | byte-identical 重复，去重后替换为"清空但不重建"的半修复 |

共 2 处替换（D、E），保留 1 处（B）。M=1 个 shallow（B）落在关键控制流节点，按规则保留。
两个 🔴 全部替换。

## 各组 Mutation 分析

### 组 B — KEEP
- 原 diff：把 `if not getattr(self.rhs, 'has_select_fields', True):` 改为
  `if getattr(self.rhs, 'has_select_fields', True):`（删除 `not`）。
- 分类：🟡 SEMANTIC-SHALLOW（单 token）。
- 理由：虽为单 token，但作用于本 patch 的核心边界判断，是"把布尔条件判反"的真实开发者错误，
  且经验证只破坏 F2P、不影响 P2P，符合"保留落在关键控制流节点的 shallow"原则。
- 最终 diff：保持原样（已验证）。
- 变异语义：仅当子查询**确有** select 字段时才清列/补 pk，正好把修复作用在错误的分支上；
  对默认列子查询（出错场景）反而跳过修复，F2P 再次报多列错误。

### 组 D — REPLACE
- 原 diff：用注释 `# Removed pre-condition check - now depends on RHS being pre-configured`
  替换整个 `if not ...: clear_select_clause(); add_fields(['pk'])` 守卫块。
- 分类：🔴 MUST REPLACE。
- 理由：含 "Removed pre-condition check" 这类解释性注释属于不自然的人工痕迹，且等价于直接
  撤销 golden 的核心逻辑（裸露的 golden 反向 revert），易被识别。
- 最终 diff：将守卫条件中的 `self.rhs` 改为 `self.lhs`
  （`if not getattr(self.lhs, 'has_select_fields', True):`）。
- 变异语义：操作数混淆——从 LHS（字段列表达式，永远没有 select 子句，`getattr` 取默认
  `True`）读取 `has_select_fields`，使 `not True == False`，守卫体永不执行；RHS 子查询保留全部
  默认列，复现多列错误。看似 lhs/rhs 笔误，恰落在合法混用二者的方法里。

### 组 E — REPLACE
- 原 diff：与组 B 字节完全相同（删除 `not`）。
- 分类：🔴 MUST REPLACE（与 B 重复）。
- 理由：byte-identical duplicate，必须替换以保证多样性。
- 最终 diff：仅删除 `self.rhs.add_fields(['pk'])`，保留 `clear_select_clause()`。
- 变异语义：清空但不重建——SELECT 被清空后没有补回单列 pk，子查询输出 0 列，生成空 SELECT
  的非法 GROUP BY 子查询（SQLite 报 `near "FROM": syntax error`）。这是"半修复"失败模式，
  与 B/D 的条件误判正交。

## 新设计 Mutation 说明（正交性）

三组覆盖三种不同失败机理：
- **B**：布尔守卫条件被判反（错误分支执行修复）。
- **D**：守卫读取错误操作数（lhs vs rhs），守卫体恒不执行。
- **E**：守卫体被部分删除（清空 SELECT 但不重建 pk），产生空列子查询。

三者错误表象也不同：B/D 触发"多列"错误，E 触发"空 SELECT 语法"错误，提升对 LLM 生成测试
的检测难度与多样性。

## 实际验证结果

验证环境：在 base repo 上 `patch -p1` 应用 golden `patch` + `test_patch`，
`git commit` 形成 POST-PATCH HEAD。F2P 模块：
`aggregation_regress.tests`（新增测试 `AggregationTests.test_having_subquery_select`）。
运行命令：`PYTHONPATH=<tmp> python3 tests/runtests.py aggregation_regress.tests --parallel 1 -v`。

- **BASELINE（golden 无变异）**：F2P 单测 `OK`；全模块 `OK (skipped=5)`，65 测试全过。
- **Buggy 前置确认**：移除 golden 方法后全模块仅 `test_having_subquery_select` 失败，
  确认三个兄弟测试为真实 P2P。
- **组 B**：每个 diff 经 `git apply` 与 `patch -p1` 双重应用成功，`py_compile` OK；
  全模块运行 `FAILED (errors=1)`，唯一失败为 `test_having_subquery_select`，无 P2P 回归。
- **组 D**：同上，全模块仅 F2P 失败。
- **组 E**：同上，全模块仅 F2P 失败。
- 所有最终 diff 末尾均以 `\n` 结尾（已用 `xxd` 校验最后字节为 0x0a）。

> 备注：验证过程中发现 `In.process_rhs` 与 `get_group_by_cols` 含字面相同的三行守卫，
> 早期用宽匹配字符串替换会误改两处而错误地破坏 P2P；最终所有变异均锚定
> `get_group_by_cols` 内唯一上下文（`cols.extend(...)`）进行精确替换并复核
> `count==1`，确保仅改目标方法。
