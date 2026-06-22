# django__django-13401

## 问题背景

当多个 concrete 模型继承同一个 abstract 模型时，每个 concrete 模型都会获得 abstract 字段的一个拷贝（通过 `contribute_to_class` 深拷贝）。这些字段拷贝都共享相同的 `creation_counter`（在 abstract 模型定义时分配），但 `field.model` 不同（分别指向各自的 concrete 模型）。

原始的 `Field.__eq__` 只比较 `creation_counter`，导致来自不同 concrete 模型的同名继承字段被认为相等，放入 set 中时只保留一个。修复方案：同时比较 `field.model`，并同步修复 `__hash__` 和 `__lt__` 以保持一致性。

## Golden Patch 语义分析

修复涉及三个方法：

**`__eq__`**：增加 `getattr(self, 'model', None) == getattr(other, 'model', None)` 条件，确保不同模型的字段不相等。

**`__lt__`**：先按 `creation_counter` 排序（保持向后兼容），当 counter 相等时按 `(app_label, model_name)` 元组排序。特殊处理：一个有 model 一个没有时，无 model 的排前面。

**`__hash__`**：加入 `app_label` 和 `model_name` 参与哈希计算，保证 `a == b → hash(a) == hash(b)` 的不变量在修改后仍然成立。

## 调用链分析

```
# Abstract 字段拷贝路径
AbstractModel.field (creation_counter=N)
  → contribute_to_class() → deepcopy → InheritModel1.field (model=InheritModel1, counter=N)
  → contribute_to_class() → deepcopy → InheritModel2.field (model=InheritModel2, counter=N)

# 比较路径
InheritModel1._meta.get_field('field') == InheritModel2._meta.get_field('field')
  → Field.__eq__(self, other)
  → self.creation_counter == other.creation_counter  # True (both = N)
  → getattr(self, 'model', None) == getattr(other, 'model', None)  # False (InheritModel1 ≠ InheritModel2)
  → 修复后返回 False

# 哈希路径
hash(field) → hash((creation_counter, app_label, model_name))
  → InheritModel1.field 和 InheritModel2.field 哈希不同
  → set 中可以同时存在两者
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新建 | 数据集无 A 组，设计 __hash__ 中 model_name 被 app_label 替换 |
| B | 缺失 | 新建 | 数据集无 B 组，设计 __lt__ 条件逻辑反转 |
| C | 高质量 | 保留 | 删除 __eq__ 的 model 检查，自然、直接，C1 策略 |
| D | 缺失 | 新建 | 数据集无 D 组，设计 __lt__ 末端比较符号反转 |
| E | 高质量 | 保留 | __eq__ 引入 check_model=False 参数，E2 策略 |

## 各组 Mutation 分析

### Group A — 新建

**最终 mutation**：
```diff
         return hash((
             self.creation_counter,
             self.model._meta.app_label if hasattr(self, 'model') else None,
-            self.model._meta.model_name if hasattr(self, 'model') else None,
+            self.model._meta.app_label if hasattr(self, 'model') else None,
         ))
```
**变异语义**：`__hash__` 中将第三个分量从 `model_name` 改为再次使用 `app_label`，导致哈希元组变为 `(counter, app_label, app_label)`。同一 Django app 中所有继承同一 abstract 字段的模型（如 `app.InheritModel1` 和 `app.InheritModel2`）的字段哈希完全相同。虽然 `__eq__` 正确返回不相等，但 hash 相同导致 set 中退化为线性查找时可能发生意外行为，且 F2P 中 `assertNotEqual(hash(...), hash(...))` 断言失败。模拟了开发者 copy-paste 时错误地复制了 `app_label` 行而未改为 `model_name`。

---

### Group B — 新建

**最终 mutation**：
```diff
             if (
-                self.creation_counter != other.creation_counter or
+                self.creation_counter == other.creation_counter or
                 not hasattr(self, 'model') and not hasattr(other, 'model')
             ):
                 return self.creation_counter < other.creation_counter
```
**变异语义**：`__lt__` 中反转了 `!=` 为 `==`。原逻辑：当 counter 不同时用 counter 比较（快路径），counter 相同时走后续 model 比较逻辑。变异后：当 counter 相同时（abstract-inherited 字段的典型情况）直接用 counter 比较，返回 `N < N` = `False`，导致所有 abstract-inherited 字段之间 `__lt__` 恒为 False，破坏 F2P 中的 `assertLess` 断言。

---

### Group C — 保留

**原 mutation**：
```diff
-                self.creation_counter == other.creation_counter and
-                getattr(self, 'model', None) == getattr(other, 'model', None)
+                self.creation_counter == other.creation_counter
```
**分类**：🟢 保留
**理由**：直接删除 `__eq__` 的 model 检查，恢复原始仅按 counter 比较的逻辑。自然且直接：开发者可能认为 model 检查是多余的，或误解为只需保证 counter 唯一性。导致 F2P 中 `assertNotEqual` 失败。

---

### Group D — 新建

**最终 mutation**：
```diff
                 return (
-                    (self.model._meta.app_label, self.model._meta.model_name) <
+                    (self.model._meta.app_label, self.model._meta.model_name) >=
                     (other.model._meta.app_label, other.model._meta.model_name)
                 )
```
**变异语义**：`__lt__` 的末端 model 元组比较从 `<` 改为 `>=`，完全反转了 model 层面的排序。原逻辑按字母序（`app_label, model_name`）小的排在前面；变异后按字母序大的排在前面。F2P 测试中 `assertLess(inherit1_model_field, inherit2_model_field)` 失败（InheritAbstractModel1 < InheritAbstractModel2 按字母序，但 `>=` 使 1's field 不小于 2's field）。

---

### Group E — 保留

**原 mutation**：
```diff
-    def __eq__(self, other):
+    def __eq__(self, other, check_model=False):
         ...
-                getattr(self, 'model', None) == getattr(other, 'model', None)
+                (not check_model or getattr(self, 'model', None) == getattr(other, 'model', None))
```
**分类**：🟢 保留
**理由**：E2 策略，将隐式行为变为显式参数控制。`check_model=False` 意味着默认不比较 model，Python 的比较协议（`a == b`）从不传递额外参数，所以 model 永远不被比较，F2P 中的 `assertNotEqual` 失败。代码看起来像是在提供更灵活的 API，实则破坏了默认行为。

## 新设计 Mutation 说明

### Group A（A1 — hash 组件重复）
`__hash__` 使用三元组 `(counter, app_label, model_name)` 来唯一标识字段。开发者在修复时可能 copy-paste 了 app_label 行（第 2 行）而忘记将第 3 行改为 `model_name`，导致 `(counter, app_label, app_label)` 无法区分同 app 不同 model 的字段。

### Group B（B3 — 条件反转）
`__lt__` 的分支逻辑：`counter != other.counter` 为 True 时走快路径（counter 比较），False 时走慢路径（model 比较）。将 `!=` 改为 `==` 后，快慢路径的触发条件完全互换：counter 相同时（正是需要 model 比较的场景）却用 counter 比较，model 不同时却用 model 比较（但此时 counter 已不同，model 比较逻辑实际不影响 counter 不同的情况）。

### Group D（D1 — 比较符号反转）
`__lt__` 末端的元组比较 `<` 改为 `>=`，将排序方向完全反转。开发者可能将 `>=` 误理解为"至少不小于意味着大于或等于"，或在 code review 中未注意到 `<` 被改为 `>=` 的差异。
