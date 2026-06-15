# astropy__astropy-7606

## 问题背景

当用户对 `UnrecognizedUnit`（通过 `parse_strict='silent'` 创建的无法识别的单位）与 `None` 进行相等比较时，会抛出 `TypeError: None is not a valid Unit`，而不是返回 `False`（或 `NotImplemented`）。

原始代码中 `UnrecognizedUnit.__eq__` 直接调用 `Unit(other, parse_strict='silent')`，没有任何异常处理。当 `other=None` 时，`Unit.__call__` 内部检测到 `s is None` 并抛出 `TypeError`，该异常直接传播到用户代码。

golden patch 修复了两处：
1. `UnitBase.__eq__`：将 `return False` 改为 `return NotImplemented`（当捕获到 ValueError/UnitsError/TypeError 时），使比较协议更正确（允许对方类型尝试反射比较）。
2. `UnrecognizedUnit.__eq__`：添加 try/except 块捕获 `(ValueError, UnitsError, TypeError)`，返回 `NotImplemented`；同时将 `isinstance(other, UnrecognizedUnit)` 改为 `isinstance(other, type(self))`（更通用，支持子类）。

## Golden Patch 语义分析

核心修复逻辑：

**修复1（UnitBase.__eq__）**：原来捕获异常后返回 `False`，这违反了 Python 比较协议——当一个对象无法与另一个对象比较时，应返回 `NotImplemented` 而不是 `False`，以允许对方对象尝试反射操作（`other.__eq__(self)`）。返回 `False` 会"吞掉"比较机会，使 `some_obj == unit` 总是 `False` 即使 `some_obj.__eq__(unit)` 本应返回 `True`。

**修复2（UnrecognizedUnit.__eq__）**：原来完全没有异常处理，导致 `Unit(None)` 抛出的 `TypeError` 直接传播。修复后用 try/except 包裹，将无法解析的输入统一返回 `NotImplemented`。`isinstance(other, type(self))` 比 `isinstance(other, UnrecognizedUnit)` 更正确，因为如果有 `UnrecognizedUnit` 的子类，前者能正确区分。

**关键语义**：`unit == None` 的正确行为是：
- `unit.__eq__(None)` 捕获 TypeError → 返回 `NotImplemented`
- Python 尝试 `None.__eq__(unit)` → 返回 `NotImplemented`
- Python 回退到身份比较 → `unit is not None` → False
- 最终 `unit == None` 为 `False`

`unit != None` 的正确行为：
- `unit.__ne__(None)` → `not (self == other)` → `not False` → `True`
- 注意：`self == other` 是 Python 表达式，经过完整比较协议，最终得到 `False`，而非直接调用 `__eq__`

## 调用链分析

```
unit == None
  └─ UnrecognizedUnit.__eq__(unit, None)
       └─ Unit(None, parse_strict='silent')  [_UnitMetaClass.__call__]
            └─ raises TypeError("None is not a valid Unit")
       └─ except (ValueError, UnitsError, TypeError): return NotImplemented
  └─ None.__eq__(unit) → NotImplemented
  └─ identity fallback: unit is None → False

unit != None
  └─ UnrecognizedUnit.__ne__(unit, None)
       └─ not (self == other)  [Python operator ==, not direct __eq__ call]
            └─ unit.__eq__(None) → NotImplemented
            └─ None.__eq__(unit) → NotImplemented
            └─ identity: False
       └─ not False → True

unit not in (None, u.m)
  └─ unit == None → False (via above)
  └─ unit == u.m
       └─ UnrecognizedUnit.__eq__(unit, u.m)
            └─ Unit(u.m) → u.m (UnitBase instance)
            └─ isinstance(u.m, type(unit)) → isinstance(u.m, UnrecognizedUnit) → False
            └─ return False
  └─ unit not in (None, u.m) → True
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 替换 | 仅删除 TypeError，是5组中语义最浅、位置最孤立的变化，与E重复 |
| B | 语义浅层 | 保留 | `not isinstance` 改变了核心相等逻辑，破坏 `unit == "FOO"` 测试，位置关键 |
| C | 必须替换 | 替换 | 与D功能完全相同（仅空行差异），直接还原 golden patch |
| D | 必须替换 | 替换 | 直接还原 golden patch（去除整个 try/except 块） |
| E | 必须替换 | 替换 | 在两处同时删除 TypeError，不自然，等价于A的双倍版本 |

语义浅层共 2 个（A、B），替换其中最弱的 floor(2/2) = 1 个：[A]

必须替换：C、D、E（3个）

总替换：A、C、D、E（4个）

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046..decfeb0bb 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -1712,7 +1712,7 @@ class UnrecognizedUnit(IrreducibleUnit):
     def __eq__(self, other):
         try:
             other = Unit(other, parse_strict='silent')
-        except (ValueError, UnitsError, TypeError):
+        except (ValueError, UnitsError):
             return NotImplemented
 
         return isinstance(other, type(self)) and self.name == other.name
```

