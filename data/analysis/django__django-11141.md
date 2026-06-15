# django__django-11141

## 问题背景

Python 3 中，没有 `__init__.py` 的目录会被 Python 隐式视为**命名空间包（namespace package）**。命名空间包没有 `__file__` 属性，但有 `__path__` 属性。

Django 的 `MigrationLoader.load_disk()` 在加载 migrations 目录时，存在对 `__file__` 属性的检查：
```python
if getattr(module, '__file__', None) is None:
    self.unmigrated_apps.add(app_config.label)
    continue
```
此检查本意是识别"空命名空间包"，但实际上也错误拒绝了**含有迁移文件的命名空间包**（即有 migrations 但没有 `__init__.py` 的目录）。

此外，原始代码总是无条件执行 `self.migrated_apps.add(app_config.label)`，不管 migration 目录是否真的含有迁移文件。

## Golden Patch 语义分析

Golden patch 做了两处关键修复：

1. **删除 `__file__` 检查**（第 87–91 行原始代码）：
   - 删除了 `if getattr(module, '__file__', None) is None: unmigrated_apps.add(); continue`
   - 允许命名空间包继续执行后续的 migration 发现逻辑

2. **条件化 migrated/unmigrated 分类**（原第 99 行 → 新的 if/else）：
   - 从无条件 `self.migrated_apps.add(app_config.label)`
   - 改为：`if migration_names or self.ignore_no_migrations: migrated_apps.add() else: unmigrated_apps.add()`
   - 使空 migrations 目录（无迁移文件的命名空间包）正确归入 `unmigrated_apps`

修复的核心语义：**一个包有没有 `__file__` 不再决定是否加载其迁移文件；是否含有实际的迁移文件才是关键判断依据。**

## 调用链分析

```
MigrationLoader.__init__()
  └─ build_graph()
       ├─ load_disk()           ← 核心修复所在
       │    ├─ migrations_module(app_label) → (module_name, explicit)
       │    ├─ import_module(module_name) → module
       │    ├─ 检查 __path__ (非包则 unmigrated)
       │    ├─ reload(module) if was_loaded
       │    ├─ pkgutil.iter_modules(module.__path__) → migration_names
       │    ├─ 条件分类: migrated_apps / unmigrated_apps  ← 关键修复
       │    └─ import_module(migration_path) → 加载各 migration 文件
       ├─ graph.add_node(key, migration)  ← 受 D 组 mutation 影响
       ├─ add_internal_dependencies()
       ├─ add_external_dependencies()
       │    └─ check_key()      ← unmigrated_apps / migrated_apps 的消费者
       └─ graph.validate_consistency()

MigrationLoader.project_state()
  └─ graph.make_state(real_apps=list(unmigrated_apps))
```

数据流关键点：
- `unmigrated_apps` 和 `migrated_apps` 由 `load_disk()` 填充
- `check_key()` 消费这两个集合决定依赖处理方式
- `graph.nodes` 决定 `forwards_plan()` 的返回结果

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失（新设计） | 新设计 | mutations.jsonl 中无 A 组记录 |
| B | 🔴 必须替换 | 替换 | 原 B 组删除了 `__path__` 检查，会导致 AttributeError 崩溃 |
| C | 缺失（新设计） | 新设计 | mutations.jsonl 中无 C 组记录 |
| D | 缺失（新设计） | 新设计 | mutations.jsonl 中无 D 组记录 |
| E | 🔴 必须替换 | 替换 | 原 E 组直接还原了被 golden patch 删除的 `__file__` 检查，属于功能等价冗余 |

原始 mutations.jsonl 中仅有 B 和 E 两组，均属必须替换。A、C、D 三组需全新设计。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：无（mutations.jsonl 中不存在）

**分类**：新设计（路径 B 模式，为每组设计全新 mutation）

**最终 mutation**：

```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index 6be8048299..5ed1b4e399 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -88,6 +88,10 @@ class MigrationLoader:
                 if not hasattr(module, '__path__'):
                     self.unmigrated_apps.add(app_config.label)
                     continue
+                # Namespace packages lack a concrete origin; skip them.
+                if getattr(getattr(module, '__spec__', None), 'origin', None) is None:
+                    self.unmigrated_apps.add(app_config.label)
+                    continue
                 # Force a reload if it's already loaded (tests need this)
                 if was_loaded:
                     reload(module)
```

**变异语义**：此 mutation 在 `__path__` 检查之后、reload 之前，新增了一个基于 `__spec__.origin` 的检查。对于命名空间包，`module.__spec__.origin` 为 `None`（命名空间包没有具体的文件来源），因此会被错误地归入 `unmigrated_apps`。对于普通包，`__spec__.origin` 指向 `__init__.py` 路径，不为 `None`，正常通过。此 mutation 使用 Python 3 的导入系统 API（`__spec__`），看起来是对旧版 `__file__` 检查的"现代化改写"，但语义上同样拒绝了命名空间包。代码审查者难以发现——注释声称"Namespace packages lack a concrete origin"读起来是合理的技术说明。

