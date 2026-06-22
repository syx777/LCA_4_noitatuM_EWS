# django__django-16315

## 问题背景

`QuerySet.bulk_create(update_conflicts=True, ...)` 在字段设了 `db_column`（且大小写与字段名不同）时生成非法 SQL。`INSERT` 用 db_column（如 `"BlacklistID"`），但 `ON CONFLICT`/`DO UPDATE SET` 子句用字段名（如 `blacklistid`），PostgreSQL 报 `column "blacklistid" does not exist`。Golden patch 双文件协同：`query.py` 在 `bulk_create` 里把 `unique_fields`/`update_fields` 从名字字符串解析成字段对象（`get_field`），`compiler.py` 的 `as_sql` 把这些字段对象转成 `f.column`（真实 db 列名）传给 `on_conflict_suffix_sql`。

## Golden Patch 语义分析

`query.py`（bulk_create）：
```python
if unique_fields:
    unique_fields = [
        self.model._meta.get_field(opts.pk.name if name == "pk" else name)
        for name in unique_fields
    ]
if update_fields:
    update_fields = [self.model._meta.get_field(name) for name in update_fields]
```
`compiler.py`（SQLInsertCompiler.as_sql）：
```python
on_conflict_suffix_sql = self.connection.ops.on_conflict_suffix_sql(
    fields, self.query.on_conflict,
    (f.column for f in self.query.update_fields),
    (f.column for f in self.query.unique_fields),
)
```
核心语义：**`unique_fields`/`update_fields` 在 bulk_create 入口被解析为字段对象，编译 ON CONFLICT 时取 `f.column`（db_column 感知的真实列名）而非字段名**。`get_field` 把名字转成 Field 对象（同时把 `_check_bulk_create_options` 里原本的转换上移）；`f.column` 返回 db_column（若设）或默认列名。两处缺一不可：query.py 不转字段对象则 compiler 拿不到 `.column`；compiler 用 `.name`/原值则仍是字段名。

F2P 测试 `BulkCreateTests.test_update_conflicts_unique_fields_update_fields_db_column`：模型字段设 `db_column="rAnK"`/`"oTheRNaMe"`，`bulk_create(update_conflicts=True, unique_fields=["rank"], update_fields=["name"])`，断言冲突更新成功（生成的 ON CONFLICT 用 db_column）。

## 调用链分析

`bulk_create` 把 `unique_fields`/`update_fields`（名字列表）解析为 Field 对象列表，传给 `_check_bulk_create_options`（校验 concrete），并最终存入 `query.update_fields`/`query.unique_fields`。`SQLInsertCompiler.as_sql` 调 `on_conflict_suffix_sql(fields, on_conflict, update_cols, unique_cols)`，其中 `update_cols`/`unique_cols` 由 `(f.column for f in ...)` 生成。`f.column` 对设了 db_column 的字段返回 db 列名。任何让 query.py 不转字段对象、或 compiler 用 `f.name`/原值/字段对象本身的改动，都会让 ON CONFLICT 用错列名 → SQL 错误。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | compiler `f.column`→`f.name`，用字段名而非 db 列名 |
| B | 🟢 高质量 | 保留 | query.py 删除 update_fields 转字段对象，compiler 取 `.column` 失败 |
| C | 🟢 高质量 | 保留 | compiler 直接传 query.update_fields（字段对象生成器→对象本身） |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 字节相同；改为 query.py unique_fields 不转字段对象 |
| E | 🔴 必须替换 | 替换 | 原 E 与 A 字节相同；改为 compiler 默认用 f.name 的开关 |

原 A、D、E 字节完全相同（compiler `f.column`→`f.name`）。保留 A，把 D、E 重做为不同机制；B、C 已各异保留。

## 各组 Mutation 分析

