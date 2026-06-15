# django__django-11095

## 问题背景

Django admin 的 `ModelAdmin` 类中，`get_inline_instances` 方法直接硬编码使用 `self.inlines` 来遍历内联类。如果开发者想根据当前请求（如用户权限、请求参数）或模型实例动态决定展示哪些 inline，必须完整复制 `get_inline_instances` 中的 for 循环，代码侵入性强。Golden patch 添加了 `get_inlines(request, obj)` 钩子方法，并修改 `get_inline_instances` 调用该钩子，使开发者可以通过重写钩子来动态控制 inline 列表，而无需重写整个 `get_inline_instances`。

## Golden Patch 语义分析

修复的核心是**引入可扩展钩子**：
1. 在 `BaseModelAdmin` 中新增 `get_inlines(self, request, obj)` 方法，默认返回 `self.inlines`（保持向后兼容）。
2. 将 `get_inline_instances` 中的 `for inline_class in self.inlines:` 改为 `for inline_class in self.get_inlines(request, obj):`。

这样，子类只需重写 `get_inlines` 即可在不破坏权限检查逻辑的情况下，动态调整 inline 类的集合。权限检查仍由 `get_inline_instances` 统一处理。

## 调用链分析

```
ModelAdmin.add_view / change_view
  └── ModelAdmin._create_formsets(request, obj, change)
        └── ModelAdmin.get_formsets_with_inlines(request, obj)
              └── ModelAdmin.get_inline_instances(request, obj)   ← 核心入口
                    └── BaseModelAdmin.get_inlines(request, obj)  ← 新增钩子
                          → 返回 self.inlines（可被子类覆写）
```

`get_formsets_with_inlines` 也直接被 changelist_view 等视图调用，它将 `obj` 传给 `get_inline_instances`，`get_inline_instances` 再将 `request` 和 `obj` 都传给 `get_inlines`——因此两个参数对子类自定义行为都很重要。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 直接冗余：等同于还原 golden patch（参数顺序还原为调用时的顺序，但定义签名颠倒，这是一个人为构造的错误，对 base class 行为无影响且极其刻意） |
| B | 语义浅层 | 保留 | 跳过第一个 inline，位置在 golden fix 的核心调用点，能模拟边界切片失误 |
| C | 必须替换 | 替换 | 不自然且功能错误：`self.inlines = list(self.inlines)` 在 patch 后状态完全无效（get_inline_instances 已调用 get_inlines，与 self.inlines 无直接关系），无法使任何 F2P 测试失败 |
| D | 必须替换 | 替换 | 不自然：`_inline_cache` 隐藏状态明显人工，且跨请求缓存 inline 实例是已知的 Django 反模式，审查时立即可发现 |
| E | 必须替换 | 替换 | 不自然：添加 `use_custom_inlines=False` flag 是明显的人工标志，代码审查一眼可识别，且实际行为是将新功能完全默认关闭 |

语义浅层共 1 个（B），替换其中最弱的 floor(1/2)=0 个：无

**最终替换：A、C、D、E（共4个）**

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..2bb3aaa6e4 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -586,7 +586,7 @@ class ModelAdmin(BaseModelAdmin):
 
     def get_inline_instances(self, request, obj=None):
         inline_instances = []
