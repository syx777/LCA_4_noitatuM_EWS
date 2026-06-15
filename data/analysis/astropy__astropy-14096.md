# astropy__astropy-14096

## 问题背景

用户继承 `SkyCoord` 并添加了自定义属性（property），该属性内部访问了一个不存在的属性 `random_attr`。期望的错误信息应提示 `random_attr` 不存在，但实际报错却说 `prop`（自定义属性名）不存在。根本原因是 `SkyCoord.__getattr__` 在所有路径都未命中时，直接 `raise AttributeError(f"... has no attribute '{attr}'")`，其中 `attr` 是触发 `__getattr__` 的外层属性名（`prop`），而不是 property 内部实际缺失的属性（`random_attr`）。

## Golden Patch 语义分析

Golden patch 将 `__getattr__` 末尾的 `raise AttributeError(...)` 替换为 `return self.__getattribute__(attr)`。

关键语义：`self.__getattribute__(attr)` 会走 Python 标准的属性查找机制，能够找到并执行子类定义的 property descriptor。如果 property 内部抛出 `AttributeError`，该异常会原样向上传播，错误信息中包含的是 property 内部缺失的属性名（`random_attr`），而不是 `__getattr__` 收到的外层属性名（`prop`）。

这个修复的核心是：`__getattr__` 只在 `__getattribute__` 找不到属性时才被调用；但如果 `__getattribute__` 本身能找到 property（类级别定义），则执行 property，其内部的 `AttributeError` 应该透传，而不应被 `__getattr__` 的 `raise` 语句吞掉并替换为错误的错误信息。

## 调用链分析

```
c.prop  (SkyCoord subclass instance)
  → Python descriptor lookup: finds `prop` property on class
  → executes prop.fget(self)
    → self.random_attr
      → Python: not in __dict__, not a descriptor → calls __getattr__("random_attr")
        → __getattr__ checks _sky_coord_frame, frame_attributes, etc. → no match
        → BASE_COMMIT: raise AttributeError("...has no attribute 'prop'")  ← BUG (attr='prop' from outer call)
        → GOLDEN PATCH: return self.__getattribute__("random_attr")
          → __getattribute__ raises AttributeError("...has no attribute 'random_attr'")  ← CORRECT
```

关键：`__getattr__("prop")` 被调用时，`attr="prop"`；但 `__getattribute__("prop")` 找到了 property 并执行它；property 内部再次触发 `__getattr__("random_attr")`，此时 `attr="random_attr"`，最终 `__getattribute__("random_attr")` 抛出正确的错误。

相关函数：
- `SkyCoord.__getattr__`：核心修改点
- `SkyCoord._is_name`：判断 attr 是否为 frame 别名
- `SkyCoord.__setattr__` / `__delattr__`：镜像逻辑，使用相同的 `_sky_coord_frame` guard 和 `startswith("_")` 检查

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 与 C 完全相同的 diff，且效果等价于 base_commit 的原始 bug |
| B | 高质量（多行多函数） | 保留 | `and`→`or` 修改3处 guard 逻辑，跨 `__getattr__`/`__setattr__`/`__delattr__` |
| C | 必须替换 | 替换 | 与 A 完全相同的 diff，直接冗余 |
| D | 必须替换 | 替换 | 注释掉代码行，含明显人工痕迹（`# commented out`），不自然 |
| E | 必须替换 | 替换 | 就是 base_commit 的原始代码（`raise AttributeError(...)`），直接冗余 |

语义浅层共 0 个，必须替换共 4 个（A、C、D、E）。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302..6e8aa2ec7 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -895,7 +895,12 @@ class SkyCoord(ShapedLikeNDArray):
                 return self.transform_to(attr)
 
         # Call __getattribute__; this will give correct exception.
-        return self.__getattribute__(attr)
+        try:
+            return self.__getattribute__(attr)
+        except AttributeError:
+            raise AttributeError(
+                f"'{self.__class__.__name__}' object has no attribute '{attr}'"
+            )
 
     def __setattr__(self, attr, val):
         # This is to make anything available through __getattr__ immutable