**分类**：🟡 语义浅层（替换）

**理由**：仅删除了 except 元组中的 `TypeError`，是最简单的单符号删除。修改位置虽在关键函数内，但与 Group E 的第二处修改完全相同，高度冗余。作为5组中语义最浅且与E重复的一组，选择替换。

**最终 mutation**：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046d..7e7f49dc9d 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -1718,7 +1718,7 @@ class UnrecognizedUnit(IrreducibleUnit):
         return isinstance(other, type(self)) and self.name == other.name
 
     def __ne__(self, other):
-        return not (self == other)
+        return not self.__eq__(other)
 
     def is_equivalent(self, other, equivalencies=None):
         self._normalize_equivalencies(equivalencies)
```

**变异语义**：将 `__ne__` 中的 `self == other`（Python 比较运算符，走完整比较协议）替换为 `self.__eq__(other)`（直接调用方法，绕过协议）。当 `__eq__` 返回 `NotImplemented` 时，`not NotImplemented` → `False`（因为 `NotImplemented` 是 truthy），导致 `unit != None` 返回 `False` 而非 `True`。代码看起来只是"等价重写"，实则破坏了 Python 比较协议中 `NotImplemented` 的传播机制。通过 `unit == "FOO"`、`unit != u.m` 等普通测试，只在 `unit != None` 场景下失败。

---

### Group B — 保留

**原 mutation**：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046..5b1790eba 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -1715,7 +1715,7 @@ class UnrecognizedUnit(IrreducibleUnit):
         except (ValueError, UnitsError, TypeError):
             return NotImplemented
 
-        return isinstance(other, type(self)) and self.name == other.name
+        return not isinstance(other, type(self)) and self.name == other.name
 
     def __ne__(self, other):
         return not (self == other)
```

**分类**：🟡 语义浅层（保留）

**理由**：在 `isinstance` 前添加 `not`，反转了类型检查逻辑，使得只有当 `other` 不是 `UnrecognizedUnit` 时才可能相等。这改变了 `__eq__` 的核心语义契约，破坏 `unit == "FOO"` 这一关键测试。虽然是单字修改，但位于最关键的逻辑节点（返回值判断），且模拟了真实的逻辑误解（"不是同类才相等"）。保留。

**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046..5b1790eba 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -1715,7 +1715,7 @@ class UnrecognizedUnit(IrreducibleUnit):
         except (ValueError, UnitsError, TypeError):
             return NotImplemented
 
-        return isinstance(other, type(self)) and self.name == other.name
+        return not isinstance(other, type(self)) and self.name == other.name
 
     def __ne__(self, other):
         return not (self == other)
```

**变异语义**：`isinstance` 前的 `not` 使相等条件反转：只有 `other` 不是 `UnrecognizedUnit` 时才进一步比较名称。`unit == unit2`（两个相同名称的 UnrecognizedUnit）→ `not True and ...` → `False`，即相同单位被判定为不相等。通过 `unit != None` 等 None 相关测试，只在 `unit == "FOO"` 和同类单位比较时失败。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046..2e0abf0f8 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -1710,13 +1710,8 @@ class UnrecognizedUnit(IrreducibleUnit):
         _unrecognized_operator
 
     def __eq__(self, other):
-        try:
-            other = Unit(other, parse_strict='silent')
-        except (ValueError, UnitsError, TypeError):
-            return NotImplemented
-
+        other = Unit(other, parse_strict='silent')
         return isinstance(other, type(self)) and self.name == other.name
-
     def __ne__(self, other):
         return not (self == other)
```

**分类**：🔴 必须替换

**理由**：与 Group D 功能完全相同（仅多删了一个空行），都是直接还原 golden patch 的核心修复（去除整个 try/except 块）。两个 mutation 在所有测试场景下行为完全一致，高度冗余，必须替换其中一个。

**最终 mutation**：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046d..3eca8fa2e6 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -1712,8 +1712,10 @@ class UnrecognizedUnit(IrreducibleUnit):
     def __eq__(self, other):
         try:
             other = Unit(other, parse_strict='silent')
