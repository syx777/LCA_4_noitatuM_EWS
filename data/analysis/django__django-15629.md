# django__django-15629

## 问题背景

使用 `db_collation` 的主键被外键引用时，MySQL 因 FK 列与 PK 列 collation 不一致产生外键约束错误。根因：FK 列的 `db_parameters` 没有继承所引用 PK 的 collation，迁移生成的 `ALTER TABLE ... MODIFY account_id ...` 缺少 `COLLATE` 子句。Golden patch 多文件协同修复：

1. `related.py::ForeignKey.db_parameters` 从 `target_field`（被引用字段）继承 `collation`；
2. `base/schema.py::_alter_field` 在 collation 变化时也 drop/重建 FK 约束，并在 FK 列重建时用 collation-aware 的 alter；
3. `sqlite3/schema.py::_alter_field` 把"是否重建引用表"的条件从仅 `old_type != new_type` 扩展为也考虑 collation 变化；
4. `oracle/features.py` 标记该测试为 Oracle 已知不支持。

## Golden Patch 语义分析

最核心的是 `ForeignKey.db_parameters`：
```python
def db_parameters(self, connection):
    target_db_parameters = self.target_field.db_parameters(connection)
    return {
        "type": self.db_type(connection),
        "check": self.db_check(connection),
        "collation": target_db_parameters.get("collation"),
    }
```
核心语义：**FK 的 db 参数必须从它指向的目标字段（PK）继承 collation**——FK 列本身没有独立 collation 概念，它必须与被引用列一致才能建立外键约束。`sqlite3/schema.py` 的条件扩展保证当 PK collation 变化时，引用它的表也会被重建以同步 collation。

F2P 测试 `OperationTests.test_alter_field_pk_fk_db_collation`：把 Pony.id 改为带 `db_collation` 的 CharField，断言 Rider.pony_id、Stable.ponies.pony_id 列都获得相同 collation。

## 调用链分析

迁移执行 `AlterField` → `schema_editor._alter_field`。其中收集 `rels_to_update`（指向该字段的 FK），对每个 FK 调 `db_parameters` 取 `collation`，再决定用 `_alter_column_collation_sql` 还是 `_alter_column_type_sql`。`ForeignKey.db_parameters` 是数据源头——它若不返回目标 PK 的 collation，整条链拿到的 collation 都是 None，FK 列不会带 COLLATE。sqlite 走 `_remake_table` 路径，需 `new_field.unique and (type 变 or collation 变)` 才重建引用表。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | sqlite 重建条件 `or`→`and ... ==`，collation 变化不再触发重建 |
| B | ➕ 补充 | 新增 | 原缺 B 组；`db_parameters` 读错 key（`"check"` 而非 `"collation"`） |
| C | ➕ 补充 | 新增 | 原缺 C 组；collation 来源取错对象（`super()` 而非 `target_field`） |
| D | 🟢 高质量 | 保留 | 注释掉 `db_parameters` 的 collation 行 |
| E | 🔴 必须替换 | 替换 | 原 E 与 D 近似（都去掉 collation 行）；改为默认关闭的开关 |

原 D（注释 collation 行）与 E（删除 collation 行）机制几乎相同。保留 A、D，补充 B、C，把 E 重做为开关 gate。

## 各组 Mutation 分析

### Group A — 保留（B3 复合条件：or→and 且反转）
```diff
         if new_field.unique and (
-            old_type != new_type or old_collation != new_collation
+            old_type != new_type and old_collation == new_collation
         ):
```
**变异语义**：sqlite 决定是否重建引用表的条件，从"type 变 **或** collation 变"改成"type 变 **且** collation **不变**"。当只有 collation 变化（type 不变）时，`old_type != new_type` 为 False，整个条件 False，引用表不被重建，FK 列拿不到新 collation。模拟"复合布尔条件 or/and + ==/!= 同时改错"。保留。

### Group B — 补充（C1 类型/数据形状：读错 dict key）
```diff
         return {
             "type": self.db_type(connection),
             "check": self.db_check(connection),
-            "collation": target_db_parameters.get("collation"),
+            "collation": target_db_parameters.get("check"),
         }
```
**变异语义**：`collation` 字段去 `target_db_parameters` 里读了 `"check"` 这个 key（FK 约束的 check 子句）而非 `"collation"`。`check` 通常为 None 或与 collation 无关的值，故 FK 列的 collation 实际是错的/缺失。模拟"复制粘贴相邻 key、取错字段名"。

### Group C — 补充（A1 接口契约：collation 来源对象错误）
```diff
     def db_parameters(self, connection):
-        target_db_parameters = self.target_field.db_parameters(connection)
+        target_db_parameters = super().db_parameters(connection)
         return {
```
**变异语义**：collation 来源从 `self.target_field.db_parameters`（被引用 PK 的参数）改成 `super().db_parameters`（FK 字段自身基类的参数）。FK 自身的 db_parameters 不含目标 PK 的 collation（基类默认无 collation），故 `.get("collation")` 得 None。`type`/`check` 仍用 self 的值，看起来变量名 `target_db_parameters` 还在、字典结构没变，但数据源已错。模拟"该问目标字段却问了自己/基类"的契约错误。

### Group D — 保留（D2 死代码注释）
```diff
         return {
             "type": self.db_type(connection),
             "check": self.db_check(connection),
-            "collation": target_db_parameters.get("collation"),
+            # "collation": target_db_parameters.get("collation"),
         }
```
**变异语义**：注释掉 collation 字段，返回字典里根本没有 `"collation"` key。下游 `.get("collation")` 得 None，FK 列不带 COLLATE。形式是"临时注释忘恢复"。保留。

### Group E — 替换（E2 隐式→显式开关）
**原**：与 D 近似（删除 collation 行）。
**最终 mutation**：
```diff
     def db_parameters(self, connection):
         target_db_parameters = self.target_field.db_parameters(connection)
-        return {
+        params = {
             "type": self.db_type(connection),
             "check": self.db_check(connection),
-            "collation": target_db_parameters.get("collation"),
         }
+        if getattr(self, "propagate_collation", False):
+            params["collation"] = target_db_parameters.get("collation")
+        return params
```
**变异语义**：把 collation 传播藏到实例属性开关 `propagate_collation` 后，默认 `False`，故默认不传播 collation。只有显式设 `propagate_collation=True` 才恢复。代码看起来"支持可配置的 collation 传播"，实则默认关闭等于不修复。模拟"把行为做成可配置、默认却关掉"。

## 新设计 Mutation 说明

原实例只有 A/D/E 三组，且 D（注释 collation 行）与 E（删除 collation 行）机制几乎相同，缺 B、C。本次保留 A（sqlite 复合条件错）、D（注释），补充 B（读错 dict key `"check"`）、C（collation 来源取 `super()` 而非 `target_field`），把与 D 重复的 E 重做为默认关闭的 `propagate_collation` 开关。五组分布在 `sqlite3/schema.py`（A）与 `related.py`（B/C/D/E）两个文件、覆盖"复合条件 / 错 key / 错来源对象 / 注释 / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令 F2P（`test_alter_field_pk_fk_db_collation`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