```
**分类**：🔴 必须替换
**理由**：与 Group C 的 diff 完全相同（重复），且语义上等价于 base_commit 原始 bug——捕获了 `__getattribute__` 传播的内层 `AttributeError` 并替换为外层 `attr` 的错误信息，完全复现了 issue 中描述的错误行为。
**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302a..6c8d9f857a 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -895,7 +895,7 @@ class SkyCoord(ShapedLikeNDArray):
                 return self.transform_to(attr)
 
         # Call __getattribute__; this will give correct exception.
-        return self.__getattribute__(attr)
+        return object.__getattribute__(self, attr)
 
     def __setattr__(self, attr, val):
         # This is to make anything available through __getattr__ immutable
```
**变异语义**：将 `self.__getattribute__(attr)` 改为 `object.__getattribute__(self, attr)`。两者在大多数情况下等价，但关键区别在于：`self.__getattribute__` 会走完整的 MRO 查找（包括子类覆盖的 `__getattribute__`），而 `object.__getattribute__` 直接调用基类实现，跳过了子类可能覆盖的 `__getattribute__`。对于 `SkyCoord` 子类，如果子类覆盖了 `__getattribute__`，则此 mutation 会导致该覆盖被绕过。此外，`object.__getattribute__` 对于 property 的处理与 `self.__getattribute__` 在标准情况下相同，但当子类的 `__getattribute__` 做了额外处理时会有差异。该 mutation 代码风格合理，难以在代码审查中被发现。

---

### Group B — 保留
**原 mutation**：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302..322e8c4c5 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -886,7 +886,7 @@ class SkyCoord(ShapedLikeNDArray):
 
             # Some attributes might not fall in the above category but still
             # are available through self._sky_coord_frame.
-            if not attr.startswith("_") and hasattr(self._sky_coord_frame, attr):
+            if not attr.startswith("_") or hasattr(self._sky_coord_frame, attr):
                 return getattr(self._sky_coord_frame, attr)
 
             # Try to interpret as a new frame for transforming.
@@ -903,7 +903,7 @@ class SkyCoord(ShapedLikeNDArray):
             if self._is_name(attr):
                 raise AttributeError(f"'{attr}' is immutable")
 
-            if not attr.startswith("_") and hasattr(self._sky_coord_frame, attr):
+            if not attr.startswith("_") or hasattr(self._sky_coord_frame, attr):
                 setattr(self._sky_coord_frame, attr, val)
                 return
 
@@ -930,7 +930,7 @@ class SkyCoord(ShapedLikeNDArray):
             if self._is_name(attr):
                 raise AttributeError(f"'{attr}' is immutable")
 
-            if not attr.startswith("_") and hasattr(self._sky_coord_frame, attr):
+            if not attr.startswith("_") or hasattr(self._sky_coord_frame, attr):
                 delattr(self._sky_coord_frame, attr)
                 return
 
```
**分类**：🟢 保留
**理由**：跨三个方法（`__getattr__`、`__setattr__`、`__delattr__`）的多行修改，将 `and` 改为 `or` 改变了 guard 逻辑的语义契约。原条件 `not attr.startswith("_") and hasattr(...)` 意为"非私有属性且 frame 上存在"；改为 `or` 后变为"非私有属性或 frame 上存在"，导致所有以 `_` 开头的属性（只要 frame 上存在）也会被路由到 frame，破坏了私有属性的访问隔离。这是真实开发者可能犯的逻辑运算符误用错误，影响多个方法，难以被简单测试发现。
**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302..322e8c4c5 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -886,7 +886,7 @@ class SkyCoord(ShapedLikeNDArray):
 
             # Some attributes might not fall in the above category but still
             # are available through self._sky_coord_frame.
-            if not attr.startswith("_") and hasattr(self._sky_coord_frame, attr):
+            if not attr.startswith("_") or hasattr(self._sky_coord_frame, attr):
                 return getattr(self._sky_coord_frame, attr)
 
             # Try to interpret as a new frame for transforming.
@@ -903,7 +903,7 @@ class SkyCoord(ShapedLikeNDArray):
             if self._is_name(attr):
                 raise AttributeError(f"'{attr}' is immutable")
 
-            if not attr.startswith("_") and hasattr(self._sky_coord_frame, attr):
+            if not attr.startswith("_") or hasattr(self._sky_coord_frame, attr):
                 setattr(self._sky_coord_frame, attr, val)
                 return
 
@@ -930,7 +930,7 @@ class SkyCoord(ShapedLikeNDArray):
             if self._is_name(attr):
                 raise AttributeError(f"'{attr}' is immutable")
 
-            if not attr.startswith("_") and hasattr(self._sky_coord_frame, attr):
+            if not attr.startswith("_") or hasattr(self._sky_coord_frame, attr):
                 delattr(self._sky_coord_frame, attr)
                 return
 
```
**变异语义**：`and`→`or` 改变了私有属性的访问路由逻辑，使 `_` 开头的属性（如内部状态变量）在 frame 上存在时被错误地路由到 frame。通过典型的公共属性访问测试，只在访问私有属性或内部状态时失败。

