# django__django-15104

## 问题背景

迁移自动检测器 `MigrationAutodetector.only_relation_agnostic_fields` 在剥离外键字段的 `to` 信息以做"关系无关"比较时，用 `del deconstruction[2]['to']` 直接删键。若某自定义 FK 在 `deconstruct()` 里已经把 `to` 删掉（hardcoded reference 场景），该 dict 里就没有 `to` 键，`del` 抛 `KeyError`，导致 `makemigrations`/测试在 verbose 模式崩溃。Golden patch 把 `del deconstruction[2]['to']` 改成 `deconstruction[2].pop('to', None)`，键不存在时安全跳过。

## Golden Patch 语义分析

```python
-                del deconstruction[2]['to']
+                deconstruction[2].pop('to', None)
```
核心语义：**用带默认值的 `pop('to', None)` 容忍 `to` 键缺失**。`deconstruction` 是 `(name, path, kwargs)` 三元组，`deconstruction[2]` 是 kwargs 字典。修复保证：当字段的 `deconstruct()` 已移除 `to`（自定义 hardcoded FK）时，这里不再 `KeyError`。判据依赖 `field.remote_field and field.remote_field.model` 为真才尝试移除。

F2P 测试 `test_add_custom_fk_with_hardcoded_to`：定义一个 `HardcodedForeignKey`，其 `deconstruct()` 里 `del kwargs['to']`，然后调用 `get_changes`，断言能正常检测出 1 个 `CreateModel`（即不崩溃）。

## 调用链分析

`only_relation_agnostic_fields(fields)` 对每个字段 `deep_deconstruct` 得到 `(name, path, kwargs)`，若有远端模型则移除 `kwargs['to']` 再收入 `fields_def`。`get_changes` → `_detect_changes` → 比较新旧 ModelState 时调用此函数。本测试的字段 `deconstruct()` 主动删了 `to`，因此移除步骤必须容错。**该 F2P 只检验"不崩溃、能产出 CreateModel"**，并不检验 rename-agnostic 比较结果，所以让它失败的唯一途径是**重新引发移除步骤的异常**（KeyError/TypeError 等）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| B | 🔴 必须替换 | 替换 | 原 `pop('to', None)`→`del ...['to']`，与 D/E 字节级重复（直接还原） |
| C | 🟡 | 替换 | 原 `pop('to')`（无默认）→KeyError，是合理的浅层变体之一，保留其思路 |
| D | 🔴 必须替换 | 替换 | 与 B/E 完全相同 |
| E | 🔴 必须替换 | 替换 | 与 B/D 完全相同 |

原四组实质只有两种（`del` 重复 3 次 + `pop('to')` 1 次）。由于该 F2P 只能靠"重新触发异常"来失败，替代必须都引发异常，但通过**不同的代码机制与异常类型**实现多样化。

## 各组 Mutation 分析

### Group B — 替换（B2 缺失键处理）
```diff
-                deconstruction[2].pop('to', None)
+                deconstruction[2].pop('to')
```
**变异语义**：去掉 `pop` 的默认值参数。`to` 键缺失时（hardcoded FK 场景）`pop('to')` 抛 `KeyError: 'to'`——正是 golden 修复的原始 bug。模拟"以为 pop 一定有这个键"的 None-case 处理缺失。

### Group C — 替换（C1 类型/数据形状）
```diff
-                deconstruction[2].pop('to', None)
+                del deconstruction[2]['to']
```
**变异语义**：还原成 `del`，键缺失即 `KeyError`。作为 C（数据形状）代表：`del dict[key]` 假设字典形状一定含该键。与 B 的 `pop('to')` 是不同 API 写法但同类失败。

### Group D — 替换（D-状态：先读后删）
```diff
-                deconstruction[2].pop('to', None)
+                to_value = deconstruction[2]['to']
+                deconstruction[2].pop('to', None)
```
**变异语义**：在安全 `pop` 之前，先用下标 `deconstruction[2]['to']` 读取该键（仿佛要记录/日志 `to` 旧值）。安全 pop 本身没问题，但**前置的下标读取**在键缺失时先抛 `KeyError`。模拟"加一行读取做日志、却忘了它同样会因缺键崩溃"的状态读取 bug。异常类型同为 KeyError 但触发点不同（读取而非删除）。

### Group E — 替换（B1 off-by-one 索引）
```diff
-                deconstruction[2].pop('to', None)
+                deconstruction[1].pop('to', None)
```
**变异语义**：把元组索引从 `[2]`（kwargs 字典）误写成 `[1]`（path 字符串/或 args）。`deconstruction[1]` 不是 dict，`.pop('to', None)` 在其上要么因 `list.pop` 只接受 1 个参数而抛 `TypeError: pop expected at most 1 argument, got 2`，要么因 str 无 pop 抛 `AttributeError`。模拟解构三元组时**索引 off-by-one**取错元素。异常类型与 B/C/D 不同（TypeError），机制独特。

## 新设计 Mutation 说明

该 F2P 的特性决定了所有有效变异都必须在"移除 `to`"这一步重新引发异常（它只检验不崩溃）。四个替代因此覆盖**四种不同的出错机制/异常**：B（`pop` 去默认值→KeyError）、C（`del`→KeyError，不同 API）、D（前置下标读取→KeyError，不同触发点）、E（索引 off-by-one `[1]`→TypeError，取错容器）。相比原始的"3 个相同 `del` + 1 个 `pop('to')`"，多样性显著提升。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
