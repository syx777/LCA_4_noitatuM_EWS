# django__django-15732

## 问题背景

当某字段同时拥有 `unique=True`（或为主键）与 `unique_together=(('id',),)` 两个唯一约束时，无法用迁移删除 `unique_together`。`_delete_composed_index` 通过列匹配查约束，期望恰好找到 1 个，但实际找到 2 个（如 PRIMARY KEY + UNIQUE CONSTRAINT），于是抛 `ValueError: Found wrong number (2) of constraints`。Golden patch 在 `_delete_composed_index` 中：当确为删 unique、找到多个约束、且后端 `allows_multiple_constraints_on_same_fields` 时，用 `_unique_constraint_name` 算出 unique_together 的默认约束名，若它在候选集中则只保留它；同时把名字生成逻辑抽成 `_unique_constraint_name(table, columns, quote=)` 复用。

## Golden Patch 语义分析

```python
if (
    constraint_kwargs.get("unique") is True
    and constraint_names
    and self.connection.features.allows_multiple_constraints_on_same_fields
):
    # Constraint matching the unique_together name.
    default_name = str(
        self._unique_constraint_name(model._meta.db_table, columns, quote=False)
    )
    if default_name in constraint_names:
        constraint_names = [default_name]
```
核心语义：**当一列上存在多个唯一约束、无法用列集合唯一定位 unique_together 约束时，用其确定性的默认命名（`<table>_<col>_<hash>_uniq`）从候选集中精确挑出它**，避免误删主键/独立 unique 约束、也避免"找到多个"的 ValueError。`quote=False` 关键——`constraint_names` 里是未加引号的真实约束名，比较时默认名也必须不带引号。`_unique_constraint_name` 抽出的 helper 用 `quote` 参数区分"生成 SQL 用引号名"与"比较用裸名"。

F2P 测试 `test_remove_unique_together_on_pk_field` 与 `test_remove_unique_together_on_unique_field`：建带 `unique_together` 的模型（列上另有 pk 或 unique），执行 `AlterUniqueTogether(set())`，断言只有 unique_together 约束被删、pk/unique 约束保留。

## 调用链分析

`AlterUniqueTogether.database_forwards` → `schema_editor.alter_unique_together` → 对删除的约束调 `_delete_composed_index(model, fields, {"unique": True, "primary_key": False}, sql_delete_unique)`。后者用 `_constraint_names` 按列查约束，得到多个名字 → 新增的去重逻辑用 `_unique_constraint_name(..., quote=False)` 生成默认名挑出唯一目标 → `_delete_constraint_sql` 删除。`_unique_constraint_name` 同时被 `_create_unique_sql` 调用（`quote=True`）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A 组；`quote=False`→`quote=True`，默认名带引号无法匹配裸约束名 |
| B | 🟢 高质量 | 保留 | `in`→`not in`，去重条件反转 |
| C | 🟢 高质量 | 保留 | 去掉 `str()` 包裹，`default_name` 保持 IndexName 对象，`in` 比较失败 |
| D | 🟢 高质量 | 保留 | `if False and ...` 短路，去重永不执行 |
| E | ➕ 补充 | 新增 | 原缺 E 组；去重逻辑藏到默认关闭的开关后 |

原实例只有 B/C/D 三组，缺 A、E。补充 A、E，B/C/D 各为不同机制故保留。

## 各组 Mutation 分析

### Group A — 补充（A1 接口契约：quote 参数错误）
```diff
             default_name = str(
-                self._unique_constraint_name(model._meta.db_table, columns, quote=False)
+                self._unique_constraint_name(model._meta.db_table, columns, quote=True)
             )
             if default_name in constraint_names:
```
**变异语义**：把生成默认名的 `quote` 从 `False` 改成 `True`。`quote=True` 走 `quote_name(_create_index_name(...))`，返回带数据库引号（如 `"app_pony_id_xxx_uniq"`）的名字；而 `constraint_names` 里是 `_constraint_names` 返回的**裸名**（无引号）。带引号的 `default_name` 永远 `not in constraint_names` → 去重不生效 → 仍找到多个约束 → ValueError。模拟"误用了带引号的命名变体做比较"，比删除逻辑更隐蔽。

### Group B — 保留（B3 条件反转）
```diff
-            if default_name in constraint_names:
+            if default_name not in constraint_names:
```
**变异语义**：去重判定反转。`default_name` 正常**在**候选集中（unique_together 约束确实存在），`not in` 为 False → 不收窄 → 保留全部多个约束 → ValueError。仅当默认名碰巧不在集合时才会错误地收窄。还原核心 bug。保留。

### Group C — 保留（C1 类型/数据形状：去 str 包裹）
```diff
-            default_name = str(
-                self._unique_constraint_name(model._meta.db_table, columns, quote=False)
-            )
+            default_name = self._unique_constraint_name(model._meta.db_table, columns, quote=False)
```
**变异语义**：去掉 `str(...)`，`default_name` 保持为 `IndexName` 对象而非纯字符串。`constraint_names` 里是 str，`IndexName(...) in [str,...]` 因类型/相等语义不匹配多半为 False → 去重不生效。模拟"忘了 IndexName 需显式转 str 才能与约束名比较"。保留。

### Group D — 保留（B2 短路禁用）
```diff
-            if default_name in constraint_names:
+            if False and default_name in constraint_names:
```
**变异语义**：用 `False and` 短路使整个去重分支永不执行，`constraint_names` 保持多个 → ValueError。形式上像"调试时临时禁用"。保留。

### Group E — 补充（E2 隐式→显式开关）
```diff
         if (
             constraint_kwargs.get("unique") is True
             and constraint_names
             and self.connection.features.allows_multiple_constraints_on_same_fields
+            and getattr(self, "dedupe_unique_together", False)
         ):
```
**变异语义**：在去重的进入条件里多加一个实例属性开关 `dedupe_unique_together`，默认 `False`，故默认不去重。只有显式设 True 才恢复。代码看起来"去重是个可配置特性"，实则默认关闭等于未修复。模拟"把修复做成可配置、默认却关掉"。

## 新设计 Mutation 说明

原实例只有 B/C/D 三组，缺 A、E。本次保留 B（条件反转）、C（去 str 类型不匹配）、D（短路禁用），补充 A（`quote=True` 使默认名带引号无法匹配裸约束名）、E（默认关闭的 `dedupe_unique_together` 开关）。五组覆盖"quote 参数契约 / 条件反转 / 类型不匹配 / 短路禁用 / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令两个 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
