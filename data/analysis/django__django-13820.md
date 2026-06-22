# django__django-13820

## 问题背景

Django 的迁移加载器（`MigrationLoader.load_disk()`）在检测命名空间包（namespace package）时使用了过于宽泛的条件：`if getattr(module, '__file__', None) is None`。这个检查的本意是排除没有 `__init__.py` 的目录（即命名空间包），但 Python 文档明确指出 `__file__` 属性是可选的——在"冻结环境"（frozen environment，如 PyInstaller 打包的程序）中，普通包（regular package）也可能没有 `__file__` 属性。

golden patch 的修复：将条件改为同时检查 `__file__` 为 None **且** `module.__path__` 不是 `list` 类型。Python 规范规定命名空间包使用自定义可迭代类型（如 CPython 中的 `_NamespacePath`）作为 `__path__`，而普通包使用 `list`。因此，冻结环境中的普通包（`__file__` 为 None 但 `__path__` 为 `list`）会被正确识别为普通包，不再被误判为命名空间包。

## Golden Patch 语义分析

修复核心：**从单条件检查升级为双条件联合检查**。

- 旧逻辑：`__file__ is None` → 误排除冻结环境中的普通包
- 新逻辑：`__file__ is None AND not isinstance(__path__, list)` → 只排除真正的命名空间包

正确性依据：Python 导入系统规范指出，命名空间包的 `__path__` 是特殊的可迭代对象（不是 `list`），而普通包的 `__path__` 是标准的 `list`。无论 `__file__` 是否存在，`isinstance(__path__, list)` 是区分两者的可靠方式。

## 调用链分析

```
MigrationLoader.__init__()
  └── build_graph()
        └── load_disk()                          ← 修改点
              ├── import_module(module_name)     ← 导入迁移模块
              ├── [namespace check]              ← golden patch 修改了此处
              ├── pkgutil.iter_modules(...)      ← 枚举迁移文件
              └── disk_migrations[...] = ...    ← 存储迁移对象
        └── [构建依赖图]
        └── [validate_consistency()]
```

`load_disk()` 的结果（`disk_migrations`, `migrated_apps`, `unmigrated_apps`）被 `build_graph()` 中的后续步骤直接使用。如果一个应用被错误地加入 `unmigrated_apps`，其迁移文件将完全不被加载，导致迁移图不完整。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 与 B、E 完全相同的 diff，三个 mutation 一模一样 |
| B | 🔴 必须替换 | 替换 | 与 A、E 完全相同的 diff，三个 mutation 一模一样 |
| C | 🔴 必须替换（不存在） | 替换 | mutations.jsonl 中只有 A/B/E 三组，C/D 缺失，需要新建 |
| D | 🔴 必须替换（不存在） | 替换 | 同上 |
| E | 🔴 必须替换 | 替换 | 与 A、B 完全相同的 diff，三个 mutation 一模一样 |

语义浅层共 0 个。所有5个 mutation 均需替换（原有3个完全相同，另外2组缺失）。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index eb370164f3..d2935c0a63 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -96,7 +96,7 @@ class MigrationLoader:
                 if (
-                    getattr(module, '__file__', None) is None and
+                    getattr(module, '__file__', None) is None or
                     not isinstance(module.__path__, list)
                 ):
```

**分类**：🔴 必须替换

**理由**：三个 mutation (A/B/E) 完全相同。该 diff 将 `and` 改为 `or`，使得任何 `__file__` 为 None 或 `__path__` 不是 list 的包都被排除，这实际上是对 golden patch 逻辑的一种"过度限制"版本。此外，三组 mutation 完全重复，严重浪费了多样性。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index eb370164f3..53f4cb2f3b 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -97,7 +97,7 @@ class MigrationLoader:
                 # https://docs.python.org/3/reference/import.html#namespace-packages
                 if (
                     getattr(module, '__file__', None) is None and
-                    not isinstance(module.__path__, list)
+                    not getattr(getattr(module, '__spec__', None), 'has_location', False)
                 ):
                     self.unmigrated_apps.add(app_config.label)
                     continue
```

**变异语义**：用 `__spec__.has_location` 替代 `isinstance(__path__, list)` 作为判断依据。看起来很合理——"如果模块没有文件位置信息，则视为命名空间包"。但测试中显式设置了 `has_location=False` 来模拟冻结环境，该 mutation 会将冻结包误判为命名空间包。对于普通包（`has_location=True`），行为与修复后一致，因此简单测试无法发现此问题。

---

### Group B — 替换

**原 mutation**：（与 Group A 相同，略）

**分类**：🔴 必须替换

**理由**：与 A、E 完全相同。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index eb370164f3..7200368db8 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -97,7 +97,7 @@ class MigrationLoader:
                 # https://docs.python.org/3/reference/import.html#namespace-packages
                 if (
                     getattr(module, '__file__', None) is None and
-                    not isinstance(module.__path__, list)
+                    not getattr(getattr(module, '__spec__', None), 'origin', None)
                 ):
                     self.unmigrated_apps.add(app_config.label)
                     continue