-        except (ValueError, UnitsError, TypeError):
+        except (ValueError, UnitsError):
             return NotImplemented
+        except TypeError:
+            raise
 
         return isinstance(other, type(self)) and self.name == other.name
 
```

**变异语义**：将 except 子句拆分：`ValueError` 和 `UnitsError` 仍然被捕获并返回 `NotImplemented`，但 `TypeError` 被单独捕获后重新抛出（`raise`）。这模拟了开发者认为"TypeError 是编程错误，应该传播而非静默处理"的思维。`unit == None` 时，`Unit(None)` 抛出 `TypeError`，被 `except TypeError: raise` 重新抛出，导致 `unit != None` 和 `unit not in (None, u.m)` 都抛出异常。代码逻辑清晰合理，代码审查中难以发现问题。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046..941ffe081 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -1710,11 +1710,7 @@ class UnrecognizedUnit(IrreducibleUnit):
         _unrecognized_operator
 
     def __eq__(self, other):
-        try:
-            other = Unit(other, parse_strict='silent')
-        except (ValueError, UnitsError, TypeError):
-            return NotImplemented
-
+        other = Unit(other, parse_strict='silent')
         return isinstance(other, type(self)) and self.name == other.name
 
     def __ne__(self, other):
```

**分类**：🔴 必须替换

**理由**：直接还原 golden patch 的核心修复（去除 try/except 块），是 patch 的逆操作。与 Group C 功能完全等价，必须替换。

**最终 mutation**：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046d..0400a7578a 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -1718,7 +1718,7 @@ class UnrecognizedUnit(IrreducibleUnit):
         return isinstance(other, type(self)) and self.name == other.name
 
     def __ne__(self, other):
-        return not (self == other)
+        return self == other
 
     def is_equivalent(self, other, equivalencies=None):
         self._normalize_equivalencies(equivalencies)
```

**变异语义**：`__ne__` 中去掉 `not`，使不等运算符返回与相等运算符相同的结果。`unit != unit2`（相同单位）→ `unit == unit2` → `True`，即相同单位被判定为"不相等"（返回 True）。`unit != unit3`（不同单位）→ `unit == unit3` → `False`，即不同单位被判定为"相等"（返回 False）。`unit != None` → `unit == None` → `False`（通过 Python 协议）→ 返回 `False`，而测试断言 `unit != None` 为 True，直接失败。这是一个经典的逻辑取反遗漏错误，看起来像是代码重构时不小心删除了 `not`。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046..acca1e7fb 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -727,7 +727,7 @@ class UnitBase(metaclass=InheritDocstrings):
 
         try:
             other = Unit(other, parse_strict='silent')
-        except (ValueError, UnitsError, TypeError):
+        except (ValueError, UnitsError):
             return NotImplemented
 
         # Other is Unit-like, but the test below requires it is a UnitBase
@@ -1712,7 +1712,7 @@ class UnrecognizedUnit(IrreducibleUnit):
     def __eq__(self, other):
         try:
             other = Unit(other, parse_strict='silent')
-        except (ValueError, UnitsError, TypeError):
+        except (ValueError, UnitsError):
             return NotImplemented
 
         return isinstance(other, type(self)) and self.name == other.name
```

**分类**：🔴 必须替换

**理由**：在两个不同位置做了完全相同的修改（都是删除 `TypeError`），其中第二处与 Group A 原始 mutation 完全相同，高度冗余。两处同时修改显得不自然，不符合真实开发者的错误模式。

**最终 mutation**：
```diff
diff --git a/astropy/units/core.py b/astropy/units/core.py
index 533dac046d..d62ff74941 100644
--- a/astropy/units/core.py
+++ b/astropy/units/core.py
@@ -741,7 +741,7 @@ class UnitBase(metaclass=InheritDocstrings):
             return False
 
     def __ne__(self, other):
-        return not (self == other)
+        return not self.__eq__(other)
 
     def __le__(self, other):
         scale = self._to(Unit(other))
@@ -1718,7 +1718,7 @@ class UnrecognizedUnit(IrreducibleUnit):
         return isinstance(other, type(self)) and self.name == other.name
 
     def __ne__(self, other):
-        return not (self == other)
+        return UnitBase.__ne__(self, other)
 
     def is_equivalent(self, other, equivalencies=None):
         self._normalize_equivalencies(equivalencies)
```

