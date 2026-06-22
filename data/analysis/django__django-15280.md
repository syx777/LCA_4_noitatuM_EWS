# django__django-15280

## 问题背景

当沿 prefetch 链"回指"父对象时，deferred（`only()`）字段会出错：嵌套 prefetch 中子对象自带一个指回父对象的 prefetch（`Prefetch('user', queryset=User.objects.only('kind'))`），但反向 manager 的 `get_prefetch_queryset` 会无条件用父对象覆盖子对象上已 prefetch 的关系，导致 deferred 字段丢失、产生额外查询。Golden patch 在覆盖前加守卫：`if not self.field.is_cached(rel_obj)`，仅当该关系尚未缓存时才设置。

## Golden Patch 语义分析

```python
for rel_obj in queryset:
    if not self.field.is_cached(rel_obj):
        instance = instances_dict[rel_obj_attr(rel_obj)]
        setattr(rel_obj, self.field.name, instance)
```
核心语义：**若 `rel_obj` 上的反向关系已被（嵌套 prefetch）缓存，就不要用父对象覆盖它**。`self.field.is_cached(rel_obj)` 判定 `rel_obj` 上 `self.field` 是否已有缓存值。守卫保证嵌套 prefetch 指定的 `only('address')` 版本父对象被保留，而非被外层完整父对象顶替。

F2P 测试 `NestedPrefetchTests.test_nested_prefetch_is_not_overwritten_by_related_object`：嵌套 prefetch 后断言 `Room.house.is_cached(room) is True` 且访问 `house.rooms.first().house.address` 时 0 查询。

## 调用链分析

`get_prefetch_queryset(instances, queryset)` 在反向 many-to-one manager 内，遍历 `queryset` 中的 `rel_obj`，按需把父 `instance` 设置回 `rel_obj` 的 `self.field`。`is_cached(rel_obj)` 检查 `rel_obj` 的字段缓存。守卫被绕过/判错对象/判错关系，都会导致覆盖发生、缓存被破坏、F2P 失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A 组 |
| B | 🟡 | 替换 | 原 B=C 重复（都把 `not is_cached` 反转）；保留反转机制作为 B |
| C | 🔴 必须替换 | 替换 | 与 B 字节级重复；改为"判错关系侧" |
| D | 🟢 高质量 | 保留 | 注释掉守卫、无条件覆盖 |
| E | 🟢 高质量 | 保留 | 加 `respect_cache` 参数把守卫藏在默认关闭的开关后 |

原仅 B/C/D/E 且 B=C 重复。补 A，并把重复的 C 改为不同机制。

## 各组 Mutation 分析

### Group A — 补充（A1 接口契约：判错对象）
```diff
-                if not self.field.is_cached(rel_obj):
+                if not self.field.is_cached(instances_dict[rel_obj_attr(rel_obj)]):
```
**变异语义**：把 `is_cached` 的检查对象从 `rel_obj`（子对象）换成 `instances_dict[...]`（父对象 instance）。守卫检查的是错误的对象——父对象的字段缓存状态与"子对象的反向关系是否已被嵌套 prefetch 缓存"无关，于是守卫失效、覆盖照常发生，`is_cached(room)` 被破坏。模拟"传错了 is_cached 的实参"。

### Group B — 替换/保留机制（B3 逻辑反转）
```diff
-                if not self.field.is_cached(rel_obj):
+                if self.field.is_cached(rel_obj):
```
**变异语义**：守卫条件取反——变成"仅当已缓存时才覆盖"，语义彻底颠倒：已被嵌套 prefetch 缓存的关系反被父对象顶替。F2P 失败。原 B/C 即此写法，保留为 B。

### Group C — 替换（C1 数据形状：判错关系侧）
**原**：与 B 重复。
**最终 mutation**：
```diff
-                if not self.field.is_cached(rel_obj):
+                if not self.field.remote_field.is_cached(rel_obj):
```
**变异语义**：把 `self.field.is_cached` 换成 `self.field.remote_field.is_cached`——检查的是**关系的另一侧**（remote_field）的缓存状态，而非当前字段。relation 的两侧缓存语义不同，守卫判定的对象不对，无法正确识别"已缓存"，覆盖发生。模拟"forward/reverse 关系侧混淆"的数据形状错误。与 A（判错对象）不同，这里是判错关系属性。

### Group D — 保留（D1 状态：移除守卫）
```diff
-                if not self.field.is_cached(rel_obj):
-                    instance = instances_dict[rel_obj_attr(rel_obj)]
-                    setattr(rel_obj, self.field.name, instance)
+                instance = instances_dict[rel_obj_attr(rel_obj)]
+                setattr(rel_obj, self.field.name, instance)
```
**变异语义**：完全去掉守卫，无条件覆盖。任何嵌套 prefetch 的缓存都被父对象顶替——直接撤销修复。保留。

### Group E — 保留（E2 隐式→显式参数）
```diff
             # Since we just bypassed this class' get_queryset(), we must manage
             # the reverse relation manually.
+            overwrite_cached = True
             for rel_obj in queryset:
-                if not self.field.is_cached(rel_obj):
+                if overwrite_cached or not self.field.is_cached(rel_obj):
```
**变异语义**：引入 `overwrite_cached = True` 局部开关，条件改成 `overwrite_cached or not is_cached`。由于开关恒 True，守卫被短路，永远覆盖。模拟"加了个可配置开关、默认却开成会覆盖"的隐式行为退化。保留。

## 新设计 Mutation 说明

原仅 B/C/D/E 且 B=C 重复。补齐 A（is_cached 判错对象——传父 instance），把重复的 C 改为"判错关系侧"（`remote_field.is_cached`）。五组机制各异：A 判错对象、B 反转条件、C 判错关系属性、D 移除守卫、E 默认开启的覆盖开关。全部实测：golden 通过、变异令 F2P（`test_nested_prefetch_is_not_overwritten_by_related_object`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
