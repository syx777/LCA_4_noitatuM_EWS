# django__django-13794

## 问题背景

Django 的模板过滤器 `add` 在尝试连接普通字符串与惰性字符串（`gettext_lazy()`）时抛出 `TypeError: can only concatenate str (not "__proxy__") to str`，导致结果返回空字符串。

根本原因：`lazy()` 创建的 `__proxy__` 类继承自 `Promise`，**不是** `str` 的子类（MRO: `__proxy__` → `Promise` → `object`）。虽然 `__prepare_class__` 为 `__proxy__` 动态安装了 str 方法的 wrapper，但 Python 的 `str.__add__` 是 C 级实现，会拒绝非 str 对象，抛出 TypeError 而非返回 `NotImplemented`。因此当 `'string' + lazy_proxy` 时，Python 无法触发 `lazy_proxy.__radd__` 的反射协议。

## Golden Patch 语义分析

**修复核心**：为 `lazy()` 生成的 `__proxy__` 类添加两个魔法方法：

```python
def __add__(self, other):
    return self.__cast() + other   # lazy + anything

def __radd__(self, other):
    return other + self.__cast()   # anything + lazy
```

- `__add__`：当左操作数是 lazy proxy 时，先将自身 `__cast()` 为实际类型再相加
- `__radd__`：当右操作数是 lazy proxy 而左操作数不支持（`str.__add__` 拒绝 proxy）时调用，将 `self.__cast()` 转换后完成加法
- `self.__cast()` 在 `_delegate_text` 时返回 `func(*args, **kw)`（即实际 str 值），对 int lazy 返回实际 int

## 调用链分析

```
add filter(value='string', arg=lazy('lazy')):
  └─ int('string') -> ValueError
  └─ int(lazy('lazy')) -> ValueError  
  └─ value + arg -> 'string' + lazy_proxy
       └─ str.__add__('string', lazy_proxy) -> TypeError (C level rejects)
       └─ lazy_proxy.__radd__('string') <- 需要此方法存在
            └─ return 'string' + lazy_proxy.__cast() = 'string' + 'lazy' = 'stringlazy'

add filter(value=lazy('string'), arg=lazy('lazy')):
  └─ lazy_s1 + lazy_s2
  └─ lazy_s1.__add__(lazy_s2) <- 需要此方法存在
       └─ self.__cast() + lazy_s2 = 'string' + lazy_s2
       └─ str.__add__('string', lazy_s2) -> NotImplemented
       └─ lazy_s2.__radd__('string') -> 'string' + lazy_s2.__cast() = 'stringlazy'
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 保留 | isinstance 类型守卫，模拟只支持 str+str 的实现，test_lazy_add (int+int) 失败 |
| B | 必须替换 | 替换 | 删除 __radd__，test_add08 (str+lazy) 直接失败，属于删方法 |
| C | 必须替换 | 替换 | `self + other`（无 __cast）导致无限递归，明显人工痕迹 |
| D | 必须替换 | 替换 | `other + self`（__radd__ 无 __cast）导致无限递归，明显人工痕迹 |
| E | 必须替换 | 替换 | `raise TypeError('Cannot add')`，人工痕迹极明显 |

语义浅层共 1 个（A），floor(1/2)=0 个需替换，A 保留。

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
         def __add__(self, other):
+            if not isinstance(other, str):
+                return NotImplemented
             return self.__cast() + other
```
**分类**：🟡 语义浅层（保留）
**理由**：添加 `isinstance(other, str)` 守卫，使 `__add__` 只处理 str+str 情况，对非 str 的 other（如 lazy_int）返回 `NotImplemented`。test_add08/09 通过（lazy_s2 的 __radd__ 处理 str+lazy，lazy+lazy 通过 __radd__ 链路也可完成）。但 test_lazy_add 失败：`lazy_4 + lazy_5` → `lazy_4.__add__(lazy_5)` → NotImplemented → `lazy_5.__radd__(lazy_4)` → `lazy_4 + 5` → `lazy_4.__add__(5)` → NotImplemented → `5.__radd__(lazy_4)` → Python 无法找到 int+proxy 路径 → TypeError。模拟开发者认为"__add__应该只处理 str 类型，让其他类型自行处理"的限制性设计。

