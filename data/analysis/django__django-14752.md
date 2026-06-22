# django__django-14752 Mutation 分析

## 问题背景
`AutocompleteJsonView.get()` 内部把每个结果对象硬编码序列化为 `{'id': str(getattr(obj, to_field_name)), 'text': str(obj)}`。第三方无法在不复制整个 `get()` 方法的情况下，向自动补全结果中添加额外字段。Issue 要求把序列化逻辑抽取成一个可重写的钩子方法，方便扩展。

## Golden Patch 语义分析
Golden patch 是一次纯粹的"提取方法"重构：

- 新增 `serialize_result(self, obj, to_field_name)`，返回 `{'id': str(getattr(obj, to_field_name)), 'text': str(obj)}`。
- `get()` 中的列表推导由内联字典改为调用 `self.serialize_result(obj, to_field_name)`。

默认输出与重构前完全一致（字节级相同），唯一新增的能力是：子类可重写 `serialize_result` 来增删字段。

## 调用链分析
`get()` → 列表推导 → `self.serialize_result(obj, to_field_name)` → 返回 dict → 组装进 `JsonResponse({'results': [...]})`。

- P2P 测试（`test_success`、`test_custom_to_field`、`test_get_paginator` 等）断言默认输出严格等于 `{'id': ..., 'text': ...}`，因此任何改变默认 payload 的改动都会破坏 P2P。
- F2P 测试 `test_serialize_result` 定义子类，重写 `serialize_result` 调用 `super()` 并追加 `'posted'` 键，断言响应中包含该额外键。

因此一个合格的 F2P-only mutation 必须：保持默认 `{id,text}` 输出不变（P2P 通过），但使子类重写新增的额外键丢失或重写不生效（F2P 失败）。突变点集中在 `get()` 的分派逻辑上。

## 替换决策总览

| 槽位 | 原策略组 | 原始状态 | 判定 | 新策略码 | F2P 失败测试 |
|------|----------|----------|------|----------|--------------|
| A | A | `serialize_result` 参数顺序互换 | 🔴 必须替换（破坏全部 P2P，10 errors）| A2 | test_serialize_result |
| B | B | 去掉 `id` 的 `str()` 包装 | 🔴 必须替换（破坏 P2P，7 failures；与 C 字节相同）| B2 | test_serialize_result |
| C | C | 与 B 字节完全相同的重复 diff | 🔴 必须替换（重复 + 破坏 P2P）| C1 | test_serialize_result |
| E | E | 新增 `include_text` 默认丢弃 text | 🔴 必须替换（破坏 P2P，9 failures）| E1 | test_serialize_result |

## 各组 Mutation 分析（原始）
- **A（参数互换 `to_field_name, obj`）**：`getattr(obj, to_field_name)` 变成对整数取属性，触发 `TypeError`，10 个用例报错。直接破坏所有 P2P，无效。
- **B（去掉 `str()`）**：`'id'` 变成原始整数/UUID，破坏所有断言 `str(q.pk)` 的 P2P 测试。
- **C**：与 B 的 diff 字节级完全相同——典型的生成器重复输出，必须替换。
- **E（新增 `include_text=False` 默认丢弃 text）**：默认输出缺少 `'text'` 键，破坏 9 个 P2P。

四个原始突变全部为无效突变（破坏 P2P），且 B/C 重复，全部替换。

## 新设计 Mutation 说明
所有新突变都作用于 `get()` 的分派表达式，保持默认 payload 字节级不变（P2P 全通过），仅让 `serialize_result` 重写新增的额外键丢失，从而精确失败 `test_serialize_result`。四者机制正交：

- **槽位 A（A2 签名/分派）**：`self.serialize_result(...)` 改为 `AutocompleteJsonView.serialize_result(self, ...)`。显式类限定调用绕过 Python 的动态分派，子类重写被静默忽略。这是一个看似"更明确"的写法，极难被静态审查发现。
- **槽位 B（B2 移除处理）**：用字典推导 `{k: v ... if k in ('id','text')}` 对钩子返回值做键白名单过滤，丢弃任何额外键。看起来像"防御性过滤"，但破坏了扩展契约。
- **槽位 C（C1 类型/数据形状）**：把结果逐字段重建 `{'id': ...['id'], 'text': ...['text']}`，只搬运已知字段，额外键被丢弃。像是"显式构造"，对默认情形完全等价。
- **槽位 E（E1 期望）**：只从钩子结果取 `id`，`text` 在本地用 `str(obj)` 重新计算，钩子新增的键全部丢失。看似无害的"局部优化"。

四种失败模式（绕过多态 / 白名单过滤 / 逐字段重建 / 局部重算）互相正交，最大化了检测难度的多样性。
