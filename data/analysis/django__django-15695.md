# django__django-15695

## 问题背景

`RenameIndex()` 在处理无名索引（unique_together 自动生成名）的来回迁移时崩溃：当索引名向后回退到原自动名后再次 forward，重新应用 `RenameIndex` 会因为"重命名到自身"而报错。Golden patch 在 `database_forwards` 中加 noop 守卫：当 `old_index.name == self.new_name`（名字未变）时直接 return。

## Golden Patch 语义分析

```python
old_index = from_model_state.get_index_by_name(self.old_name)
# Don't alter when the index name is not changed.
if old_index.name == self.new_name:
    return
to_model_state = to_state.models[app_label, self.model_name_lower]
new_index = to_model_state.get_index_by_name(self.new_name)
schema_editor.rename_index(model, old_index, new_index)
```
核心语义：**当旧索引名已等于目标新名时，重命名是 noop，直接返回，避免重复 rename 崩溃**。判据是 `old_index.name == self.new_name`。该守卫在 unnamed index 来回迁移的"再次 forward"场景下防止 `rename_index` 把索引重命名到自身。

F2P 测试 `OperationTests.test_rename_index_unnamed_index`：backward 后再 forward，断言不崩溃且索引名保持 `new_pony_test_idx`。

## 调用链分析

`RenameIndex.database_forwards` 取 `old_index`（按 old_name 或 unique_together 匹配），与 `self.new_name` 比较；若相等则 noop。否则取 `new_index` 调 `schema_editor.rename_index`。当 backward 已把名字还原成自动名、再次 forward 时，old_index.name 恰等于 new_name，守卫触发 noop。守卫失效则 `rename_index` 试图重命名到已存在/相同名而报错。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `== self.new_name`→`== self.old_name`，比较错对象 |
| B | 🟢 高质量 | 保留 | 删除整个 noop 守卫 |
| C | 🔴 必须替换 | 替换 | 原 C≈B（删 return 留注释）；改为 `is` 身份比较 |
| D | 🔴 必须替换 | 替换 | 原 D≈B（注释掉守卫）；改为比较 index 对象而非名字 |
| E | 🟢 高质量 | 保留 | `==`→`!=`，逻辑反转 |

原 B/C/D 三者都是"删/注释守卫"的变体。保留 B，重做 C、D 为不同机制。

## 各组 Mutation 分析

### Group A — 保留（B3 条件语义：比较错属性）
```diff
-        if old_index.name == self.new_name:
+        if old_index.name == self.old_name:
```
**变异语义**：noop 判据从"旧索引名 == 新名"改成"== 旧名"。`old_index.name` 通常就等于 `old_name`（它是按 old_name 取的），故守卫几乎总触发 → 正常的重命名也被 noop 跳过，索引名不更新。但在 unnamed 场景 old_index.name 是自动名、old_name 可能不同，行为错乱。模拟 new/old 混淆。保留。

### Group B — 保留（D-删除守卫）
```diff
-        # Don't alter when the index name is not changed.
-        if old_index.name == self.new_name:
-            return
```
**变异语义**：删除整个 noop 守卫。再次 forward 时 `rename_index` 试图把索引重命名到自身/已存在名而崩溃。直接还原原 bug。保留。

### Group C — 替换（C1 类型/数据形状：身份比较）
**原**：删 return 留注释（≈B）。
**最终 mutation**：
```diff
-        if old_index.name == self.new_name:
+        if old_index.name is self.new_name:
```
**变异语义**：用 `is`（对象身份）比较两个字符串名而非 `==`（值相等）。两个来源不同的等值字符串通常不是同一对象（除非 interned），`is` 多半为假 → 守卫失效 → 崩溃。模拟"用 is 比较字符串值"的常见错误，比删除守卫更隐蔽。

### Group D — 替换（A1 接口契约：比较 index 对象）
**原**：注释掉守卫（≈B）。
**最终 mutation**：
```diff
-        # Don't alter when the index name is not changed.
-        if old_index.name == self.new_name:
-            return
-
        to_model_state = to_state.models[app_label, self.model_name_lower]
        new_index = to_model_state.get_index_by_name(self.new_name)
+        # Don't alter when the index name is not changed.
+        if old_index == new_index:
+            return
```
**变异语义**：把守卫移到 new_index 取出之后，改用 `old_index == new_index`（Index 对象相等）判定。两个 Index 对象即便同名也可能因 fields/其它属性不同而不相等 → 守卫不触发 → 崩溃。模拟"比较整个对象而非关键的名字字段"。

### Group E — 保留（B3 逻辑反转）
```diff
-        if old_index.name == self.new_name:
+        if old_index.name != self.new_name:
```
**变异语义**：守卫条件反转。名字**不同**时 noop return（跳过本该执行的重命名），名字相同时反而执行（触发崩溃）。语义完全颠倒。保留。

## 新设计 Mutation 说明

原 B/C/D 三者都是删除/注释 noop 守卫的近似重复。本次保留 B（删守卫）、A（比较错属性）、E（逻辑反转），把 C 改为 `is` 身份比较、D 改为比较 Index 对象（移到 new_index 之后）。五组覆盖"错属性 / 删守卫 / 身份比较 / 对象比较 / 逻辑反转"五个角度。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用。
