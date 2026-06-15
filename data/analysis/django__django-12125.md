# django__django-12125

## 问题背景

当用户将 `django.db.models.Field` 的子类定义为另一个类的**内部类（inner class）**，并在 Django Model 中使用该字段时，`makemigrations` 生成的迁移文件中，内部类的路径不正确——使用了 `__name__`（只有类名，不含外层类名）而非 `__qualname__`（包含完整的 `Outer.Inner` 路径）。

例如 `Outer.Inner` 序列化为 `mymodule.Inner`，而正确结果应为 `mymodule.Outer.Inner`。

Golden patch 的修复：在 `TypeSerializer.serialize()` 的 `else` 分支中，将 `self.value.__name__` 改为 `self.value.__qualname__`，从而正确保留内部类的完整限定名。

## Golden Patch 语义分析

```diff
-                return "%s.%s" % (module, self.value.__name__), {"import %s" % module}
+                return "%s.%s" % (module, self.value.__qualname__), {"import %s" % module}
```

- `__name__`：仅为类自身的名称，如 `Inner`
- `__qualname__`：包含外层作用域的完整限定名，如 `Outer.Inner` 或 `WriterTests.NestedEnum`

修复正确性：Python 的 `__qualname__` 属性天然携带了嵌套关系，用点号 `.` 分隔，与 Python 模块访问路径一致。修复后，`import mymodule` + `mymodule.Outer.Inner` 可以正确定位到内部类。

## 调用链分析

```
serializer_factory(value)
    └─ isinstance(value, type) → TypeSerializer(value)
           └─ TypeSerializer.serialize()
                  ├─ special_cases: models.Model, type(None) → 直接返回
                  └─ hasattr(__module__) → module
                        ├─ module == builtins.__name__ → return __name__, set()
                        └─ else → return (module + __qualname__, {import module})  ← 此处为 golden fix
```

- `TypeSerializer` 仅被 `serializer_factory` 调用，当 `value` 是一个 `type`（类对象）时使用
- 上游：`MigrationWriter` → `OperationWriter` → `serializer_factory`
- 无下游调用（叶节点）

F2P 测试 `test_serialize_nested_class` 直接调用 `assertSerializedResultEqual(nested_cls, ...)` 验证类型序列化输出，覆盖 `NestedEnum`（IntegerChoices 子类）和 `NestedChoices`（TextChoices 子类），两者均为 `enum.Enum` 的间接子类。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | — | 新设计 | 原不存在，为 Group A（API契约）新增 |
| B | 🟡 语义浅层 | 保留 | `== → !=` 位于关键条件分支，floor(1/2)=0 无需替换 |
| C | 🔴 必须替换 | 替换 | 直接还原 golden patch（`__qualname__` → `__name__`），完全等价于原始 bug |
| D | — | 新设计 | 原不存在，为 Group D（状态初始化）新增 |
| E | — | 新设计 | 原不存在，为 Group E（测试期望对齐）新增 |

语义浅层共 1 个（B），替换其中最弱的 floor(1/2)=0 个：无需替换。

## 各组 Mutation 分析

### Group A — 新设计（API Specifications & Contracts）

**原 mutation**：不存在

**分类**：新设计

**最终 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index ead81c398a..5e20f23501 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -269,7 +269,10 @@ class TypeSerializer(BaseSerializer):
             if module == builtins.__name__:
                 return self.value.__name__, set()
             else:
-                return "%s.%s" % (module, self.value.__qualname__), {"import %s" % module}
+                qualname = self.value.__qualname__
+                if '.' in qualname:
+                    return "%s.%s" % (module, qualname), {"import %s" % module, "from %s import %s" % (module, qualname.rsplit('.', 1)[0])}
+                return "%s.%s" % (module, qualname), {"import %s" % module}
```

**变异语义**：对于内部类（`__qualname__` 含点），在生成正确的类路径名的同时，额外添加一个 `from module import OuterClass` 的导入语句。类名输出完全正确，但 import set 多了一个错误条目。

- **为何难以发现**：类名部分完全正确，仅 import 集合中多了一项。代码逻辑看起来像是"为内部类特别处理 import，确保外层类也能被导入"——符合直觉，但实际上错误（外层类不是模块，无法直接 import）。
- **F2P 测试失败原因**：`test_serialize_nested_class` 检查 `(name, imports)` 元组，期望 `{'import migrations.test_writer'}`，但 mutation 返回 `{'import migrations.test_writer', 'from migrations.test_writer import WriterTests'}`，断言失败。
- **通过的测试**：所有非内部类的类型序列化测试（`if '.' in qualname` 为 False，走原来路径）。

---

### Group B — 保留（Boundaries & Conditional Logic）

**原 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index ead81c398a..cc200a5772 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -266,7 +266,7 @@ class TypeSerializer(BaseSerializer):
                 return string, set(imports)
         if hasattr(self.value, "__module__"):
             module = self.value.__module__
-            if module == builtins.__name__:
+            if module != builtins.__name__:
                 return self.value.__name__, set()
             else:
                 return "%s.%s" % (module, self.value.__qualname__), {"import %s" % module}
```

