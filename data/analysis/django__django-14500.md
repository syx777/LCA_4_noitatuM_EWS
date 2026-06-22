# django__django-14500 Mutation 分析

## 问题背景
Squashed (压缩) migration 在被回滚 (unapply) 时没有被标记为未应用。当被替换的原始 migration 文件仍然存在时，`MigrationExecutor.unapply_migration` 只为被替换的子 migration 记录 `record_unapplied`，而 squash migration 自身在 `django_migrations` 表中的记录没有被删除，导致状态不一致。

## Golden Patch 语义分析
源文件 `django/db/migrations/executor.py` 的 `unapply_migration` 原逻辑为二选一 (if/else)：
```diff
-        # For replacement migrations, record individual statuses
+        # For replacement migrations, also record individual statuses.
         if migration.replaces:
             for app_label, name in migration.replaces:
                 self.recorder.record_unapplied(app_label, name)
-        else:
-            self.recorder.record_unapplied(migration.app_label, migration.name)
+        self.recorder.record_unapplied(migration.app_label, migration.name)
```
修复将 if/else 改为：先在 `migration.replaces` 为真时记录每个被替换子项的 `record_unapplied`，然后 **无条件** 再对 migration 自身执行一次 `record_unapplied`。这样无论是普通 migration 还是 squash migration，自身的记录都会被删除。

## 调用链分析
- `migrate([(app, None)])` 触发回滚 → `_migrate_all_backwards` → 对 plan 中每个 migration 调用 `unapply_migration`。
- `unapply_migration` 调用 `recorder.record_unapplied(app, name)`，其内部对 `django_migrations` 表执行 `filter(app, name).delete()`。
- 回滚后 `migrate` 末尾调用 `check_replacements()`：若某 squash 的全部 replaced 子项都仍在 applied 集合中且 squash key 不在 applied 中，则把 squash 重新 `record_applied`。
- F2P 测试 `test_migrate_marks_replacement_unapplied`：先 migrate 到 `0001_squashed_0002` 并断言其 applied；随后 `migrate(None)` 回滚，断言 squash 不再 applied。该断言依赖两点：(1) unapply 删除 squash 自身记录；(2) check_replacements 不把它重新加回。

## 替换决策总览
| 组 | 原类别 | 决策 | 原因 |
|----|--------|------|------|
| A | 🟡 单 token (插入 `not`) | 保留 | 唯一的 shallow，落在关键控制流分支布尔上，建模真实边界错误；M=1, floor(1/2)=0 不替换 |
| B | 🔴 功能等价回退 | 替换 | 用 `if not migration.replaces:` 重新包裹自身记录行，与 golden 前的 else 行为等价，是 golden 的功能性回退 |
| C | 🔴 直接回退 | 替换 | 与 D/E 字节相同，直接恢复 `else:`，即 golden 的逆操作 |
| D | 🔴 重复+直接回退 | 替换 | 与 C/E 字节相同的重复 diff，直接 golden 回退 |
| E | 🔴 重复+直接回退 | 替换 | 与 C/D 字节相同的重复 diff，直接 golden 回退 |

shallow 计数 M=1 → 需替换的 🟡 数 = floor(1/2)=0；🔴 = {B,C,D,E} 全部替换。共替换 4 个，保留 A。

## 各组 Mutation 分析

### 组 A（保留）
- 原 diff：在 `if migration.replaces:` 前插入 `not`。
- 分类：🟡 SEMANTIC-SHALLOW（单 token 取反）。
- 理由：作为集合中唯一 shallow，且其落在区分 squash / 普通 migration 的核心控制流节点上，建模真实的分支判断反转错误，按规则 floor(1/2)=0 不需替换，保留。
- 最终 diff：
```diff
diff --git a/django/db/migrations/executor.py b/django/db/migrations/executor.py
index a8a189f7d9..b80aa52d6e 100644
--- a/django/db/migrations/executor.py
+++ b/django/db/migrations/executor.py
@@ -251,7 +251,7 @@ class MigrationExecutor:
             with self.connection.schema_editor(atomic=migration.atomic) as schema_editor:
                 state = migration.unapply(state, schema_editor)
         # For replacement migrations, also record individual statuses.
-        if migration.replaces:
+        if not migration.replaces:
             for app_label, name in migration.replaces:
                 self.recorder.record_unapplied(app_label, name)
         self.recorder.record_unapplied(migration.app_label, migration.name)
```
- 变异语义：取反后 squash migration 不再进入 replaced 循环（其子项不被回滚），普通 migration 反而错误地试图迭代空的 replaces；squash 自身仍被删除，故普通回滚测试仍过，仅 squash 回滚一致性场景失败。F2P 验证 rc=1（FAILS，符合预期），全模块仅 F2P 失败。

