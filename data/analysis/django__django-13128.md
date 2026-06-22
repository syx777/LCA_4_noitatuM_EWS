# django__django-13128 Mutation 策展分析

## 问题背景

`make temporal subtraction work without ExpressionWrapper`。原本对时间类型做减法（如 `F('end') - F('start')`）必须显式包裹 `ExpressionWrapper(..., output_field=DurationField())`，否则抛出
`FieldError: Expression contains mixed types ... You must set output_field`。需求是让框架在 `CombinedExpression` 中自动识别时间字段相减/时长混算，自动路由到 `TemporalSubtraction` / `DurationExpression`，无需用户手写 wrapper。

## Golden Patch 语义分析

文件：`django/db/models/expressions.py`，类 `CombinedExpression`。

核心改动是把类型判断与子类路由逻辑从 `as_sql()` **移动到** `resolve_expression()`：

1. `as_sql()` 删除了对 `lhs_type/rhs_type` 的探测与 Duration/Temporal 分流（删除）。
2. `resolve_expression()` 中先解析出 `lhs/rhs`，随后在 `not isinstance(self, (DurationExpression, TemporalSubtraction))` 前提下：
   - `lhs_type/rhs_type` 通过 `output_field.get_internal_type()` 探测，异常捕获从 `FieldError` 放宽到 `(AttributeError, FieldError)`；
   - 若 `'DurationField' in {lhs_type, rhs_type}` 且两者不同 → 路由到 `DurationExpression`；
   - `datetime_fields = {'DateField','DateTimeField','TimeField'}`，若 `connector == SUB` 且 `lhs_type in datetime_fields` 且 `lhs_type == rhs_type` → 路由到 `TemporalSubtraction`。
3. `DurationExpression.as_sql()` 增加：若 `has_native_duration_field` 直接走父类 SQL。

关键在于：自动路由现在发生在 **resolve 阶段**，使得返回的表达式天然带 `DurationField` 输出类型，用户无需 `ExpressionWrapper`。

## 调用链分析

`QuerySet.annotate(x = F('a') - F('b'))` →
`Combinable.__sub__` 构造 `CombinedExpression(connector=SUB)` →
`Query.add_annotation` → `expr.resolve_expression(query,...)` →
**本 patch 的 `CombinedExpression.resolve_expression`**：先 resolve 两侧，再按 `connector`/类型路由到 `TemporalSubtraction.resolve_expression`（其 `output_field` 固定为 `DurationField`）→
后续 `as_sql` 由具体子类生成 `subtract_temporals` SQL，过滤/比较 `datetime.timedelta` 得到正确结果。

F2P 测试位于 `tests/expressions/tests.py::FTimeDeltaTests`（`@skipUnlessDBFeature('supports_temporal_subtraction')`，sqlite 支持）：`test_date_subtraction` / `test_date_subquery_subtraction` / `test_date_case_subtraction` / `test_time_subtraction` / `test_time_subquery_subtraction` / `test_datetime_subtraction` / `test_datetime_subquery_subtraction` / `test_datetime_subtraction_microseconds`。test_patch 把这些用例里的 `ExpressionWrapper(..., output_field=DurationField())` 全部去掉，直接断言裸减法可用。

## 替换决策总览

| 原组 | 原 diff 摘要 | 分类 | 决策 | 最终 strategy_code |
|------|--------------|------|------|--------------------|
| A | `connector == SUB` → `connector != SUB` | 🟡 SHALLOW（关键控制流，真实操作符错误） | KEEP | A1 |
| C | `lhs_type == rhs_type` → `lhs_type != rhs_type` | 🔴 与 A 功能等价冗余（失败集完全相同）+ 标签错配 | REPLACE | C1 |
| E | 新增 `enable_temporal_subtraction=False` 类属性门控该分支 | 🟢 多行契约变更 | KEEP | E1 |

M（单 token swap 数）= 2（A、C）。其中 C 经实测与 A 产生**完全一致**的失败集合（`failures=1, errors=7`，逐用例 ERROR/FAIL 列表逐项相同），属功能等价冗余且其 “Break Implicit Type Coercion” 标签名不副实，判为 🔴 必换。A 建模真实的减法判定操作符笔误（误把相等当不等），落在关键路由控制流上，保留。E 为深层契约级改动（引入未定义即生效的开关类属性），保留。共替换 1 个。

## 各组 Mutation 分析

### A 组（KEEP，A1）