**变异语义**：多位置协调变异：
1. `UnitBase.__ne__` 改为 `return not self.__eq__(other)`（直接调用 `__eq__`，绕过 Python 比较协议）
2. `UnrecognizedUnit.__ne__` 改为 `return UnitBase.__ne__(self, other)`（委托给父类的 `__ne__`）

组合效果：`unit != None` → `UnitBase.__ne__(unit, None)` → `not unit.__eq__(None)` → `not NotImplemented` → `False`（因为 `NotImplemented` 是 truthy）→ 返回 `False`，而测试断言 `unit != None` 为 True，直接失败。

这模拟了一个真实的重构错误：开发者将 `UnrecognizedUnit.__ne__` 的"重复"代码替换为父类委托，同时将父类 `__ne__` 改为更"直接"的实现，未意识到 `self == other`（Python 协议）和 `self.__eq__(other)`（直接调用）在处理 `NotImplemented` 时的本质区别。代码看起来合理，逐行审查难以发现问题。

---

## 新设计 Mutation 说明

### Group A（新）：`__ne__` 直接调用 `__eq__`

**代码分析基础**：`UnrecognizedUnit.__ne__` 当前实现为 `return not (self == other)`，其中 `self == other` 是 Python 运算符表达式，会触发完整的比较协议（先调 `__eq__`，若返回 `NotImplemented` 则尝试反射，最后回退到身份比较）。如果改为 `return not self.__eq__(other)`，则直接调用方法，`NotImplemented` 不会触发协议，而是被 `not` 运算符当作 truthy 值取反为 `False`。

**选择位置的理由**：`__ne__` 是 `__eq__` 的直接配套方法，开发者在实现时容易混淆"调用运算符"和"调用方法"的区别。这是一个在代码审查中极难发现的细节差异，因为两种写法看起来完全等价。

**模拟的真实错误**：开发者在重写 `__ne__` 时，认为 `not self.__eq__(other)` 与 `not (self == other)` 等价（在大多数情况下确实如此），但忽略了 `NotImplemented` 的特殊处理。

### Group C（新）：拆分 except 子句，重新抛出 TypeError

**代码分析基础**：golden patch 在 `UnrecognizedUnit.__eq__` 中捕获 `(ValueError, UnitsError, TypeError)` 并返回 `NotImplemented`。将 `TypeError` 单独处理并重新抛出，模拟了开发者认为"TypeError 是类型错误，不应该被静默处理"的思维。

**选择位置的理由**：`except` 子句拆分是常见的重构模式，看起来像是代码质量改进（"明确区分不同异常类型"）。这种修改在代码审查中会被认为是合理的，甚至是更好的做法。

**模拟的真实错误**：开发者认为 `TypeError` 表示调用者传入了错误类型（编程错误），应该传播而非静默处理；而 `ValueError` 和 `UnitsError` 才是"正常"的解析失败。

### Group D（新）：`__ne__` 缺少 `not`

**代码分析基础**：`return not (self == other)` 中的 `not` 是关键逻辑取反。去掉 `not` 后，`__ne__` 返回与 `__eq__` 相同的值，完全颠倒了不等语义。

**选择位置的理由**：`not` 的遗漏是最经典的逻辑错误，但在单行实现的 `__ne__` 中，这个错误会让整个不等比较语义颠倒，影响所有使用 `!=` 的测试场景。

**模拟的真实错误**：代码重构时不小心删除了 `not`，或者误将 `return not (self == other)` 写成 `return self == other`。

### Group E（新）：跨函数协调变异

**代码分析基础**：`UnitBase.__ne__` 和 `UnrecognizedUnit.__ne__` 都实现为 `return not (self == other)`。如果将 `UnitBase.__ne__` 改为使用直接 `__eq__` 调用，再将 `UnrecognizedUnit.__ne__` 委托给父类，则两处变化协同产生 bug。

**选择位置的理由**：多文件/多函数的协调变异更难被简单测试检测，因为每处单独看都"合理"。`UnitBase.__ne__` 的改变对大多数普通单位比较没有影响（因为 `UnitBase.__eq__` 返回 `False` 而非 `NotImplemented`），只有通过 `UnrecognizedUnit.__ne__` 委托时才暴露问题。

**模拟的真实错误**：开发者在重构时"消除重复代码"，将子类中看起来相同的 `__ne__` 实现替换为父类委托，同时将父类实现改为更"直接"的形式，未意识到这两个看似等价的变化组合后会破坏比较协议。
