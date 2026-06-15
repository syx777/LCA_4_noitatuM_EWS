# django__django-13112

## 问题背景

`ForeignObject.deconstruct()` 在处理字符串形式的关联模型引用（如 `'MiXedCase_migrations.Author'`）时，使用 `self.remote_field.model.lower()` 对整个字符串进行小写转换，导致 `app_label` 中的大小写信息丢失（变成 `'mixedcase_migrations.author'`）。当 Django 的 `StateApps` 尝试按 `app_label` 查找应用配置时（使用大小写敏感的字典查找），会找不到用混合大小写注册的应用，导致 `project_state.apps.get_models()` 失败。

Golden patch 将字符串处理分为两种情况：
- 含 `.` 的字符串（`app_label.ModelName` 格式）：仅对 `model_name` 部分小写，保留 `app_label` 原始大小写
- 不含 `.` 的字符串：整体小写（原有行为，适用于单词形式的模型名）

## Golden Patch 语义分析

```python
# base_commit 状态（有 bug）：
if isinstance(self.remote_field.model, str):
    kwargs['to'] = self.remote_field.model.lower()  # 整体小写，破坏 app_label 大小写

# patched 状态（修复后）：
if isinstance(self.remote_field.model, str):
    if '.' in self.remote_field.model:
        app_label, model_name = self.remote_field.model.split('.')
        kwargs['to'] = '%s.%s' % (app_label, model_name.lower())  # 只小写 model_name
    else:
        kwargs['to'] = self.remote_field.model.lower()
```

修复的核心语义：
- `app_label` 是应用标识符，大小写由用户定义（如 `'MiXedCase_migrations'`），必须保留
- `model_name` 在 Django 内部统一小写存储（`name_lower`），因此需要小写化
- `StateApps.app_configs` 是大小写敏感的字典，`get_app_config('mixedcase_migrations')` 无法找到 key 为 `'MiXedCase_migrations'` 的应用

## 调用链分析

```
project_state.apps  (访问 StateApps)
  → StateApps.__init__(models=..., ...)
      app_labels = {model_state.app_label ...}  # = {'MiXedCase_migrations'}
      app_configs = [AppConfigStub('MiXedCase_migrations')]
      super().__init__(app_configs)  # app_configs['MiXedCase_migrations'] = stub
      → render_multiple(model_states)
          → model_state.render(apps)  # 重建 FK cloned field
              → FK.clone() → FK.deconstruct()  ← 触发 bug/fix
                  remote_field.model = 'MiXedCase_migrations.Author' (string)
                  With fix: kwargs['to'] = 'MiXedCase_migrations.author' ← app_label 保留
                  With bug: kwargs['to'] = 'mixedcase_migrations.author' ← app_label 丢失
              → FK(to='...')  → lazy_related_operation(...)
                  → make_model_tuple('MiXedCase_migrations.author')
                    → ('MiXedCase_migrations', 'author') ← 匹配注册的 key
                  With bug: ('mixedcase_migrations', 'author') ← 找不到 'MiXedCase_migrations' app!
                    → LookupError / ValueError in _check_lazy_references
                    → StateApps.__init__ raises ValueError
      → project_state.apps → 抛异常 or 返回不完整结果
  → get_models() 失败或返回 0
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新设计 | A 组缺失 |
| B | 缺失 | 新设计 | B 组缺失 |
| C | 缺失 | 新设计 | C 组缺失 |
| D | 🔴 必须替换 | 替换 | `app_label.lower()` + `model_name.lower()` 等同于原始 bug 的整体 `.lower()` 行为 |
| E | 🔴 必须替换 | 替换 | `.lower().split('.')` 先整体小写再分割，等同于原始 bug，与 D 功能冗余 |

所有 5 组均需新设计，D/E 的逻辑等价于直接还原原始代码。

## 各组 Mutation 分析

### Group A — 新设计（替换）

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index 397146a354..4c1eb7e045 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -584,7 +584,7 @@ class ForeignObject(RelatedField):
         if isinstance(self.remote_field.model, str):
             if '.' in self.remote_field.model:
                 app_label, model_name = self.remote_field.model.split('.')
-                kwargs['to'] = '%s.%s' % (app_label, model_name.lower())
+                kwargs['to'] = '%s.%s' % (app_label.upper(), model_name.lower())
             else:
                 kwargs['to'] = self.remote_field.model.lower()
         else:
```

**分类**：A1（修改格式化语义：将 app_label 转为全大写而非保留原始）

**变异语义**：将 `app_label` 转为全大写（`.upper()`）而非保留原始大小写。`'MiXedCase_migrations'` → `'MIXEDCASE_MIGRATIONS'`，与注册时的 `'MiXedCase_migrations'` 不匹配 → app_label 查找失败 → F2P 失败。对于全小写 app_label（P2P 场景）：`.upper()` 改变大小写但注册键仍是小写 → 同样会失败？不，P2P 测试使用 model 类（not string）→ 走 else 分支 → 不受影响。

---

### Group B — 新设计

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index 397146a354..c90efc7a8b 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -582,7 +582,7 @@ class ForeignObject(RelatedField):
         if self.remote_field.parent_link:
             kwargs['parent_link'] = self.remote_field.parent_link
         if isinstance(self.remote_field.model, str):
-            if '.' in self.remote_field.model:
+            if '.' not in self.remote_field.model:
                 app_label, model_name = self.remote_field.model.split('.')
                 kwargs['to'] = '%s.%s' % (app_label, model_name.lower())
             else:
