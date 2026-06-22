# django__django-16256

## 问题背景

异步接口被引入 QuerySet 后，相关管理器（related manager）意外地从 QuerySet 继承了 `acreate()`/`aget_or_create()`/`aupdate_or_create()`，但这些方法调的是 QuerySet 的同名方法、而非相关管理器自己的 `create()`/`get_or_create()`/`update_or_create()`——后者会自动设置外键/M2M 关系。结果异步版不挂关系。Golden patch 在三类相关管理器（reverse FK、generic relation、forward M2M）中显式定义 `acreate`/`aget_or_create`/`aupdate_or_create`，用 `sync_to_async(self.create)(...)` 等包装相关管理器**自己**的同步方法。

## Golden Patch 语义分析

以 forward M2M 为例：
```python
async def acreate(self, *, through_defaults=None, **kwargs):
    return await sync_to_async(self.create)(through_defaults=through_defaults, **kwargs)
acreate.alters_data = True
```
核心语义：**异步方法必须 `sync_to_async` 包装相关管理器自身的同步方法（`self.create`/`self.get_or_create`/`self.update_or_create`），并完整透传参数（含 `through_defaults`）**。这样异步路径与同步路径行为一致——会设置外键/M2M 关系、走相关管理器的逻辑。签名要匹配同步版（M2M 版含 `*, through_defaults=None`），参数要原样转发。任何签名错配、参数漏传、或调错方法都会让异步版行为偏离同步版。

F2P 测试 `AsyncRelatedManagersOperationTest`（test_acreate/aget_or_create/aupdate_or_create 及 reverse 版）与 `GenericRelationsTests.test_generic_async_acreate` 等：通过 `await mtm.simples.acreate(...)`、`aget_or_create(..., through_defaults=...)`、`aupdate_or_create(..., defaults=...)` 断言对象被正确创建/更新且 `created` 标志正确。

## 调用链分析

`mtm1.simples.acreate(field=2)` → 相关管理器的 `acreate` → `sync_to_async(self.create)(through_defaults=None, field=2)` → 相关管理器的同步 `create`（设置 M2M 关系并落库）。`aget_or_create`/`aupdate_or_create` 类似，包装各自同步版，透传 `through_defaults`/`defaults`/`kwargs`。M2M 版签名带 `*, through_defaults=None`（keyword-only），reverse FK 版只有 `**kwargs`。`created` 标志由同步版返回的 `(obj, created)` 透传。签名、参数转发、所调方法、返回值任一出错都会令对应 F2P 失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | acreate 用可变默认 `{}`、aget_or_create 参数名打错 `through_defaults_` |
| B | 🟢 高质量 | 保留 | aget_or_create 返回 `(obj, not created)`，created 标志反转 |
| C | 🟢 高质量 | 保留 | M2M 三个异步方法去掉 `*, through_defaults=None` 签名，引用未定义名 |
| D | 🟢 高质量 | 保留 | aupdate_or_create 漏传 `**kwargs`，只传 through_defaults |
| E | 🟢 高质量 | 保留 | reverse FK 的 create 加 `_auto_fk=False` 开关，默认不设外键 |

五组机制各异且均有效（分布在不同管理器、不同方法、不同失效方式），全部保留（仅核验）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：可变默认 + 参数名打错）
```diff
-        async def acreate(self, *, through_defaults=None, **kwargs):
+        async def acreate(self, *, through_defaults={}, **kwargs):
...
-        async def aget_or_create(self, *, through_defaults=None, **kwargs):
-            return await sync_to_async(self.get_or_create)(
-                through_defaults=through_defaults, **kwargs)
+        async def aget_or_create(self, *, through_defaults_=None, **kwargs):
+            return await sync_to_async(self.get_or_create)(
+                through_defaults_=through_defaults_, **kwargs)
```
**变异语义**：acreate 用可变对象 `{}` 作默认值（经典反模式，多次调用共享同一 dict）；aget_or_create 把参数名打成 `through_defaults_`（多了下划线），转发给 `get_or_create(through_defaults_=...)` 时同步版不认识该 kwarg → TypeError。模拟"复制时参数名打错 + 可变默认值"。保留。

### Group B — 保留（D1 状态：created 标志反转）
```diff
         async def aget_or_create(self, **kwargs):
-            return await sync_to_async(self.get_or_create)(**kwargs)
+            obj, created = await sync_to_async(self.get_or_create)(**kwargs)
+            return obj, not created
```
**变异语义**：reverse FK 的 aget_or_create 把 `created` 标志取反返回。对象本身正确创建，但返回的 `(obj, created)` 中 created 与事实相反。测试断言 `self.assertIs(created, True/False)` 失败。模拟"返回元组时把布尔标志搞反"。隐蔽——对象操作正确，只有标志错。保留。

### Group C — 保留（A2 接口契约：签名缺失 + 未定义名）
```diff
-        async def acreate(self, *, through_defaults=None, **kwargs):
+        async def acreate(self, **kwargs):
             return await sync_to_async(self.create)(
                 through_defaults=through_defaults, **kwargs)
```
（同样作用于 aget_or_create、aupdate_or_create）
**变异语义**：M2M 版三个异步方法去掉 `*, through_defaults=None` 形参，但函数体仍引用 `through_defaults` → `NameError`（局部未定义）。调用时崩溃。模拟"改签名时删了形参、忘了函数体还在用它"。保留。

### Group D — 保留（D1 状态：漏传 kwargs）
```diff
         async def aupdate_or_create(self, *, through_defaults=None, **kwargs):
             return await sync_to_async(self.update_or_create)(
-                through_defaults=through_defaults, **kwargs
+                through_defaults=through_defaults
             )
```
**变异语义**：aupdate_or_create 转发时漏传 `**kwargs`，只传 `through_defaults`。调用 `aupdate_or_create(field=2)` 时 `field` 等查找/更新参数丢失，update_or_create 拿不到 → 行为错误或缺参。模拟"转发参数时漏了 **kwargs"。保留。

### Group E — 保留（D1 状态：外键自动设置开关）
```diff
-        def create(self, **kwargs):
+        def create(self, *, _auto_fk=False, **kwargs):
             self._check_fk_val()
-            kwargs[self.field.name] = self.instance
+            if _auto_fk:
+                kwargs[self.field.name] = self.instance
             db = router.db_for_write(self.model, instance=self.instance)
             return super(RelatedManager, self.db_manager(db)).create(**kwargs)
```
**变异语义**：reverse FK 的同步 `create` 把"自动设置外键"藏到 `_auto_fk` 开关后，默认 False → 不设外键。`acreate` 包装 `self.create` 时不传 `_auto_fk` → 创建的对象外键为空，`new_relatedmodel.simple` 不等于预期实例。影响 acreate（依赖 create）。模拟"把自动关联做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复、无必须替换项，故全部保留并逐一核验。五组分布在 reverse FK 管理器（B、E）与 forward M2M 管理器（A、C、D），覆盖"可变默认+参数名打错 / created 标志反转 / 签名缺失引用未定义名 / 漏传 kwargs / 外键自动设置开关"五个角度，分别破坏异步方法的签名、返回值、参数转发、依赖的同步方法等不同环节。全部实测：golden 通过、五个变异均令对应 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
