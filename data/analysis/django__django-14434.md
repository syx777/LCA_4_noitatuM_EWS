# django__django-14434 Mutation 分析

## 问题背景

`_create_unique_sql` 在为 `UniqueConstraint` 生成延迟 DDL 语句时，错误地把一个 `Table` 实例作为
参数传给了期望接收字符串的逻辑。原始 bug 导致生成的 `Statement` 的 `references_column` 永远返回
`False`：当列引用对象 `Columns` 的 `self.table` 被设置成了 `Table` 对象（而非数据库表名字符串）时，
`TableColumns.references_column` 中的 `self.table == table` 比较恒为假。这会破坏迁移过程中对延迟
SQL 的表/列引用跟踪（例如重命名表或列时无法正确识别需要调整的语句）。

## Golden Patch 语义分析

Golden patch 的核心是把局部变量 `table` 从 `Table(model._meta.db_table, self.quote_name)` 改回
纯字符串 `model._meta.db_table`，并在真正需要 `Table` 对象的地方（`Statement(... table=...)`）才显式
包一层 `Table(table, self.quote_name)`：

- `table = model._meta.db_table`（字符串）
- `_index_columns(table, ...)` 与 `Expressions(table, ...)` 接收字符串表名，使 `Columns.self.table`
  正确保存表名字符串。
- `Statement(table=Table(table, self.quote_name), ...)` 仅在 `Statement` 的 table 部分使用 `Table`。

修复后，`Columns.references_column(table, col)` 中 `self.table == table` 才能正确为真。

## 调用链分析

`UniqueConstraint.create_sql` (django/db/models/constraints.py:200)
→ `schema_editor._create_unique_sql(model, fields, name, ...)` (schema.py:1225)
→ `_index_columns(table, columns, ...)` (schema.py:1068) 返回 `Columns(table, columns, quote_name)`
→ 构造 `Statement(sql, table=Table(...), name=IndexName(...), columns=Columns(...), ...)`。

关键语义：`Columns.__str__`（继承自 `ddl_references.Columns`）只渲染列名列表，**完全不使用
`self.table`**。因此把 `_index_columns` 的 table 参数破坏掉，并不会改变最终 SQL 字符串，也不会影响
数据库实际建/删约束的执行；唯一受影响的是 `Statement.references_column` 的引用跟踪——而这正是
F2P 测试 `test_unique_constraint` 中 `self.assertIs(sql.references_column(table, 'name'), True)`
所断言的部分。

F2P 测试模块：`schema.tests`，新增用例 `SchemaTests.test_unique_constraint`。
基线（golden，无 mutation）：F2P 通过；完整模块 168 tests，OK（skipped=28）。

## 替换决策总览

| 组 | 原 diff 语义 | 分类 | 决策 | 最终设计 |
|----|--------------|------|------|----------|
| A | 把 `table` 重新包成 `Table(table, self.quote_name)`（golden 的直接逆操作） | 🔴 直接冗余 | 替换 | `_index_columns(table[:-1], ...)` 截断表名 |
| B | `_index_columns("", ...)` 单 token 替换为空串 | 🟡 浅层 | 保留 | 原样保留 |
| D | 与 A 字节完全相同 | 🔴 重复 + 直接冗余 | 替换 | `_index_columns(table.upper(), ...)` 大写化表名 |
| E | 与 A 字节完全相同 | 🔴 重复 + 直接冗余 | 替换 | `_index_columns(name, ...)` 误传相邻变量 |

浅层统计：M（浅层数）= 1（仅 B）。应替换最弱的 floor(1/2)=0 个浅层 → B 保留。
🔴 必替换：A、D、E（三者字节相同，均为 golden 直接逆操作 / 互为重复）。

## 各组 Mutation 分析

### Group A —— 🔴 替换
- 原 diff：`columns = self._index_columns(Table(table, self.quote_name), columns, ...)`
- 分类：直接冗余。这是把 golden 修复点原样还原为 buggy 行为（重新包 `Table`），等价于反向 golden。
- 最终 diff：`columns = self._index_columns(table[:-1], columns, col_suffixes=(), opclasses=opclasses)`
- 变异语义：截断表名最后一个字符后传入 `_index_columns`。由于 `Columns.__str__` 不使用 `self.table`，
  SQL 与建/删约束执行不受影响（执行类测试全过）；但 `references_column(table,'name')` 因
  `self.table == table` 不成立而返回 `False`，仅 F2P 断言能捕获。形似无意的 off-by-one 切片错误。
- 验证：git apply / patch -p1 均通过；py_compile OK；F2P FAILED(failures=1)；完整模块仅该 1 例失败。

### Group B —— 🟡 保留
- 原 diff：`columns = self._index_columns("", columns, ...)`
- 分类：浅层单 token 替换。M=1，需替换 floor(1/2)=0 个，故保留。
- 变异语义：空串作表名。SQL 渲染不变（执行通过），仅引用跟踪失效，F2P 断言失败。模拟“忘记填变量”。
- 验证：F2P FAILED(failures=1)；完整模块仅该 1 例失败。

### Group D —— 🔴 替换
- 原 diff：与 A 字节完全相同（重复 + golden 直接逆操作）。
- 最终 diff：`columns = self._index_columns(table.upper(), columns, col_suffixes=(), opclasses=opclasses)`
- 变异语义：将表名大写化后传入。`TableColumns.references_column` 的 `self.table == table` 因大小写
  不一致恒为假，而生成 SQL 不变，DB 操作与 P2P 全过；仅 F2P 引用跟踪断言失败。形似误加的标识符规范化。
- 验证：git apply / patch -p1 均通过；py_compile OK；F2P FAILED(failures=1)；完整模块仅该 1 例失败。

### Group E —— 🔴 替换
- 原 diff：与 A 字节完全相同（重复 + golden 直接逆操作）。
- 最终 diff：`columns = self._index_columns(name, columns, col_suffixes=(), opclasses=opclasses)`
- 变异语义：误传相邻的局部变量 `name`（一个 `IndexName` 对象，非字符串），使 `Columns.self.table`
  成为非字符串，`self.table == table` 恒为假。`Columns.__str__` 不依赖 `self.table`，故 SQL 与
  建/删约束执行正常，P2P 通过；仅 F2P 检测到。形似复制粘贴了错误的相邻变量。
- 验证：git apply / patch -p1 均通过；py_compile OK；F2P FAILED(failures=1)；完整模块仅该 1 例失败。

## 新设计 Mutation 说明

三个替换（A/D/E）共享同一“SQL 不变量”杠杆：`ddl_references.Columns.__str__` 仅渲染列名而忽略
`self.table`，因此唯一能在不破坏生成 SQL / 数据库执行的前提下、专门击穿
`references_column` 引用跟踪的入口，就是 `_index_columns` 的 table 实参。经验证，任何在 `table`
赋值处或 `Statement(table=...)` 部分破坏表名的尝试都会污染真实 SQL（导致 `no such table` 类
DB 错误并破坏大量 P2P），不满足正交且不破坏 P2P 的要求；故三个替换均落在该唯一安全入口，但采用
互不相同的现实开发者错误：截断（A）、大小写规范化（D）、误传相邻变量（E），以及保留的空串（B），
形成四种不同笔误的失败模式。所有四个 mutation 均：在 golden+test_patch 后的代码上以
`git apply` 与 `patch -p1` 干净应用、py_compile 通过、单独运行 F2P 失败、完整 `schema.tests`
模块中仅 F2P 这一例失败（28 个表达式相关 P2P 在 sqlite 上被 skip，不受影响）。
