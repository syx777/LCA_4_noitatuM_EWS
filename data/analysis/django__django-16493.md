# django__django-16493

## 问题背景

`FileField` 的 `storage` 设为一个返回 `default_storage` 的可调用对象时，反序列化（`deconstruct`）会错误地省略 `storage` 参数，而非保留对该 callable 的引用。导致重复 `makemigrations` 随机生成包含或省略 `storage=...` 的迁移。根因：`deconstruct` 用 `self.storage is not default_storage` 判断是否加 `storage` kwarg，但此时 `self.storage` 已是 callable **求值后**的结果——若 callable 返回 default_storage，则误判为 default、省略了对 callable 的引用。Golden patch 先取 `storage = getattr(self, "_storage_callable", self.storage)`（优先用未求值的 callable），再用 `storage is not default_storage` 判断、并把 `storage`（callable）存入 kwargs。

## Golden Patch 语义分析

```python
kwargs["upload_to"] = self.upload_to
storage = getattr(self, "_storage_callable", self.storage)
if storage is not default_storage:
    kwargs["storage"] = storage
```
核心语义：**判断与序列化都应基于"原始的 storage 引用"（callable，存于 `_storage_callable`），而非求值后的 `self.storage`**。`_storage_callable` 在字段初始化时保存用户传入的 callable；`getattr(self, "_storage_callable", self.storage)` 优先取它（非 callable storage 时回退到 self.storage）。这样：(1) 判断 `storage is not default_storage` 比较的是 callable 对象本身（callable ≠ default_storage 实例，恒为真）；(2) 存入 kwargs 的也是 callable，反序列化能还原引用。原 bug 用求值后的 self.storage，callable 返回 default 时丢失引用。两处（判断、赋值）都必须用 `storage` 这个变量。

F2P 测试 `FieldCallableFileStorageTests.test_deconstruction_storage_callable_default`：字段 `storage=callable_default_storage`（返回 default_storage 的 callable），断言 `deconstruct()` 的 kwargs["storage"] is callable_default_storage（callable 被保留）。

## 调用链分析

`makemigrations` → `FileField.deconstruct()` 生成迁移用的 `(name, path, args, kwargs)`。`_storage_callable` 由 `FileField.__init__` 在 storage 是 callable 时保存原始 callable（self.storage 则存其求值结果）。`getattr(self, "_storage_callable", self.storage)` 取原始引用；`is not default_storage` 判断；存入 `kwargs["storage"]`。判断用错对象（self.storage）、赋值用错对象、getattr 取错属性、或运算符反转，都会让 callable-returning-default 的字段丢失 storage 引用或错误处理。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 判断用 `self.storage`（求值后）而非 `storage`（callable），还原原 bug |
| B | ➕ 补充 | 新增 | `is not`→`is`，判断反转，只在等于 default 时才加 storage |
| C | 🔴 必须替换 | 替换 | 原 C 与 A 等价；改为赋值用 `self.storage`（判断对但存错对象） |
| D | ➕ 补充 | 新增 | getattr 取错属性名 `_storage_callable_`，回退到求值后的 self.storage |
| E | 🟢 高质量 | 保留（重做）| callable 优先取值藏到默认关闭开关后 |

原 A、C、E 字节相同（`if storage`→`if self.storage`）。保留 A，补充 B、D，重做 C、E。

## 各组 Mutation 分析

### Group A — 保留（C1 值：判断用求值后对象）
```diff
         storage = getattr(self, "_storage_callable", self.storage)
-        if storage is not default_storage:
+        if self.storage is not default_storage:
             kwargs["storage"] = storage
```
**变异语义**：判断改用 `self.storage`（callable 求值后的结果）而非 `storage`（原始 callable）。当 callable 返回 default_storage 时，`self.storage is default_storage` 为真 → 判断为假 → 不加 storage kwarg → callable 引用丢失。这正是原 bug。赋值仍用 `storage`（callable），但判断已让它进不去。保留。

### Group B — 补充（B3 逻辑反转：is not→is）
```diff
-        if storage is not default_storage:
+        if storage is default_storage:
```
**变异语义**：判断从 `is not` 反转成 `is`。只有当 storage **是** default_storage 时才加 kwarg——对正常的非 default storage（含返回 default 的 callable，因为比较的是 callable 对象，`callable is default_storage` 为假）反而不加。语义完全颠倒：该序列化的不序列化、不该的反而尝试。F2P 断言 callable 被保留失败。保留为 B。

### Group C — 替换（C1 值：赋值用求值后对象）
**原**：与 A 等价（`if storage`→`if self.storage`）。
**最终 mutation**：
```diff
         storage = getattr(self, "_storage_callable", self.storage)
         if storage is not default_storage:
-            kwargs["storage"] = storage
+            kwargs["storage"] = self.storage
```
**变异语义**：判断用 `storage`（callable，正确），但存入 kwargs 的是 `self.storage`（求值后的 storage 实例）。判断通过了（callable ≠ default），但序列化保存的是求值结果而非 callable 引用——反序列化得到的是 storage 实例而非 callable。F2P 断言 `kwargs["storage"] is callable_default_storage` 失败（实际是求值后的 default_storage）。与 A 互补：A 判断错、C 赋值错。模拟"判断对了、却存错了对象"。

### Group D — 补充（D1 状态：getattr 取错属性）
```diff
-        storage = getattr(self, "_storage_callable", self.storage)
+        storage = getattr(self, "_storage_callable_", self.storage)
```
**变异语义**：getattr 的属性名打成 `_storage_callable_`（多个尾下划线），该属性不存在 → 回退到默认 `self.storage`（求值后结果）。于是 `storage` 实际是求值后的 storage，后续判断/赋值都基于它——callable 返回 default 时丢失引用。模拟"属性名打错、静默回退到求值结果"。比 A（直接写 self.storage）隐蔽——表面用了 getattr callable，实则取错名回退。保留为 D。

### Group E — 重做（E2 隐式→显式开关）
**原**：与 A 字节相同（`if storage`→`if self.storage`）。
**最终 mutation**：
```diff
-        storage = getattr(self, "_storage_callable", self.storage)
-        if storage is not default_storage:
+        storage = getattr(self, "_storage_callable", self.storage) if getattr(self, "_deconstruct_callable_storage", False) else self.storage
+        if storage is not default_storage:
```
**变异语义**：是否优先取原始 callable 取决于开关 `_deconstruct_callable_storage`，默认 `False` → `storage = self.storage`（求值后），即原 bug 行为。只有显式开启才取 callable。模拟"把 callable 感知做成可配置、默认却关掉"。重做为 E。

## 新设计 Mutation 说明

原 A、C、E 字节完全相同（`if storage`→`if self.storage`），实际只有"判断用 self.storage"一种机制。本次保留 A（判断用求值后对象），补充 B（`is not`→`is` 逻辑反转）、D（getattr 取错属性名静默回退），重做 C（赋值用 self.storage，与 A 互补——判断对赋值错）、E（`_deconstruct_callable_storage` 默认关闭开关）。五组覆盖"判断用错对象 / 逻辑反转 / 赋值用错对象 / getattr 取错属性 / 默认关闭开关"五个角度，A 与 C 互补（判断 vs 赋值），D 比 A 更隐蔽。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P（`test_deconstruction_storage_callable_default`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