**策略**：A1（改变代码对模块属性的处理语义，使用不同但相关的 API `__spec__` 替代 `__file__`）

---

### Group B — 替换

**原 mutation**：
```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index 6be8048299..d23d24c43d 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -85,9 +85,6 @@ class MigrationLoader:
                 raise
             else:
                 # Module is not a package (e.g. migrations.py).
-                if not hasattr(module, '__path__'):
-                    self.unmigrated_apps.add(app_config.label)
-                    continue
                 # Force a reload if it's already loaded (tests need this)
                 if was_loaded:
                     reload(module)
```

**分类**：🔴 必须替换

**理由**：删除了 `if not hasattr(module, '__path__')` 检查块。当 migrations 模块是一个 `.py` 文件（非包）时，后续代码会访问 `module.__path__`，导致 `AttributeError: module 'xxx' has no attribute '__path__'` 崩溃。这是一个明显的运行时错误，而非难以发现的语义 bug。另外，此 mutation 不会影响命名空间包（它有 `__path__`），因此不会破坏 F2P 测试 `test_loading_namespace_package`。

**最终 mutation**：

```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index 6be8048299..0e4561ee81 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -96,7 +96,10 @@ class MigrationLoader:
                 if not is_pkg and name[0] not in '_~'
             }
             if migration_names or self.ignore_no_migrations:
-                self.migrated_apps.add(app_config.label)
+                if getattr(module, '__file__', None):
+                    self.migrated_apps.add(app_config.label)
+                else:
+                    self.unmigrated_apps.add(app_config.label)
             else:
                 self.unmigrated_apps.add(app_config.label)
             # Load migrations
```

**变异语义**：在 `if migration_names or self.ignore_no_migrations:` 分支内，新增了一个嵌套的 `__file__` 检查，决定具体归入哪个集合。对于含有迁移文件的命名空间包：`migration_names` 非空 → 外层条件为 True → 内层检查 `__file__` → 无 `__file__` → 错误归入 `unmigrated_apps`。对于普通包：`__file__` 存在 → 正确归入 `migrated_apps`。此 mutation 位于 golden patch 新增的条件逻辑内部，看起来像是"对命名空间包的进一步处理细化"，但实际上将有迁移文件的命名空间包错误分类。

**策略**：B1（在边界条件逻辑内嵌套额外检查，改变命名空间包的分类结果）

---

### Group C — 替换（新设计）

**原 mutation**：无（mutations.jsonl 中不存在）

**分类**：新设计

**最终 mutation**：

```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index 6be8048299..69dbd2db18 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -93,7 +93,7 @@ class MigrationLoader:
                     reload(module)
             migration_names = {
                 name for _, name, is_pkg in pkgutil.iter_modules(module.__path__)
-                if not is_pkg and name[0] not in '_~'
+                if not is_pkg and name[0] not in '_~' and hasattr(module, '__file__')
             }
             if migration_names or self.ignore_no_migrations:
                 self.migrated_apps.add(app_config.label)
```

**变异语义**：在 `migration_names` 集合推导式的过滤条件中，添加了 `and hasattr(module, '__file__')` 作为额外的过滤器。由于这是对父模块（migration 包本身）的检查，对于命名空间包，`hasattr(module, '__file__')` 为 False，导致所有 migration 文件名都被过滤掉，`migration_names` 变成空集。空集加上 `ignore_no_migrations=False`，该 app 被归入 `unmigrated_apps`，migration 不被加载。对于普通包，`hasattr(module, '__file__')` 为 True，条件等价于原始逻辑，正常工作。此 mutation 极其隐蔽：`hasattr(module, '__file__')` 看起来是对每个 migration name 的检查，但实际上是在检查父 package 模块——注意循环变量是 `(_, name, is_pkg)` 而 `module` 是外层作用域的包对象。

**策略**：C1（在数据集合构建中混入对父模块类型的隐式检查，改变集合内容）

---

### Group D — 替换（新设计）

**原 mutation**：无（mutations.jsonl 中不存在）

**分类**：新设计

**最终 mutation**：

```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index 6be8048299..31799de83b 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -213,6 +213,10 @@ class MigrationLoader:
         self.graph = MigrationGraph()
         self.replacements = {}
         for key, migration in self.disk_migrations.items():
+            module_name, _ = self.migrations_module(key[0])
+            pkg = module_name and sys.modules.get(module_name)
+            if pkg is not None and not getattr(pkg, '__file__', None):
+                continue
             self.graph.add_node(key, migration)
             # Replacing migrations.
             if migration.replaces:
```

**变异语义**：在 `build_graph()` 方法的节点添加循环中，添加了一个预检查：对于每个待添加到图中的 migration，查找其所属 app 的 migration package 模块，若该模块在 `sys.modules` 中存在但没有 `__file__` 属性（即为命名空间包），则跳过添加。此 mutation 的效果是：`disk_migrations` 中已经正确加载了来自命名空间包的迁移文件（`load_disk()` 工作正常），但在构建图时被静默丢弃。`forwards_plan(('migrations', '0001_initial'))` 会因节点不存在而抛出 `NodeNotFoundError`。此 mutation 最为隐蔽：bug 出现在 `build_graph()` 而非 `load_disk()`，调用链更深，且仅影响命名空间包，普通迁移完全不受影响。开发者审查 `load_disk()` 时看起来完全正确，需要追溯到 `build_graph()` 才能发现问题。

