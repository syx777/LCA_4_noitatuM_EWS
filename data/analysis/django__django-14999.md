# django__django-14999

## 问题背景

当一个模型显式定义了 `db_table`（如 `Meta.db_table = 'rider'`），对它执行 `RenameModel` 操作时数据库表名并不会变化，因此应当是 noop。但旧实现仍会调用 `alter_db_table`：在 Postgres 上导致外键约束被 drop+recreate，在 sqlite 上重建整张表。Golden patch 在 `RenameModel.database_forwards` 中比较旧/新 `db_table`，相等则直接 `return`（noop）。

## Golden Patch 语义分析

```python
old_db_table = old_model._meta.db_table
new_db_table = new_model._meta.db_table
# Don't alter when a table name is not changed.
if old_db_table == new_db_table:
    return
schema_editor.alter_db_table(new_model, old_db_table, new_db_table)
```
核心语义：**以"实际数据库表名是否改变"为准决定是否执行 schema 操作**，而非以"模型名是否改变"为准。当两表名相等时不仅跳过主表 rename，也跳过后续对相关外键字段的 alter。`database_backwards` 复用 `database_forwards`（交换 from/to），所以反向同样 noop。

F2P 测试 `test_rename_model_with_db_table_noop`：给 Rider 固定 `db_table='rider'`，RenameModel('Rider','Runner') 后用 `assertNumQueries(0)` 断言 forwards 与 backwards 都不产生任何 SQL。

## 调用链分析

`database_forwards(app_label, schema_editor, from_state, to_state)` 从两个 `ProjectState` 取出 old/new model，比较 `_meta.db_table`。`RenameModel` 持有 `self.old_name`/`self.new_name`（模型名，本例 Rider/Runner，二者不同）。相关外键的迁移在 noop 之后的循环里（`old_model._meta.related_objects`），noop 提前 return 即整体跳过。`database_backwards` 调用同一函数。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原为 `==`→`!=`，反转 noop 守卫，与 B 字节级重复 |
| B | 🔴 必须替换 | 替换 | 与 A 完全相同 |
| C | 🔴 必须替换 | 替换 | 直接删除整个 noop 守卫（含注释），等价于还原 golden patch 的逆操作 |
| D | 🔴 必须替换 | 替换 | 仅删去守卫的另一种写法，与 C 高度重复，仍是直接还原 |

四个均必须替换，各设计一个语义不同的高质量变异。

## 各组 Mutation 分析

### Group A — 替换
**最终 mutation**：
```diff
-            # Don't alter when a table name is not changed.
-            if old_db_table == new_db_table:
+            # Don't alter when a table name is not changed.
+            if self.old_name == self.new_name:
```
**变异语义**：把 noop 判据从"db_table 是否相同"偷换成"模型名是否相同"。两者在绝大多数场景一致（重命名模型通常表名也变），但恰恰漏掉了"显式 db_table 固定、仅模型名变"这一 issue 场景：此时 `old_name != new_name` → 条件为假 → 仍执行 alter，产生 SQL，`assertNumQueries(0)` 失败。模拟开发者"模型名没变才算 noop"的直觉误解。

### Group B — 替换
**最终 mutation**：
```diff
             old_db_table = old_model._meta.db_table
             new_db_table = new_model._meta.db_table
-            # Don't alter when a table name is not changed.
-            if old_db_table == new_db_table:
-                return
             # Move the main table
-            schema_editor.alter_db_table(new_model, old_db_table, new_db_table)
+            if old_db_table != new_db_table:
+                schema_editor.alter_db_table(new_model, old_db_table, new_db_table)
```
**变异语义**：把"提前 return 的整体 noop 守卫"重构成"只在 `old_db_table != new_db_table` 时才执行主表 alter"。乍看等价——主表确实不会被多余地 rename——但这是**只保护了主表、却没保护相关对象**的不完整修复：early-return 被删除后，db_table 相等时控制流不再提前退出，继续走到下方 `for related_object in old_model._meta.related_objects` 循环，对相关外键字段执行 `alter_field`，从而产生 SQL。本测试中 Pony 有一个指向 Rider 的 FK，故 forwards/backwards 都会发出查询，`assertNumQueries(0)` 失败。模拟"把守卫下沉到具体操作处、漏掉了同样需要短路的相关对象处理"的真实重构失误。

### Group C — 替换
**最终 mutation**：
```diff
-            if old_db_table == new_db_table:
+            if old_db_table == new_db_table and self.old_name == self.new_name:
                 return
```
**变异语义**：给 noop 守卫追加一个"且模型名相同"的合取条件，使守卫更"严格"。但模型名本就不同（Rider→Runner），合取后条件恒假，守卫形同虚设，alter 照常执行。看起来是"更精确地判断 noop"的增强，实际上让守卫永不触发。

### Group D — 替换
**最终 mutation**：
```diff
-            if old_db_table == new_db_table:
+            if old_model == new_model:
                 return
```
**变异语义**：用模型对象相等替代表名字符串相等作为 noop 判据。`old_model` 与 `new_model` 来自不同 state、是不同的模型类对象，`==` 永不成立，故守卫失效、产生 SQL。模拟"两个 model 是同一个就 noop"的对象层面误解，比纯删除守卫更隐蔽。

## 新设计 Mutation 说明

四个替代分别从**判据来源（A：名字 vs 表名）**、**保护覆盖面（B：主表 vs 相关对象）**、**条件强度（C：多余合取）**、**比较对象（D：model vs db_table）**四个不同维度引入 bug，互不重复，全部保留了"看似在做 noop 判断"的可读性。实测均仅令该 F2P 失败，且对 migrations.test_operations 整套仅新增 1 个失败，blast radius 极小。
