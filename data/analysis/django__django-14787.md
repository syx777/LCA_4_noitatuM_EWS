# django__django-14787 Mutation 分析

## 问题背景

`method_decorator()` 在把函数装饰器转换为方法装饰器时，会先用 `partial(method.__get__(self, type(self)))`
构造一个 `bound_method` 再交给用户的装饰器。问题在于 `functools.partial` 对象**没有** `__name__`、
`__module__` 等函数属性。当用户的装饰器内部访问 `func.__name__`（例如日志装饰器）时，会抛出
`AttributeError: 'functools.partial' object has no attribute '__name__'`。

## Golden Patch 语义分析

修复位于 `django/utils/decorators.py` 的 `_multi_decorate._wrapper`：

```python
-        bound_method = partial(method.__get__(self, type(self)))
+        bound_method = wraps(method)(partial(method.__get__(self, type(self))))
```

`wraps(method)` 使用默认的 `WRAPPER_ASSIGNMENTS = ('__module__', '__name__', '__qualname__',
'__annotations__', '__doc__')`，把原方法的这些属性复制到 partial 对象上，从而让用户装饰器能够正常
读取 `func.__name__` / `func.__module__`。关键点是 `wraps` 的**第一个参数（包装来源）必须是 `method`**，
且 `assigned` 必须**完整包含 `__name__` 与 `__module__`**。

## 调用链分析

`method_decorator(deco)` → `_dec(obj)` → `_multi_decorate(decorator, method)` → 返回 `_wrapper`。
调用 `Test().method()` 时进入 `_wrapper`，构造 `bound_method` 并依次套用 `decorators`，最终
`bound_method(*args, **kwargs)`。F2P 测试 `decorators.tests.MethodDecoratorTests.test_wrapper_assignments`
在用户装饰器内部断言 `func.__name__ == 'method'` 且 `func.__module__ is not None`。
注意 `_multi_decorate` 末尾还有一处 `update_wrapper(_wrapper, method)`，它只影响**外层** `_wrapper`
（被 P2P 测试 `test_preserve_attributes` 覆盖），与 F2P 无关——这正是原 D 槽 mutation 失败的原因。

## 替换决策总览

| 槽位 | 原始 mutation | 原始判定 | 决策 | 新 strategy_code | 失败的 F2P |
|------|--------------|----------|------|------------------|-----------|
| A | 直接回退 golden（删 `wraps`） | 🔴 golden-revert | 替换 | A1 | test_wrapper_assignments |
| B | 与 A 字节相同 | 🔴 重复 | 替换 | B1 | test_wrapper_assignments |
| C | 与 A 字节相同 | 🔴 重复 | 替换 | C1 | test_wrapper_assignments |
| D | 注释掉外层 `update_wrapper` | 🔴 破坏 P2P | 替换 | A2 | test_wrapper_assignments |

## 各组 Mutation 分析

- **A/B/C（原始）**：三者 `diff` 完全字节相同，都是把 golden 的 `wraps(method)(...)` 直接还原为
  `partial(...)`，属于最低质量的 golden-revert 冗余，必须全部替换。
- **D（原始）**：注释掉 `_multi_decorate` 末尾的 `update_wrapper(_wrapper, method)`。该行只影响外层
  `_wrapper` 的属性，会让 P2P 测试 `test_preserve_attributes`（断言 `Test.method.__name__ == 'method'`）
  失败，属于破坏 P2P 的不合格 mutation，必须替换。

## 新设计 Mutation 说明

四个新 mutation 全部聚焦真正决定 F2P 的 `bound_method` 这一行，但失败机理彼此正交：

- **A（A1，API 默认语义）**：显式给 `wraps` 传 `assigned=('__module__','__qualname__','__doc__',
  '__annotations__')`，看似完整却**独缺 `__name__`**。`__module__` 仍被复制，故只有 name 断言失败。
  伪装性强：列表看起来像一份刻意写全的 `WRAPPER_ASSIGNMENTS`。
- **B（B1，off-by-one 边界）**：引入 `WRAPPER_ASSIGNMENTS` 常量并用 `[1:]` 切片，少了**第一个元素
  `__module__`**。`__name__` 仍在，故 name 断言通过而 module 断言失败。使用标准库常量比硬编码更显
  "稳健"，掩盖了切片越界一位的错误。
- **C（C1，类型/数据形状）**：把包装来源写成 `wraps(partial)`——包装的是 `partial` **类型本身**而非绑定方法，
  partial 继承的是 `'partial'`/builtins 元数据。`partial` 与 `method` 视觉相邻且都可调用，极难一眼看出。
- **D（A2，签名/参数集）**：把 `assigned` 收窄为 `('__qualname__','__doc__')`，**同时丢掉 `__name__`
  与 `__module__`**。`__qualname__`/`__doc__` 保留使得 `help()`、traceback 表现正常，但 name 与 module
  两个断言都失败——是 A 与 B 失败模式的叠加，提供与单属性丢失不同的覆盖。

所有四个 mutation 均经真实测试验证：`py_compile` 通过，仅 `test_wrapper_assignments` 失败（1 failure），
无任何 P2P 回归。
