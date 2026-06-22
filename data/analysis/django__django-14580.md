# django__django-14580 Mutation 分析

## 问题背景

Django 迁移序列化器 `TypeSerializer` 在序列化 `models.Model` 这个类型时，
返回了正确的字符串字面量 `"models.Model"`，但 **遗漏了对应的 import 语句**。
这导致自动生成的迁移文件中出现了未导入的 `models.Model` 引用，迁移无法运行。

## Golden Patch 语义分析

```python
special_cases = [
-   (models.Model, "models.Model", []),
+   (models.Model, "models.Model", ['from django.db import models']),
    (type(None), 'type(None)', []),
]
```

修复仅是把 `models.Model` 这个特例的 imports 列表从空列表改为
`['from django.db import models']`。`serialize()` 遍历 `special_cases`，命中后
`return string, set(imports)`，即返回 `("models.Model", {"from django.db import models"})`。

## 调用链分析

`MigrationWriter.serialize(value)` → `serializer_factory(value).serialize()` →
对类型对象命中 `TypeSerializer`。F2P 测试 `test_serialize_type_model`：

1. `assertSerializedEqual(models.Model)` —— 往返序列化相等；
2. `assertSerializedResultEqual(..., ("('models.Model', {'from django.db import models'})", set()))`
   —— 断言序列化后的字符串字面量 **以及** import 集合都精确匹配。

任何破坏「字符串标签」或「import 集合」的改动都会被第 2 条精确断言捕获。

## 替换决策总览

| 槽位 | 原策略 | 分类 | 决策 | 新 strategy_code | 失败断言维度 |
|------|--------|------|------|------------------|--------------|
| A | A | 🟢 语义守卫，独立分支 | 保留 | A2 | imports 集合（走通用分支） |
| B | B | 🔴 与 golden 字节级反转 | 替换 | C3 | imports 文本内容 |
| C | C | 🟢 类型/数据形态改动 | 保留 | C1 | 返回值类型 set→list |
| D | D | 🟢 多行、幂等性破坏 | 保留 | D2 | 重复调用 imports 丢失 |
| E | E | 🔴 与 B 字节级重复反转 | 替换 | C3 | 字符串标签内容 |

原始 5 个槽位中 B 与 E 是 **字节完全相同** 的 golden 直接反转（imports 改回 `[]`），
属于 🔴 必须替换；A/C/D 各自触碰不同语义维度，保留。

## 各组 Mutation 分析

- **A（保留，A2）**：`if case is self.value and case is not models.Model`。
  增加一个看似无害的守卫，使 `models.Model` 不命中特例而落入通用
  `__module__` 分支，产生错误 import（`import django.db.models.base` 类）。失败维度=imports。
- **B（替换）**：原为 golden 反转，与 E 字节重复，已替换。
- **C（保留，C1）**：`return string, imports` 去掉 `set()` 包装，返回 list 而非 set，
  类型形态错误，仅被精确结果断言捕获。
- **D（保留，D2）**：引入类级 `_imports_cache`，首次序列化才发出 import，
  之后返回空 set，破坏幂等性——自然但隐蔽的「优化」错误。
- **E（替换）**：原为 golden 反转，与 B 重复，已替换。

## 新设计 Mutation 说明

- **新 B（C3，imports 文本错误）**：把 import 写成
  `['from django.db.models import Model']`。这是一个看似完全合理的等价写法，
  但与期望的 `from django.db import models` 不一致，命中 import 集合断言。
  与 A（集合走通用分支）、C（类型形态）正交。
- **新 E（C3，字符串标签错误）**：把标签改为真实但错误的模块路径
  `"models.base.Model"`（`models.Model` 的实际定义位置），像一次重构笔误，
  通过 `assertSerializedResultEqual` 的字面量比较被捕获。失败维度=字符串标签，
  与新 B（import 文本）正交。

所有 5 个 mutation 均经真实运行 `migrations.test_writer` 验证：py_compile 通过，
F2P 测试 `test_serialize_type_model` 各产生恰好 1 个 FAILURE，其余 49 个 P2P 测试通过。
