# django__django-11815

## 问题背景

当使用带有 Django 翻译（`gettext_lazy`）的 Enum 对象作为模型字段的默认值时，自动生成的迁移文件使用了 Enum 对象的 **值（value）** 而非其 **名称（name）** 来构造枚举引用。例如，对于值为 `_('Good')` 的枚举成员，迁移文件生成 `Status('Good')` 而非 `Status['GOOD']`。一旦翻译语言切换，`'Good'` 变为其他语言的字符串，旧迁移文件就会抛出 `ValueError: 'Good' is not a valid Status`，因为枚举中不再存在该值。

Golden patch 将 `EnumSerializer.serialize()` 的输出从 `Module.EnumClass(value)` 构造器调用形式改为 `Module.EnumClass['name']` 字典访问形式，彻底绕开了值的序列化，直接使用枚举成员的不变名称。

## Golden Patch 语义分析

**修复前**：
```python
v_string, v_imports = serializer_factory(self.value.value).serialize()
imports = {'import %s' % module, *v_imports}
return "%s.%s(%s)" % (module, enum_class.__name__, v_string), imports
```
通过 `self.value.value` 获取枚举的底层值，然后序列化该值（若为 `Promise`，`serializer_factory` 会先将其 `str()` 转换），最终生成调用构造器的代码。

**修复后**：
```python
return (
    '%s.%s[%r]' % (module, enum_class.__name__, self.value.name),
    {'import %s' % module},
)
```
直接使用 `self.value.name`（枚举成员名，永远是普通 Python `str`，不受翻译影响），生成字典查找语法 `EnumClass['NAME']`。import 集合也简化为只需导入模块本身，不再需要序列化底层值所带来的额外 imports。

**核心语义变化**：访问路径从"按值构造"→"按名称查找"；同时完全解耦了枚举底层值的类型（可以是 `Promise`、`bytes`、`int` 等任何类型）对序列化结果的影响。

## 调用链分析

```
MigrationWriter.serialize(field)
  └─ serializer_factory(value)           # 根据 value 类型分派 Serializer
       ├─ isinstance check: enum.Enum    → EnumSerializer(value)
       │    └─ EnumSerializer.serialize()
       │         ├─ value.__class__      → enum_class
       │         ├─ enum_class.__module__ → module
       │         └─ return ('%s.%s[%r]' % ..., {'import %s' % module})
       └─ isinstance check: re.RegexFlag (IntFlag, int subclass)
            └─ 同 EnumSerializer（因为 IntFlag ⊂ enum.Enum）
                 后被 RegexSerializer 包装处理
                 flags = self.value.flags ^ re.compile('').flags  → re.RegexFlag 值
                 serializer_factory(flags) → EnumSerializer(re.RegexFlag.DOTALL)
```

被修改函数 `EnumSerializer.serialize()` 的上游：
- `serializer_factory()`：分派入口，根据类型选择 Serializer
- `RegexSerializer.serialize()`：调用 `serializer_factory(flags)` 序列化正则 flags

被修改函数的下游（修复后不再有）：
- ~~`serializer_factory(self.value.value)`~~：旧代码对枚举值再次序列化

关键类型：`Serializer._registry` 字典的遍历顺序决定哪个 serializer 被优先使用，`enum.Enum` 必须在 `(bool, int, ...)` 之前，否则 `IntEnum`/`IntFlag` 会被 `BaseSimpleSerializer` 捕获。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换（原缺失） | 替换（新设计） | 原 mutations.jsonl 中缺少 A 组，需新设计 |
| B | 必须替换（直接冗余） | 替换 | 与 C、D 完全相同的 diff，且是 golden patch 的直接逆操作 |
| C | 必须替换（直接冗余） | 替换 | 与 B、D 完全相同的 diff |
| D | 必须替换（直接冗余） | 替换 | 与 B、C 完全相同的 diff |
| E | 必须替换（原缺失） | 替换（新设计） | 原 mutations.jsonl 中缺少 E 组，需新设计 |

语义浅层共 0 个（原有3个是完全相同的必须替换），全部5个均重新设计。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：（缺失，无原始 diff）

**分类**：🔴 必须替换（原本不存在）

**理由**：原 mutations.jsonl 中没有 A 组 mutation，需全新设计。策略为 A2（Break API Signature/Convention）：修改 `Serializer._registry` 中 `enum.Enum: EnumSerializer` 的注册顺序，将其移至 `(bool, int, type(None), bytes, str, range): BaseSimpleSerializer` 之后。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index 8aa6a04935..c0e60138c1 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -286,12 +286,12 @@ class Serializer:
         tuple: TupleSerializer,
         dict: DictionarySerializer,
         models.Choices: ChoicesSerializer,