### 组 B（替换：check_replacements 守卫 and→or）
- 原 diff：把无条件 `record_unapplied(self,…)` 重新加 `if not migration.replaces:` 守卫 → golden 的功能等价回退。
- 分类：🔴 功能等价冗余。
- 理由：在所有 F2P 场景下行为等同 golden 前的 buggy else 分支。
- 最终 diff：
```diff
diff --git a/django/db/migrations/executor.py b/django/db/migrations/executor.py
index a8a189f7d9..6d374beaa8 100644
--- a/django/db/migrations/executor.py
+++ b/django/db/migrations/executor.py
@@ -274,7 +274,7 @@ class MigrationExecutor:
         applied = self.recorder.applied_migrations()
         for key, migration in self.loader.replacements.items():
             all_applied = all(m in applied for m in migration.replaces)
-            if all_applied and key not in applied:
+            if all_applied or key not in applied:
                 self.recorder.record_applied(*key)
 
     def detect_soft_applied(self, project_state, migration):
```
- 变异语义：`check_replacements` 的再应用守卫由 `and` 改为 `or`，回滚正确删除 squash 行后，`key not in applied` 单独成立即把 squash 重新 `record_applied`，破坏 F2P 的回滚断言；bug 位于与 unapply 修复不同的方法，难检测。F2P rc=1，全模块仅 F2P 失败。

### 组 C（替换：replaced 循环记录目标错误）
- 原 diff：恢复 `else:`（与 D/E 字节相同），直接 golden 回退。
- 分类：🔴 直接回退 + 跨组重复。
- 最终 diff：
```diff
diff --git a/django/db/migrations/executor.py b/django/db/migrations/executor.py
index a8a189f7d9..6380de5f9d 100644
--- a/django/db/migrations/executor.py
+++ b/django/db/migrations/executor.py
@@ -253,7 +253,7 @@ class MigrationExecutor:
         # For replacement migrations, also record individual statuses.
         if migration.replaces:
             for app_label, name in migration.replaces:
-                self.recorder.record_unapplied(app_label, name)
+                self.recorder.record_unapplied(migration.app_label, migration.name)
         self.recorder.record_unapplied(migration.app_label, migration.name)
         # Report progress
         if self.progress_callback:
```
- 变异语义：被替换循环内 `record_unapplied(app_label, name)` 被改成记录 squash 自身 `(migration.app_label, migration.name)`，子项从未被回滚而 squash 行被重复删除；单步 migration 看不出问题，仅 squash+replaced 顺序回滚状态失配。F2P rc=1，全模块仅 F2P 失败。

### 组 D（替换：record_unapplied 参数顺序交换）
- 原 diff：恢复 `else:`（与 C/E 字节相同），直接 golden 回退。
- 分类：🔴 直接回退 + 跨组重复。
- 最终 diff：
```diff
diff --git a/django/db/migrations/executor.py b/django/db/migrations/executor.py
index a8a189f7d9..6ed6406705 100644
--- a/django/db/migrations/executor.py
+++ b/django/db/migrations/executor.py
@@ -253,7 +253,7 @@ class MigrationExecutor:
         # For replacement migrations, also record individual statuses.
         if migration.replaces:
             for app_label, name in migration.replaces:
-                self.recorder.record_unapplied(app_label, name)
+                self.recorder.record_unapplied(name, app_label)
         self.recorder.record_unapplied(migration.app_label, migration.name)
         # Report progress
         if self.progress_callback:
```
- 变异语义：把 `record_unapplied(app_label, name)` 实参顺序写反为 `(name, app_label)`，被替换子项以转置后的键存储/查询，实际从未被清除；表面像正确代码，凡不检查 squash 回滚后 replaced 子记录的测试都能通过。F2P rc=1，全模块仅 F2P 失败。

### 组 E（替换：all_applied 生成器附加虚假过滤）
- 原 diff：恢复 `else:`（与 C/D 字节相同），直接 golden 回退。
- 分类：🔴 直接回退 + 跨组重复。
- 最终 diff：
```diff
diff --git a/django/db/migrations/executor.py b/django/db/migrations/executor.py
index a8a189f7d9..f06d71c2bc 100644
--- a/django/db/migrations/executor.py
+++ b/django/db/migrations/executor.py
@@ -273,7 +273,7 @@ class MigrationExecutor:
         """
         applied = self.recorder.applied_migrations()
         for key, migration in self.loader.replacements.items():
-            all_applied = all(m in applied for m in migration.replaces)
+            all_applied = all(m in applied for m in migration.replaces if m in self.loader.replacements)
             if all_applied and key not in applied:
                 self.recorder.record_applied(*key)
 
```
- 变异语义：在 `check_replacements` 的 `all_applied` 生成器上加 `if m in self.loader.replacements` 过滤；真实 squash 的 replaced 子项本身并非 replacements，生成器变空导致 `all()` 真空为真，回滚后 squash 被重新标记 applied。大多数流程不受影响，仅回滚 round-trip 失败。F2P rc=1，全模块仅 F2P 失败。

## 新设计 Mutation 说明
为保证多样性与正交失败模式，4 个替换分布在不同函数/数据流上：
- B 与 E 攻击 `check_replacements`（再应用守卫 / all_applied 真空真），但分别是布尔守卫与生成器过滤两种不同机制。
- C 与 D 攻击 `unapply_migration` 内的 replaced 循环，分别为记录目标错误与实参顺序交换两种独立错误。
- A（保留）攻击分支判定布尔。
全部 5 个变异均：在 golden+test_patch 之上以 `git apply` 与 `patch -p1` 干净应用、`py_compile` 通过、并使 `migrations.test_executor` 全模块中 **仅** `test_migrate_marks_replacement_unapplied` 失败（无 P2P 回归）。Baseline（无变异）全模块 F2P 通过 (rc=0) 已确认。
