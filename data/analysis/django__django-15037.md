# django__django-15037

## 问题背景

`inspectdb` 从既有数据库反向生成 model 时，若某外键指向被引用表的**非主键**唯一列（如 `FOREIGN KEY(other_id) REFERENCES foo(other_id)`，而 foo 的主键是 `id`），生成的 model 只会写成普通指向 foo 的 ForeignKey，丢失了 `to_field='other_id'` 信息。Golden patch 让 inspectdb 在被引用列不是目标表主键时补上 `to_field`。

## Golden Patch 语义分析

```python
ref_db_column, ref_db_table = relations[column_name]
if extra_params.pop('unique', False) or extra_params.get('primary_key'):
    rel_type = 'OneToOneField'
else:
    rel_type = 'ForeignKey'
    ref_pk_column = connection.introspection.get_primary_key_column(cursor, ref_db_table)
    if ref_pk_column and ref_pk_column != ref_db_column:
        extra_params['to_field'] = ref_db_column
```
核心语义：**只有当外键引用的列 (`ref_db_column`) 不是目标表的主键列 (`ref_pk_column`) 时，才需要显式 `to_field`**。若引用的就是主键，则默认行为已正确，无需 `to_field`。判据是 `ref_pk_column != ref_db_column`，且把 `ref_db_column`（被引用列名）作为 `to_field` 的值。

F2P 测试 `test_foreign_key_to_field`：断言生成输出包含 `to_field_fk = models.ForeignKey('InspectdbPeoplemoredata', models.DO_NOTHING, to_field='people_unique_id')`，即必须出现正确的 `to_field='people_unique_id'`。

## 调用链分析

`inspectdb` 的 `handle_inspection` 遍历列，对 `is_relation` 的列取 `relations[column_name]`（被引用列、被引用表），用 `get_primary_key_column` 取目标表主键列，比较后决定是否设 `to_field`，再拼接字段定义字符串。`column_name` 是当前表的列名，`ref_db_column` 是被引用表里的目标列名，`ref_pk_column` 是被引用表的主键列名。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 `!=`→`==`，与 B/C/E 字节级重复 |
| B | 🔴 必须替换 | 替换 | 与 A/C/E 完全相同 |
| C | 🔴 必须替换 | 替换 | 与 A/B/E 完全相同 |
| D | 🔴 必须替换 | 替换 | 删除 `ref_pk_column = ...` 赋值行，导致 NameError（崩溃式、非微妙） |
| E | 🔴 必须替换 | 替换 | 与 A/B/C 完全相同 |

五个原 mutation 实质只有两种（4 个相同的 `!=`→`==` + 1 个删行 NameError），必须全部替换为五种不同语义的高质量变异。

## 各组 Mutation 分析

### Group A — 替换（A1 参数/赋值语义）
```diff
-                                extra_params['to_field'] = ref_db_column
+                                extra_params['to_field'] = ref_pk_column
```
**变异语义**：判据正确（仍在"引用列≠主键"时进入），但把 `to_field` 错误地赋成**主键列名** `ref_pk_column` 而非被引用列名 `ref_db_column`。输出会变成 `to_field='id'`（目标表主键）而非期望的 `to_field='people_unique_id'`。模拟两个相邻变量名混淆——`ref_pk_column` 与 `ref_db_column` 仅一词之差，审查极易看走眼。

### Group B — 替换（B-边界/条件）
```diff
-                            if ref_pk_column and ref_pk_column != ref_db_column:
+                            if not ref_pk_column and ref_pk_column != ref_db_column:
```
**变异语义**：把"主键列存在"的前置条件取反成"主键列不存在"。正常表都有主键，`ref_pk_column` 为真，`not ref_pk_column` 为假 → 整个条件恒假 → 永不设 `to_field`。模拟把 `and ref_pk_column`（确保拿到主键）误写成 `not`，看似在处理"无主键"边界，实则关闭了正常路径。

### Group C — 替换（C1 类型/数据形状）
```diff
-                                extra_params['to_field'] = ref_db_column
+                                extra_params['to_field'] = ref_db_column.rstrip('_id')
```
**变异语义**：对被引用列名做 `rstrip('_id')` "清洗"，意图去掉列名尾部的 `_id` 后缀。但 `rstrip` 按字符集合而非后缀剥离，且本意本就错误——`to_field` 应保留真实列名 `people_unique_id`。结果生成 `to_field='people_unique'`（甚至更短），与断言不符。模拟"反向生成时顺手规整列名"的数据形状误处理。

### Group D — 替换（D-状态/资源门控）
```diff
-                            if ref_pk_column and ref_pk_column != ref_db_column:
+                            if ref_pk_column and ref_pk_column != ref_db_column and extra_params.get('unique'):
```
**变异语义**：给设置 `to_field` 的条件额外加上"该字段是 unique"的门控。但代码上方已通过 `extra_params.pop('unique', False)` 决定走 OneToOne 还是 ForeignKey——进入 else（ForeignKey）分支时 `unique` 已被 pop 掉，`extra_params.get('unique')` 恒为 None/假，故普通外键永不设 `to_field`。模拟"只有唯一外键才需要 to_field"的错误资源/状态门控。

### Group E — 替换（E1 测试期望/赋值目标）
```diff
-                                extra_params['to_field'] = ref_db_column
+                                extra_params['to_field'] = column_name
```
**变异语义**：把 `to_field` 赋成**当前表的本地列名** `column_name`（如 `to_field_fk_id`）而非被引用表的目标列名 `ref_db_column`。语义上张冠李戴——`to_field` 指的是目标模型上的字段，却填了源列名。输出 `to_field` 值错误，精确断言失败。模拟"分不清 to_field 指向源还是目标列"的语义混淆。

## 新设计 Mutation 说明

五个替代覆盖五个不同维度且互不重复：A 赋错变量（主键列 vs 引用列）、B 取反前置条件、C 对列名做错误的字符串清洗、D 加了错误的 unique 门控、E 赋成本地列名而非目标列名。除 B/D 改的是条件、A/C/E 改的是赋值目标，整体不集中在同一行。全部通过 `py_compile`、在 `base→golden→test_patch` 后干净应用，实测均仅令 `test_foreign_key_to_field` 失败（rc=1），无崩溃式 NameError（修正了原 D 的缺陷）。
