# django__django-15525

## 问题背景

`loaddata` 在非默认数据库上加载"自然键依赖外键"的 fixture 时失败。当 natural_key 通过外键计算（如 `Book.natural_key` 调 `self.author.natural_key()`）时，`build_instance` 构造的临时对象其 `_state.db` 未设为目标库，导致跟随 FK 的查询打到了默认库（无数据）而报错。Golden patch 在调用 `natural_key()` 前把 `obj._state.db = db`，使 FK 跟随查询走正确的数据库。

## Golden Patch 语义分析

```python
obj = Model(**data)
obj._state.db = db
natural_key = obj.natural_key()
```
核心语义：**临时构造的实例必须先绑定到目标数据库 `db`，再计算 natural_key**。因为 `natural_key()` 可能跟随外键（`self.author`）触发数据库查询，`_state.db` 决定该查询走哪个库。不设或设错，FK 查询会落到默认库，找不到依赖对象而失败。

F2P 测试 `NaturalKeyFixtureOnOtherDatabaseTests.test_natural_key_dependencies`：在 `other` 库 loaddata 含 FK 自然键依赖的 fixture，断言对象及其 author 正确加载。

## 调用链分析

`build_instance(Model, data, db)` 在反序列化时构造无 pk 的对象，计算其 natural_key 以查已存在记录。`obj.natural_key()` → `self.author.natural_key()` → 跟随 FK 触发 `author` 的查询，该查询用 `obj._state.db`。`default_manager.db_manager(db).get_by_natural_key(*natural_key)` 在目标库查。若 `_state.db` 不是 `db`，FK 查询落错库。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `obj._state.db = db`→`None`，FK 跟随查询落到默认库 |
| B | ➕ 补充 | 新增 | 原缺 B 组（硬编码 `'default'`） |
| C | 🟢 高质量 | 保留 | 删除赋值行，`_state.db` 保持默认 |
| D | 🟢 高质量 | 保留 | 注释掉赋值行，等效未设置 |
| E | 🟢 高质量 | 保留 | 把赋值藏到 `use_db_for_natural_key` 开关后，默认 False |

原缺 B，补充硬编码错误库的边界变异。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：设为 None）
```diff
-        obj._state.db = db
+        obj._state.db = None
```
**变异语义**：把目标库 `db` 设为 `None`。`_state.db = None` 时 FK 跟随查询用默认路由（通常默认库），在 `other` 库 loaddata 场景下查不到依赖对象，natural_key 计算/查询失败。模拟"把变量误设为 None"。保留。

### Group B — 补充（B2 边界：硬编码默认库）
```diff
-        obj._state.db = db
+        obj._state.db = 'default'
```
**变异语义**：硬编码 `'default'` 而非用传入的 `db`。在默认库场景碰巧正确，但在 `other` 库 loaddata 时 FK 查询落到 `default`（无对应数据）→ 失败。模拟"以为总是默认库、硬编码库别名"的边界假设——典型值（default DB）测试通过，非默认库才暴露。

### Group C — 保留（B2 移除 case）
```diff
         obj = Model(**data)
-        obj._state.db = db
         natural_key = obj.natural_key()
```
**变异语义**：删掉赋值行，`obj._state.db` 保持构造默认（None/默认路由）。FK 跟随查询落错库。直接还原原 bug。保留。

### Group D — 保留（D-注释）
```diff
-        obj._state.db = db
+#         obj._state.db = db
```
**变异语义**：注释掉赋值行，效果同 C（未设置目标库），但形式是被注释的死代码。模拟"临时注释调试、忘了恢复"。保留。

### Group E — 保留（E2 隐式→显式参数）
```diff
-def build_instance(Model, data, db):
+def build_instance(Model, data, db, use_db_for_natural_key=False):
     ...
         obj = Model(**data)
-        obj._state.db = db
+        if use_db_for_natural_key:
+            obj._state.db = db
```
**变异语义**：把"绑定目标库"藏到 `use_db_for_natural_key` 参数后，默认 False。调用方不传 → 默认不绑定 → FK 查询落错库。模拟"把修复做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原缺 B 组。补充 B（硬编码 `'default'`）——这是与 A（None）、C（删除）、D（注释）、E（开关）不同的"错误库别名"边界变异，在默认库场景碰巧通过、仅非默认库暴露，最贴合本 issue 的多库特性。五组覆盖"设 None / 硬编码默认库 / 删除 / 注释 / 默认关闭开关"五个角度。

注：该 F2P 测试 `databases={"other"}` 单独运行会触发测试运行器的 "Circular dependency in TEST[DEPENDENCIES]"，验证时与一个默认库测试类一同运行以满足 DB 初始化顺序。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用。