- 原 diff：`if self.connector != self.SUB and lhs_type in datetime_fields and lhs_type == rhs_type:`
- 分类：🟡 SHALLOW 单 token（`==`→`!=`）。
- 理由：位于 `TemporalSubtraction` 路由的关键判定上，建模“减法连接符判定写反”的真实控制流错误。`!=` 使所有真正的时间减法（connector 恒为 SUB）都不再被识别，同时还会把加法等错误地误判进时间减法分支。保留为浅层关键控制流变异。
- 最终 diff：同原 diff。
- 变异语义：时间减法连接符判定反转，使 `SUB` 永不进入 `TemporalSubtraction`，所有时间相减回退为原始 `FieldError`/混合类型路径。
- 验证：F2P `FTimeDeltaTests` 24 用例 → `failures=1, errors=7, skipped=1`（FAIL）。

### C 组（REPLACE，C1）

- 原 diff：`if self.connector == self.SUB and lhs_type in datetime_fields and lhs_type != rhs_type:`
- 分类：🔴 MUST REPLACE。
- 理由：把 `lhs_type == rhs_type` 改成 `!=`，效果与 A 组等价——同样让相同类型的时间减法无法进入路由，实测失败集合与 A 组逐项完全相同（功能等价冗余）。且其 strategy_group 标称 “Break Implicit Type Coercion”，但实际只是守卫条件取反，标签与机制不符。需替换。
- 最终 diff（新设计，见下）：从 `datetime_fields` 集合中移除 `'TimeField'`。
- 变异语义：识别可做时间减法的字段集合不完整，遗漏 `TimeField`。
- 验证：见“新设计 Mutation 说明”。

### E 组（KEEP，E1）

- 原 diff：在 `CombinedExpression` 上新增类属性 `enable_temporal_subtraction = False`，并把路由条件改为
  `if self.enable_temporal_subtraction and self.connector == self.SUB and ...`。
- 分类：🟢 KEEP。
- 理由：多行、跨“类属性定义 + 条件使用”的契约级改动。引入一个默认 `False` 且无任何处会被置 `True` 的开关，使整个 `TemporalSubtraction` 自动路由能力被静默关闭。属 E2(implicit→explicit) 风格的隐式行为门控；按本任务允许的 E 组编码统一记为 `E1`（行为变化使精确断言失效）。这是一个看似“为可配置性预留开关”的合理伪装，难以被一眼识破。
- 最终 diff：同原 diff。
- 变异语义：自动时间减法被一个新引入、永不开启的特性开关全局禁用。
- 验证：F2P `FTimeDeltaTests` 24 用例 → `failures=1, errors=7, skipped=1`（FAIL）。

## 新设计 Mutation 说明（C 组替换）

- 设计思路：选择与 A/E **正交**的失败模式。A、E 都让 *所有* 时间减法失效（全量 ERROR/FAIL）。新 C 仅破坏“可参与时间减法的字段类型集合”，从 `{'DateField','DateTimeField','TimeField'}` 中漏掉 `'TimeField'`，模拟“枚举支持类型时漏写一种”的真实疏忽。
- 最终 diff：
  ```
  -            datetime_fields = {'DateField', 'DateTimeField', 'TimeField'}
  +            datetime_fields = {'DateField', 'DateTimeField'}
  ```
- 行为：`Date`/`DateTime` 减法仍正常（典型用例通过），唯独 `Time` 字段相减不再路由到 `TemporalSubtraction`，回退到通用组合表达式，对 `time` 列调用 `get_internal_type` 路径触发 `TypeError: expected string or bytes-like object`。
- 验证（POST-PATCH 内容上 clean diff，`patch -p1` 可应用，`py_compile` 通过）：
  `python3 tests/runtests.py expressions.tests.FTimeDeltaTests`（tmp 内，`PYTHONPATH=<tmp>`）→
  `errors=2, skipped=1`，仅 `test_time_subtraction` 与 `test_time_subquery_subtraction`（均为 F2P 用例）失败，其余 22 个用例（含全部 P2P）通过。仅 F2P 相关失败，无 P2P 破坏。

## 验证结果汇总

- 基线（golden 无变异）：`FTimeDeltaTests` 24 用例全过（`OK, skipped=1`）。
- A（保留）：FAIL（`failures=1, errors=7`）。
- C（替换为去除 TimeField）：FAIL（`errors=2`，仅 2 个 TimeField F2P 用例）。
- E（保留）：FAIL（`failures=1, errors=7`）。
- 三者均：`patch -p1` 可应用、`py_compile` 通过、F2P 失败、P2P 不破。
