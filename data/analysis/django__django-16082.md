# django__django-16082

## 问题背景

用 `MOD` 运算符组合不同数值类型（如 `DecimalField % IntegerField`）时，Django 不能像其他算术运算符那样把结果 `output_field` 解析为 `DecimalField`。根因：`_connector_combinations` 中"不同类型数值运算"那组只为 `ADD/SUB/MUL/DIV` 注册了 `(Integer, Decimal)→Decimal` 等组合，漏了 `MOD`。Golden patch 在该组的连接符元组里加上 `Combinable.MOD`。

## Golden Patch 语义分析

```python
{
    connector: [
        (fields.IntegerField, fields.DecimalField, fields.DecimalField),
        (fields.DecimalField, fields.IntegerField, fields.DecimalField),
        (fields.IntegerField, fields.FloatField, fields.FloatField),
        (fields.FloatField, fields.IntegerField, fields.FloatField),
    ]
    for connector in (
        Combinable.ADD,
        Combinable.SUB,
        Combinable.MUL,
        Combinable.DIV,
        Combinable.MOD,    # ← 新增
    )
},
```
核心语义：**`MOD` 必须被加入"不同类型数值运算"的连接符集合**，这样 `_resolve_combined_type` 才能为 `Decimal % Integer` 等混合类型查到 `(Integer, Decimal)→Decimal` 规则、解析出正确 output_field。该字典推导式为列表中每个 connector 注册同一组类型组合；漏掉 MOD 则混合类型 MOD 查不到规则、output_field 无法解析。注意同模块另有"相同类型数值运算"组已含 MOD——bug 仅在"不同类型"组。

F2P 测试 `CombinedExpressionTests.test_resolve_output_field_number`：把 `Combinable.MOD` 加入待测连接符列表，对各混合类型对断言 `MOD` 的 combined output_field 与 ADD/SUB 等一致（如 Integer%Decimal→Decimal）。

## 调用链分析

`_connector_combinations` 是模块级列表，每个元素是 `{connector: [(lhs_type, rhs_type, result_type), ...]}` 的字典，由字典推导式 `{connector: [...] for connector in (...)}` 生成。`CombinedExpression._resolve_output_field` → `_resolve_combined_type(connector, lhs_type, rhs_type)` 遍历这些字典查 `(lhs,rhs)→result`。MOD 不在"不同类型"组的 connector 元组里时，`Decimal % Integer` 查不到规则，返回 None，output_field 解析失败。F2P 正是断言混合类型 MOD 能解析。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 删除 `Combinable.MOD,` 行，直接还原 bug |
| B | 🟢 高质量 | 保留 | 注释掉 `Combinable.MOD,` 行 |
| C | 🔴 必须替换 | 替换 | 原 C 用空行替换（效果同删除，且留空连接符行不自然）；改为换成 `Combinable.POW` |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 字节相同；改为单独注册 MOD 但结果类型错误 |
| E | ➕ 补充 | 新增 | 原缺有效 E；MOD 注册藏到默认关闭开关后 |

原 A=D=E 三组都是"删除 MOD 行"，C 是"用空行替换"（实质也是删除）。保留 A（删除）、B（注释），把 C 重做为 `POW`（错误常量）、D 重做为"注册但结果类型错"、E 重做为开关 gate。

## 各组 Mutation 分析

### Group A — 保留（B2 删除 case）
```diff
             Combinable.DIV,
-            Combinable.MOD,
         )
     },
     # Bitwise operators.
```
**变异语义**：从"不同类型数值运算"组删除 `Combinable.MOD,`，混合类型 MOD 查不到组合规则，output_field 解析失败。直接还原 bug。保留。

### Group B — 保留（D2 死代码注释）
```diff
             Combinable.DIV,
-            Combinable.MOD,
+            # Combinable.MOD,
         )
```
**变异语义**：注释掉 MOD 行，效果同删除，形式是"临时注释忘恢复"的死代码。保留。

### Group C — 替换（C1 类型/数据形状：错误常量）
**原**：用纯空行替换 MOD（实质删除、且留下尴尬的空行不自然）。
**最终 mutation**：
```diff
             Combinable.DIV,
-            Combinable.MOD,
+            Combinable.POW,
         )
```
**变异语义**：把 `Combinable.MOD` 错写成 `Combinable.POW`。MOD 仍未注册到"不同类型"组（漏检 bug 仍在），同时 POW 被**重复**注册（它在"相同类型"组已有，这里多注册一次混合类型组合）。看起来"加了个运算符"，实则加错了——MOD 依旧解析失败。比删除/空行更自然（像是写错了相邻常量名），且不留可疑空行。

### Group D — 替换（D1 状态：注册但结果类型错误）
**原**：与 A 字节完全相同（删除 MOD 行）。
**最终 mutation**：
```diff
             Combinable.DIV,
-            Combinable.MOD,
         )
     },
+    {
+        Combinable.MOD: [
+            (fields.IntegerField, fields.DecimalField, fields.IntegerField),
+            (fields.DecimalField, fields.IntegerField, fields.IntegerField),
+            (fields.IntegerField, fields.FloatField, fields.IntegerField),
+            (fields.FloatField, fields.IntegerField, fields.IntegerField),
+        ]
+    },
     # Bitwise operators.
```
**变异语义**：把 MOD 从"不同类型"组移除，改为**单独注册**一个字典，但结果类型全错——`Integer%Decimal` 被注册成 `→IntegerField`（应是 `DecimalField`）。MOD 这次"被解析了"，但解析出错误的 output_field。F2P 断言混合 MOD 结果与 ADD 一致（Decimal）失败。比"完全不注册"更隐蔽：MOD 看起来已支持，只是结果类型悄悄错了。模拟"注册了但抄错了结果类型"。

### Group E — 补充（E2 隐式→显式开关）
```diff
             Combinable.DIV,
-            Combinable.MOD,
+            *([Combinable.MOD] if globals().get("RESOLVE_MOD_MIXED", False) else []),
         )
```
**变异语义**：用解包表达式把 MOD 的注册做成条件——只有模块全局 `RESOLVE_MOD_MIXED` 为 True 时才把 `Combinable.MOD` 加入连接符元组，默认（未设该全局）为空，MOD 不注册。模拟"把支持做成 feature flag、默认却关掉"。语法合法（解包空列表无副作用），默认行为等于未修复。

## 新设计 Mutation 说明

原 A=D=E 三组都是删除 MOD 行、C 用空行替换（实质也是删除），五组实际只有"删除/注释"两种机制，且 C 的空连接符行不自然。本次保留 A（删除）、B（注释），把 C 重做为 `Combinable.POW`（写错相邻常量，更自然且不留空行）、D 重做为"单独注册 MOD 但结果类型全错"（解析成功但类型错，更隐蔽）、E 重做为"`RESOLVE_MOD_MIXED` 全局开关默认关闭"。五组覆盖"删除 / 注释 / 错误常量 / 错误结果类型 / 默认关闭开关"五个角度。受限于 golden 是单行注册，五组在保持各自机制独立的前提下均精确作用于该注册点。全部实测：golden 通过、五个变异均令 F2P（`test_resolve_output_field_number`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
