# django__django-14915

## 问题背景

用户从 Django 3.0 升级到 3.1 后，向 select widget 的 option 添加自定义 `data-*` 属性的代码报错 `TypeError: unhashable type: 'ModelChoiceIteratorValue'`。原因是 3.1 引入了 `ModelChoiceIteratorValue` 包装类替代裸的 value，而该类定义了 `__eq__` 却没有定义 `__hash__`。在 Python 中，一旦类自定义了 `__eq__`，其 `__hash__` 会被设为 `None`，对象变为不可哈希。因此 `value in self.show_fields`（dict 的 key 查找需要哈希）会失败，而 `value in [1, 2]`（list 只用 `==`）不会。

Golden patch 为 `ModelChoiceIteratorValue` 补回 `__hash__` 方法，返回 `hash(self.value)`，使其哈希语义与 `__eq__` 保持一致（`__eq__` 只比较 `self.value`）。

## Golden Patch 语义分析

```python
def __hash__(self):
    return hash(self.value)
```

核心语义不是"加一个方法"，而是**重建 hash/eq 契约的一致性**：
- `__eq__` 只比较底层 `self.value`（解包另一个 `ModelChoiceIteratorValue` 后比 value）。
- 因此 `__hash__` 必须**只依赖 `self.value`**，不能依赖 `self.instance`。
- 这样保证：两个 value 相等的对象哈希也相等（哈希契约 `a == b ⇒ hash(a) == hash(b)`），并且 `ModelChoiceIteratorValue(pk, instance)` 与 `ModelChoiceIteratorValue(pk, None)` 可以互换作为 dict key。

F2P 测试 `test_choice_value_hash` 精确锁死了这一契约：
```python
self.assertEqual(hash(value_1), hash(ModelChoiceIteratorValue(self.c1.pk, None)))  # hash 必须与 instance 无关
self.assertNotEqual(hash(value_1), hash(value_2))                                  # 不同 pk 必须哈希不同
```

任何让哈希**掺入 instance 信息**的实现都会违反第一条断言；任何让哈希**丢失 value 区分度**的实现都会违反第二条断言。

## 调用链分析

- `ModelChoiceIterator.choice(obj)` → 构造 `ModelChoiceIteratorValue(self.field.prepare_value(obj), obj)`，即 `value=pk`、`instance=model 对象`。
- 这些 value 进入 widget 渲染层，用户在 `create_option` 中执行 `value in some_dict`，触发 `__hash__`。
- `Model.__hash__`（`django/db/models/base.py:540`）：`pk is None` 时抛 `TypeError`，否则返回 `hash(self.pk)`。这意味着把 instance 卷进哈希时，若 instance 为 `None` 则 `hash(None)` 是常量、若为有 pk 的 model 则等于 `hash(pk)`——这给了多种"看似合理却破坏契约"的变异空间。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层（保留） | 保留 | `hash(self.instance)`，把哈希键从 value 误换成 instance，是自然的开发者属性混淆，位于核心契约节点 |
| B | 🟢 高质量 | 保留 | `hash((self.value, id(self.instance)))`，"为唯一性把实例身份并入哈希"的真实误解，破坏 value↔(value,None) 互换性 |
| C | 🔴 必须替换 | 替换 | 原为 `__hash__ = None`，等价于还原 base_commit 的不可哈希状态（功能等价冗余），且缩进怪异不自然 |
| E | 🔴 必须替换 | 替换 | 原 diff 与 B 字节级完全相同（重复），无多样性 |

语义浅层共 1 个（A），按规则替换 floor(1/2)=0 个，故 A 保留。
必须替换 2 个（C、E），各设计高质量替代。

## 各组 Mutation 分析

### Group A — 保留
**原 mutation**：
```diff
@@ -1167,7 +1167,7 @@ class ModelChoiceIteratorValue:
         return str(self.value)
 
     def __hash__(self):
-        return hash(self.value)
+        return hash(self.instance)
```
**分类**：🟡 语义浅层（属性名替换 value→instance）
**理由**：虽是单 token 替换，但位置在 hash/eq 契约的核心节点，模拟开发者"该哈希哪个字段"的真实混淆。`hash(self.instance)` 等于 `hash(pk)`，所以"不同 pk 哈希不同"碰巧仍满足；但 `ModelChoiceIteratorValue(pk, None)` 触发 `hash(None)`（常量），与 `hash(c1实例)=hash(pk)` 不相等，从而打破第一条断言。它不破坏典型的"value in list"路径（list 不用 hash），只在依赖 hash/eq 一致性的字典查找下暴露。保留。
**最终 mutation**：同原。
**变异语义**：把哈希身份从"逻辑值"偷换为"模型实例"。绝大多数典型测试（用真实 instance 作 key、用同一对象查同一对象）都通过，只有当 value 与 None-instance 形式互换时才暴露契约破坏。

