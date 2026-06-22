# django__django-16819

## 问题背景

迁移优化器未归并 `AddIndex`/`RemoveIndex` 操作。当一个迁移先 `AddIndex` 再 `RemoveIndex` 同一个索引时，二者应被优化器消除（互相抵消，归并为空）。根因：`AddIndex` 没有实现针对 `RemoveIndex` 的 `reduce`。Golden patch 给 `AddIndex` 新增 `reduce` 方法：当后续 operation 是 `RemoveIndex` 且索引名相同时返回 `[]`（消除两者），否则委托 `super().reduce`。

## Golden Patch 语义分析

```python
def reduce(self, operation, app_label):
    if isinstance(operation, RemoveIndex) and self.index.name == operation.name:
        return []
    return super().reduce(operation, app_label)
```
核心语义：**当后续操作是 `RemoveIndex`（类型）且其 `name` 等于本 `AddIndex` 的 `self.index.name`（同一索引）时，`reduce` 返回 `[]`——把"加了又删"这对操作彻底消除**。两个判定缺一不可：`isinstance(operation, RemoveIndex)` 限定类型、`self.index.name == operation.name` 限定同名索引。返回 `[]` 表示归并为空；不匹配则 `super().reduce` 走默认（不归并）。

F2P 测试 `OptimizerTests.test_add_remove_index`：`AddIndex("Pony", Index(name="idx_pony_weight_pink"))` 后接 `RemoveIndex("Pony", "idx_pony_weight_pink")`，断言 `assertOptimizesTo([...], [])`（优化结果为空列表）。

## 调用链分析

迁移优化器对相邻操作两两调 `reduce`。`AddIndex.reduce(operation, app_label)`：若 operation 是 RemoveIndex 且 `self.index.name == operation.name` 返回 `[]`（消除），否则 `super().reduce`。`RemoveIndex.name` 是索引名字符串，`AddIndex.index.name` 是 Index 对象的 name。类型判断、名字比较、模型名比较、或整体藏到开关后出错，都会让 Add/Remove 不被消除（优化结果非空）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 加 `and self.model_name != operation.model_name`，同模型恒不折叠 |
| B | 🟢 高质量 | 保留 | `self.index.name == operation.name`→`!=`，名字比较反转 |
| C | 🟢 高质量 | 保留 | 加 `self.model_name == operation.model_name_lower`，大小写不符恒假 |
| D | 🟢 高质量 | 保留 | 去掉 `isinstance` 类型检查 + model_name_lower 反转比较 |
| E | ➕ 新增 | 新增 | 折叠藏到 `reduce_add_remove_index` 开关后（默认关） |

原始仅 A/B/C/D 四组，缺 E。四组机制各异（加模型名不等条件 / 名字比较反转 / 大小写不符 / 删类型检查+反转），全部保留；补充 E（默认关闭开关）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：加模型名不等条件）
```diff
-        if isinstance(operation, RemoveIndex) and self.index.name == operation.name:
+        if isinstance(operation, RemoveIndex) and self.index.name == operation.name and self.model_name != operation.model_name:
```
**变异语义**：折叠条件追加 `and self.model_name != operation.model_name`，要求模型名**不同**才折叠。而 `test_add_remove_index` 的 Add/Remove 都针对 `"Pony"`（同模型），`model_name != model_name` 恒假 → 条件恒不满足 → 不折叠。优化结果非空，`assertOptimizesTo([...], [])` 失败。模拟"多加了一个看似合理但方向错的守卫"。保留。

### Group B — 保留（B3 条件反转：名字比较取反）
```diff
-        if isinstance(operation, RemoveIndex) and self.index.name == operation.name:
+        if isinstance(operation, RemoveIndex) and self.index.name != operation.name:
```
**变异语义**：`self.index.name == operation.name` 反转成 `!=`。同名索引（正是要消除的）`==` 为真 → `!=` 为假 → 不折叠；不同名索引反而被错误折叠（丢失操作）。语义颠倒。F2P 的同名 Add/Remove 不被消除 → 失败。保留。

### Group C — 保留（C1 值：大小写不一致比较）
```diff
-        if isinstance(operation, RemoveIndex) and self.index.name == operation.name:
+        if isinstance(operation, RemoveIndex) and self.model_name == operation.model_name_lower and self.index.name == operation.name:
```
**变异语义**：追加 `self.model_name == operation.model_name_lower` 比较——`model_name`（保留原始大小写，如 `"Pony"`）与 `model_name_lower`（小写化，如 `"pony"`）。`"Pony" == "pony"` 恒假 → 条件永不满足 → 不折叠。模拟"比较时一边用了未小写的属性、一边用了小写属性，大小写不匹配"。比 A 隐蔽——看着像是在比模型名，实则大小写恒不等。保留。

### Group D — 保留（D1 状态：删类型检查 + 反转模型名比较）
```diff
-        if isinstance(operation, RemoveIndex) and self.index.name == operation.name:
+        if self.index.name == operation.name and self.model_name_lower != operation.model_name_lower:
```
**变异语义**：去掉 `isinstance(operation, RemoveIndex)` 类型检查，仅留名字比较，并追加反转的 `self.model_name_lower != operation.model_name_lower`。同模型（model_name_lower 相等）→ `!=` 为假 → 不折叠；同时缺类型检查，对非 RemoveIndex 的 operation（可能无 `.name` 属性）也会进入比较 → 潜在 AttributeError。F2P 同模型 Add/Remove 不被消除 → 失败。模拟"删掉类型守卫并写了方向相反的模型名比较"。保留。

### Group E — 新增（E2 隐式→显式开关）
```diff
     def reduce(self, operation, app_label):
-        if isinstance(operation, RemoveIndex) and self.index.name == operation.name:
+        if (
+            getattr(self, "reduce_add_remove_index", False)
+            and isinstance(operation, RemoveIndex)
+            and self.index.name == operation.name
+        ):
             return []
         return super().reduce(operation, app_label)
```
**变异语义**：折叠条件前置 `getattr(self, "reduce_add_remove_index", False)` 开关（默认不存在 → False）。默认情况下整个 `if` 恒假 → 不折叠、走 `super().reduce`（基类不消除 Add/Remove）。只有显式设 `reduce_add_remove_index=True` 才生效。默认构造的 AddIndex 不设该属性 → AddIndex+RemoveIndex 不被优化消除。模拟"把 Add/Remove 索引折叠做成可配置、默认却关掉"。新增为 E。

## 新设计 Mutation 说明

原始仅 A/B/C/D 四组，缺第五组。四组机制互异：A（加 `model_name != model_name` 同模型恒假）、B（名字比较 `==`→`!=` 反转）、C（`model_name` vs `model_name_lower` 大小写恒不等）、D（删 isinstance 类型检查 + 反转 model_name_lower 比较），全部保留。补充 E（`reduce_add_remove_index` 默认关闭开关）。五组覆盖"加错误守卫 / 名字反转 / 大小写不符 / 删类型检查 / 默认关闭开关"五个角度，分别作用于模型名比较、索引名比较、属性大小写、类型守卫、特性开关五个环节——全部令 AddIndex+RemoveIndex 不被消除、优化结果非空。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
