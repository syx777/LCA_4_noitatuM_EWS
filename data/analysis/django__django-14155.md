# django__django-14155 Mutation 分析

## 问题背景

`ResolverMatch.__repr__()` 在 view 为 `functools.partial` 对象时输出不友好：旧实现直接用 `self._func_path`，而 partial 没有 `__name__`，会回退到 `func.__class__`，显示为 `functools.partial`，既看不到底层函数也看不到预绑定参数。修复目标是当 `self.func` 是 partial 时改用 `repr(self.func)` 暴露底层函数与绑定参数，同时把所有字段的格式化从 `%s` 改为 `%r`（给字符串字段加引号）。

涉及文件：`django/urls/resolvers.py`（`ResolverMatch.__repr__`）。
F2P 测试：`tests/urlpatterns_reverse/tests.py` 的 `ResolverMatchTests.test_repr`（修改，字段加引号）与新增 `test_repr_functools_partial`（验证 partial/nested/wrapped 三种 fixture 的 repr 字符串）。

## Golden Patch 语义分析

```python
if isinstance(self.func, functools.partial):
    func = repr(self.func)
else:
    func = self._func_path
return (
    'ResolverMatch(func=%s, args=%r, kwargs=%r, url_name=%r, '
    'app_names=%r, namespaces=%r, route=%r)' % (...)
)
```

两个语义点：(1) partial 分支用 `repr(self.func)` 产出 `functools.partial(<function empty_view ...>, template_name='...')`；(2) 全字段 `%r`，使 `url_name`/`route` 等字符串带引号。任一点被破坏都会让 F2P 失败。

## 调用链分析

URL 解析时 `URLResolver.resolve()` 构造 `ResolverMatch(func, ...)`，`func` 即 urlpatterns 里注册的视图。fixture `empty_view_partial = partial(empty_view, template_name="template.html")` 等被映射到 `/partial/` 等路径。测试通过 `resolve('/partial/')` 拿到 `ResolverMatch` 并断言 `repr(...)`。partial 的关键属性：`self.func.func`（内部函数）、`self.func.args`（位置参数，本 fixture 为空）、`self.func.keywords`（`template_name=...`）。

## 替换决策总览

| 组 | 原 diff 性质 | 分类 | 决策 |
|----|------|------|------|
| A | 加 `and hasattr(self.func,'__wrapped__')` 守卫，关闭对普通 partial 的修复 | 🔴 unnatural guard disabling fix | 替换为 A1 |
| B | `not isinstance(...)` 逻辑反转，等于 golden 反向 | 🔴 direct reverse | 替换为 B1 |
| C | 删除 partial 分支 `func=self._func_path`，等于 golden 反向 | 🔴 direct reverse | 替换为 C1 |
| D | 与 C 完全相同（重复） | 🔴 redundant duplicate | 替换为 D1 |

四组全部 🔴 MUST REPLACE（M_shallow 不适用），共替换 4 个，全部重新设计为正交失败模式。

## 各组 Mutation 分析

### A 组
- 原 diff：`if isinstance(self.func, functools.partial) and hasattr(self.func, '__wrapped__')`。
- 分类：🔴。`__wrapped__` 守卫是不自然的人为补丁，作用是让多数 partial 走 else 分支，直接关闭修复，属于“disabling the fix”工件。
- 替换 A1 diff：
```diff
     def __repr__(self):
         if isinstance(self.func, functools.partial):
-            func = repr(self.func)
+            func = repr(self.func.func)
         else:
             func = self._func_path
```
- 变异语义：多解一层 partial，`repr(self.func.func)` 只给内部函数 repr，丢掉 `functools.partial(...)` 包裹与绑定 kwargs。像是“解包 partial”意图写过头的真实错误。

### B 组
- 原 diff：`if not isinstance(self.func, functools.partial)`。
- 分类：🔴。逻辑反转是 golden 的直接反向，redundancy。
- 替换 B1 diff：
```diff
         return (
-            'ResolverMatch(func=%s, args=%r, kwargs=%r, url_name=%r, '
-            'app_names=%r, namespaces=%r, route=%r)' % (
+            'ResolverMatch(func=%s, args=%r, kwargs=%r, url_name=%s, '
+            'app_names=%r, namespaces=%r, route=%s)' % (
```
- 变异语义：仅把 `url_name` 与 `route` 两个字段的转换符从 `%r` 回退为 `%s`，丢掉引号。针对 golden 第二个语义点（字段引号）的局部回退，正交于 partial 分支逻辑，会同时打到 `test_repr` 与 `test_repr_functools_partial`。

### C 组
- 原 diff：删除整个 partial 分支，`func = self._func_path`。
- 分类：🔴，golden 直接反向。
- 替换 C1 diff：
```diff
     def __repr__(self):
-        if isinstance(self.func, functools.partial):
+        if isinstance(self.func, functools.partial) and self.func.args:
             func = repr(self.func)
         else:
             func = self._func_path
```
- 变异语义：增加 `self.func.args` 真值守卫，只有当 partial 绑定了位置参数才走特殊分支。fixture 都是关键字 partial（`template_name=...`），`args` 为空 → 落入 `_func_path`。一个关于“何时 partial 才值得特殊处理”的边界误判。

### D 组
- 原 diff：与 C 相同（重复）。
- 分类：🔴 重复工件。
- 替换 D1 diff：
```diff
     def __repr__(self):
         if isinstance(self.func, functools.partial):
-            func = repr(self.func)
+            func = self.func.func.__module__ + '.' + self.func.func.__name__
         else:
             func = self._func_path
```
- 变异语义：partial 分支改为手工拼接内部函数的 `module.name` 点路径，丢掉 `functools.partial(...)` 包裹和 kwargs。是一种“看似更干净”的替代格式化选择，仅在精确 repr 断言上失败。

## 新设计 Mutation 说明（正交性）

- A1：解包层级错误（结构正确但少了一层包裹信息）。
- B1：格式化转换符局部回退（引号缺失，正交于分支逻辑，且额外触及 `test_repr`）。
- C1：分支触发条件加 args 守卫（边界条件误判，partial 整体失效）。
- D1：替代格式化拼接路径（丢弃 partial 元信息）。
四者失败成因互不相同（解包深度 / 引号格式 / 分支条件 / 输出构造），保证多样性，且都通过普通非-partial 视图的 repr，仅在 partial 精确断言（及 B1 的引号断言）上失败。

## 实测验证结果

Harness：`cp -r` base → 应用 golden `patch` + `test_patch`（均 `patch -p1` rc0）→ commit。

- 基线（golden 无变异）：`runtests.py urlpatterns_reverse` → **Ran 103 tests OK**（通过）。
- A1：py_compile OK；test_rc=1；仅 `test_repr_functools_partial`(partial/partial_nested/partial_wrapped) FAIL。
- B1：py_compile OK；test_rc=1；`test_repr` + `test_repr_functools_partial`×3 FAIL（均为 F2P，引号断言）。
- C1：py_compile OK；test_rc=1；仅 `test_repr_functools_partial`×3 FAIL。
- D1：py_compile OK；test_rc=1；仅 `test_repr_functools_partial`×3 FAIL。

所有失败均落在 test_patch 新增/修改的 F2P 测试上，无 P2P 回归（其余 99~100 项通过）。