```

**分类**：B3（反转布尔逻辑）

**变异语义**：反转 `'.' in ...` 条件。对于含 `.` 的字符串（如 `'MiXedCase_migrations.Author'`）：`'.' not in` 为 False → 进入 else 分支 → `model.lower()` → `'mixedcase_migrations.author'`，app_label 大小写丢失 → F2P 失败。对于不含 `.` 的字符串：进入 if 块 → `split('.')` 分割只有一个元素 → `app_label, model_name = [single_name]` → `ValueError`（不够赋值）。P2P 测试使用 model 类 → 走 isinstance else 分支 → 不受影响。

---

### Group C — 新设计

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index 397146a354..16813c28f3 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -584,7 +584,7 @@ class ForeignObject(RelatedField):
         if isinstance(self.remote_field.model, str):
             if '.' in self.remote_field.model:
                 app_label, model_name = self.remote_field.model.split('.')
-                kwargs['to'] = '%s.%s' % (app_label, model_name.lower())
+                kwargs['to'] = '%s.%s' % (model_name.lower(), app_label)
             else:
                 kwargs['to'] = self.remote_field.model.lower()
         else:
```

**分类**：C1（数据顺序反转：app_label 和 model_name 互换位置）

**变异语义**：将 format 字符串中 app_label 和 model_name 的位置互换。`'MiXedCase_migrations.Author'` → `'author.MiXedCase_migrations'`。这个字符串完全颠倒了 `app_label.model_name` 约定：`make_model_tuple('author.MiXedCase_migrations')` → `('author', 'mixedcase_migrations')`，与注册模型完全不匹配 → 所有 lazy FK 引用失败 → F2P 失败。看起来像是参数顺序写错，视觉上很难发现（格式字符串中两个 `%s` 互换）。

---

### Group D — 替换

**原 mutation**：`app_label.lower() + model_name.lower()`（与原始 bug 等价）

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index 397146a354..6ae36ac189 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -583,7 +583,7 @@ class ForeignObject(RelatedField):
             kwargs['parent_link'] = self.remote_field.parent_link
         if isinstance(self.remote_field.model, str):
             if '.' in self.remote_field.model:
-                app_label, model_name = self.remote_field.model.split('.')
+                model_name, app_label = self.remote_field.model.split('.')
                 kwargs['to'] = '%s.%s' % (app_label, model_name.lower())
             else:
                 kwargs['to'] = self.remote_field.model.lower()
```

**分类**：D1（初始化错误：解构赋值顺序相反）

**变异语义**：在 `split('.')` 解构赋值时，将 `app_label` 和 `model_name` 的接收顺序颠倒。结果：`app_label` 实际持有 `'Author'`，`model_name` 持有 `'MiXedCase_migrations'`。格式化结果：`'Author.mixedcase_migrations'`（因为 `app_label='Author'` 不 lower，`model_name.lower()='mixedcase_migrations'`）。这个错误的字符串既倒置了 app 和 model，又破坏了大小写 → F2P 完全失败。赋值解构顺序的 bug 极其难以视觉发现，因为 `split('.')` 的两侧看起来完全对称。

---

### Group E — 替换

**原 mutation**：`.lower().split('.')` 先整体小写再分割（与 D/原始 bug 功能等价）

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index 397146a354..41fa75ead8 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -582,7 +582,7 @@ class ForeignObject(RelatedField):
         if self.remote_field.parent_link:
             kwargs['parent_link'] = self.remote_field.parent_link
         if isinstance(self.remote_field.model, str):
-            if '.' in self.remote_field.model:
+            if '__' in self.remote_field.model:
                 app_label, model_name = self.remote_field.model.split('.')
                 kwargs['to'] = '%s.%s' % (app_label, model_name.lower())
             else:
```

**分类**：E2（隐式变显式 → 使用错误的分隔符条件）

**变异语义**：将 `'.' in ...` 条件改为检查 `'__'`（Django 的 lookup 分隔符）。对于 `'MiXedCase_migrations.Author'`：不含 `'__'` → False → else 分支 → `model.lower()` → `'mixedcase_migrations.author'` → app_label 大小写丢失 → F2P 失败。对于含 `'__'` 的字符串（几乎不存在于模型引用中）：尝试 `split('.')` 可能失败。P2P 测试使用 model 类 → 不涉及字符串分支 → 安全。开发者可能误以为 `'__'` 是 Django 模型引用的分隔符（在其他 Django 上下文中确实如此，如 lookup 查询）。

---

## 新设计 Mutation 说明

所有 5 个 mutation 针对的都是 `ForeignObject.deconstruct()` 中新增的字符串处理逻辑，每个以不同方式破坏 `app_label` 的正确传递：

| 组 | 方式 | 输入 `'MiXedCase_migrations.Author'` | 输出 kwargs['to'] |
|---|---|---|---|
| A | app_label.upper() | 格式化时转大写 | `'MIXEDCASE_MIGRATIONS.author'` |
| B | 反转 '.' 条件 | 走 else 分支 → 全体 .lower() | `'mixedcase_migrations.author'` |
| C | 互换 format 参数 | app_label/model_name 位置交换 | `'author.MiXedCase_migrations'` |
| D | 解构赋值反转 | app_label 实际获得 'Author' | `'Author.mixedcase_migrations'` |
| E | 检查 '__' 而非 '.' | 走 else 分支 → 全体 .lower() | `'mixedcase_migrations.author'` |

P2P 安全性：Django 标准测试使用 model 类引用（not string），因此 `isinstance(self.remote_field.model, str)` 为 False，走 `label_lower` 分支，所有 mutation 均不影响 P2P 测试。
