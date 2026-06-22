# django__django-15315

## 问题背景

`Field.__hash__` 不应随字段被赋给模型类而改变。`#31750` 引入的 `__hash__` 把 `model._meta.app_label/model_name` 纳入哈希，导致字段在被赋给模型前后哈希值变化，破坏其作为 dict key 的使用（`f in d` 断言失败）。Golden patch 把 `__hash__` 简化为 `hash(self.creation_counter)`——`creation_counter` 在字段创建时固定、永不改变，保证哈希不可变。

## Golden Patch 语义分析

```python
def __hash__(self):
    return hash(self.creation_counter)
```
核心语义：**字段的哈希只能依赖在对象生命周期内不变的属性**。`creation_counter` 在 `Field.__init__` 时赋值且此后不变，因此哈希恒定。任何把"赋给模型时才设置/改变的属性"（`model`、`attname`、`column` 等）纳入哈希的改动，都会让哈希在绑定模型前后变化，违反哈希不可变契约。

F2P 测试 `BasicFieldTests.test_hash_immutability`：`field = IntegerField(); h = hash(field)`，然后定义 `class MyModel: rank = field`（绑定模型），断言 `h == hash(field)`——哈希必须不变。

## 调用链分析

`Field.__hash__` 在字段被用作 dict key 或放入 set 时调用。字段绑定到模型时（`contribute_to_class`）会设置 `self.model`、`self.attname`、`self.column` 等属性——这些在绑定前不存在或为不同值。若哈希依赖其中任何一个，绑定前后哈希就会变化。`__eq__` 基于 `creation_counter`（同一字段相等），与简化后的哈希一致。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A 组（纳入 attname） |
| B | ➕ 补充 | 新增 | 原缺 B 组（纳入 column） |
| C | ➕ 补充 | 新增 | 原缺 C 组（纳入 model 对象） |
| D | 🟢 高质量 | 保留 | 条件性纳入 model._meta（D3 顺序依赖），绑定后哈希变 |
| E | 🟢 高质量 | 保留 | 纳入 `getattr(self,'model',None)`，绑定前 None、绑定后模型类 |

原仅 D、E。补齐 A、B、C，分别纳入**不同的"绑定时才设置"的属性**，机制虽同源（重新引入可变属性）但属性各异，且这正是该 bug 的本质——任何此类属性都会破坏不可变性。

## 各组 Mutation 分析

### Group A — 补充（A1 接口契约：纳入 attname）
```diff
-        return hash(self.creation_counter)
+        return hash((self.creation_counter, getattr(self, 'attname', None)))
```
**变异语义**：把 `attname` 纳入哈希。字段未绑定时 `attname` 不存在（`getattr` 返回 None），绑定到模型后 `contribute_to_class` 设置 `attname='rank'`，于是哈希从 `hash((c, None))` 变为 `hash((c, 'rank'))`，绑定前后不一致，F2P 失败。模拟"以为 attname 是字段身份的一部分"。

### Group B — 补充（B-边界：纳入 column）
```diff
-        return hash(self.creation_counter)
+        return hash((self.creation_counter, self.column if hasattr(self, 'column') else None))
```
**变异语义**：把 `column`（数据库列名）纳入哈希。绑定前无 `column`（None），绑定后被设置，哈希变化。模拟"用列名增强哈希唯一性"的边界考虑，忽略了 column 是绑定后才有的。

### Group C — 补充（C1 类型/数据形状：纳入 model 对象）
```diff
-        return hash(self.creation_counter)
+        return hash((self.creation_counter, self.model if hasattr(self, 'model') else None))
```
**变异语义**：把 `model`（模型类对象本身）纳入哈希。绑定前 `model` 不存在（None），绑定后是模型类，`hash((c, None))` ≠ `hash((c, MyModel))`，F2P 失败。模拟"把字段所属的 model 直接塞进哈希"的数据形状误解。

### Group D — 保留（D3 顺序依赖）
```diff
-        return hash(self.creation_counter)
+        if hasattr(self, 'model') and self.model is not None:
+            return hash((
+                self.creation_counter,
+                self.model._meta.app_label,
+                self.model._meta.model_name,
+            ))
+        return hash(self.creation_counter)
```
**变异语义**：当 `model` 已设置时，把 `app_label/model_name` 纳入哈希，否则只用 `creation_counter`。这制造了"绑定前后哈希不同"的顺序依赖——必须先绑定模型哈希才会变。恰好复现了 `#31750` 引入的原 bug。保留。

### Group E — 保留（E1：纳入 model getattr）
```diff
-        return hash(self.creation_counter)
+        return hash((self.creation_counter, getattr(self, 'model', None)))
```
**变异语义**：把 `getattr(self,'model',None)` 纳入哈希。绑定前 None、绑定后模型类，哈希变化。与 C 类似但用 `getattr` 单行写法、无 hasattr 分支。保留。

## 新设计 Mutation 说明

该 bug 的本质是"哈希依赖了绑定模型时才设置的属性"，因此所有有效变异都是重新引入某个此类属性。本次补齐缺失的 A、B、C，分别选用**不同的绑定后属性**：A=attname、B=column、C=model 对象，与保留的 D（model._meta 的 app_label/model_name，条件分支）、E（getattr model）形成五种不同的属性/写法。虽机制同源，但每个变异选取的"可变属性"与代码形态各异，符合该实例的内在约束。全部实测：golden 通过、变异令 F2P（`test_hash_immutability`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