---

### Group C — 替换
**原 mutation**：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302..6e8aa2ec7 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -895,7 +895,12 @@ class SkyCoord(ShapedLikeNDArray):
                 return self.transform_to(attr)
 
         # Call __getattribute__; this will give correct exception.
-        return self.__getattribute__(attr)
+        try:
+            return self.__getattribute__(attr)
+        except AttributeError:
+            raise AttributeError(
+                f"'{self.__class__.__name__}' object has no attribute '{attr}'"
+            )
 
     def __setattr__(self, attr, val):
         # This is to make anything available through __getattr__ immutable
```
**分类**：🔴 必须替换
**理由**：与 Group A 的 diff 完全相同，直接冗余。两组 mutation 内容一字不差，且语义上等价于 base_commit 的原始 bug。
**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302a..61da8416e5 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -882,7 +882,7 @@ class SkyCoord(ShapedLikeNDArray):
                 if attr in self.frame.frame_attributes:
                     return getattr(self.frame, attr)
                 else:
-                    return getattr(self, "_" + attr, None)
+                    return getattr(self, "_" + attr)
 
             # Some attributes might not fall in the above category but still
             # are available through self._sky_coord_frame.
```
**变异语义**：在 `__getattr__` 的 `frame_attributes` 分支中，将 `getattr(self, "_" + attr, None)` 改为 `getattr(self, "_" + attr)`（去掉默认值 `None`）。原代码对于不在当前 frame 中但属于 `frame_transform_graph.frame_attributes` 的全局 frame 属性（如 `obstime`、`equinox` 等），若未被设置则返回 `None`；去掉默认值后，访问这些未设置的可选 frame 属性会抛出 `AttributeError`，而不是返回 `None`。这模拟了开发者误以为"所有 frame 属性都应该有值"的逻辑误解，在访问可选 frame 属性时失败，通过大多数只使用已设置属性的测试。

---

### Group D — 替换
**原 mutation**：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302..f4b3a089c 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -895,7 +895,7 @@ class SkyCoord(ShapedLikeNDArray):
                 return self.transform_to(attr)
 
         # Call __getattribute__; this will give correct exception.
-        return self.__getattribute__(attr)
+        #         return self.__getattribute__(attr)
```
**分类**：🔴 必须替换
**理由**：注释掉了 `return self.__getattribute__(attr)` 这行代码，使函数在该路径下隐式返回 `None`。这是极不自然的 mutation：注释掉关键返回语句在代码审查中会立即引起注意，且含有明显人工痕迹（注释内容就是原始代码行）。
**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302a..2bb4d82f3f 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -863,7 +863,7 @@ class SkyCoord(ShapedLikeNDArray):
         Returns whether a string is one of the aliases for the frame.
         """
         return self.frame.name == string or (
-            isinstance(self.frame.name, list) and string in self.frame.name
+            isinstance(self.frame.name, list) or string in self.frame.name
         )
 
     def __getattr__(self, attr):
```
**变异语义**：在 `_is_name` 方法中，将 `isinstance(self.frame.name, list) and string in self.frame.name` 改为 `isinstance(self.frame.name, list) or string in self.frame.name`。原逻辑：当 frame.name 是列表时，检查 string 是否在列表中。改后逻辑：当 frame.name 是列表（无论 string 是否在其中），或者 string 在 frame.name 中（即使 frame.name 是字符串时也成立），都返回 True。实际效果：对于 frame.name 是列表的 frame（如某些有多个别名的 frame），`_is_name` 对任意 string 都返回 True，导致任何属性访问都被视为 frame 别名（`return self`），`__setattr__` 中任何属性都被视为不可变（`raise AttributeError`)。这模拟了开发者误用 `or` 代替 `and` 的常见错误，在使用多别名 frame 时才会暴露。

---