**最终 mutation**（保留，与原相同）：
```diff
diff --git a/django/utils/functional.py b/django/utils/functional.py
index 5c8a0c233f..0c8eac9879 100644
--- a/django/utils/functional.py
+++ b/django/utils/functional.py
@@ -177,6 +177,8 @@ def lazy(func, *resultclasses):
             return self.__cast() % rhs
 
         def __add__(self, other):
+            if not isinstance(other, str):
+                return NotImplemented
             return self.__cast() + other
 
         def __radd__(self, other):
```
**变异语义**：test_lazy_add 失败（4+5=9 期望，但 int+int lazy 路径中断），test_add08/09 通过（str 路径完整）。

---

### Group B — 替换

**原 mutation**（必须替换）：删除整个 `__radd__` 方法 — test_add08（str+lazy 需要 __radd__）直接失败。

**最终 mutation**（B1 — 用 str(self) 替代 self.__cast()）：
```diff
diff --git a/django/utils/functional.py b/django/utils/functional.py
index 5c8a0c233f..86d8b1962d 100644
--- a/django/utils/functional.py
+++ b/django/utils/functional.py
@@ -177,7 +177,7 @@ def lazy(func, *resultclasses):
             return self.__cast() % rhs
 
         def __add__(self, other):
-            return self.__cast() + other
+            return str(self) + other
 
         def __radd__(self, other):
             return other + self.__cast()
```
**变异语义**：`__add__` 中用 `str(self)` 替代 `self.__cast()`。对 str lazy 而言 `str(self) == self.__cast()` 是正确的，但对 int lazy（如 `lazy(4, int)`）：`str(lazy_4) = '4'`（字符串）而非 `4`（整数）。`lazy_4.__add__(lazy_5)` = `str(lazy_4) + lazy_5` = `'4' + lazy_5` → `lazy_5.__radd__('4')` = `'4' + str(lazy_5)` = `'4' + '5'` = `'45'` != `9`，或直接因 `'4' + 5 TypeError` 失败。test_lazy_add FAIL。test_add08/09 PASS（str(lazy_str) = lazy_str__cast() 相同）。模拟开发者用 `str()` 转换而非 `__cast()` 的类型混淆错误。

---

### Group C — 替换

**原 mutation**（必须替换）：`self + other` 无限递归，明显人工痕迹。

**最终 mutation**（C1 — __radd__ 操作数顺序反转）：
```diff
diff --git a/django/utils/functional.py b/django/utils/functional.py
index 5c8a0c233f..c711f81d07 100644
--- a/django/utils/functional.py
+++ b/django/utils/functional.py
@@ -180,7 +180,7 @@ def lazy(func, *resultclasses):
             return self.__cast() + other
 
         def __radd__(self, other):
-            return other + self.__cast()
+            return self.__cast() + other
 
         def __deepcopy__(self, memo):
```
**变异语义**：`__radd__` 将 `other + self.__cast()` 改为 `self.__cast() + other`（操作数顺序颠倒）。test_add08（`'string' + lazy('lazy')`）：`lazy.__radd__('string')` = `lazy.__cast() + 'string'` = `'lazy' + 'string'` = `'lazystring'` ≠ `'stringlazy'` **FAIL**。test_add09 也 FAIL（lazy+lazy 最终通过 __radd__ 完成，结果 `'lazystring'`）。test_lazy_add PASS（int+int 加法满足交换律，`5+4=9`）。模拟开发者在实现 `__radd__` 时弄反了操作数顺序，这是实现 `__radd__` 时最常见的错误（`__radd__` 本身就表示"反射"，很容易弄混 self/other 角色）。

---

### Group D — 替换

**原 mutation**（必须替换）：`__radd__` 中 `other + self`（无 __cast）导致无限递归。

