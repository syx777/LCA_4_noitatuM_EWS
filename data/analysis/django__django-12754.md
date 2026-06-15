# django__django-12754

## 问题背景

当用户在同一个迁移步骤中创建一个新的模型子类并将父类的字段移动到子类上时，`makemigrations` 生成的迁移操作顺序有误。自动检测器生成的顺序是先 `CreateModel(Book)`，再 `RemoveField(Readable.title)`，但 Django 执行时会因为 `Book.title` 和从 `Readable` 继承来的 `title` 字段冲突而抛出 `FieldError`。

正确顺序应是先 `RemoveField(Readable.title)`，再 `CreateModel(Book)`。Golden patch 的修复思路是：在 `generate_created_models` 方法中，为新创建的继承模型添加一个额外的依赖——依赖于父类中与新模型同名字段的删除操作。这样 `_sort_migrations` 中的拓扑排序会将 `RemoveField` 排在 `CreateModel` 之前。

## Golden Patch 语义分析

Golden patch 在 `generate_created_models` 的"依赖所有基类"循环中增加了额外逻辑：

1. 对于每个字符串形式的基类 `base_app_label.base_name`，查找该基类在 `from_state`（旧状态）和 `to_state`（新状态）中的模型状态。
2. 若两者都存在（即基类在迁移前后均存在，只是字段有变化），计算：
   - `removed_base_fields` = 旧基类字段集合 **差** 新基类字段集合（被删掉的字段）
   - 再与 `model_state.fields`（新建子类的字段集合）求 **交集**
   - 结果是：同时出现在"被从基类删除的字段"和"新子类的字段"中的字段名（即字段从基类移到子类的情况）
3. 为这些字段各追加一个依赖 `(base_app_label, base_name, removed_base_field, False)`，表示"在创建本子类之前，必须先完成对基类该字段的删除"。

这样，`_sort_migrations` 的拓扑排序就能看到 `CreateModel(Book)` 依赖于 `RemoveField(Readable, title)`，从而强制正确排序。

## 调用链分析

```
MigrationAutodetector._detect_changes()
  └── generate_created_models()       # 新建模型时收集依赖，patch 在此处修改
  └── generate_removed_fields()       # 生成 RemoveField 操作
  └── _sort_migrations()              # 拓扑排序：根据 _auto_deps 重排各 app 内的操作
        └── stable_topological_sort() # Kahn 算法实现（django/utils/topological_sort.py）
  └── _build_migration_list()         # 将排序后的操作切割成若干迁移文件
        └── check_dependency()        # 判断某操作是否满足某依赖
```

关键数据流：
- `add_operation()` 将 `dependencies` 列表附加到操作的 `_auto_deps` 属性
- `_sort_migrations()` 遍历 `_auto_deps`，调用 `check_dependency()` 判断是否有同 app 内的前驱操作，构建 `dependency_graph`
- `stable_topological_sort()` 对 `dependency_graph` 执行拓扑排序，决定操作在 app 内的最终顺序
- `_build_migration_list()` 按排序结果检查跨 app 依赖是否已满足

`check_dependency(op, dep)` 中，`dep[3] == False` 对应 `RemoveField`，`dep[3] == True` 对应 `CreateModel`/`AddField`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层 | 保留 | `.intersection` → `.difference`，关键逻辑节点，能模拟真实边界误判 |
| B | 🟡 语义浅层 | 替换 | `intersection(model_state.fields)` → `intersection(new_base_model_state.fields)`，效果等同于无修复，用新替换 |
| C | 🟢 高质量 | 保留 | 修改在不同函数（`_build_migration_list`），涉及迭代器失效，隐蔽性强 |
| D | 🔴 必须替换 | 替换 | 含显式注释 "# Bug: ..."，立即可被代码审查发现 |
| E | 🔴 必须替换 | 替换 | `not new_base_model_state` 导致 `new_base_model_state` 为 None 时进入块并 AttributeError 崩溃 |