**分类**：🟡 语义浅层（`== → !=`），仅1个，floor(1/2)=0，**保留**

**理由**：条件翻转后，非 builtin 类型（如 Django 类）会进入 `return self.value.__name__, set()` 分支——缺少模块前缀且无 import，而 builtin 类型反而走 `__qualname__` 分支。这会导致非 builtin 类型序列化结果缺失模块路径，F2P 测试的内部类（非 builtin）会得到 `WriterTests_NestedChoices`（仅用 `__name__`），而非预期的 `migrations.test_writer.WriterTests.NestedChoices`。修改位置处于核心分支节点，测试覆盖到位，属于语义浅层中质量较好的一个，按规则保留。

**变异语义**：所有非 builtin 类型序列化结果缺失模块前缀；所有 builtin 类型（int/str/bool 等）反而携带模块前缀 `builtins.int`，在 migration 文件中引入不可执行代码。

---

### Group C — 替换（Type & Data Shape）

**原 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index ead81c398a..7d226955d5 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -269,7 +269,7 @@ class TypeSerializer(BaseSerializer):
             if module == builtins.__name__:
                 return self.value.__name__, set()
             else:
-                return "%s.%s" % (module, self.value.__qualname__), {"import %s" % module}
+                return "%s.%s" % (module, self.value.__name__), {"import %s" % module}
```

**分类**：🔴 必须替换 — 原 mutation 直接将 `__qualname__` 还原为 `__name__`，与 golden patch 完全逆操作，等价于将 base_commit 中的原始 bug 重新引入，零额外信息量。

**最终 mutation（替换为新设计）**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index ead81c398a..0bcbe3c64b 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -269,6 +269,8 @@ class TypeSerializer(BaseSerializer):
             if module == builtins.__name__:
                 return self.value.__name__, set()
             else:
+                if issubclass(self.value, enum.Enum):
+                    return "%s.%s" % (module, self.value.__name__), {"import %s" % module}
                 return "%s.%s" % (module, self.value.__qualname__), {"import %s" % module}
```

**变异语义**：为 `enum.Enum` 的子类（包括 `IntegerChoices`、`TextChoices`、自定义 Enum）单独走 `__name__` 路径，非 Enum 类型仍用 `__qualname__`。

- **为何难以发现**：对于非 Enum 的内部类（如 `Outer.Inner(models.CharField)`），序列化仍然正确；只有 Enum 子类会出错。代码逻辑看起来像是"Enum 类型有特殊处理逻辑（因为有 EnumSerializer 处理实例），所以在 TypeSerializer 中特别分支处理"——这种直觉在 Django 代码中很常见。
- **F2P 测试失败原因**：`NestedEnum`（IntegerChoices 子类）和 `NestedChoices`（TextChoices 子类）均为 `enum.Enum` 子类，走 `__name__` 分支，返回 `migrations.test_writer.NestedEnum` 而非 `migrations.test_writer.WriterTests.NestedEnum`。
- **通过的测试**：所有非 Enum 类型的序列化测试。

---

### Group D — 新设计（I/O & Environment Handling / State Initialization）

**原 mutation**：不存在

**分类**：新设计

**最终 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index ead81c398a..8fb3136395 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -269,6 +269,8 @@ class TypeSerializer(BaseSerializer):
             if module == builtins.__name__:
                 return self.value.__name__, set()
             else:
+                if hasattr(self.value, '_value2member_map_'):
+                    return "%s.%s" % (module, self.value.__name__), {"import %s" % module}
                 return "%s.%s" % (module, self.value.__qualname__), {"import %s" % module}
```

**变异语义**：`_value2member_map_` 是 Python `enum.EnumMeta` 元类在初始化 Enum 类时自动生成的内部字典（将枚举值映射到枚举成员），是 Enum 类对象上的"状态属性"。利用这个内部属性检测 Enum 类，然后退回使用 `__name__`。

- **为何难以发现**：`_value2member_map_` 是 Python enum 内部实现细节，不会出现在 Django 官方文档中。代码读起来像是"检测对象是否有枚举成员映射（一种 duck-typing 的 Enum 检测方式）"，语义上比 `issubclass(self.value, enum.Enum)` 更隐晦。
- **F2P 测试失败原因**：`NestedEnum` 和 `NestedChoices` 都是 Enum 类，都具有 `_value2member_map_`，因此均使用 `__name__`，内部类路径错误。
- **通过的测试**：所有非 Enum 类型的序列化测试；非 Enum 的内部类测试（如果有）也能通过。

---

### Group E — 新设计（Test-expectation Alignment）

**原 mutation**：不存在

**分类**：新设计

**最终 mutation**：
```diff
diff --git a/django/db/migrations/serializer.py b/django/db/migrations/serializer.py
index ead81c398a..23c724da04 100644
--- a/django/db/migrations/serializer.py
+++ b/django/db/migrations/serializer.py
@@ -269,7 +269,10 @@ class TypeSerializer(BaseSerializer):
             if module == builtins.__name__:
                 return self.value.__name__, set()
             else:
