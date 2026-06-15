# astropy__astropy-13977

## 问题背景

当用户实现 `astropy.units.Quantity` 的鸭子类型（duck type）时，如果左操作数是 `Quantity` 而右操作数是鸭子类型，且两者单位不同（如 `1*u.m + DuckArray(1*u.mm)`），`Quantity.__array_ufunc__` 会在尝试单位转换时抛出 `ValueError`，而不是返回 `NotImplemented`。按照 Python 数据模型，当操作数无法处理时应返回 `NotImplemented`，以便 Python 触发右操作数的反射操作（`__radd__` 等），让鸭子类型有机会处理该运算。

golden patch 将 `__array_ufunc__` 的主体逻辑包裹在 `try/except (TypeError, ValueError)` 块中：若捕获到异常，则检查所有输入/输出中是否有自定义 `__array_ufunc__` 的类型——若有，则返回 `NotImplemented`；若全是标准类型（`None`、`np.ndarray.__array_ufunc__`、`Quantity.__array_ufunc__`），则重新抛出异常。

## Golden Patch 语义分析

核心修复逻辑：

1. **将主逻辑包裹在 try/except 中**：捕获 `TypeError` 和 `ValueError`，这两种异常都可能在单位转换失败时出现（`TypeError` 来自 `converters_and_unit`，`ValueError` 来自 `_condition_arg`）。

2. **构建 `ignored_ufunc` 集合**：包含 `None`（无 `__array_ufunc__`）、`np.ndarray.__array_ufunc__`（标准数组）、`type(self).__array_ufunc__`（当前 Quantity 类或子类）。

3. **关键判断**：`if not all(getattr(type(io), "__array_ufunc__", None) in ignored_ufunc for io in inputs_and_outputs)` — 若存在任何输入或输出的类型拥有自定义 `__array_ufunc__`（不在 ignored 集合中），说明有鸭子类型参与，应返回 `NotImplemented` 让它处理；否则说明异常是真实错误，重新抛出。

4. **`inputs_and_outputs = inputs + out_normalized`**：同时检查输入和输出，确保 `out` 参数中的鸭子类型也被考虑。

5. **`type(self).__array_ufunc__`**：使用动态类型而非硬编码 `Quantity`，支持 Quantity 子类正确工作。

## 调用链分析

```
用户代码: ufunc(quantity, duck_quantity)
  └─> Quantity.__array_ufunc__(function, method, *inputs, **kwargs)
        ├─> converters_and_unit(function, method, *inputs)   [可能抛 TypeError/ValueError]
        │     └─> UFUNC_HELPERS[function](function, *units)
        │           └─> _condition_arg(value)                [抛 ValueError: "Value not scalar compatible..."]
        ├─> check_output(out, unit, inputs, function)        [可能抛 TypeError]
        ├─> self._to_own_unit(kwargs["initial"], ...)
        ├─> [arrays 构建循环] converter(input_)              [可能抛 ValueError]
        └─> super().__array_ufunc__(function, method, *arrays, **kwargs)
              └─> self._result_as_quantity(result, unit, out)
```

异常路径：当 `duck_quantity` 作为输入时，`converters_and_unit` 或 `converter(input_)` 在尝试将鸭子类型转换为数值时失败，触发 except 块。except 块通过检查 `inputs_and_outputs` 中各类型的 `__array_ufunc__` 来决定是返回 `NotImplemented` 还是重新抛出异常。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 替换 | `inputs + out_normalized` → `inputs`，修改位置孤立，是5个中最弱的浅层 mutation |
| B | 语义浅层 | 保留 | `not all` → `all` 逻辑反转，位于关键控制节点，能模拟真实逻辑误解 |
| C | 必须替换 | 替换 | `if False and not all(...)` 是明显人工痕迹，永假条件代码审查即可发现 |
| D | 必须替换 | 替换 | 注释 "Always raise the exception, don't check..." 直接暴露 bug 意图，极不自然 |
| E | 必须替换 | 替换 | 与 Group B 完全相同的 diff（重复 mutation），必须替换为不同位置的变异 |

语义浅层共 2 个（A、B），替换其中最弱的 floor(2/2) = 1 个：[A]

总替换：A、C、D、E，共 4 个。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb..d14bc8ec8 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -682,7 +682,7 @@ class Quantity(np.ndarray):
 
             return self._result_as_quantity(result, unit, out)
 