```

**变异语义**：用 `__spec__.origin` 替代 `isinstance(__path__, list)`。看起来合理——"如果模块的 spec 没有 origin（即来源路径），则视为命名空间包"。测试中设置了 `__spec__.origin = None`，使该条件成立。而普通包 `__spec__.origin` 是实际文件路径（非 None），因此不受影响。此 mutation 模拟了开发者参考 `__spec__` 文档时选错属性的错误。

---

### Group C — 替换（新建）

**原 mutation**：（缺失）

**分类**：🔴 必须替换（不存在，需新建）

**最终 mutation**：
```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index eb370164f3..08a8fcb845 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -97,7 +97,7 @@ class MigrationLoader:
                 # https://docs.python.org/3/reference/import.html#namespace-packages
                 if (
                     getattr(module, '__file__', None) is None and
-                    not isinstance(module.__path__, list)
+                    not isinstance(module.__path__, tuple)
                 ):
                     self.unmigrated_apps.add(app_config.label)
                     continue
```

**变异语义**：将 `isinstance(__path__, list)` 改为 `isinstance(__path__, tuple)`。对于冻结环境的普通包，`__path__` 是 `list` 而非 `tuple`，因此 `not isinstance(list, tuple)` = True，整个条件为 True，包被误归为未迁移。对命名空间包，`__path__` 是 `_NamespacePath`（不是 tuple），也被排除。对普通包（有 `__file__`），条件第一项为 False，不受影响。此 mutation 模拟了开发者误以为应检查 `tuple` 类型的错误（可能因为 `sys.path` 是 list 而产生混淆）。

---

### Group D — 替换（新建）

**原 mutation**：（缺失）

**分类**：🔴 必须替换（不存在，需新建）

**最终 mutation**：
```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index eb370164f3..91340b029c 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -97,7 +97,8 @@ class MigrationLoader:
                 # https://docs.python.org/3/reference/import.html#namespace-packages
                 if (
                     getattr(module, '__file__', None) is None and
-                    not isinstance(module.__path__, list)
+                    not isinstance(module.__path__, list) or
+                    not getattr(getattr(module, '__spec__', None), 'has_location', True)
                 ):
                     self.unmigrated_apps.add(app_config.label)
                     continue
```

**变异语义**：在原有命名空间包检查的基础上，通过 OR 运算添加了一个额外条件：`not __spec__.has_location`。看起来像是"防御性编程"——如果 spec 明确说明模块没有位置信息，也视为命名空间包。但测试中设置了 `has_location=False`，导致冻结包被误判。对普通包（`has_location=True`），额外条件为 False，不受影响。此 mutation 模拟了开发者试图"更全面地检测命名空间包"但逻辑出错的情形。

---

### Group E — 替换

**原 mutation**：（与 Group A 相同，略）

**分类**：🔴 必须替换

**理由**：与 A、B 完全相同。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index eb370164f3..d806ab36cc 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -98,7 +98,7 @@ class MigrationLoader:
                 if (
                     getattr(module, '__file__', None) is None and
                     not isinstance(module.__path__, list)
-                ):
+                ) or not getattr(module, '__file__', None):
                     self.unmigrated_apps.add(app_config.label)
                     continue
                 # Force a reload if it's already loaded (tests need this)
```

**变异语义**：在命名空间包检查语句的关闭括号后追加 `or not getattr(module, '__file__', None)`。这看起来像是开发者试图"确保只处理有文件位置的模块"，但实际上这等价于重新引入了旧的 `__file__ is None` 单一检查（因为 `not getattr(module, '__file__', None)` 当 `__file__` 为 None 时为 True，整个 OR 条件立即成立）。冻结环境中的普通包（`__file__` 为 None，`__path__` 为 list）会被该 OR 子句捕获，从而被错误排除。对普通包（有 `__file__`）无影响。

---

## 新设计 Mutation 说明

### Group A（替换 `isinstance` 检查为 `__spec__.has_location`）

基于对 Python 导入系统文档的分析：`__spec__` 对象的 `has_location` 属性在 PEP 451 中定义，表示模块是否有可定位的物理文件。F2P 测试显式设置 `has_location=False` 来模拟冻结环境。该 mutation 模拟了开发者在修复时参考了 `__spec__` 文档但选择了错误的属性（`has_location` 代替 `isinstance(__path__, list)`）——一个真实的、语义上"合理"的开发错误。

### Group B（替换 `isinstance` 检查为 `__spec__.origin`）

`__spec__.origin` 表示模块的"来源路径"，通常是 `__file__` 的别名。F2P 测试设置 `origin=None`。该 mutation 模拟开发者误以为"origin 为 None 等价于 __path__ 不是 list"——一个同样参考了 __spec__ 但选错属性的错误。与 Group A 的区别：A 用 has_location（布尔值），B 用 origin（路径字符串）。

### Group C（`list` → `tuple`）

基于对 Python `__path__` 类型的分析：标准普通包使用 `list`，冻结环境下通常也是 `list`。开发者可能因为混淆了 `sys.path`（也是 list）或想用更严格的类型而改成 `tuple`。这个单字符替换会导致所有冻结环境的普通包（list `__path__`）都被误判为命名空间包。

### Group D（添加 OR + `has_location` 检查）

这个 mutation 涉及多行修改（添加一整行），模拟了开发者出于"防御性"目的添加额外检查但引入 OR 逻辑错误的情形。表面上看 `or not has_location` 是一个合理的补充，但由于 OR 运算符优先级，它与前面的 AND 条件组合方式改变了整体逻辑语义。

### Group E（追加 `or not __file__`）

这个 mutation 在括号外追加 OR 条件，视觉上不明显（看起来只是括号位置的微调）。它有效地恢复了旧版本的错误行为，但以一种"补充防护"的面貌出现，很难在代码审查中被发现。
