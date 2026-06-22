# django__django-15161

## 问题背景

为简化生成的迁移代码，表达式类的 `deconstruct()` 应使用简化的导入路径（如 `django.db.models.F()` 而非 `django.db.models.expressions.F()`）。`F` 已用 `@deconstructible(path='django.db.models.F')` 处理过。Golden patch 把同样的技巧应用到 `Func`、`Value`、`ExpressionWrapper`、`When`、`Case`、`OrderBy`，为每个类加上 `@deconstructible(path='django.db.models.X')` 装饰器。

## Golden Patch 语义分析

```python
@deconstructible(path='django.db.models.Func')
class Func(...): ...
@deconstructible(path='django.db.models.Value')
class Value(...): ...
# 同样为 ExpressionWrapper / When / Case / OrderBy 添加
```
核心语义：`@deconstructible(path=...)` 让该类的 `deconstruct()` 返回**指定的简化路径**。`deconstructible` 还会**校验该路径确实能解析到对象**（否则在序列化时抛 `ValueError: Could not find object X in ...`）。修复保证：(1) `Value().deconstruct()[0] == 'django.db.models.Value'`；(2) `MigrationWriter` 序列化复杂索引时所有这些表达式都用 `models.X(...)` 简化形式并只 import `from django.db import models`。

F2P 测试：`ValueTests.test_deconstruct` / `test_deconstruct_output_field`（Value 路径为 `django.db.models.Value`）、`WriterTests.test_serialize_complex_func_index`（序列化含 Func/Case/When/Value/ExpressionWrapper/OrderBy 的 Index，期望全部简化路径且 imports 仅 `from django.db import models`）。

## 调用链分析

`@deconstructible(path=P)` 包装类的 `deconstruct()`，使其首元素返回 `P`。`MigrationWriter.serialize` 递归序列化表达式树，对每个节点取其 deconstruct 路径并收集 import。若某节点路径未简化（仍是 `...expressions.X`）或装饰器缺失（回退到默认完整路径），writer 输出字符串与 import 集合都会偏离断言。若路径字符串指向不存在的对象，`deconstructible` 在序列化时抛 `ValueError`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 | 替换 | 原把 Func 路径改回 `...expressions.Func`，是合理的"未简化"变体，保留思路但移到 Value（直接命中 ValueTests） |
| B | 🟢 | 替换 | 原删 OrderBy 装饰器；保留"删装饰器"思路但移到 Func（writer 测试覆盖） |
| C | 🔴 必须替换 | 替换 | 原把 F 的 `@deconstructible(path=...)`→裸 `@deconstructible`，但 F 不在本 patch 范围（golden 未改 F），属改错对象 |
| D | 🔴 必须替换 | 替换 | 原注释掉 `self.value = value`，导致 `Value.__repr__`/序列化 AttributeError，崩溃且与 deconstruct 路径无关 |
| E | 🟢 | 替换 | 原删 Value 装饰器；与本组其它项配合，移到 ExpressionWrapper 以分散修改点 |

为获得"每组改不同表达式类 + 不同机制"的清晰矩阵，五组分别落在 Value/Func/When/Case/ExpressionWrapper，机制为"错误模块路径 / 删装饰器 / 路径 typo"。

## 各组 Mutation 分析

### Group A — 替换（A1 路径语义：错误模块路径，Value）
```diff
-@deconstructible(path='django.db.models.Value')
+@deconstructible(path='django.db.models.expressions.Value')
```
**变异语义**：把 Value 的简化路径改回完整模块路径 `django.db.models.expressions.Value`。`Value().deconstruct()[0]` 变成未简化路径，直接令 `ValueTests.test_deconstruct`（断言 `== 'django.db.models.Value'`）失败，writer 测试的 Value 片段与 import 也偏离。模拟"复制 F 的简化技巧时漏改、沿用旧完整路径"。路径仍能解析（对象存在），不报错，只是没简化——隐蔽。

### Group B — 替换（D-删装饰器，Func）
```diff
-@deconstructible(path='django.db.models.Func')
 class Func(SQLiteNumericMixin, Expression):
```
**变异语义**：删掉 Func 的 `@deconstructible(path=...)` 装饰器。Func 的 `deconstruct()` 回退到默认（完整 `...expressions.Func`）路径，`test_serialize_complex_func_index` 中 Func 片段不再是简化的 `models.Func(...)`，断言失败。模拟"漏给某个类加装饰器"。

### Group C — 替换（路径 typo，When）
```diff
-@deconstructible(path='django.db.models.When')
+@deconstructible(path='django.db.models.Whenn')
```
**变异语义**：When 的路径字符串拼错成 `Whenn`。`deconstructible` 在序列化 When 时校验该路径，发现 `django.db.models` 下没有 `Whenn`，抛 `ValueError: Could not find object Whenn in django.db.models.`，`test_serialize_complex_func_index` 失败。模拟手敲路径字符串时的 typo。与 A/D 的"错模块路径"不同——这里是不可解析的拼写错误，且改的是 When。

### Group D — 替换（A1 错误模块路径，Case）
```diff
-@deconstructible(path='django.db.models.Case')
+@deconstructible(path='django.db.models.expressions.Case')
```
**变异语义**：把 Case 的简化路径改回完整模块路径。writer 序列化复杂索引时 Case 片段变成 `django.db.models.expressions.Case(...)`（或破坏 import 集合），断言失败。与 A 同类机制（错模块路径）但作用于不同类 Case，且替换掉原来"注释 `self.value=value`"那种崩溃式、与路径无关的变异。

### Group E — 替换（D-删装饰器，ExpressionWrapper）
```diff
-@deconstructible(path='django.db.models.ExpressionWrapper')
 class ExpressionWrapper(SQLiteNumericMixin, Expression):
```
**变异语义**：删掉 ExpressionWrapper 的装饰器，其 `deconstruct()` 回退到完整路径，`test_serialize_complex_func_index` 中 ExpressionWrapper 片段不再简化，断言失败。与 B 同类机制（删装饰器）但作用于不同类，分散修改点避免与 B 重叠。

## 新设计 Mutation 说明

五组构成清晰的"类 × 机制"矩阵：A=Value(错模块路径)、B=Func(删装饰器)、C=When(路径 typo→ValueError)、D=Case(错模块路径)、E=ExpressionWrapper(删装饰器)。每组改一个**不同的表达式类**，避免集中；机制有三种（错模块路径 / 删装饰器 / 路径 typo），其中 C 的不可解析路径产生 ValueError、其余产生断言不符。替换掉原来改错对象（C 改 F）、崩溃式且与路径无关（D 注释 self.value）的低质量变异。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
