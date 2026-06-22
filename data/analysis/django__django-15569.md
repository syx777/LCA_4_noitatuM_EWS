# django__django-15569

## 问题背景

`RegisterLookupMixin._unregister_lookup()` 注销 lookup 时没有清除 lookup 缓存（而 `register_lookup` 注册时会清）。导致已注销的 lookup 仍残留在 `get_lookups` 的缓存中。Golden patch 在 `_unregister_lookup` 末尾加 `cls._clear_cached_lookups()`，与 `register_lookup` 对称。

## Golden Patch 语义分析

```python
@classmethod
def _unregister_lookup(cls, lookup, lookup_name=None):
    if lookup_name is None:
        lookup_name = lookup.lookup_name
    del cls.class_lookups[lookup_name]
    cls._clear_cached_lookups()
```
核心语义：**注销 lookup 后必须清缓存，使 `get_lookups()`（带 `functools.lru_cache`）重新计算、不再返回已删 lookup**。`_clear_cached_lookups` 遍历子类调 `get_lookups.cache_clear()`。注册/注销必须对称地清缓存。

F2P 测试 `LookupTests.test_lookups_caching`：注册后缓存命中，注销后断言 `assertNotIn("exactly", field.get_lookups())`——即注销必须使缓存失效。

## 调用链分析

`_unregister_lookup` → `del cls.class_lookups[name]` → `cls._clear_cached_lookups()`（清各子类 `get_lookups` 缓存）。`get_lookups` 用 lru_cache，不清则返回旧结果（仍含已删 lookup）。测试在 `register_lookup` 上下文退出（触发 unregister）后检查缓存是否被清。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 删除 `cls._clear_cached_lookups()` 行 |
| B | 🟢 高质量 | 保留 | 注释掉该行 |
| C | ➕ 补充 | 新增 | 原缺 C 组（调用错对象 `lookup._clear_cached_lookups()`） |
| D | 🔴 必须替换 | 替换 | 原 D=A（删行）；改为仅在 lookup_name 显式传入时才清 |
| E | ➕ 补充 | 新增 | 原缺 E 组（清缓存藏到开关后） |

原 A=D 重复（删行）。补 C、E，重做 D。

## 各组 Mutation 分析

### Group A — 保留（B2 移除调用）
```diff
         del cls.class_lookups[lookup_name]
-        cls._clear_cached_lookups()
```
**变异语义**：删除清缓存调用。注销后 `get_lookups` 缓存仍含已删 lookup，`assertNotIn` 失败。直接还原原 bug。保留。

### Group B — 保留（D-注释调用）
```diff
         del cls.class_lookups[lookup_name]
-        cls._clear_cached_lookups()
+        # cls._clear_cached_lookups()
```
**变异语义**：注释掉清缓存调用，效果同 A 但形式是死代码注释。模拟"临时注释调试、忘了恢复"。保留。

### Group C — 补充（A1 接口契约：调用错对象）
```diff
         del cls.class_lookups[lookup_name]
-        cls._clear_cached_lookups()
+        lookup._clear_cached_lookups()
```
**变异语义**：在 `lookup`（被注销的 lookup 类/对象）而非 `cls`（注册它的 Mixin 类）上调 `_clear_cached_lookups`。`lookup` 通常没有该方法 → AttributeError；即便有，清的也是错误类的缓存，`cls` 的 `get_lookups` 缓存未清。模拟"cls/lookup 混淆"。

### Group D — 替换（B2 边界：条件清缓存）
**原**：与 A 重复（删行）。
**最终 mutation**：
```diff
     def _unregister_lookup(cls, lookup, lookup_name=None):
+        _explicit_name = lookup_name is not None
         ...
         del cls.class_lookups[lookup_name]
-        cls._clear_cached_lookups()
+        if _explicit_name:
+            cls._clear_cached_lookups()
```
**变异语义**：只在调用方**显式传入 `lookup_name`** 时才清缓存。测试通过 `_unregister_lookup(Exactly)`（只传 lookup、不传 lookup_name）触发，`lookup_name is None` → `_explicit_name` 为 False → 不清缓存。模拟"以为只有显式命名时才需清缓存"的边界误解。

### Group E — 补充（E2 隐式→显式开关）
```diff
         del cls.class_lookups[lookup_name]
-        cls._clear_cached_lookups()
+        if getattr(cls, 'clear_cache_on_unregister', False):
+            cls._clear_cached_lookups()
```
**变异语义**：把清缓存藏到 `clear_cache_on_unregister` 类属性开关后，默认 False。默认情况下注销不清缓存。模拟"把行为做成可配置、默认却关掉"。

## 新设计 Mutation 说明

原 A=D 重复（都删清缓存行）。本次保留 A（删行）、B（注释），补充 C（调用错对象 `lookup.` 而非 `cls.`）、E（默认关闭开关），把重复的 D 改为"仅显式 lookup_name 时才清"（边界条件）。五组覆盖"删除 / 注释 / 错对象 / 条件清 / 默认关闭开关"五个角度。全部实测：golden 通过、变异令 F2P（`test_lookups_caching`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
