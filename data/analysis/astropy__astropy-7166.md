# astropy__astropy-7166

## 问题背景

`InheritDocstrings` 是 astropy 中的一个元类（metaclass），用于让子类方法自动继承父类的文档字符串（docstring）。原始实现中使用 `inspect.isfunction(val)` 来判断是否需要继承 docstring，但 `inspect.isfunction` 对 `property` 对象返回 `False`，因此 property 类型的属性无法通过该元类继承 docstring。

Golden patch 的修复是将条件改为 `inspect.isfunction(val) or inspect.isdatadescriptor(val)`，从而将 property（属于 data descriptor）也纳入继承逻辑。

## Golden Patch 语义分析

修复的核心逻辑：

1. **原始 bug**：`inspect.isfunction(val)` 只对普通函数返回 `True`，而 `@property` 装饰器创建的是一个 `property` 对象（data descriptor），`isfunction` 对其返回 `False`。因此 property 永远不会进入 docstring 继承的代码路径。

2. **修复方法**：添加 `inspect.isdatadescriptor(val)` 判断。`isdatadescriptor` 对具有 `__set__` 或 `__delete__` 属性的对象返回 `True`，Python 的 `property` 满足此条件。

3. **为什么 `val.__doc__ = super_method.__doc__` 对 property 有效**：`property` 对象的 `__doc__` 属性是可变的（mutable），直接赋值即可修改。`getattr(base, key, None)` 在类上访问 property 时返回 property 对象本身（不触发 `__get__`），因此 `super_method.__doc__` 正确获取父类 property 的 docstring。

## 调用链分析

```
InheritDocstrings.__init__(cls, name, bases, dct)
  ├── is_public_member(key) [内部辅助函数]
  │     判断成员名是否为公开成员（dunder 方法或非下划线开头）
  ├── inspect.isfunction(val) / inspect.isdatadescriptor(val)
  │     判断 val 是否为函数或 data descriptor（property）
  ├── cls.__mro__[1:]
  │     遍历 MRO（方法解析顺序），跳过当前类，从直接父类开始
  ├── getattr(base, key, None)
  │     在父类上查找同名成员；对 property 返回 property 对象本身
  └── val.__doc__ = super_method.__doc__
        直接修改 val 的 __doc__ 属性（对函数和 property 均有效）
```

该元类被用于 astropy 整个代码库中需要自动继承文档的类（如 `ShapedLikeNDArray` 等）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 保留 | `ismethoddescriptor` 替换 `isdatadescriptor`，修改位置关键，能模拟真实的 API 混淆错误 |
| B | 必须替换 | 替换 | `or` 改 `and` 导致条件恒为 False，功能等价于直接还原 base_commit 行为 |
| C | 必须替换 | 替换 | 直接删除 `or inspect.isdatadescriptor(val)`，是 golden patch 的精确逆操作 |
| D | 必须替换 | 替换 | 与 C 完全相同的 diff，重复冗余 |
| E | 必须替换 | 替换 | `not isinstance(val, property)` 直接排除 property，功能等价于还原 base_commit |

语义浅层共 1 个（A），替换其中最弱的 floor(1/2) = 0 个。
必须替换 4 个（B、C、D、E）。

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c7..473eff1e2 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -524,7 +524,7 @@ class InheritDocstrings(type):
                 not key.startswith('_'))
 
         for key, val in dct.items():