语义浅层共 2 个（A、B），替换其中最弱的 floor(2/2)=1 个：替换 B（修改位置与 A 重叠度高，且语义等价于"始终不添加依赖"）。

## 各组 Mutation 分析

### Group A — 保留
**原 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..1c8b20d74f 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -570,7 +570,7 @@ class MigrationAutodetector:
                     if old_base_model_state and new_base_model_state:
                         removed_base_fields = set(old_base_model_state.fields).difference(
                             new_base_model_state.fields,
-                        ).intersection(model_state.fields)
+                        ).difference(model_state.fields)
                         for removed_base_field in removed_base_fields:
                             dependencies.append((base_app_label, base_name, removed_base_field, False))
```
**分类**：🟡 语义浅层（保留）
**理由**：修改位置是 golden fix 的核心过滤步骤——将"只关注子类也有的字段"改为"只关注子类没有的字段"。这是关键控制流节点上的语义反转，能模拟真实开发者将集合操作方向搞错的错误。虽然是单方法替换，但其效果是将依赖对象从"移到子类的字段"变成"移走但子类没有要的字段"，测试用例中恰好会因此不添加正确依赖而失败。相对于 B，该修改更具语义欺骗性。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..1c8b20d74f 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -570,7 +570,7 @@ class MigrationAutodetector:
                     if old_base_model_state and new_base_model_state:
                         removed_base_fields = set(old_base_model_state.fields).difference(
                             new_base_model_state.fields,
-                        ).intersection(model_state.fields)
+                        ).difference(model_state.fields)
                         for removed_base_field in removed_base_fields:
                             dependencies.append((base_app_label, base_name, removed_base_field, False))
```
**变异语义**：将"只对子类也声明了该字段"的过滤改为"只对子类未声明的字段"。在测试场景（Book.title 与 Readable.title 同名）中，`removed_base_fields.difference(model_state.fields)` 结果为空集，不追加任何依赖，CreateModel 仍然会被排在 RemoveField 之前。普通测试（不涉及同名字段继承移动）全部通过。

---

### Group B — 替换
**原 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..1bf5d6c273 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -572,7 +572,7 @@ class MigrationAutodetector:
                             new_base_model_state.fields,
                         ).intersection(model_state.fields)
                         for removed_base_field in removed_base_fields:
-                            dependencies.append((base_app_label, base_name, removed_base_field, False))
+                            dependencies.append((base_app_label, base_name, removed_base_field, True))
```
**分类**：🟡 语义浅层（替换）
**理由**：`False` → `True` 是 4-tuple 最后一位的单值改变，属于 `+1/-1` 类型的符号替换。虽然语义上从"removed field"依赖改为"created field"依赖，但这与 A 组修改位置仅差两行，且两个 mutation 都指向同一逻辑路径，组内重叠度高。相较 A 的"集合操作方向"错误，B 的布尔翻转更机械、更难体现真实开发者误解。选 B 作为两个语义浅层中最弱的一个进行替换。
**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..3f5a6fad8e 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -570,7 +570,7 @@ class MigrationAutodetector:
                     if old_base_model_state and new_base_model_state:
                         removed_base_fields = set(old_base_model_state.fields).difference(
                             new_base_model_state.fields,
-                        ).intersection(model_state.fields)
+                        ).intersection(new_base_model_state.fields)
                         for removed_base_field in removed_base_fields:
                             dependencies.append((base_app_label, base_name, removed_base_field, False))
```
**变异语义**：将交集的对象从"新子类的字段集合（`model_state.fields`）"改为"新基类的字段集合（`new_base_model_state.fields`）"。由于 `removed_base_fields` 是从旧基类字段中**减去**新基类字段得到的（即已不在新基类中的字段），与新基类字段集合的交集必然为空。结果是 `for removed_base_field in removed_base_fields` 循环体从未执行，不添加任何依赖——fix 完全失效。代码读起来逻辑合理（"从新基类视角过滤"），只有对集合语义深入分析才能发现问题。

---