**最终 mutation**（D1 — __radd__ 用 str(self) 破坏 int 类型）：
```diff
diff --git a/django/utils/functional.py b/django/utils/functional.py
index 5c8a0c233f..225a1a7fa8 100644
--- a/django/utils/functional.py
+++ b/django/utils/functional.py
@@ -180,7 +180,7 @@ def lazy(func, *resultclasses):
             return self.__cast() + other
 
         def __radd__(self, other):
-            return other + self.__cast()
+            return other + str(self)
 
         def __deepcopy__(self, memo):
```
**变异语义**：`__radd__` 中用 `str(self)` 替代 `self.__cast()`。test_add08/09（str lazy）：`str(lazy_str) = 'lazy'`，与 `self.__cast()` 结果相同，PASS。test_lazy_add（int lazy）：`lazy_5.__radd__(4)` = `4 + str(lazy_5)` = `4 + '5'` → TypeError（int + str 失败）**FAIL**。仅在需要 int lazy（或其他非 str lazy）的加法场景下失败。模拟开发者将 `__radd__` 中的 `__cast()` 错误地替换为 `str()` 转换（过于保守的字符串化）。

---

### Group E — 替换

**原 mutation**（必须替换）：`raise TypeError('Cannot add')`，明显人工痕迹（硬编码错误消息）。

**最终 mutation**（E2 — __add__ 操作数顺序反转）：
```diff
diff --git a/django/utils/functional.py b/django/utils/functional.py
index 5c8a0c233f..daac6090dc 100644
--- a/django/utils/functional.py
+++ b/django/utils/functional.py
@@ -177,7 +177,7 @@ def lazy(func, *resultclasses):
             return self.__cast() % rhs
 
         def __add__(self, other):
-            return self.__cast() + other
+            return other + self.__cast()
 
         def __radd__(self, other):
             return other + self.__cast()
```
**变异语义**：`__add__` 中将 `self.__cast() + other` 改为 `other + self.__cast()`（操作数颠倒）。单字符差异（仅调换 self.__cast() 和 other 的位置）。由于字符串加法不可交换，`lazy('hello') + ' world'` 会返回 `' worldhello'` 而非 `'helloworld'`。

但 F2P 测试场景：
- test_add08（str + lazy via __radd__）：不经过 __add__ → PASS
- test_add09（lazy_s1='string' + lazy_s2='lazy'）：`lazy_s1.__add__(lazy_s2)` = `lazy_s2 + lazy_s1.__cast()` = `lazy_s2 + 'string'` → `lazy_s2.__add__('string')` = `'string' + lazy_s2.__cast()` = `'string'+'lazy'` = `'stringlazy'` PASS！（两次反转互相抵消）
- test_lazy_add（4+5=9）：`lazy_4.__add__(lazy_5)` = `lazy_5 + 4` → `lazy_5.__add__(4)` = `4 + 5` = `9` PASS（整数加法交换律）

所有 F2P 和 P2P 测试通过！漏洞在于：`lazy('foo') + 'bar'` 会得到 `'barfoo'` 而非 `'foobar'`，但 F2P 测试只覆盖了 lazy+lazy 场景（测试值恰好是 'string'+'lazy' = 'stringlazy'，通过两次反转得到同一结果），无法检测到 lazy+str 的顺序错误。极难发现的 mutation。

---

## 新设计 Mutation 说明

### B 设计说明
`__cast()` 返回实际类型（str 为 str，int 为 int），而 `str(self)` 总是返回字符串。对 str lazy 两者等价，所以 str 路径测试全通过。但对 int lazy（test_lazy_add），`str()` 将 4 转为 '4'，导致 '4' + 5 TypeError。真实开发者可能认为"lazy proxy 应该先转换为字符串再相加"，忽略了 int lazy 的使用场景。

### C 设计说明
`__radd__(self, other)` 的语义是"other + self"（反射加法，other 在左），但实现写成 `self.__cast() + other`（self 在左），这是最直觉上的错误——开发者可能误以为 `__radd__` 也是 "self op other" 的格式。这个错误使得字符串连接顺序反转，但整数加法（交换律）不受影响。

### D 设计说明
与 B 对称：在 `__radd__` 中使用 `str(self)` 而非 `self.__cast()`。对 str lazy 无影响，对 int lazy 将整数强制转换为字符串，导致 `4 + '5'` TypeError。

### E 设计说明
`__add__` 的两种操作数顺序在 F2P 测试中恰好等价：
1. lazy+lazy 场景：两次反转互相抵消，结果正确
2. int+int 场景：加法满足交换律，结果正确
3. 只有 lazy+str 场景（F2P 未覆盖）才会暴露顺序错误

这是最难发现的 mutation，需要专门针对 lazy+str（非 str+lazy）的测试才能检测到。
