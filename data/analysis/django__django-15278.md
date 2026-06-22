# django__django-15278

## 问题背景

在 SQLite 上为模型添加可空 OneToOneField（即可空 UNIQUE 列）会崩溃：`OperationalError: Cannot add a UNIQUE column`。SQLite 的 `ALTER TABLE ADD COLUMN` 不支持 UNIQUE/主键列。旧 `add_field` 只在"非空或有默认值"时走 `_remake_table`（重建表），漏掉了 unique/主键的情况。Golden patch 扩充条件：主键、unique、非空、有默认值任一成立都走 `_remake_table`。

## Golden Patch 语义分析

```python
if (
    # Primary keys and unique fields are not supported in ALTER TABLE
    # ADD COLUMN.
    field.primary_key or field.unique or
    # Fields with default values cannot by handled by ALTER TABLE ADD
    # COLUMN statement because DROP DEFAULT is not supported in ALTER TABLE.
    not field.null or self.effective_default(field) is not None
):
    self._remake_table(model, create_field=field)
else:
    super().add_field(model, field)
```
核心语义：**unique（含 O2O）或主键字段必须走 `_remake_table` 而非 ALTER TABLE ADD COLUMN**。对本 issue 的可空 O2O 字段：`not field.null` 为 False（可空）、`effective_default` 为 None、`primary_key` 为 False，因此唯一能让它走 `_remake_table` 的就是新增的 **`field.unique`** 子句。任何削弱/绕过 `field.unique` 判定的改动都会让它退回到崩溃的 ALTER TABLE 路径。

F2P 测试 `SchemaTests.test_add_field_o2o_nullable`：建表后 `add_field` 一个可空 O2O 字段，断言 `note_id` 列存在且为 nullable。若走 ALTER TABLE 路径，SQLite 抛 `Cannot add a UNIQUE column`。

## 调用链分析

`add_field(model, field)` → 条件判断 → `_remake_table`（安全，重建整表）或 `super().add_field`（执行 `ALTER TABLE ... ADD COLUMN ... UNIQUE`，SQLite 报错）。本测试字段是可空 unique O2O，必须命中 `field.unique` 才能走安全路径。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `or field.unique`→`and field.unique`，可空 O2O（pk=False）整体为假，退回 ALTER TABLE |
| B | ➕ 补充 | 新增 | 原缺 B 组（删 `field.unique`，最简的 null/case 移除） |
| C | 🟡 | 替换 | 原 C=D 重复（都删 `field.unique`）；改为"unique 仅在有默认值时生效" |
| D | 🔴 必须替换 | 替换 | 与 C 字节级重复；改为"unique 仅在非空时生效" |
| E | 🟢 高质量 | 保留 | 删 `field.unique` 并改写注释为"仅主键"，更像有意为之 |

原 C、D 与 E 都是"删 `field.unique`"，三者高度重复。补 B（仍用最简删除作为代表）、把 C/D 改为带条件的削弱以增加多样性。

## 各组 Mutation 分析

### Group A — 保留（B3 布尔逻辑）
```diff
-            field.primary_key or field.unique or
+            field.primary_key and field.unique or
```
**变异语义**：把 `primary_key or unique` 改成 `primary_key and unique`。可空 O2O 的 `primary_key` 为 False，`and` 短路使整个 `field.primary_key and field.unique` 为 False；其余子句也都为假，于是走 `super().add_field` → SQLite UNIQUE 列报错。模拟 `or`/`and` 混用。保留。

### Group B — 补充（B2 移除 null/case 处理）
```diff
-            field.primary_key or field.unique or
+            field.primary_key or
```
**变异语义**：直接删掉 `field.unique` 子句。可空 unique O2O 不再被 `field.unique` 捕获，且非空/默认值条件也不满足，退回 ALTER TABLE 崩溃路径。这是最朴素的"漏处理 unique 这个 case"。

### Group C — 替换（C1 类型/数据形状：unique 仅在有默认值时生效）
**原**：与 D 重复（删 unique）。
**最终 mutation**：
```diff
-            field.primary_key or field.unique or
+            field.primary_key or (field.unique and self.effective_default(field) is not None) or
```
**变异语义**：把 `field.unique` 削弱为"unique **且有默认值**才算"。本 issue 的可空 O2O 没有默认值（`effective_default` 为 None），故 `field.unique and ...` 为假，退回崩溃路径。模拟"把 unique 与 default 两个本不相关的条件错误地耦合"——看似在收紧判定，实则放过了无默认值的 unique 字段。

### Group D — 替换（B-边界：unique 仅在非空时生效）
**原**：与 C 重复（删 unique）。
**最终 mutation**：
```diff
-            field.primary_key or field.unique or
+            field.primary_key or (field.unique and not field.null) or
```
**变异语义**：把 `field.unique` 削弱为"unique **且非空**才算"。本 issue 字段恰是**可空** unique，`field.unique and not field.null` 为假，退回崩溃路径。模拟"以为只有非空 unique 才有 ALTER TABLE 问题"的边界误解——恰好漏掉可空 unique 这一真实 bug 场景。

### Group E — 保留（E1 测试期望/注释改写）
```diff
-            # Primary keys and unique fields are not supported in ALTER TABLE
-            # ADD COLUMN.
-            field.primary_key or field.unique or
+            # Primary keys are not supported in ALTER TABLE ADD COLUMN.
+            field.primary_key or
```
**变异语义**：删 `field.unique` 并把注释改成"仅主键不支持"，让删除看起来是**有意的、经过深思的**修改（注释与代码一致），更难在审查中引起怀疑。效果同样是可空 unique 退回崩溃路径。保留。

## 新设计 Mutation 说明

原 C/D/E 三者都是"删 `field.unique`"，重复度高。本次补齐缺失的 B（用最简删除），并把重复的 C、D 改为**带条件的削弱**：C 把 unique 耦合到"有默认值"、D 耦合到"非空"，两者都精准漏掉本 issue 的"可空无默认 unique"场景，机制各异且更隐蔽。A（and 逻辑）、E（删 + 改注释）保留。五组覆盖"逻辑运算 / 直接移除 / default 耦合 / null 耦合 / 注释伪装"五个角度。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用。
