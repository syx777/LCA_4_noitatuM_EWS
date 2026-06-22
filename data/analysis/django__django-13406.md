# django__django-13406

## 问题背景

当用户对一个 `values()` 或 `values_list()` 的 queryset 的 `.query` 对象进行 pickle/unpickle，然后将其赋给一个新的 queryset 时，新 queryset 的 `_iterable_class` 仍然是默认的 `ModelIterable`（在 `QuerySet.__init__` 设置），而不是 `ValuesIterable`。导致查询结果被错误地解析为 model 实例，并因为字段列不完整而崩溃。

修复：在 `query.setter` 中检查 `value.values_select`，若非空则将 `_iterable_class` 设置为 `ValuesIterable`，确保 unpickle 后的 queryset 也能正确以 dict 形式返回结果。

## Golden Patch 语义分析

修复仅 2 行：

```python
@query.setter
def query(self, value):
    if value.values_select:            # 新增：检查 query 是否有 values 列
        self._iterable_class = ValuesIterable  # 新增：切换到正确的迭代器
    self._query = value
```

`value.values_select` 是 `sql.Query` 对象的一个属性，存储 `.values()` 调用时指定的字段元组。非空即表明这是一个 values 查询，需要返回 dict 而非 model 实例。

注意：这里选择 `ValuesIterable` 而非 `ValuesListIterable`/`FlatValuesListIterable`/`NamedValuesListIterable`，是因为 pickle 后无法区分原来是哪种 list 变体，统一退回到最基础的 dict 迭代器。

## 调用链分析

```
prices2 = Toy.objects.all()
  → QuerySet.__init__() → self._iterable_class = ModelIterable

prices2.query = pickle.loads(pickle.dumps(prices.query))
  → query.setter(value)
  → value.values_select = ('material',)  # 非空
  → self._iterable_class = ValuesIterable  ← 修复后设置
  → self._query = value

prices2[0]
  → QuerySet.__iter__()
  → ValuesIterable(self)  ← 迭代产生 dict
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 原 mutation 用 annotation_select 而非 values_select，语义错误 |
| B | 语义浅层 | 保留 | 关键判断节点 `not` 反转，直接导致 F2P 失败 |
| C | 缺失 | 新建 | len > 1 边界错误，单字段 values() 跳过修复 |
| D | 缺失 | 新建 | ValuesListIterable 替换 ValuesIterable，返回类型错误 |
| E | 必须替换 | 替换 | 设置 None 不自然，替换为 else 分支重置为 ModelIterable |

语义浅层 1 个（B），保留。A、E 必须替换，C、D 新建。

## 各组 Mutation 分析

### Group A — 替换（原：🔴 必须替换）

**原 mutation**：
```diff
-        if value.values_select:
+        if value.annotation_select:
```
**分类**：🔴 必须替换
**理由**：`annotation_select` 检查的是 annotation 是否有输出列，与 values 查询完全不相关。即使有 annotation，也不意味着需要 ValuesIterable。代码在功能上完全错误且容易被察觉。

**最终 mutation**（替换）：
```diff
-            self._iterable_class = ValuesIterable
+            self._iterable_class = ModelIterable
```
**变异语义**：发现 `values_select` 非空时，将 `_iterable_class` 设为 `ModelIterable` 而非 `ValuesIterable`。开发者可能认为应该"重置"到默认的 ModelIterable 以避免状态污染，实际上这与不设置没有区别（新建 queryset 默认已是 ModelIterable），且明确抹除了正确行为。F2P 测试中 `reloaded.get()` 返回 model 实例而非 dict，断言失败。

---

### Group B — 保留

**原 mutation**：
```diff
-        if value.values_select:
+        if not value.values_select:
```
**分类**：🟡 语义浅层（保留）
**理由**：位于修复的核心条件判断，`not` 使逻辑完全颠倒：有 values 列时不设 ValuesIterable，没有 values 列时反而设 ValuesIterable（破坏普通 queryset 赋值行为）。保留原因：修改位置精确，处于关键逻辑节点，模拟了真实的 `is None`/`is not None` 或条件方向混淆错误。

---

### Group C — 新建

**最终 mutation**：
```diff
-        if value.values_select:
+        if len(value.values_select) > 1:
```
**变异语义**：将真值检查改为 `> 1` 边界判断。`values_select` 为空元组时两者均为 False（行为相同），但单字段 `values('name')` 时 `bool(('name',))` 为 True 而 `len > 1` 为 False，所以单字段 values 查询不触发修复。F2P 测试中的 `values('name')` 测试失败，多字段情况通过。开发者可能误认为需要至少 2 个字段才有"values 查询"的语义。

---

### Group D — 新建

**最终 mutation**：
```diff
-            self._iterable_class = ValuesIterable
+            self._iterable_class = ValuesListIterable
```
**变异语义**：`ValuesListIterable` 返回元组（tuple），而 `ValuesIterable` 返回字典（dict）。F2P 测试 `test_annotation_values` 中 `reloaded.get()` 返回的是 `('test', datetime(...))` 而非 `{'name': 'test', 'latest_time': datetime(...)}`，与期望的 dict 不相等。开发者可能认为 values_list 和 values 的区别不重要，或误用了语义更接近"列表"的类。

---

### Group E — 替换（原：🔴 必须替换，设 None 不自然）

**原 mutation**：`self._iterable_class = None`（明显不自然）

**最终 mutation**：
```diff
         if value.values_select:
             self._iterable_class = ValuesIterable
+        else:
+            self._iterable_class = ModelIterable
         self._query = value
```
**变异语义**：增加 else 分支，非 values 查询时将 `_iterable_class` 重置为 `ModelIterable`。表面上看很合理（"如果不是 values 查询就用模型迭代器"），但这会强制覆盖已有 queryset 的迭代器设置。`test_annotation_values_list` 中测试了 `values_list()` queryset 的 query 被重新赋值：先前 `_iterable_class` 被 `values_list()` 设为 `ValuesListIterable`，但 `query` setter 中的 else 分支将其重置为 `ModelIterable`（因为 `values_select` 为空），导致 `reloaded.get()` 返回 model 实例而非 dict。

## 新设计 Mutation 说明

### Group A（替换原 annotation_select）
将 `ValuesIterable` 替换为 `ModelIterable`，语义正确但行为错误：当 values_select 非空时故意将 queryset 固定在 ModelIterable，与不加 if 分支的原始行为相同，不起修复效果。

### Group C（C1 边界错误）
`len(...) > 1` 是一种常见的"off-by-one"类错误，将"非空"误写为"多于一个"。对 `values('field1', 'field2')` 这类多字段情况有效，但测试用例恰好只有一个字段，导致测试失败而通常代码看起来合理。

### Group D（D1 状态初始化错误）
pickle 后重新加载时应当使用 `ValuesIterable`（dict），但赋值为 `ValuesListIterable`（tuple）。两者都是合理的"values 类迭代器"，开发者可能分不清两者的区别，认为 ValuesListIterable 更通用。