-        for inline_class in self.get_inlines(request, obj):
+        for inline_class in self.get_inlines(obj, request):
             inline = inline_class(self.model, self.admin_site)
             if request:
                 if not (inline.has_view_or_change_permission(request, obj) or
```
**分类**：🔴 必须替换

**理由**：该 mutation 将调用端的参数顺序互换（`get_inlines(obj, request)`），等效于还原了钩子机制的接口契约——当子类按正确文档重写 `get_inlines(self, request, obj)` 时，`request` 会被传入 `obj` 位置，导致行为错误。但这是一个非常刻意的参数互换，代码审查者阅读调用点时会立即注意到参数顺序不对。此外，原始 mutation 集中在调用点而非定义点，与策略组 A（应当多样化攻击位置）重复性高。

**最终 mutation**（替换）：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..d077c4904a 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -327,7 +327,7 @@ class BaseModelAdmin(metaclass=forms.MediaDefiningClass):
             return self.fieldsets
         return [(None, {'fields': self.get_fields(request, obj)})]
 
-    def get_inlines(self, request, obj):
+    def get_inlines(self, obj, request=None):
         """Hook for specifying custom inlines."""
         return self.inlines
```
**变异语义**：在 `BaseModelAdmin` 的 `get_inlines` **定义**中颠倒参数顺序（`obj, request=None`），而调用端 `get_inline_instances` 仍用 `self.get_inlines(request, obj)` 调用。对于默认实现（仅返回 `self.inlines`，不用参数），行为完全无变化，所有简单测试通过。但当子类按照正确文档签名 `def get_inlines(self, request, obj)` 重写时，实际接收的参数被颠倒：`request` 参数槽收到的是 `obj`，`obj` 参数槽收到的是 `request`。F2P 测试中子类的 `get_inlines` 使用 `request.name` 进行分支，而此时 `request` 位置实际是 `obj`（为 None），会触发 `AttributeError` 或返回错误结果，测试失败。代码审查时只看定义点很难发现——参数名 `obj` 在前、`request` 在后看似合理（某些 Django 方法确实以 obj 为第一参数）。

---

### Group B — 保留

**原 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..2d09c70e2e 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -586,7 +586,7 @@ class ModelAdmin(BaseModelAdmin):
 
     def get_inline_instances(self, request, obj=None):
         inline_instances = []
-        for inline_class in self.get_inlines(request, obj):
+        for inline_class in self.get_inlines(request, obj)[1:]:
             inline = inline_class(self.model, self.admin_site)
             if request:
                 if not (inline.has_view_or_change_permission(request, obj) or
```
**分类**：🟡 语义浅层（保留）

**理由**：虽然是单行 `[1:]` 切片，但修改位置位于 golden fix 的核心调用点——直接破坏了 `get_inlines` 返回结果的完整性，会导致 `inlines[0]`（第一个 inline）的实例永远不被创建，稳定地导致 F2P 测试失败。作为语义浅层的唯一一个，保留标准满足（关键控制流节点），floor(1/2)=0 个需替换。

**最终 mutation**（保留，与原相同）：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..2d09c70e2e 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -586,7 +586,7 @@ class ModelAdmin(BaseModelAdmin):
 
     def get_inline_instances(self, request, obj=None):
         inline_instances = []
-        for inline_class in self.get_inlines(request, obj):
+        for inline_class in self.get_inlines(request, obj)[1:]:
             inline = inline_class(self.model, self.admin_site)
             if request:
                 if not (inline.has_view_or_change_permission(request, obj) or
```
**变异语义**：第一个 inline 类始终被跳过，导致 F2P 测试中 `AlternateInline`（inlines[0]）的实例从不出现在 `get_inline_instances` 结果中。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..9895107338 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -579,6 +579,7 @@ class ModelAdmin(BaseModelAdmin):
         self.model = model
         self.opts = model._meta
         self.admin_site = admin_site
+        self.inlines = list(self.inlines)
         super().__init__()
```
**分类**：🔴 必须替换

**理由**：在 patch 后状态中，`get_inline_instances` 调用 `self.get_inlines(request, obj)` 而非 `self.inlines`，所以在 `__init__` 中将 `self.inlines` 转换为 list 完全不影响行为（`get_inlines` 的默认实现返回的 `self.inlines` 也只是遍历，无实质差别）。该 mutation 无法使任何 F2P 测试失败，属于功能无关的修改。

**最终 mutation**（替换）：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..c0d709891e 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -589,10 +589,6 @@ class ModelAdmin(BaseModelAdmin):
         for inline_class in self.get_inlines(request, obj):
             inline = inline_class(self.model, self.admin_site)
             if request:
-                if not (inline.has_view_or_change_permission(request, obj) or
-                        inline.has_add_permission(request, obj) or
-                        inline.has_delete_permission(request, obj)):
-                    continue
                 if not inline.has_add_permission(request, obj):
                     inline.max_num = 0
             inline_instances.append(inline)
```
**变异语义**：移除了 `get_inline_instances` 中的权限门卫检查（`has_view_or_change_permission` / `has_add_permission` / `has_delete_permission` 三重检查后的 `continue`），保留了 `max_num=0` 的限制。这模拟了开发者在重构时遗漏权限检查的错误。在测试环境中，mock request 通常默认允许所有权限，所以大多数简单测试会通过；但在专门测试权限控制的 F2P 场景（如 `test_get_inline_instances_override_get_inlines` 中通过 `request.name` 控制返回空列表的测试用例）下，由于权限检查被跳过，可能导致本应返回空列表的情况下仍然返回实例。此外，涉及 `has_view_or_change_permission` 返回 False 时应排除 inline 的测试会失败。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..1bf9dc228c 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -585,7 +585,7 @@ class ModelAdmin(BaseModelAdmin):
         return "%s.%s" % (self.model._meta.app_label, self.__class__.__name__)
 
     def get_inline_instances(self, request, obj=None):
-        inline_instances = []
+        inline_instances = self.__dict__.setdefault("_inline_cache", [])
         for inline_class in self.get_inlines(request, obj):
```
**分类**：🔴 必须替换（不自然）

**理由**：`self.__dict__.setdefault("_inline_cache", [])` 是明显的人工构造——用 `__dict__` 直接访问实例字典极为少见，加上 `_inline_cache` 命名和 setdefault 模式，代码审查者会立即识别为刻意引入的状态缓存 bug。Django 代码风格中从不使用此类模式。

**最终 mutation**（替换）：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..64ed645b45 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -788,7 +788,7 @@ class ModelAdmin(BaseModelAdmin):
         """
         Yield formsets and the corresponding inlines.
         """
-        for inline in self.get_inline_instances(request, obj):
+        for inline in self.get_inline_instances(request):
             yield inline.get_formset(request, obj), inline
```
**变异语义**：`get_formsets_with_inlines` 调用 `get_inline_instances` 时不再传递 `obj`，导致 `get_inline_instances` 用 `obj=None` 调用 `get_inlines(request, None)`。子类如果在 `get_inlines` 中依赖 `obj` 参数（例如根据当前实例的某个字段决定 inline 列表），在通过 `get_formsets_with_inlines` 路径时会收到 `None` 而非真实对象，产生错误行为。这模拟了开发者在调用链上漏传参数的真实错误，且 `get_inline_instances` 的签名中 `obj=None` 是默认值，非常不易察觉。对于 F2P 测试（子类用 `request.name` 而非 `obj` 分支），直接调用 `get_inline_instances(request)` 路径仍能通过，但通过 `get_formsets_with_inlines` 的集成路径会失败。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..8e214ded4c 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -584,9 +584,10 @@ class ModelAdmin(BaseModelAdmin):
     def __str__(self):
         return "%s.%s" % (self.model._meta.app_label, self.__class__.__name__)
 
-    def get_inline_instances(self, request, obj=None):
+    def get_inline_instances(self, request, obj=None, use_custom_inlines=False):
         inline_instances = []
-        for inline_class in self.get_inlines(request, obj):
+        inlines_to_use = self.get_inlines(request, obj) if use_custom_inlines else self.inlines
+        for inline_class in inlines_to_use:
```
**分类**：🔴 必须替换（不自然）

**理由**：`use_custom_inlines=False` 是典型的人工特征标志，任何代码审查者看到这个参数都会立即意识到这是刻意引入的开关来绕过新增的钩子机制。Django 框架代码风格中绝无此类 feature flag 模式。

**最终 mutation**（替换）：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index d64a2e9a28..01fd63a167 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -586,7 +586,7 @@ class ModelAdmin(BaseModelAdmin):
 
     def get_inline_instances(self, request, obj=None):
         inline_instances = []
-        for inline_class in self.get_inlines(request, obj):
+        for inline_class in self.get_inlines(request, None):
             inline = inline_class(self.model, self.admin_site)
             if request:
                 if not (inline.has_view_or_change_permission(request, obj) or
```
**变异语义**：`get_inline_instances` 调用 `get_inlines` 时硬编码传入 `None` 作为 `obj`，导致子类重写 `get_inlines` 时永远看不到真实的模型实例。当子类在 `get_inlines` 中根据 `obj` 的属性（如 `obj.pk`、`obj.status` 等）来决定显示哪些 inline 时，始终会收到 `None`，逻辑判断失效。这模拟了开发者在重构时将原本没有 `obj` 参数的代码升级为有 `obj` 参数的版本时，忘记在调用链中传递实际对象的真实错误。代码看起来合理（`None` 是合法的默认值，方法签名本身就有 `obj=None`），极难通过代码审查发现。F2P 测试中子类用 `request.name` 分支（不用 obj），所以简单场景通过，但专门测试 obj 相关行为的测试失败。

---

## 新设计 Mutation 说明

### Mutation A（替换原 A）

**位置选择依据**：原 mutation A 在调用点交换参数，新设计转移到**定义点**（`BaseModelAdmin.get_inlines` 的签名），攻击接口契约而非调用约定。

**模拟的真实错误**：开发者在添加新钩子方法时，不确定参数应该以 `obj` 还是 `request` 开头（参考 Django 中有些方法如 `get_queryset(request)` 只有 request，有些如 `save_model(request, obj, ...)` 以 request 开头，但某些方法的文档可能让人误以为应该 `(self, obj, request)`）。

**为何难发现**：默认实现不使用任何参数，因此 base class 行为完全不变。只有在子类重写时参数接收顺序才出错，且子类的重写代码看起来完全正确。

### Mutation C（替换原 C）

**位置选择依据**：golden patch 修复的两件事是"添加钩子"和"修改 get_inline_instances 调用点"，新 mutation C 攻击 `get_inline_instances` 中的**权限检查逻辑**——这是 golden patch 没有修改但在同一函数中紧密相关的代码。

**模拟的真实错误**：开发者在阅读 `get_inline_instances` 时，误以为 `has_add_permission` 检查已经覆盖了"是否显示 inline"的判断（因为 `max_num=0` 隐含了某种限制），从而删除了前面更严格的三重权限检查。这是一个典型的"权限检查层次混淆"错误。

### Mutation D（替换原 D）

**位置选择依据**：`get_formsets_with_inlines` 是调用 `get_inline_instances` 的上层函数，攻击它制造**跨函数参数传播**错误，让 bug 的根因（`obj` 没有被传递）和表现（inline 列表不随 obj 变化）在不同的函数层出现。

**模拟的真实错误**：开发者修改 `get_formsets_with_inlines` 时，用 `self.get_inline_instances(request)` 的简短形式调用，忘记传 `obj`——Django 中很多辅助方法接受 `obj=None` 默认值，这类遗漏极其常见。

### Mutation E（替换原 E）

**位置选择依据**：与 mutation D 类似，在 `get_inline_instances` 内部硬编码 `None`，但攻击的是向下传递给 `get_inlines` 的参数，而非从上层接收的参数。这使 bug 更隐蔽——函数签名是正确的，只是内部转发时丢失了参数。

**模拟的真实错误**：开发者在将 `get_inline_instances` 从 `self.inlines` 改为调用 `self.get_inlines(request, obj)` 时，最初写的是 `self.get_inlines(request)` 然后发现需要加 obj 参数，但在某个中间版本或 merge conflict 时错误地将 obj 写成了字面量 `None`。
