# django__django-15731

## 问题背景

`inspect.signature()` 对 manager 方法返回错误签名 `(*args, **kwargs)`，而非真实的 queryset 方法签名（如 `(objs, batch_size=None, ...)`）。原因：`BaseManager._get_queryset_methods` 手动复制 `__name__`/`__doc__` 到包装方法，但没复制签名信息。Golden patch 改用 `@functools.wraps(method)` 装饰 `manager_method`，它会同时复制 `__name__`、`__doc__` 和 `__wrapped__`（后者让 `inspect.signature` 透视到原方法签名）。

## Golden Patch 语义分析

```python
def create_method(name, method):
    @wraps(method)
    def manager_method(self, *args, **kwargs):
        return getattr(self.get_queryset(), name)(*args, **kwargs)
    return manager_method
```
核心语义：**用 `@wraps(method)` 一次性复制元数据**。`functools.wraps` 复制 `__name__`/`__qualname__`/`__doc__`/`__module__`/`__dict__` 并设置 `__wrapped__ = method`。`inspect.signature` 优先沿 `__wrapped__` 解析，从而得到真实签名。仅手动设 `__name__`/`__doc__`（旧代码）不会设 `__wrapped__`，签名仍是 `(*args, **kwargs)`。

F2P 测试两个：`test_manager_method_attributes`（`__doc__`/`__name__` 正确）与 `test_manager_method_signature`（`inspect.signature(bulk_create)` 等于真实签名）。

## 调用链分析

`_get_queryset_methods` 为每个 queryset 方法生成 `manager_method` 包装并复制元数据。`inspect.signature(Article.objects.bulk_create)` 沿 `manager_method.__wrapped__`（由 wraps 设置）找到真实 `QuerySet.bulk_create` 的签名。缺 `@wraps`/缺 `__wrapped__` → 签名测试失败；缺 `__name__`/`__doc__` → 属性测试失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 A=B=C（都注释 `@wraps`）；改为只设 `__name__ = name` |
| B | 🔴 必须替换 | 替换 | 与 A/C 重复；改为 `@wraps(name)` 错误目标 |
| C | 🔴 必须替换 | 替换 | 与 A/B 重复；改为只设 `__doc__` |
| D | 🟢 高质量 | 保留 | 还原旧代码：手动设 `__name__`/`__doc__`，签名仍坏 |
| E | 🟢 高质量 | 保留 | 删除 `@wraps` 行，无任何元数据复制 |

原 A=B=C 三者完全相同（注释 `@wraps`）。重做 A、B、C 为不同机制。

## 各组 Mutation 分析

### Group A — 替换（D1 状态：只设 __name__=name）
```diff
-            @wraps(method)
             def manager_method(self, *args, **kwargs):
                 return getattr(self.get_queryset(), name)(*args, **kwargs)
+
+            manager_method.__name__ = name
             return manager_method
```
**变异语义**：去掉 `@wraps`，只手动设 `__name__ = name`（用方法名字符串），既不设 `__doc__` 也不设 `__wrapped__`。属性测试因缺 `__doc__` 失败、签名测试因缺 `__wrapped__` 失败。模拟"只补了部分元数据"。

### Group B — 替换（A1 接口契约：wraps 错误目标）
```diff
-            @wraps(method)
+            @wraps(name)
```
**变异语义**：`@wraps(name)` 包裹的是字符串 `name` 而非 `method` 函数对象。`wraps` 试图从字符串复制 `__name__`/`__doc__`/`__wrapped__`，要么取到错误值要么 AttributeError，签名/属性均不正确。模拟"传错 wraps 的参数（name 而非 method）"。

### Group C — 替换（C1 类型/数据形状：只设 __doc__）
```diff
-            @wraps(method)
             def manager_method(self, *args, **kwargs):
                 return getattr(self.get_queryset(), name)(*args, **kwargs)
+
+            manager_method.__doc__ = method.__doc__
             return manager_method
```
**变异语义**：去掉 `@wraps`，只手动复制 `__doc__`，不设 `__name__`/`__wrapped__`。属性测试因 `__name__` 错失败、签名测试因缺 `__wrapped__` 失败。模拟"只关心 docstring、漏了名字和签名"。

### Group D — 保留（D1 状态：还原旧代码）
```diff
-            @wraps(method)
             def manager_method(self, *args, **kwargs):
                 return getattr(self.get_queryset(), name)(*args, **kwargs)
+
+            manager_method.__name__ = method.__name__
+            manager_method.__doc__ = method.__doc__
             return manager_method
```
**变异语义**：完全还原 golden 之前的旧实现——手动设 `__name__`/`__doc__` 但不用 `@wraps`。属性测试通过，但签名测试失败（无 `__wrapped__`，`inspect.signature` 仍返回 `(*args, **kwargs)`）。这正是原 bug。保留。

### Group E — 保留（D-删 @wraps）
```diff
-            @wraps(method)
             def manager_method(self, *args, **kwargs):
```
**变异语义**：仅删除 `@wraps(method)` 行，不补任何元数据。`__name__`/`__doc__`/`__wrapped__` 全错，两个 F2P 都失败。保留。

## 新设计 Mutation 说明

原 A=B=C 三者完全相同（注释 `@wraps`）。本次保留 D（还原旧手动代码，仅签名坏）、E（删 @wraps，全坏），把 A、B、C 重做为不同的"部分/错误元数据复制"：A 只设 `__name__=name`、B 用 `@wraps(name)` 错误目标、C 只设 `__doc__`。五组覆盖"部分 name / 错误 wraps 目标 / 部分 doc / 旧手动双属性 / 全删"五个角度。全部实测：golden 通过、变异令两个 F2P 失败、`base→golden→test_patch` 后干净应用。