### Group C — 保留
**原 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..9045544429 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -271,7 +271,7 @@ class MigrationAutodetector:
             for app_label in sorted(self.generated_operations):
                 chopped = []
                 dependencies = set()
-                for operation in list(self.generated_operations[app_label]):
+                for operation in self.generated_operations[app_label]:
                     deps_satisfied = True
                     operation_dependencies = set()
                     for dep in operation._auto_deps:
```
**分类**：🟢 保留
**理由**：修改在 `_build_migration_list` 中，与 golden patch 修改的 `generate_created_models` 属于不同函数。去掉 `list(...)` 拷贝后，在 `for operation in self.generated_operations[app_label]:` 迭代期间，`del self.generated_operations[app_label][0]` 和末尾的 `self.generated_operations[app_label] = chopped + self.generated_operations[app_label]` 都会修改底层列表，导致迭代器行为未定义。这模拟了真实开发者"认为迭代列表时修改是安全的"的误解，只在特定操作组合下暴露，难以被简单测试捕获。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..9045544429 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -271,7 +271,7 @@ class MigrationAutodetector:
             for app_label in sorted(self.generated_operations):
                 chopped = []
                 dependencies = set()
-                for operation in list(self.generated_operations[app_label]):
+                for operation in self.generated_operations[app_label]:
                     deps_satisfied = True
                     operation_dependencies = set()
                     for dep in operation._auto_deps:
```
**变异语义**：`_build_migration_list` 在迭代 `generated_operations[app_label]` 时同时通过 `del self.generated_operations[app_label][0]` 修改该列表。去掉保护性 `list()` 拷贝后，Python 的列表迭代器在底层数组被修改时会跳过元素，导致部分操作未被处理，最终迁移生成不完整。在单操作场景下不触发（只迭代一次就结束），在多操作场景才暴露。

---