-        enum.Enum: EnumSerializer,
         datetime.datetime: DatetimeDatetimeSerializer,
         (datetime.date, datetime.timedelta, datetime.time): DateTimeSerializer,
         SettingsReference: SettingsReferenceSerializer,
         float: FloatSerializer,
         (bool, int, type(None), bytes, str, range): BaseSimpleSerializer,
+        enum.Enum: EnumSerializer,
         decimal.Decimal: DecimalSerializer,
         (functools.partial, functools.partialmethod): FunctoolsPartialSerializer,
         (types.FunctionType, types.BuiltinFunctionType, types.MethodType): FunctionTypeSerializer,
```

**变异语义**：`serializer_factory()` 遍历 `_registry` 时，`isinstance(IntEnum.B, (bool, int, ...))` 为 True，因此 `IntEnum` 和 `IntFlag`（`re.RegexFlag`）成员在到达 `enum.Enum: EnumSerializer` 之前已被 `BaseSimpleSerializer` 截获，序列化为 `repr(value)` 的普通整数形式。纯 `enum.Enum` 子类（`TextEnum`、`BinaryEnum`）不是 `int` 子类，不受影响，仍正确序列化。代码审查者看到的只是"注释里说 order-dependent"，移动一行位置看起来无关痛痒。F2P 测试中 `IntEnum` 和 `re.RegexFlag` 序列化测试均会失败。

---

### Group B — 替换

**原 mutation**：
```diff
-            '%s.%s[%r]' % (module, enum_class.__name__, self.value.name),
+            '%s.%s[%r]' % (module, enum_class.__name__, self.value.value),
```

**分类**：🔴 必须替换（直接冗余，且与 C/D 完全相同）

**理由**：B/C/D 三组的 diff 完全一致，且都是将 golden patch 的 `self.value.name` 直接还原为 `self.value.value`，即 golden patch 的逆操作。严重冗余，缺乏多样性。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index 8aa6a04935..a886d4763c 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -122,7 +122,7 @@ class EnumSerializer(BaseSerializer):
         module = enum_class.__module__
         return (
             '%s.%s[%r]' % (module, enum_class.__name__, self.value.name),
-            {'import %s' % module},
+            {'from %s import %s' % (module, enum_class.__name__)},
         )
```

**变异语义**：将 import 语句从 `import module` 形式改为 `from module import ClassName` 形式。生成的序列化字符串仍是 `migrations.test_writer.TextEnum['A']`（完整路径），但配套的 import 变成 `from migrations.test_writer import TextEnum`。这存在语义矛盾（生成的代码用全路径访问，但 import 只引入类名），且 import 集合的字符串内容与预期不符，导致 `assertSerializedResultEqual` 断言失败。开发者可能认为"应该精确 import 所需类而不是整个模块"，是真实可犯的 API 契约误解。

---

### Group C — 替换

**原 mutation**：（与 B/D 完全相同，此处略）

**分类**：🔴 必须替换（直接冗余）

**理由**：同上，与 B/D 完全重复。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index 8aa6a04935..9085680b1a 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -121,7 +121,7 @@ class EnumSerializer(BaseSerializer):
         enum_class = self.value.__class__
         module = enum_class.__module__
         return (
-            '%s.%s[%r]' % (module, enum_class.__name__, self.value.name),
+            '%s.%s.%s' % (module, enum_class.__name__, self.value.name),
             {'import %s' % module},
         )
```

**变异语义**：将 `EnumClass['NAME']`（字典访问，`__getitem__`）改为 `EnumClass.NAME`（属性访问，`__getattr__`）。两者在运行时效果相同（均返回同一枚举成员），但字符串表示形式不同：`TextEnum['A']` vs `TextEnum.A`。测试检查精确字符串相等，因此所有 F2P 枚举序列化测试均失败。此 mutation 极难通过代码审查发现，因为 `TextEnum.A` 是完全有效、惯用的 Python 枚举访问语法，比括号写法更简洁，看起来反而更"正确"。

---

### Group D — 替换

**原 mutation**：（与 B/C 完全相同，此处略）

**分类**：🔴 必须替换（直接冗余）

**理由**：同上，与 B/C 完全重复。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index 8aa6a04935..8725ba823e 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -122,7 +122,7 @@ class EnumSerializer(BaseSerializer):
         module = enum_class.__module__
         return (
             '%s.%s[%r]' % (module, enum_class.__name__, self.value.name),
-            {'import %s' % module},
+            {'import %s' % (module.split('.')[0] if '.' in module else module)},
         )
```

