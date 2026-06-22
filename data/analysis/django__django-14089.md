# django__django-14089 Mutation 分析

## 问题背景

`OrderedSet` 当前不支持 `reversed()`。Issue 要求新增 `__reversed__()` 方法，使其可以被
Python 内置 `reversed()` 调用，并保持插入顺序的逆序。

## Golden Patch 语义分析

```python
def __reversed__(self):
    return reversed(self.dict)
```

`self.dict` 是一个 `dict.fromkeys(iterable)` 构造的有序字典，键即元素、值为 `None`。
`reversed(self.dict)` 返回一个对**键**逆序的 `dict_reversekeyiterator`（属于
`collections.abc.Iterator`）。Golden 的语义有两个不可分割的契约点：

1. 返回值必须是 **Iterator**（惰性迭代器，而非 list）；
2. 迭代产出的内容必须是**键的逆序**（`[3,2,1]`）。

## 调用链分析

`reversed(OrderedSet([1,2,3]))` → 触发 `OrderedSet.__reversed__` → `reversed(self.dict)`
→ 返回 dict 键逆序迭代器。F2P 测试 `OrderedSetTests.test_reversed`:

```python
s = reversed(OrderedSet([1, 2, 3]))
self.assertIsInstance(s, collections.abc.Iterator)   # 契约点1: 类型
self.assertEqual(list(s), [3, 2, 1])                 # 契约点2: 内容/顺序
```

两条断言分别守护两个契约点，构成可被不同失败模式触发的双重边界。

## 替换决策总览

| 组 | 原 diff 语义 | 分类 | 决策 | 最终语义 |
|----|------------|------|------|---------|
| A | `iter(self.dict)`（正向） | 🟡 SHALLOW | 保留 | 正向迭代器，破坏顺序契约 |
| B | `if self.dict: return None` | 🔴 不自然伪影 | 替换 | reverse `.values()` 数据源混淆 |
| C | `list(reversed())` + “# Bug” 注释 | 🔴 伪影注释 | 替换 | range off-by-one 丢首元素 |
| D | `iter(self.dict)`（与 A 完全重复） | 🔴 直接冗余 | 替换 | 切片去头后逆序，丢最老元素 |
| E | `list(reversed(self.dict))` | 🟢 KEEP | 保留 | 返回 list 破坏 Iterator 契约 |

Shallow 数 M=1（仅 A），floor(1/2)=0，不替换任何 shallow；A 落在关键控制流
（建模真实“从 `__iter__` 复制粘贴忘改 reversed”的错误），保留。共替换 3 个 🔴（B/C/D）。

## 各组 Mutation 分析

### Group A（保留 🟡）
- 原 diff: `return reversed(self.dict)` → `return iter(self.dict)`
- 分类: SHALLOW 单 token 交换；但建模真实复制错误，保留为关键控制流。
- 变异语义: 返回正向迭代器，仍是 Iterator（契约点1通过），仅顺序断言失败。

### Group B（替换 🔴 → B1）
- 原 diff: 插入 `if self.dict: return None` —— 返回 `None` 是不可能通过任何正常调用的
  不自然伪影，且语句几乎对所有非空集合短路。
- 最终 diff: `return reversed(self.dict.values())`
- 变异语义: **接口/数据源混淆**——逆序遍历 `.values()`（全为 `None`）而非键。返回真正的
  Iterator（契约点1通过），但内容为 `[None,None,None]`，仅内容断言失败。

### Group C（替换 🔴 → C1）
- 原 diff: `return list(reversed(self.dict))` 并带 `# Bug: ...` 注释 —— 注释暴露意图，
  属伪影；且与 E 功能重复。
- 最终 diff:
  ```python
  keys = list(self.dict)
  return (keys[i] for i in range(len(keys) - 1, 0, -1))
  ```
- 变异语义: **off-by-one 边界**——`range` 终点应为 `-1` 却写成 `0`，静默丢弃最先插入的
  元素。返回正常生成器（Iterator，契约点1通过），内容 `[3,2]` 仅内容断言失败。

### Group D（替换 🔴 → D1）
- 原 diff: `return iter(self.dict)` —— 与 A 完全字节级重复，直接冗余。
- 最终 diff: `return reversed(list(self.dict)[1:])`
- 变异语义: **切片去头**——逆序前先 `[1:]` 砍掉最老元素，得到 `[3,2]`。与 C 同样丢一个
  元素但位置/机制不同（切片 vs range），与 B 数据源不同，保持失败模式正交。

### Group E（保留 🟢）
- 原 diff: `return list(reversed(self.dict))`
- 分类: 改变返回类型契约（list 而非惰性 Iterator）。
- 变异语义: 内容正确 `[3,2,1]`（内容断言通过），仅 `isinstance ... Iterator` 契约断言失败。
  与 A/B/C/D 正交（类型失败 vs 顺序/内容失败）。

## 新设计 Mutation 说明

B1/C1/D1 三者均刻意保持“返回值是 Iterator”这一契约点为真（绕过 isinstance 断言），
仅在内容/顺序上出错，使得只验证“可逆/类型”的 LLM 测试无法察觉，必须有完整内容相等断言。
三者数据偏差来源各异——B1 取错数据源（values），C1 range 边界 off-by-one，
D1 切片丢首元素——形成正交失败模式；E1 则反向只破坏类型契约。整体覆盖 Golden 双契约点的
全部失败维度，且无任何注释/`return None` 之类的人工伪影。

## 验证结果（REAL）

harness: base 仓库 → 应用 golden patch + test patch（均 rc=0）→ commit。

- 基线（golden 无变异）: `OrderedSetTests.test_reversed` PASS；全模块 44 tests OK。
- A1/B1/C1/D1/E1: 每个 `git apply` 成功、`py_compile` 通过；
  F2P `test_reversed` 均 **FAILED**；全模块 44 tests 中**仅 1 失败**（即 F2P），43 个 P2P 全通过。
- 模块: `utils_tests.test_datastructures`，类 `OrderedSetTests`，方法 `test_reversed`。