-            if ((inspect.isfunction(val) or inspect.isdatadescriptor(val)) and
+            if ((inspect.isfunction(val) or inspect.ismethoddescriptor(val)) and
                     is_public_member(key) and
                     val.__doc__ is None):
                 for base in cls.__mro__[1:]:
```

**分类**：🟡 语义浅层（保留）

**理由**：将 `isdatadescriptor` 替换为 `ismethoddescriptor`。`ismethoddescriptor` 对 Python 的 `property` 返回 `False`（property 有 `__set__`，属于 data descriptor，而 method descriptor 只有 `__get__` 没有 `__set__`），因此 property 的 docstring 继承仍然失败。修改位置在核心判断逻辑上，模拟了开发者混淆 `isdatadescriptor` 和 `ismethoddescriptor` 两个相似 API 的真实错误。这是语义浅层但修改位置关键的 mutation，保留。

**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c7..473eff1e2 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -524,7 +524,7 @@ class InheritDocstrings(type):
                 not key.startswith('_'))
 
         for key, val in dct.items():
-            if ((inspect.isfunction(val) or inspect.isdatadescriptor(val)) and
+            if ((inspect.isfunction(val) or inspect.ismethoddescriptor(val)) and
                     is_public_member(key) and
                     val.__doc__ is None):
                 for base in cls.__mro__[1:]:
```

**变异语义**：`ismethoddescriptor` 对 `property` 返回 `False`，因此 property 的 docstring 继承路径被跳过。普通函数（`isfunction` 为 True）仍正常工作，只有 property 类型的 docstring 继承失败。代码审查时 `ismethoddescriptor` 看起来与 `isdatadescriptor` 非常相似，容易被忽略。F2P 中 `assert Subclass.bar.__doc__ == "BAR"` 会失败，`assert Subclass.__call__.__doc__ == "FOO"` 仍通过。

---

### Group B — 替换

**原 mutation**：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c7..6952e4135 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -524,7 +524,7 @@ class InheritDocstrings(type):
                 not key.startswith('_'))
 
         for key, val in dct.items():
-            if ((inspect.isfunction(val) or inspect.isdatadescriptor(val)) and
+            if ((inspect.isfunction(val) and inspect.isdatadescriptor(val)) and
                     is_public_member(key) and
                     val.__doc__ is None):
                 for base in cls.__mro__[1:]:
```

**分类**：🔴 必须替换

**理由**：`or` 改 `and` 使得条件变为 `isfunction(val) AND isdatadescriptor(val)`，而一个对象不可能同时是函数和 data descriptor，因此条件恒为 `False`。这不仅让 property 无法继承 docstring，连普通函数也无法继承 docstring，功能等价于完全禁用 `InheritDocstrings` 的继承功能。这比 base_commit 的 bug 更严重，且是一个功能等价冗余（完全破坏功能），属于必须替换。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c78..5ae089a957 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -524,7 +524,7 @@ class InheritDocstrings(type):
                 not key.startswith('_'))
 
         for key, val in dct.items():
-            if ((inspect.isfunction(val) or inspect.isdatadescriptor(val)) and
+            if ((inspect.isfunction(val) or inspect.isgetsetdescriptor(val)) and
                     is_public_member(key) and
                     val.__doc__ is None):
                 for base in cls.__mro__[1:]:
```

**变异语义**：`isgetsetdescriptor` 用于 C 扩展层面的 getset descriptor（如 `datetime.year`），对 Python 定义的 `property` 返回 `False`。因此 property 的 docstring 继承失败，但普通函数仍正常工作。代码审查时 `isgetsetdescriptor` 是 `inspect` 模块中真实存在的函数，看起来像是合理的 descriptor 类型检查，难以立即发现错误。F2P 中 `assert Subclass.bar.__doc__ == "BAR"` 失败。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c7..715784e3f 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -524,7 +524,7 @@ class InheritDocstrings(type):
                 not key.startswith('_'))
 
         for key, val in dct.items():