**变异语义**：对 multi-component 模块路径（含 `.` 的模块名），只保留顶层包名作为 import 目标。`migrations.test_writer.TextEnum` 的序列化字符串仍然正确，但 import 从 `import migrations.test_writer` 变为 `import migrations`（仅顶层包）。对单组件模块（如 `re`）不受影响，`re.RegexFlag` 的序列化仍然正确。这是一个仅在子模块枚举场景下暴露的环境处理 bug：开发者可能认为"导入顶层包即可，Python 会自动加载子模块"，这在某些情况下成立但对 `migrations.test_writer` 不成立。F2P 测试中 `TextEnum`/`BinaryEnum`/`IntEnum`（均在 `migrations.test_writer` 中）的 import 集合断言失败。

---

### Group E — 替换

**原 mutation**：（缺失，无原始 diff）

**分类**：🔴 必须替换（原本不存在）

**理由**：原 mutations.jsonl 中没有 E 组 mutation，需全新设计。策略针对 `RegexSerializer` 中的类型剥离问题。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index 8aa6a04935..695647e792 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -223,7 +223,7 @@ class RegexSerializer(BaseSerializer):
         # Turn off default implicit flags (e.g. re.U) because regexes with the
         # same implicit and explicit flags aren't equal.
         flags = self.value.flags ^ re.compile('').flags
-        regex_flags, flag_imports = serializer_factory(flags).serialize()
+        regex_flags, flag_imports = serializer_factory(int(flags)).serialize()
         imports = {'import re', *pattern_imports, *flag_imports}
         args = [regex_pattern]
         if flags:
```

**变异语义**：`flags` 变量是 `re.RegexFlag`（IntFlag，enum 子类）类型。`serializer_factory(flags)` 会通过 `EnumSerializer` 将其序列化为 `re.RegexFlag['DOTALL']`（golden patch 的新行为）。加入 `int(flags)` 后，将 `re.RegexFlag` 类型信息剥离，变为普通 `int` 类型，`serializer_factory(16)` 返回 `('16', set())`，生成的序列化结果变回 `flags=16`（旧行为类似）。这个 mutation 仅影响 `RegexSerializer`，不影响普通枚举序列化。开发者可能认为"flags 应该作为整数传递以确保一致性"，`int()` 转换看起来是安全的保护性操作，极难被 code review 发现。F2P 测试 `test_serialize_class_based_validators` 中 `re.RegexFlag['DOTALL']` 断言会失败。

---

## 新设计 Mutation 说明

### Group A (A2: Break API Signature/Convention)

**代码分析基础**：`Serializer._registry` 字典注释明确写道 "Some of these are order-dependent."，说明遍历顺序对行为有影响。`serializer_factory` 逐条 `isinstance` 检查，第一个匹配的 serializer 胜出。`IntEnum` 同时是 `int` 和 `enum.Enum` 的子类，当前顺序让 `enum.Enum` 优先；将 `enum.Enum` 移到 `(bool, int, ...)` 之后会反转优先级。

**选择位置的理由**：修改位置（注册表顺序）与 `EnumSerializer.serialize()` 函数本身（golden patch 的修改位置）完全不同，属于跨函数/架构级别的变异，难以在审查 `serialize()` 函数时发现。

**模拟的真实错误**：重构注册表（如整理顺序、添加新类型）时，开发者误认为 `enum.Enum` 和 `(bool, int, ...)` 的顺序对 `int` 子类的枚举无影响。

### Group E (D1/E1: Type Stripping in I/O Path)

**代码分析基础**：`RegexSerializer.serialize()` 中 `flags = self.value.flags ^ re.compile('').flags` 返回 `re.RegexFlag` 类型（Python 3.7+ 中 compiled regex 的 `.flags` 是 `re.RegexFlag` 枚举）。这个类型信息在 golden patch 后被 `EnumSerializer` 正确处理为名称访问。

**选择位置的理由**：mutation 位于 `RegexSerializer`，而非 `EnumSerializer`，测试表现与枚举测试分离（只有 `test_serialize_class_based_validators` 失败），使 bug 更局部、更难溯源。

**模拟的真实错误**：开发者在处理 flags 时主动调用 `int()` 以"确保是整数类型"，不知道 `re.RegexFlag` 的枚举类型信息在序列化时是有意义的。