### Group D — 替换
**原 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..36c666548d 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -341,7 +341,7 @@ class MigrationAutodetector:
         """
         for app_label, ops in sorted(self.generated_operations.items()):
             # construct a dependency graph for intra-app dependencies
-            dependency_graph = {op: set() for op in ops}
+            dependency_graph = {}  # Bug: not initializing dependency graph properly
```
**分类**：🔴 必须替换
**理由**：注释中包含 "# Bug: ..."，代码审查者一眼就能看出这是人工注入的 bug。且 `dependency_graph = {}` 后续调用 `dependency_graph[op].add(op2)` 会直接抛出 `KeyError`，是崩溃级错误，不具有"通过大多数测试"的特性。
**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..f0bdd6e5a6 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -349,8 +349,8 @@ class MigrationAutodetector:
                     dep = self._resolve_dependency(dep)[0]
                     if dep[0] == app_label:
                         for op2 in ops:
-                            if self.check_dependency(op2, dep):
-                                dependency_graph[op].add(op2)
+                            if self.check_dependency(op2, dep) and op2 is not op:
+                                dependency_graph[op2].add(op)
 
             # we use a stable sort for deterministic tests & general behavior
             self.generated_operations[app_label] = stable_topological_sort(ops, dependency_graph)
```
**变异语义**：原代码语义是"op 依赖于 op2（op2 必须先执行）"，用 `dependency_graph[op].add(op2)` 表示。变异后改为 `dependency_graph[op2].add(op)`，语义变为"op2 依赖于 op（op 必须先执行）"，即将所有 intra-app 依赖边方向取反。这样 `stable_topological_sort` 会产生颠倒的排序结果——本应先执行的操作反而被排到后面。大多数迁移场景（独立操作无依赖）不受影响；只有在操作间存在必需顺序时（如本 issue 的 RemoveField→CreateModel）才暴露错误。额外的 `op2 is not op` 确保自引用不产生循环依赖，代码语法正确、逻辑看似合理。

---

### Group E — 替换
**原 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..036ff7e577 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -567,7 +567,7 @@ class MigrationAutodetector:
                     # a field with the same name.
                     old_base_model_state = self.from_state.models.get((base_app_label, base_name))
                     new_base_model_state = self.to_state.models.get((base_app_label, base_name))
-                    if old_base_model_state and new_base_model_state:
+                    if old_base_model_state and not new_base_model_state:
```
**分类**：🔴 必须替换
**理由**：`not new_base_model_state` 使条件在 `new_base_model_state is None` 时为真，随即进入块执行 `new_base_model_state.fields` → `AttributeError: 'NoneType' object has no attribute 'fields'`，是立即崩溃的 bug。不具备"通过大多数测试"的特性，且在实际运行时每次触发都崩溃，完全不符合高质量 mutation 要求。
**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 85c3013897..815070ef08 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -565,7 +565,7 @@ class MigrationAutodetector:
                     dependencies.append((base_app_label, base_name, None, True))
                     # Depend on the removal of base fields if the new model has
                     # a field with the same name.
-                    old_base_model_state = self.from_state.models.get((base_app_label, base_name))
+                    old_base_model_state = self.to_state.models.get((base_app_label, base_name))
                     new_base_model_state = self.to_state.models.get((base_app_label, base_name))
                     if old_base_model_state and new_base_model_state:
                         removed_base_fields = set(old_base_model_state.fields).difference(
```
**变异语义**：将 `old_base_model_state` 从 `from_state`（迁移前）查找改为从 `to_state`（迁移后）查找。这样 `old_base_model_state` 和 `new_base_model_state` 指向同一个对象（`to_state` 中的基类模型）。`set(old_base_model_state.fields).difference(new_base_model_state.fields)` 对同一对象求差，结果永远是空集。依赖从不被添加，fix 完全失效。代码逻辑流畅，变量名仅有 `from_state`→`to_state` 的一处改动，极难在代码审查中发现。真实开发者在处理 from/to 状态对称结构时确实容易犯此类错误。

## 新设计 Mutation 说明

### Group B 新设计依据
基于对 golden patch 集合运算链的深入分析：`set(old).difference(new_base).intersection(model_state)` 中，最终的 `intersection` 步骤是关键的"过滤器"——它决定哪些被删除的基类字段与新子类的字段重合。将过滤集从 `model_state.fields`（子类字段）改为 `new_base_model_state.fields`（新基类剩余字段），利用了集合运算的一个微妙性质：`removed_base_fields` 的元素定义上不属于 `new_base_model_state.fields`，因此交集恒空。这个错误模拟了"开发者以为应该与基类当前状态对齐，而非与子类对齐"的语义误解。

### Group D 新设计依据
`_sort_migrations` 构建的 `dependency_graph` 用于拓扑排序，其约定是 `graph[op]` 包含 op 的**前驱**（必须先于 op 执行的操作）。将 `dependency_graph[op].add(op2)` 改为 `dependency_graph[op2].add(op)` 意味着将边方向完全反转——原来"CreateModel 依赖于 RemoveField"变成"RemoveField 依赖于 CreateModel"。拓扑排序将输出反序，让 CreateModel 先执行。额外的 `op2 is not op` 防止自环（若 `check_dependency(op, dep)` 偶然对自身返回 True 则产生 `op.add(op)` 自环），确保语法正确性和运行时不崩溃。此错误模拟了开发者混淆"谁依赖谁"方向（前驱 vs 后继）的常见误解。

### Group E 新设计依据
`from_state` 和 `to_state` 是 `MigrationAutodetector` 的核心属性，在代码中频繁并列出现。golden patch 在相邻两行分别赋值 `old_base_model_state = self.from_state.models.get(...)` 和 `new_base_model_state = self.to_state.models.get(...)`。将第一行改为也使用 `self.to_state`，从视觉上两行只有变量名不同，diff 极短，且 `from_state`/`to_state` 错用是真实开发中的高频错误类型（类似复制粘贴时漏改）。由于 `difference()` 对相同对象返回空集，此 mutation 完全静默失效——无崩溃、无警告，只是排序依赖永远不被添加。