**策略**：D3（引入顺序依赖：migration 数据先被正确加载到 `disk_migrations`，但在后续图构建阶段被根据包类型过滤掉，两个阶段的行为不一致）

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index 6be8048299..b643fc2815 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -88,6 +88,11 @@ class MigrationLoader:
                 if not hasattr(module, '__path__'):
                     self.unmigrated_apps.add(app_config.label)
                     continue
+                # Empty directories are namespaces.
+                # getattr() needed on PY36 and older (replace w/attribute access).
+                if getattr(module, "__file__", None) is None:
+                    self.unmigrated_apps.add(app_config.label)
+                    continue
                 # Force a reload if it's already loaded (tests need this)
                 if was_loaded:
                     reload(module)
```

**分类**：🔴 必须替换

**理由**：这是对 golden patch 所删除代码的直接复原——连注释都完全一致（"Empty directories are namespaces. getattr() needed on PY36..."）。虽然插入位置在 `__path__` 检查之后（原始代码在之前），但语义完全等价：同样拒绝了 `__file__` 为 None 的命名空间包。代码审查者会立即认出这是原始 bug 的复原，不是真正的语义变异。

**最终 mutation**：

```diff
diff --git a/django/db/migrations/loader.py b/django/db/migrations/loader.py
index 6be8048299..06e9a17e7c 100644
--- a/django/db/migrations/loader.py
+++ b/django/db/migrations/loader.py
@@ -91,6 +91,10 @@ class MigrationLoader:
                 # Force a reload if it's already loaded (tests need this)
                 if was_loaded:
                     reload(module)
+                # Skip namespace packages that have no concrete file location.
+                if not getattr(module, '__file__', None):
+                    self.unmigrated_apps.add(app_config.label)
+                    continue
             migration_names = {
                 name for _, name, is_pkg in pkgutil.iter_modules(module.__path__)
                 if not is_pkg and name[0] not in '_~'
```

**变异语义**：在 `else` 块内、reload 之后、`migration_names` 计算之前，插入一个 `__file__` 检查。相比原始 E 组 mutation，此版本：(a) 位置在 reload 之后（原版在 reload 之前），(b) 使用 `not getattr(module, '__file__', None)` 而非 `getattr(module, '__file__', None) is None`，(c) 注释措辞不同（"no concrete file location" vs "Empty directories are namespaces"）。这些差异使它看起来不像是简单的代码还原，而像是开发者在"修复 reload 后可能出现的命名空间包问题"。对于命名空间包，此检查在 reload 之后生效，逻辑上看似合理（"reload 后如果发现没有文件位置，则跳过"），实际上仍然错误地拒绝了命名空间包。

**策略**：E2（在正确修复的代码流程末尾添加隐式的旧行为兼容检查，使命名空间包的加载在 reload 环节后被静默阻止）

---

## 新设计 Mutation 说明

### Group A 设计思路
基于对 Python 3 导入系统的深入分析：`module.__spec__` 是 Python 3.4+ 引入的模块规范对象。对于普通包，`__spec__.origin` 指向 `__init__.py`；对于命名空间包，`__spec__.origin` 为 `None`（因为没有对应的初始化文件）。此 mutation 模拟了开发者"用更现代的 Python 3 API 替换旧的 `__file__` 检查"的思路，但 `__spec__.origin is None` 和 `__file__ is None` 在命名空间包上语义等价，只是 API 不同。开发者看到这个检查会觉得"合理：命名空间包确实没有 origin"，而不会意识到这违背了 golden patch 的初衷。

### Group C 设计思路
最隐蔽的变异之一：在集合推导式内部添加对外层变量的检查。阅读代码时，`name[0] not in '_~' and hasattr(module, '__file__')` 看起来像是"过滤掉私有的且没有文件的迁移"，但实际上 `module` 是外层的 migration 包，不是单个迁移文件的模块。由于 Python 的词法作用域规则，`module` 引用的是 `for` 循环外的同名变量。这个变量捕获是完全合法的 Python，也符合代码风格，但语义是错误的。

### Group D 设计思路
此 mutation 将 bug 的位置从 `load_disk()` 移到了 `build_graph()` 中的图节点添加循环。真实的开发错误模式：开发者修复了 `load_disk()` 使其正确加载命名空间包，但在 `build_graph()` 的后续步骤中，出于"一致性检查"的理由，对迁移所属的包模块做了一次额外的 `__file__` 检查。Bug 的根源（`load_disk()` 调用结束后数据已正确）与表现（图中找不到节点，`forwards_plan()` 抛出 `NodeNotFoundError`）分离在两个不同函数中，极难定位。