### Group A — 保留（C1 类型/数据形状：name 而非 column）
```diff
-            (f.column for f in self.query.update_fields),
-            (f.column for f in self.query.unique_fields),
+            (f.name for f in self.query.update_fields),
+            (f.name for f in self.query.unique_fields),
```
**变异语义**：compiler 取 `f.name`（字段名）而非 `f.column`（db 列名）。对未设 db_column 的字段二者相同（普通用例通过），设了 db_column 的字段两者不同 → ON CONFLICT 用字段名、SQL 报列不存在。还原原 bug 表现。保留。

### Group B — 保留（B2 移除 query.py 转换）
```diff
-        if update_fields:
-            update_fields = [self.model._meta.get_field(name) for name in update_fields]
         on_conflict = self._check_bulk_create_options(
```
**变异语义**：删除 `bulk_create` 里把 `update_fields` 名字转字段对象的步骤。`query.update_fields` 仍是字符串列表，compiler 里 `f.column for f in ...` 对字符串取 `.column` 抛 AttributeError（str 无 column）。modeling "漏了一处入口转换"。保留。

### Group C — 保留（C1 类型/数据形状：传对象而非列名）
```diff
-            (f.column for f in self.query.update_fields),
-            (f.column for f in self.query.unique_fields),
+            (self.query.update_fields),
+            (self.query.unique_fields),
```
**变异语义**：compiler 直接把 `query.update_fields`（Field 对象列表）整体传给 `on_conflict_suffix_sql`，不再用生成器取 `.column`。下游期望列名字符串，却收到 Field 对象列表 → 拼 SQL 时把对象 repr 当列名或类型出错。模拟"忘了提取 .column、把字段对象原样传下去"。保留。

### Group D — 替换（D1 状态：unique_fields 不转字段对象）
**原**：与 A 字节相同（compiler `f.column`→`f.name`）。
**最终 mutation**：
```diff
             unique_fields = [
-                self.model._meta.get_field(opts.pk.name if name == "pk" else name)
+                opts.pk.name if name == "pk" else name
                 for name in unique_fields
             ]
```
**变异语义**：query.py 里 `unique_fields` 只做 `pk`→pk 名字的归一，但**不调 `get_field` 转字段对象**，仍是字符串列表。compiler 里 `f.column for f in query.unique_fields` 对字符串取 `.column` 抛 AttributeError。与 B 对称（B 漏 update_fields、D 漏 unique_fields），但 D 更隐蔽——保留了 `pk` 归一这一半逻辑，看起来"处理过了"，实则漏了 `get_field` 这关键一步。模拟"列表推导里漏套了 get_field"。

### Group E — 替换（E2 隐式→显式开关）
**原**：与 A 字节相同（compiler `f.column`→`f.name`）。
**最终 mutation**：
```diff
-            (f.column for f in self.query.update_fields),
-            (f.column for f in self.query.unique_fields),
+            (f.column if getattr(self, "use_db_columns", False) else f.name for f in self.query.update_fields),
+            (f.column if getattr(self, "use_db_columns", False) else f.name for f in self.query.unique_fields),
```
**变异语义**：compiler 里是否用 `f.column` 取决于编译器属性开关 `use_db_columns`，默认 `False` → 用 `f.name`（旧 bug 行为）。只有显式设 True 才用 db 列名。模拟"把 db_column 感知做成可配置、默认却关掉"。与 A（无条件 `f.name`）不同——E 保留了正确分支但默认走错。

## 新设计 Mutation 说明

原 A、D、E 字节完全相同（compiler `f.column`→`f.name`），B、C 已是不同机制。本次保留 A（compiler 用 name）、B（query.py 漏转 update_fields）、C（compiler 传字段对象本身），把与 A 重复的 D 重做为"query.py 漏转 unique_fields（保留 pk 归一但漏 get_field）"、E 重做为"compiler 默认用 f.name 的 use_db_columns 开关"。五组分布在 `query.py`（B/D）与 `compiler.py`（A/C/E）两文件、覆盖"name 替列名 / 漏转 update_fields / 传字段对象 / 漏转 unique_fields / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