### Group B — 保留
**原 mutation**：
```diff
@@ -1167,7 +1167,7 @@ class ModelChoiceIteratorValue:
         return str(self.value)
 
     def __hash__(self):
-        return hash(self.value)
+        return hash((self.value, id(self.instance)))
```
**分类**：🟢 高质量
**理由**：`id(self.instance)` 是对象内存地址，把"实例身份"并入哈希是开发者出于"增强唯一性"的常见误解，看起来比纯 `hash(value)` 更"严谨"，代码审查极难一眼看出错误。它使同一 pk 但不同 instance 对象（包括 `None`）哈希全部不同，破坏与 `__eq__` 的一致性，导致 dict key 查找在跨请求/跨对象场景静默失效。保留。
**最终 mutation**：同原。
**变异语义**：哈希混入非确定性的对象地址，违反 `a==b ⇒ hash(a)==hash(b)`。典型测试用同一对象引用时哈希恰好一致而通过，只有 `(pk, instance)` 与 `(pk, None)` 比较时崩溃。

### Group C — 替换
**原 mutation**：
```diff
@@ -1166,8 +1166,7 @@ class ModelChoiceIteratorValue:
     def __str__(self):
         return str(self.value)
 
-    def __hash__(self):
-        return hash(self.value)
+        __hash__ = None
```
**分类**：🔴 必须替换（功能等价冗余 + 不自然）
**理由**：`__hash__ = None` 让类显式不可哈希，这恰好**等价于还原 base_commit 的原始 bug**（base 没有 `__hash__`、对象不可哈希）。这是 golden patch 的逆操作，属直接冗余；且其缩进与位置（紧贴 `__str__` 下方、错位的缩进）在审查中一望即知是人为破坏。必须替换。
**最终 mutation**（替换为类型形状变异，契合 C 组"Type & Data Shape"）：
```diff
@@ -1167,7 +1167,7 @@ class ModelChoiceIteratorValue:
         return str(self.value)
 
     def __hash__(self):
-        return hash(self.value)
+        return hash((self.value, type(self.instance)))
```
**变异语义**：把 instance 的**类型**并入哈希键，模拟"为区分不同模型类的 value 而带上类型信息"的合理化误解（数据形状变异）。`value_1.instance` 为 `Category` 实例、对照组 instance 为 `None` → `type(...)` 分别是 `Category` 与 `NoneType`，哈希不同，打破 value↔None 互换断言。所有用同类真实 instance 的典型测试都通过，只在 instance 形状（类型）不一致时暴露。

### Group E — 替换
**原 mutation**（与 B 完全重复）：
```diff
@@ -1167,7 +1167,7 @@ class ModelChoiceIteratorValue:
         return str(self.value)
 
     def __hash__(self):
-        return hash(self.value)
+        return hash((self.value, id(self.instance)))
```
**分类**：🔴 必须替换（与 B 字节级重复，无多样性）
**理由**：E 组 diff 与 B 组逐字相同，5 个变异不应集中于同一实现。必须替换为不同语义的高质量变异。
**最终 mutation**（替换为 XOR 组合，契合 E 组"Test-expectation Alignment"——改变行为使精确断言失效）：
```diff
@@ -1167,7 +1167,7 @@ class ModelChoiceIteratorValue:
         return str(self.value)
 
     def __hash__(self):
-        return hash(self.value)
+        return hash(self.value) ^ hash(self.instance)
```
**变异语义**：用 `^` 把 value 哈希与 instance 哈希异或，模拟"组合多字段以改善哈希分布"的工程惯例。对照组 instance 为 `None`：`hash(value) ^ hash(None)` ≠ `hash(value)`（因 `hash(None)` 非零），破坏第一条断言。代码看起来是经典的哈希组合写法，审查难以察觉它违反了"哈希必须只依赖 value"这一与 `__eq__` 绑定的隐式契约。

## 新设计 Mutation 说明

C、E 两个替代均建立在对 hash/eq 契约的深层理解上：`__eq__` 只比较 `self.value`，因此任何让 `__hash__` 额外依赖 `self.instance`（无论以类型、id 还是 xor 形式）都会违反 Python 哈希契约，并精确触发 F2P 测试的第一条断言 `hash(value_1) == hash(ModelChoiceIteratorValue(pk, None))`。

- **C（`type(self.instance)`）**：选择"类型形状"维度，符合 C 组主题；模拟开发者担心"不同模型的同 pk 冲突"而引入类型区分，是合理化误解，不触发任何语法/导入错误，仅在 instance 类型不一致（如 `None` vs `Category`）时哈希分叉。
- **E（`hash(self.value) ^ hash(self.instance)`）**：选择"哈希组合"惯用法，符合 E 组"让精确断言失效"主题；XOR 是教科书式的多字段哈希组合手法，外观极其自然，但悄然把 instance 卷入，破坏与 `__eq__` 的一致性。

两个替代均通过自查：可在 `base_commit → golden patch → test_patch` 后干净应用、`py_compile` 通过，且实际运行 F2P 测试 `test_choice_value_hash` 失败（golden 通过、四个变异均 FAILED），符合"破坏修复"要求。