-        except (TypeError, ValueError) as e:
+        except TypeError as e:
             out_normalized = kwargs.get("out", tuple())
             inputs_and_outputs = inputs + out_normalized
             ignored_ufunc = (
```

**分类**：🟡 语义浅层（替换，最弱）

**理由**：单行修改，只去掉 `ValueError`。虽然能捕获部分场景，但 `ValueError` 来自 `_condition_arg`（单位转换时鸭子类型不是数值），是 F2P 测试的核心场景。该 mutation 的修改位置（except 子句）较孤立，不在任何复杂控制流上，且在同组中是最容易被测试发现的（只要测试用例触发 ValueError 路径）。同组内 B 的修改位置更关键（核心判断逻辑），故 A 是最弱的语义浅层 mutation，选择替换。

**最终 mutation**：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb5..9b92054339 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -684,7 +684,7 @@ class Quantity(np.ndarray):
 
         except (TypeError, ValueError) as e:
             out_normalized = kwargs.get("out", tuple())
-            inputs_and_outputs = inputs + out_normalized
+            inputs_and_outputs = inputs
             ignored_ufunc = (
                 None,
                 np.ndarray.__array_ufunc__,
```

**变异语义**：将 `inputs_and_outputs` 从"输入+输出"改为仅"输入"。当鸭子类型只出现在 `out` 参数中（而不在 `inputs` 中）时，该鸭子类型的自定义 `__array_ufunc__` 不会被检测到，导致应该返回 `NotImplemented` 的情况错误地重新抛出异常。对于大多数只检查输入的简单测试会通过，只在使用 `out` 参数且 `out` 中包含鸭子类型时失败（F2P 测试中的 `test_full` 系列使用 `out` 参数）。代码看起来自然，像是开发者忘记了 `out` 也需要检查。

---

### Group B — 保留

**原 mutation**：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb..72690277d 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -690,7 +690,7 @@ class Quantity(np.ndarray):
                 np.ndarray.__array_ufunc__,
                 type(self).__array_ufunc__,
             )
-            if not all(
+            if all(
                 getattr(type(io), "__array_ufunc__", None) in ignored_ufunc
                 for io in inputs_and_outputs
             ):
```

**分类**：🟡 语义浅层（保留）

**理由**：`not all` → `all` 完全反转了判断逻辑：原逻辑是"若存在非标准类型则返回 NotImplemented"，变异后变为"若全部是标准类型才返回 NotImplemented，否则重新抛出"。这个修改位于 except 块的核心判断节点，直接决定 NotImplemented vs raise 的选择，能模拟开发者在理解"何时应该放弃处理"时犯的逻辑误解。所有不涉及鸭子类型的测试不会触发 except 块，不会失败；只有 F2P 测试中涉及鸭子类型的场景才会失败。保留。

**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb..72690277d 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -690,7 +690,7 @@ class Quantity(np.ndarray):
                 np.ndarray.__array_ufunc__,
                 type(self).__array_ufunc__,
             )
-            if not all(
+            if all(
                 getattr(type(io), "__array_ufunc__", None) in ignored_ufunc
                 for io in inputs_and_outputs
             ):
```

**变异语义**：逻辑完全反转。原本"有自定义 `__array_ufunc__` → NotImplemented，全标准 → raise"，变为"全标准 → NotImplemented，有自定义 → raise"。鸭子类型场景下会错误地抛出异常而非返回 NotImplemented，导致 F2P 测试中所有 `test_full` 系列失败。同时，对于全标准类型（如 `q1 + {"a": 1}`）会错误返回 NotImplemented 而非抛出 TypeError，导致 `test_non_number_type` 等测试也失败。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb..2cb5c77e3 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -690,7 +690,7 @@ class Quantity(np.ndarray):
                 np.ndarray.__array_ufunc__,
                 type(self).__array_ufunc__,
             )
