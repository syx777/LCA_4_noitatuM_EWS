# django__django-15252

## 问题背景

`MigrationRecorder` 在多库 + db_router 的 `allow_migrate` 规则下不守规矩：即使某连接不应建表，`migrate()` 仍无条件调用 `recorder.ensure_schema()` 创建 `django_migrations` 表。Golden patch 改为：当 `plan == []`（无迁移可应用）且记录表尚不存在时，直接返回项目状态、**不创建表**；只有非空 plan 才 `ensure_schema()`。

## Golden Patch 语义分析

```python
# The django_migrations table must be present to record applied
# migrations, but don't create it if there are no migrations to apply.
if plan == []:
    if not self.recorder.has_table():
        return self._create_project_state(with_applied_migrations=False)
else:
    self.recorder.ensure_schema()
```
核心语义：**仅在确有迁移要应用时才建 `django_migrations` 表**。`plan == []` 表示"显式传入空计划、无迁移"，此时若表不存在则跳过建表直接返回；其它情况（plan 非空）才 `ensure_schema()`。判据是 `plan == []` 这个**精确的空列表相等**（区别于 `plan is None` 的"未提供计划"）。

F2P 测试：`ExecutorTests.test_migrate_skips_schema_creation`（mock `has_table=False`，`migrate([], plan=[])` 应 0 查询、不建表）、`TestDbCreationTests.test_migrate_test_setting_false_ensure_schema`（TEST.MIGRATE=False 时断言 `ensure_schema` 未被调用）。

## 调用链分析

`migrate(targets, plan=None, ...)`：空 plan 路径走 `has_table` 检查 + `_create_project_state`；非空走 `ensure_schema`。测试通过 mock `has_table`/`ensure_schema` 观察是否触发建表。任何让 `ensure_schema()` 在空 plan 路径被调用、或让空 plan 不再短路的改动，都会令 F2P 失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `plan == []`→`plan is None`，空列表场景不再短路，落入 else 调 ensure_schema |
| B | 🟢 高质量 | 保留 | `plan == []`→`plan != []`，逻辑彻底反转 |
| C | 🔴 必须替换 | 替换 | 原 C 改的是 `allow_migrate`（line 302），与本 golden patch 无关，且实测**不破坏 F2P**（无效 mutation） |
| D | ➕ 补充 | 新增 | 原缺 D 组 |
| E | 🔴 必须替换 | 替换 | 原 E 同样改 `allow_migrate`，与 golden patch 无关 |

原 C、E 改到了与本修复无关的 `allow_migrate` 代码块（那是该 ticket 历史上另一处，但本 golden patch 只动 `migrate()` 的建表逻辑），实测 C 不让 F2P 失败，属无效。C、E 重做，并补充缺失的 D。

## 各组 Mutation 分析

### Group A — 保留（B3 条件语义）
```diff
-        if plan == []:
+        if plan is None:
```
**变异语义**：把"空列表 plan"判据换成"plan 未提供（None）"。`migrate([], plan=[])` 传入的是空 list 而非 None，故 `plan is None` 为假 → 落入 else 分支 `ensure_schema()` → 建表，F2P 失败。模拟 `== []` 与 `is None` 这两种"空"语义的混淆。保留。

### Group B — 保留（B3 逻辑反转）
```diff
-        if plan == []:
+        if plan != []:
```
**变异语义**：条件取反。空 plan 时走 else 调 `ensure_schema`（不该建表却建了）；非空 plan 时反而走短路分支。F2P 的空 plan 场景触发建表，失败。保留。

### Group C — 替换（D1 状态初始化：空分支内多调 ensure_schema）
**原**：改 `allow_migrate`（无关 + 无效）。
**最终 mutation**：
```diff
        if plan == []:
+            self.recorder.ensure_schema()
            if not self.recorder.has_table():
                return self._create_project_state(with_applied_migrations=False)
```
**变异语义**：在空 plan 分支内、`has_table` 检查之前**额外调用 `ensure_schema()`**。看似"确保记录表就绪"的防御性初始化，实则违反"无迁移就别建表"的契约——`test_migrate_test_setting_false_ensure_schema` 断言 `ensure_schema` 未被调用，直接失败。

### Group D — 补充（D1 状态初始化：去掉 has_table 守卫）
```diff
        if plan == []:
-            if not self.recorder.has_table():
-                return self._create_project_state(with_applied_migrations=False)
+            self.recorder.ensure_schema()
+            return self._create_project_state(with_applied_migrations=False)
        else:
            self.recorder.ensure_schema()
```
**变异语义**：删除 `has_table` 守卫，在空 plan 分支无条件 `ensure_schema()` 再返回。这是"重构时把两个分支的 `ensure_schema` 提取到一起、顺手丢了守卫"的状态初始化错误——空 plan 也强行建表，违反修复意图。

### Group E — 替换（E2 隐式→显式参数）
**原**：改 `allow_migrate`（无关）。
**最终 mutation**：
```diff
-        if plan == []:
+        if plan == [] and fake:
            if not self.recorder.has_table():
                return self._create_project_state(with_applied_migrations=False)
```
**变异语义**：把空 plan 的短路额外绑定到 `fake` 参数。`migrate` 默认 `fake=False`，故短路条件恒假，空 plan 落入 else 调 `ensure_schema()` 建表。模拟"以为这个短路只在 fake 迁移时需要"的隐式行为显式化误解。

## 新设计 Mutation 说明

原始 C、E 错误地修改了与本 golden patch 无关的 `allow_migrate` 代码（那段并非本修复点），其中 C 实测根本不破坏 F2P。本次将 C、E 重做为针对真正修复点（`migrate()` 建表逻辑）的高质量变异，并补齐缺失的 D：C（空分支内多调 ensure_schema）、D（去守卫无条件建表）、E（短路绑定 fake 参数）。配合保留的 A（`is None`）、B（`!= []`），五组覆盖"判据语义 / 逻辑反转 / 多余初始化 / 守卫缺失 / 隐式参数"五个角度。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用。
