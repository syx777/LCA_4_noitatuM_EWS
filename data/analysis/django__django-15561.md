# django__django-15561

## 问题背景

在 SQLite 上，仅给字段添加/修改 `choices` 这种"对数据库透明"的变化也会生成 SQL（建新表+插入+删表+改名），而 postgres 等不会。Golden patch 把"不影响列定义的属性"集中到 `Field.non_db_attrs` 元组（新增 `choices`），并让 `_field_should_be_altered` 在比较新旧字段时对 `old_field`/`new_field` 各自 pop 掉这些属性，从而 choices 变化被识别为 noop。

## Golden Patch 语义分析

两部分：
1. `Field.non_db_attrs` 类属性元组，列出不影响列定义的属性（含新增的 `"choices"`）。
2. `_field_should_be_altered` 中：
```python
for attr in old_field.non_db_attrs:
    old_kwargs.pop(attr, None)
for attr in new_field.non_db_attrs:
    new_kwargs.pop(attr, None)
return ... or (old_path, old_args, old_kwargs) != (new_path, new_args, new_kwargs)
```
核心语义：**比较新旧字段是否需要 ALTER 前，先从两边的 kwargs 中剔除 non_db_attrs（含 choices），使仅 choices 不同的字段被判为无需 alter**。两个 pop 循环（old 和 new）都必须执行，且 `choices` 必须在 non_db_attrs 中。

F2P 测试 `SchemaTests.test_alter_field_choices_noop`：给字段加 choices 后 `alter_field`，断言 `assertNumQueries(0)`（双向都不产生 SQL）。

## 调用链分析

`alter_field` → `_field_should_be_altered(old, new)`：deconstruct 两字段取 kwargs，按 `non_db_attrs` pop 后比较。若 choices 未被剔除（不在元组、或 pop 循环缺失/失效），则 old_kwargs/new_kwargs 的 choices 不同 → 判定需 alter → 生成 SQL，`assertNumQueries(0)` 失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 从 non_db_attrs 元组删除 `"choices"` 行，choices 不再被剔除 |
| B | 🟢 高质量 | 保留 | 删除 `new_field.non_db_attrs` pop 循环，新字段 choices 残留 |
| C | 🟢 高质量 | 保留 | 删除两个 pop 循环，所有 non_db_attrs 都不剔除 |
| D | 🔴 必须替换 | 替换 | 原 D≈A（把 choices 行替成空行，等价删除）；改为 typo `"choice"` |
| E | 🟢 高质量 | 保留 | 把 pop 逻辑藏到 `ignore_non_db_attrs` 开关后，默认 False |

原 A 与 D 都让 `choices` 从元组消失（A 删行、D 留空行），近似重复。重做 D 为字符串 typo。

## 各组 Mutation 分析

### Group A — 保留（B2 移除 case：删 choices 条目）
```diff
     non_db_attrs = (
         "blank",
-        "choices",
         "db_column",
```
**变异语义**：从 `non_db_attrs` 元组删掉 `"choices"`。比较时 choices 不被 pop，新旧字段 kwargs 的 choices 不同 → 判定需 alter → 生成 SQL。直接还原原 bug（choices 不是 non-db 属性）。保留。

### Group B — 保留（B2 移除 case：删 new 循环）
```diff
         for attr in old_field.non_db_attrs:
             old_kwargs.pop(attr, None)
-        for attr in new_field.non_db_attrs:
-            new_kwargs.pop(attr, None)
```
**变异语义**：删掉对 `new_field` 的 pop 循环。old_kwargs 剔除了 choices 但 new_kwargs 没有，两者 choices 字段不对称 → 比较不等 → 需 alter。模拟"只处理了旧字段、漏了新字段"的不对称遗漏。保留。

### Group C — 保留（D-移除两循环）
```diff
-        for attr in old_field.non_db_attrs:
-            old_kwargs.pop(attr, None)
-        for attr in new_field.non_db_attrs:
-            new_kwargs.pop(attr, None)
```
**变异语义**：删掉两个 pop 循环，所有 non_db_attrs（含 choices）都不被剔除。任何 non-db 属性变化都被判为需 alter。最彻底地撤销修复。保留。

### Group D — 替换（C1 类型/数据形状：choices typo）
**原**：把 `"choices",` 替换成空行（等价于 A 的删除）。
**最终 mutation**：
```diff
     non_db_attrs = (
         "blank",
-        "choices",
+        "choice",
         "db_column",
```
**变异语义**：把元组里的 `"choices"` 拼成 `"choice"`（少了 s）。`for attr in non_db_attrs` 时 `kwargs.pop("choice", None)` 永远 pop 不到真正的 `choices` kwarg，故 choices 残留参与比较 → 需 alter。模拟"属性名字符串拼错"，比 A 的整行删除更隐蔽（元组看起来仍有一项处理 choices）。

### Group E — 保留（E2 隐式→显式参数）
```diff
-    def _field_should_be_altered(self, old_field, new_field):
+    def _field_should_be_altered(self, old_field, new_field, ignore_non_db_attrs=False):
         ...
-        for attr in old_field.non_db_attrs:
-            old_kwargs.pop(attr, None)
-        for attr in new_field.non_db_attrs:
-            new_kwargs.pop(attr, None)
+        if ignore_non_db_attrs:
+            for attr in old_field.non_db_attrs:
+                old_kwargs.pop(attr, None)
+            for attr in new_field.non_db_attrs:
+                new_kwargs.pop(attr, None)
```
**变异语义**：把 pop 逻辑藏到 `ignore_non_db_attrs` 参数后，默认 False。默认情况下不剔除任何 non_db_attrs，choices 变化触发 alter。模拟"把行为做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原 A、D 都让 `choices` 从元组生效中消失（A 删行、D 留空行），属近似重复。本次保留 A（删 choices 条目）、B（删 new 循环）、C（删两循环）、E（默认关闭开关），把重复的 D 改为字符串 typo `"choice"`（pop 永不命中）。五组覆盖"删元组项 / 删 new 循环 / 删两循环 / 字符串 typo / 默认关闭开关"五个角度，跨 `fields/__init__.py` 与 `schema.py` 两个文件。全部实测：golden 通过、变异令 F2P（`test_alter_field_choices_noop`）失败、`base→golden→test_patch` 后干净应用。