-            if not all(
+            if False and not all(
                 getattr(type(io), "__array_ufunc__", None) in ignored_ufunc
                 for io in inputs_and_outputs
             ):
```

**分类**：🔴 必须替换

**理由**：`if False and not all(...)` 是永假条件，使整个 if 分支永远不执行，等价于直接 `raise e`。这是明显的人工痕迹——`False and` 前缀在真实代码中极为罕见，代码审查者一眼即可发现这是故意引入的 bug。

**最终 mutation**：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb5..df66256d23 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -688,7 +688,7 @@ class Quantity(np.ndarray):
             ignored_ufunc = (
                 None,
                 np.ndarray.__array_ufunc__,
-                type(self).__array_ufunc__,
+                Quantity.__array_ufunc__,
             )
             if not all(
                 getattr(type(io), "__array_ufunc__", None) in ignored_ufunc
```

**变异语义**：将 `ignored_ufunc` 中的 `type(self).__array_ufunc__` 替换为硬编码的 `Quantity.__array_ufunc__`。对于标准 `Quantity` 实例，两者相同，行为不变。但当 `self` 是 `Quantity` 子类且该子类**重写了** `__array_ufunc__` 时，`type(self).__array_ufunc__` 是子类方法，与 `Quantity.__array_ufunc__` 不同，导致子类自身的 `__array_ufunc__` 不在 `ignored_ufunc` 中，`not all(...)` 为 True，错误地返回 `NotImplemented` 而非抛出真实异常。代码看起来完全合理（直接引用类名是常见写法），只在 Quantity 子类覆盖 `__array_ufunc__` 的特定场景下失败。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb..a2a4d33bc 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -690,13 +690,8 @@ class Quantity(np.ndarray):
                 np.ndarray.__array_ufunc__,
                 type(self).__array_ufunc__,
             )
-            if not all(
-                getattr(type(io), "__array_ufunc__", None) in ignored_ufunc
-                for io in inputs_and_outputs
-            ):
-                return NotImplemented
-            else:
-                raise e
+            # Always raise the exception, don't check for other __array_ufunc__ implementations
+            raise e
```

**分类**：🔴 必须替换

**理由**：注释 `# Always raise the exception, don't check for other __array_ufunc__ implementations` 直接说明了 bug 的意图，是极不自然的人工痕迹。真实开发者不会写这样的注释来描述一个"错误"的行为。代码审查者立即会发现这是故意引入的问题。

**最终 mutation**：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb5..e8ff250618 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -691,7 +691,7 @@ class Quantity(np.ndarray):
                 type(self).__array_ufunc__,
             )
             if not all(
-                getattr(type(io), "__array_ufunc__", None) in ignored_ufunc
+                getattr(io, "__array_ufunc__", None) in ignored_ufunc
                 for io in inputs_and_outputs
             ):
                 return NotImplemented
```

**变异语义**：将 `getattr(type(io), "__array_ufunc__", None)` 改为 `getattr(io, "__array_ufunc__", None)`——从获取**类**的 `__array_ufunc__` 改为获取**实例**的 `__array_ufunc__`。对于实例，`getattr(io, "__array_ufunc__", None)` 返回的是**绑定方法**（bound method），而 `ignored_ufunc` 中存储的是**未绑定函数**（`np.ndarray.__array_ufunc__`、`type(self).__array_ufunc__`）。绑定方法 `!=` 未绑定函数，因此 `in ignored_ufunc` 对所有正常对象（包括 Quantity 自身、ndarray）都返回 False，导致 `not all(...)` 永远为 True，`__array_ufunc__` 在任何异常情况下都返回 `NotImplemented` 而非抛出真实错误。这使得 `test_non_number_type`（期望 TypeError）、`test_incompatible_units`（期望 UnitConversionError）等测试失败。代码改动极小（去掉 `type()`），看起来像是开发者混淆了"类属性"和"实例属性"的访问方式。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb..72690277d 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -690,7 +690,7 @@ class Quantity(np.ndarray):
                 np.ndarray.__array_ufunc__,
                 type(self).__array_ufunc__,
             )
-            if not all(
+            if all(
                 getattr(type(io), "__array_ufunc__", None) in ignored_ufunc
                 for io in inputs_and_outputs
             ):
```

**分类**：🔴 必须替换

**理由**：与 Group B 的 diff **完全相同**（同一文件、同一行、同一修改），是重复 mutation。5 个 mutation 中有两个完全相同，违反了 mutation 多样性原则，必须替换为不同位置的变异。

**最终 mutation**：
```diff
diff --git a/astropy/units/quantity.py b/astropy/units/quantity.py
index 21134c0fb5..4ba3af7358 100644
--- a/astropy/units/quantity.py
+++ b/astropy/units/quantity.py
@@ -683,7 +683,7 @@ class Quantity(np.ndarray):
             return self._result_as_quantity(result, unit, out)
 
         except (TypeError, ValueError) as e:
-            out_normalized = kwargs.get("out", tuple())
+            out_normalized = tuple()
             inputs_and_outputs = inputs + out_normalized
             ignored_ufunc = (
                 None,
```