-                return "%s.%s" % (module, self.value.__qualname__), {"import %s" % module}
+                qualname = self.value.__qualname__
+                if '.' in qualname:
+                    qualname = qualname.replace('.', '_')
+                return "%s.%s" % (module, qualname), {"import %s" % module}
```

**变异语义**：对于内部类，将 `__qualname__` 中的点号 `.` 替换为下划线 `_`，生成形如 `migrations.test_writer.WriterTests_NestedChoices` 的路径（而非正确的 `migrations.test_writer.WriterTests.NestedChoices`）。

- **为何难以发现**：替换后的路径看起来像一个合法的 Python 标识符，不含语法错误；生成的迁移文件能够"执行"（只是找不到该名称），而非直接报错。import 语句也完全正确（`import migrations.test_writer`）。代码逻辑像是"内部类名需要转义以兼容某些命名约定"。
- **F2P 测试失败原因**：精确字符串断言 `"migrations.test_writer.WriterTests.NestedChoices"` 与实际输出 `"migrations.test_writer.WriterTests_NestedChoices"` 不匹配。
- **通过的测试**：所有顶层类（`__qualname__` 不含 `.`）的序列化测试完全正确。

---

## 新设计 Mutation 说明

### Mutation A 设计依据

**代码分析**：`TypeSerializer.serialize()` 返回 `(name_str, imports_set)` 元组，调用方 `MigrationWriter` 会将 imports 写入迁移文件头部。对于内部类，正确的 import 应该是模块级别的 `import module`，因为 Python 不允许 `import module.ClassName`（ClassName 不是模块）。

**选择位置的理由**：保持名称部分正确、只破坏 import 部分，这样 F2P 测试会在第二个元素（import set）上精确失败，而非名称本身。这模拟了开发者"名称写对了但 import 路径理解有误"的真实错误。

**模拟的真实错误**：开发者可能认为访问 `module.Outer.Inner` 需要先 `from module import Outer`，类似于访问 `os.path.join` 时某些人会先 `from os import path`。

### Mutation C 设计依据

**代码分析**：Django 对 Enum 实例已有专用的 `EnumSerializer`（通过 `Serializer._registry` 路由），而 Enum 类（类型对象）则通过 `TypeSerializer`。开发者可能混淆"Enum 有专门的序列化器"意味着"在 TypeSerializer 中也需要特殊处理 Enum 类"，从而为 Enum 类型添加特殊分支，并沿用 `__name__` 而非 `__qualname__`。

**选择 `issubclass(self.value, enum.Enum)` 的理由**：这是最自然的 Enum 类检测方式，代码意图清晰，但逻辑上错误（Enum 类同样需要 `__qualname__` 来正确表达内部类）。精确针对 F2P 测试中的 `NestedEnum` 和 `NestedChoices`。

### Mutation D 设计依据

**代码分析**：`_value2member_map_` 是 `enum.EnumMeta.__new__` 在类创建（初始化）阶段构建的内部数据结构，属于 Enum 类的"初始化状态"。利用 `hasattr` 检测这个内部属性来识别 Enum 类，是一种不稳定的 duck-typing 方式，但在实际代码中能正常工作（因为所有标准 Enum 都有此属性）。

**模拟的真实错误**：开发者检查"这个类是否有枚举成员映射"来判断是否为 Enum 类，而非使用正式的 `issubclass` API。这类基于内部属性的判断在 Django 代码库中偶尔出现（如检测 `_meta`、`_registry` 等），因此读起来自然。

### Mutation E 设计依据

**代码分析**：`__qualname__` 用点号分隔嵌套层级，而 Python 文件系统路径和某些标识符规范使用下划线。开发者可能认为迁移文件中的路径需要是"合法 Python 标识符"（不含点），因此对内部类的 qualname 做了这个替换。

**选择 `.replace('.', '_')` 的理由**：替换后的字符串本身是合法 Python 标识符，不会在生成迁移文件时报语法错误，而是在运行迁移时才会报 `AttributeError: module has no attribute 'WriterTests_NestedChoices'`。这类错误难以在静态分析时发现，且 F2P 测试能精确捕获（字符串断言）。