-            if ((inspect.isfunction(val) or inspect.isdatadescriptor(val)) and
+            if (inspect.isfunction(val) and
                     is_public_member(key) and
                     val.__doc__ is None):
                 for base in cls.__mro__[1:]:
```

**分类**：🔴 必须替换

**理由**：直接删除 `or inspect.isdatadescriptor(val)`，是 golden patch 的精确逆操作，等同于还原到 base_commit 的原始 bug 代码。属于直接冗余，必须替换。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c78..4df41feb80 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -527,7 +527,7 @@ class InheritDocstrings(type):
             if ((inspect.isfunction(val) or inspect.isdatadescriptor(val)) and
                     is_public_member(key) and
                     val.__doc__ is None):
-                for base in cls.__mro__[1:]:
+                for base in cls.__mro__[2:]:
                     super_method = getattr(base, key, None)
                     if super_method is not None:
                         val.__doc__ = super_method.__doc__
```

**变异语义**：将 MRO 遍历从 `cls.__mro__[1:]`（从直接父类开始）改为 `cls.__mro__[2:]`（跳过直接父类，从祖父类开始）。对于 `Sub -> Base -> object` 的继承链，跳过 `Base` 后只剩 `object`，而 `object` 没有 `bar` 属性，因此 `getattr(object, 'bar', None)` 返回 `None`，docstring 继承失败。这模拟了开发者对 MRO 索引理解错误的真实场景：`__mro__[0]` 是当前类，`__mro__[1]` 是直接父类，开发者误以为需要从 `[2:]` 开始跳过"自身和直接父类"。对于多层继承且直接父类没有目标方法时，`[2:]` 可能偶然工作，使得这个 bug 更难发现。F2P 中 `assert Subclass.bar.__doc__ == "BAR"` 和 `assert Subclass.__call__.__doc__ == "FOO"` 均失败。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c7..715784e3f 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -524,7 +524,7 @@ class InheritDocstrings(type):
                 not key.startswith('_'))
 
         for key, val in dct.items():
-            if ((inspect.isfunction(val) or inspect.isdatadescriptor(val)) and
+            if (inspect.isfunction(val) and
                     is_public_member(key) and
                     val.__doc__ is None):
                 for base in cls.__mro__[1:]:
```

**分类**：🔴 必须替换

**理由**：与 Group C 的 mutation 完全相同，是重复冗余，必须替换。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c78..510272a8cd 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -530,7 +530,10 @@ class InheritDocstrings(type):
                 for base in cls.__mro__[1:]:
                     super_method = getattr(base, key, None)
                     if super_method is not None:
-                        val.__doc__ = super_method.__doc__
+                        if isinstance(val, property):
+                            val.fget.__doc__ = super_method.__doc__
+                        else:
+                            val.__doc__ = super_method.__doc__
                         break
 
         super().__init__(name, bases, dct)
```

**变异语义**：开发者认为 property 的 docstring 存储在 `fget` 函数中，因此对 property 赋值 `val.fget.__doc__`。但 `property.__doc__` 并非动态从 `fget.__doc__` 派生——property 对象在创建时从 fget 复制一次 docstring，之后两者独立。赋值 `val.fget.__doc__` 不会影响 `property.__doc__`，因此 `Subclass.bar.__doc__` 仍为 `None`。这模拟了开发者对 property 内部结构的误解：以为需要"深入"到 fget 才能修改 docstring。代码看起来更"正确"（专门处理了 property 类型），实际上是错误的。F2P 中 `assert Subclass.bar.__doc__ == "BAR"` 失败，`assert Subclass.__call__.__doc__ == "FOO"` 通过。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c7..c897492aa 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -526,7 +526,8 @@ class InheritDocstrings(type):
         for key, val in dct.items():
             if ((inspect.isfunction(val) or inspect.isdatadescriptor(val)) and
                     is_public_member(key) and
-                    val.__doc__ is None):
+                    val.__doc__ is None and
+                    not isinstance(val, property)):
                 for base in cls.__mro__[1:]:
                     super_method = getattr(base, key, None)
                     if super_method is not None:
```

**分类**：🔴 必须替换

**理由**：`not isinstance(val, property)` 直接将 property 排除在继承逻辑之外，功能等价于还原 base_commit 的 bug（property docstring 无法继承）。虽然写法不同，但在所有 F2P 测试场景下行为与"直接还原"等价，属于功能等价冗余，必须替换。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/utils/misc.py b/astropy/utils/misc.py
index c3cbad4c78..1533fcd18c 100644
--- a/astropy/utils/misc.py
+++ b/astropy/utils/misc.py
@@ -530,7 +530,11 @@ class InheritDocstrings(type):
                 for base in cls.__mro__[1:]:
                     super_method = getattr(base, key, None)
                     if super_method is not None:
-                        val.__doc__ = super_method.__doc__
+                        if isinstance(val, property):
+                            val = property(val.fget, val.fset, val.fdel,
+                                           super_method.__doc__)
+                        else:
+                            val.__doc__ = super_method.__doc__
                         break
 
         super().__init__(name, bases, dct)
```

**变异语义**：开发者认为 property 对象是不可变的（实际上 `property.__doc__` 是可变的），因此通过 `property(fget, fset, fdel, doc)` 重新创建一个带有继承 docstring 的新 property 对象。然而，这个新对象赋值给局部变量 `val`，而 `val` 来自 `dct.items()` 的迭代——重新赋值 `val` 不会更新 `dct` 字典中的值，也不会更新类的属性。结果是类中的 property 对象仍然是原始的（没有 docstring），docstring 继承完全失败。函数的处理路径（`val.__doc__ = ...`）仍然正确。这模拟了开发者对 Python 引用语义的误解：以为修改 `val` 等同于修改容器中的对应元素。代码看起来非常合理（使用了 `property()` 构造函数的正确参数顺序），审查时难以发现。F2P 中 `assert Subclass.bar.__doc__ == "BAR"` 失败，`assert Subclass.__call__.__doc__ == "FOO"` 通过。

---

## 新设计 Mutation 说明

### Mutation B（isgetsetdescriptor）

基于对 `inspect` 模块 descriptor 检测函数族的深入分析：`isdatadescriptor`、`ismethoddescriptor`、`isgetsetdescriptor`、`ismemberdescriptor` 四个函数行为相似但适用对象不同。`isgetsetdescriptor` 专门用于 C 扩展定义的 getset（如 `datetime.year`、`int.real`），对 Python 层定义的 `property` 返回 `False`。选择此位置是因为它在核心判断条件中，且 `isgetsetdescriptor` 是真实存在的 `inspect` API，代码审查时不会触发"这个函数不存在"的警觉，而需要了解其与 `isdatadescriptor` 的区别才能发现错误。

### Mutation C（mro[2:]）

基于对 MRO 遍历逻辑的分析：`cls.__mro__[0]` 是当前类（`cls` 本身），`cls.__mro__[1]` 是直接父类。原代码从 `[1:]` 开始跳过当前类。开发者可能误以为应该跳过"当前类和直接父类"（类似于某些语言中 `super().super()` 的概念），从而写成 `[2:]`。这个 bug 在深度继承链中（方法定义在祖父类中）会偶然工作，使测试覆盖不完整时难以发现。

### Mutation D（fget.__doc__ 赋值）

基于对 `property` 内部结构的分析：property 对象包含 `fget`、`fset`、`fdel` 和 `__doc__` 四个属性。`property.__doc__` 在创建时从 `fget.__doc__` 复制，之后两者独立。开发者可能认为"property 的 docstring 存在 fget 里"，因此赋值 `fget.__doc__` 而非 `property.__doc__`。这是对 property 内部实现的误解，代码看起来更"精确"（区分处理了 property），实际上是错误的路径。

### Mutation E（property() 重建赋给局部变量）

基于对 Python 引用语义和 property 不可变性误解的分析：部分开发者认为 property 对象是不可变的（类似于 `str`、`int`），因此需要通过构造函数创建新对象。`property(fget, fset, fdel, doc)` 的参数顺序正确，代码看起来完全合理。但 `val = property(...)` 只是重新绑定了局部变量，不影响 `dct` 字典或类属性。实际上 `property.__doc__` 是可变的，直接赋值即可，无需重建。这是一个典型的"看起来更正确实际上错误"的开发者失误。