### Group E — 替换
**原 mutation**：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302..ab475f7d0 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -894,8 +894,10 @@ class SkyCoord(ShapedLikeNDArray):
             if frame_cls is not None and self.frame.is_transformable_to(frame_cls):
                 return self.transform_to(attr)
 
-        # Call __getattribute__; this will give correct exception.
-        return self.__getattribute__(attr)
+        # Fail
+        raise AttributeError(
+            f"'{self.__class__.__name__}' object has no attribute '{attr}'"
+        )
```
**分类**：🔴 必须替换
**理由**：这就是 base_commit 的原始代码（`raise AttributeError(...)`），只是把注释从 `# Fail` 改回来了。这是对 golden patch 的直接逆操作，与 base_commit 状态完全等价，属于直接冗余。
**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/coordinates/sky_coordinate.py b/astropy/coordinates/sky_coordinate.py
index a4257c302a..d18ebc4db8 100644
--- a/astropy/coordinates/sky_coordinate.py
+++ b/astropy/coordinates/sky_coordinate.py
@@ -894,6 +894,10 @@ class SkyCoord(ShapedLikeNDArray):
             if frame_cls is not None and self.frame.is_transformable_to(frame_cls):
                 return self.transform_to(attr)
 
+            raise AttributeError(
+                f"'{self.__class__.__name__}' object has no attribute '{attr}'"
+            )
+
         # Call __getattribute__; this will give correct exception.
         return self.__getattribute__(attr)
```
**变异语义**：在 `if "_sky_coord_frame" in self.__dict__:` 块的末尾（所有 frame 查找路径都未命中后）添加 `raise AttributeError(...)`。这使得对于完全初始化的 `SkyCoord` 实例，当 attr 不在 frame 路由逻辑中时，直接抛出以 `attr` 为名的错误，而不是走到块外的 `self.__getattribute__(attr)`。效果：子类 property 内部的 AttributeError 被吞掉，错误信息变为 property 名（外层 `attr`），完全复现原始 bug。而 `__getattribute__` 调用只在 `_sky_coord_frame` 不在 `__dict__` 时才被执行（初始化早期），成为死代码。这个 mutation 看起来像是开发者"明确了"错误路径，代码结构合理，难以被代码审查发现。

---

## 新设计 Mutation 说明

### Group A 替换说明
基于对 Python MRO 和 descriptor 协议的深层理解：`self.__getattribute__` 走完整 MRO，而 `object.__getattribute__` 直接调用基类。对于标准 `SkyCoord` 使用场景两者等价，但当子类覆盖 `__getattribute__` 时行为不同。这模拟了开发者在修复时"为了效率"直接调用 `object` 基类方法的误判。修改位置与 golden patch 相同（同一行），但语义不同。

### Group C 替换说明
基于对 `frame_attributes` 数据流的分析：全局 frame 属性（`obstime`、`equinox` 等）对特定 frame 是可选的，未设置时存储在 `self._<attr>` 中；若未设置，`getattr(self, "_" + attr, None)` 返回 `None`（表示"未设置"），这是 `SkyCoord` 的设计契约。去掉 `None` 默认值破坏了这个契约，使可选 frame 属性的访问从"返回 None"变为"抛出 AttributeError"。修改位置（line 885）与其他 mutation 不重叠。

### Group D 替换说明
基于对 `_is_name` 在 `__getattr__`/`__setattr__`/`__delattr__` 中的调用分析：`_is_name` 返回 True 时，`__getattr__` 返回 `self`，`__setattr__`/`__delattr__` 抛出 immutable 错误。将 `and` 改为 `or` 使得当 `frame.name` 是列表（多别名 frame）时，`_is_name` 对任意字符串返回 True，破坏了属性访问的整个分发逻辑。修改在 `_is_name` 函数内（line 866），与其他 mutation 位置完全不同，且影响多个方法。

### Group E 替换说明
基于对 `__getattr__` 控制流结构的深层理解：golden patch 将 `__getattribute__` 调用放在 `if "_sky_coord_frame" in self.__dict__:` 块外，作为兜底处理。本 mutation 在块内末尾添加 `raise`，使 `__getattribute__` 只在初始化早期（`_sky_coord_frame` 不在 `__dict__`）才被调用。这模拟了开发者"优化"错误路径时的误判——认为已初始化的对象应该在 frame 路由逻辑中完整处理所有情况，而不需要回退到 `__getattribute__`。代码结构合理，逻辑自洽，但实际上复现了原始 bug。