**变异语义**：将 `out_normalized = kwargs.get("out", tuple())` 改为 `out_normalized = tuple()`，使 `out_normalized` 永远为空元组，忽略实际的 `out` 参数。效果与 Group A 的替换 mutation 类似但实现方式不同：A 是在 `inputs_and_outputs` 赋值时丢弃 `out_normalized`，E 是在 `out_normalized` 赋值时就丢弃了 `out` 的值。当鸭子类型只出现在 `out` 参数中时，其自定义 `__array_ufunc__` 不会被检测到，导致应该返回 `NotImplemented` 的情况错误地重新抛出异常。代码看起来像是开发者认为 `out` 参数不需要检查（只用空元组作为默认值），只在 F2P 测试中使用 `out` 参数的 `test_full` 系列场景下失败。

---

## 新设计 Mutation 说明

### Group A 替换（`inputs_and_outputs = inputs`）

**代码分析基础**：golden patch 中 `inputs_and_outputs = inputs + out_normalized` 的目的是确保 `out` 参数中的鸭子类型也被检测。`out_normalized` 来自 `kwargs.get("out", tuple())`，是用户传入的输出数组元组。

**选择位置的理由**：这行代码是 `inputs_and_outputs` 的构建，直接决定哪些对象参与 `__array_ufunc__` 检测。删除 `out_normalized` 看起来像开发者只考虑了"输入"而忘记"输出"也可以是鸭子类型，是一种常见的接口契约理解错误。

**模拟的真实开发者错误**：开发者在设计时只考虑了"输入中有鸭子类型"的场景，忘记了 `out` 参数也可以包含鸭子类型，导致 `out=duck_array` 场景下行为错误。

### Group C 替换（`Quantity.__array_ufunc__` 替代 `type(self).__array_ufunc__`）

**代码分析基础**：`ignored_ufunc` 元组中包含 `type(self).__array_ufunc__` 是为了支持 Quantity 子类——当 `self` 是子类时，`type(self)` 是子类，其 `__array_ufunc__` 可能与 `Quantity.__array_ufunc__` 不同（若子类重写了该方法）。

**选择位置的理由**：`Quantity.__array_ufunc__` 是一个看起来完全合理的写法，在大多数情况下与 `type(self).__array_ufunc__` 等价（当 self 是 Quantity 实例而非子类时）。只有在测试中使用了 Quantity 子类时才会暴露差异。

**模拟的真实开发者错误**：开发者在编写时使用了硬编码类名而非动态类型，是一种常见的"忘记考虑继承"的错误。

### Group D 替换（`getattr(io, ...)` 替代 `getattr(type(io), ...)`）

**代码分析基础**：正确的写法是 `getattr(type(io), "__array_ufunc__", None)` 获取类上的未绑定方法，与 `ignored_ufunc` 中的未绑定方法比较。改为 `getattr(io, ...)` 获取实例的绑定方法，绑定方法与未绑定函数在 Python 中是不同对象，`==` 和 `in` 比较均为 False。

**选择位置的理由**：`getattr(io, ...)` vs `getattr(type(io), ...)` 是一个极其微妙的差异，需要理解 Python 描述符协议和绑定方法机制才能发现。这个改动只有一个字符的差异（去掉 `type()`），代码风格上完全合理。

**模拟的真实开发者错误**：开发者混淆了"通过实例访问方法"和"通过类访问方法"的语义差异，在 Python 中这两种方式对普通属性等价，但对方法（描述符）则不同，是一种需要深入理解 Python 对象模型才能避免的错误。

### Group E 替换（`out_normalized = tuple()`）

**代码分析基础**：与 Group A 替换类似，但从不同角度引入相同的语义错误。`out_normalized` 应该从 `kwargs["out"]` 获取，使输出数组也参与检测。

**选择位置的理由**：`out_normalized = tuple()` 看起来像是在说"没有输出需要检查"，是一个合理但错误的假设。与 A 的区别在于：A 保留了 `out_normalized` 的赋值但不使用，E 直接让它为空。两个 mutation 虽然效果相似，但修改位置不同（一个在 `inputs_and_outputs` 赋值行，一个在 `out_normalized` 赋值行），在 diff 层面是不同的变异。
