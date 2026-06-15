# django__django-11119

## 问题背景

`Engine.render_to_string()` 方法在创建 `Context` 时未传入 `autoescape` 参数，导致 `Engine(autoescape=False)` 的设置被忽略，输出始终会进行 HTML 转义。黄金补丁将 `Context(context)` 修正为 `Context(context, autoescape=self.autoescape)`，使引擎的 `autoescape` 属性能正确传递到渲染上下文中。

## Golden Patch 语义分析

修复的核心是：`render_to_string` 在非 `Context` 分支创建新 `Context` 时，必须将 `self.autoescape`（引擎级别的转义开关）传递进去。`Context.__init__` 的 `autoescape` 默认值为 `True`，因此省略该参数等同于强制开启转义，与引擎配置完全脱钩。修复后，`Context(context, autoescape=self.autoescape)` 确保了引擎设置的一致性传播。

## 调用链分析

```
Engine(autoescape=False)
  └─ Engine.render_to_string(template_name, context)
       └─ Context(context, autoescape=self.autoescape)   ← 修复点
            └─ Template.render(context)
                 └─ NodeList.render(context)
                      └─ VariableNode.render(context)
                           └─ render_value_in_context(value, context)
                                └─ if context.autoescape: conditional_escape(value)
                                   else: str(value)
```

`context.autoescape` 在 `render_value_in_context`（`base.py:970`）中被读取，决定是否对变量值进行 HTML 转义。调用链上 `autoescape` 的唯一传入点就是 `Context.__init__`，所以 `render_to_string` 若不显式传递，将使用默认值 `True`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换（原不存在） | 新设计 | 无原始 mutation，需全新设计 |
| B | 必须替换（原不存在） | 新设计 | 无原始 mutation，需全新设计 |
| C | 🔴 必须替换 | 替换 | 原 mutation 是 golden patch 的直接逆操作 |
| D | 🔴 必须替换 | 替换 | 原 mutation 与 C 完全相同（重复） |
| E | 🔴 必须替换 | 替换 | 添加无意义的 use_engine_autoescape 参数，人工痕迹明显 |

语义浅层共 0 个，全部为必须替换，共替换 5 个。

## 各组 Mutation 分析

### Group A — 替换（新设计）

**原 mutation**：不存在

**分类**：新设计

**理由**：mutations.jsonl 中仅有 C/D/E 三组，A/B 缺失，需全新设计。

**最终 mutation**：

```diff
diff --git a/django/template/engine.py b/django/template/engine.py
index ff9ce58d59..ff2b9f0261 100644
--- a/django/template/engine.py
+++ b/django/template/engine.py
@@ -41,7 +41,7 @@ class Engine:
 
         self.dirs = dirs
         self.app_dirs = app_dirs
-        self.autoescape = autoescape
+        self.autoescape = not autoescape
         self.context_processors = context_processors
         self.debug = debug
         self.loaders = loaders
```

**变异语义**：在 `Engine.__init__` 中将 `self.autoescape` 取反存储。当用户创建 `Engine(autoescape=False)` 时，引擎实际存储 `self.autoescape=True`，导致 `render_to_string` 中 `Context(context, autoescape=True)` 始终开启转义。`test_basic_context`（`obj='test'`，纯文本）因转义无副作用而通过，`test_autoescape_off`（`obj='<script>'`）则会得到转义后的 `&lt;script&gt;` 而失败。此 mutation 看似合理（仅在 `__init__` 一行），实际影响整个引擎生命周期内所有 `render_to_string` 调用。

---

### Group B — 替换（新设计）

**原 mutation**：不存在

**分类**：新设计

**理由**：B 组缺失，需全新设计，选择不同于 A 的修改位置。

**最终 mutation**：

```diff
diff --git a/django/template/engine.py b/django/template/engine.py
index ff9ce58d59..c79e2f41b2 100644
--- a/django/template/engine.py
+++ b/django/template/engine.py
@@ -160,7 +160,7 @@ class Engine:
         if isinstance(context, Context):
             return t.render(context)
         else:
-            return t.render(Context(context, autoescape=self.autoescape))
+            return t.render(Context(context, autoescape=True))
 
     def select_template(self, template_name_list):
         """
```

**变异语义**：在 `render_to_string` 中将动态的 `autoescape=self.autoescape` 替换为硬编码的 `autoescape=True`。引擎的 `autoescape` 设置被完全忽略，始终开启转义。与直接还原（删除 `autoescape` 参数）相比，此 mutation 看起来像是"显式声明默认值"，审查者可能认为这是冗余但正确的写法。`test_basic_context` 通过，`test_autoescape_off` 失败。

---

### Group C — 替换（原为直接逆操作）

**原 mutation**：

```diff
diff --git a/django/template/engine.py b/django/template/engine.py
index ff9ce58d59..dfaa67ba12 100644
--- a/django/template/engine.py
+++ b/django/template/engine.py
@@ -160,7 +160,7 @@ class Engine:
         if isinstance(context, Context):
             return t.render(context)
         else:
-            return t.render(Context(context, autoescape=self.autoescape))
+            return t.render(Context(context))
```

**分类**：🔴 必须替换 — 直接还原 golden patch（逆操作）

**理由**：该 mutation 就是将 golden patch 的修复撤销，等同于 base_commit 状态，是最低质量的 mutation。

**最终 mutation**：

```diff
diff --git a/django/template/context.py b/django/template/context.py
index 8f349a3a96..6a75cbc652 100644
--- a/django/template/context.py
+++ b/django/template/context.py
@@ -135,7 +135,7 @@ class BaseContext:
 class Context(BaseContext):
     "A stack container for variable context"
     def __init__(self, dict_=None, autoescape=True, use_l10n=None, use_tz=None):
-        self.autoescape = autoescape
+        self.autoescape = autoescape if autoescape else True
         self.use_l10n = use_l10n
         self.use_tz = use_tz
         self.template_name = "unknown"
```

**变异语义**：在 `Context.__init__` 中，用条件表达式使 `autoescape=False` 被静默覆盖为 `True`。逻辑上看像是"防御性编程"（`autoescape if autoescape else True` 似乎在处理 falsy 值），实则任何传入 `False` 的调用都会被覆盖。此 mutation 在 `context.py` 中修改，位置远离修复点，代码风格也可解释为"保持 autoescape 非空的保障"。`AutoEscapeControlNode.render` 通过直接设置 `context.autoescape = self.setting` 来改变转义状态，因此 `{% autoescape off %}` 标签仍可正常工作（该标签直接写属性，绕过 `__init__`）。

---

### Group D — 替换（原与 C 完全重复）

**原 mutation**：与 Group C 完全相同的 diff。

**分类**：🔴 必须替换 — 与 C 功能等价且内容完全重复

**理由**：两个完全相同的 mutation 没有独立测试价值。

**最终 mutation**：

```diff
diff --git a/django/template/engine.py b/django/template/engine.py
index ff9ce58d59..c9a6659351 100644
--- a/django/template/engine.py
+++ b/django/template/engine.py
@@ -160,7 +160,7 @@ class Engine:
         if isinstance(context, Context):
             return t.render(context)
         else:
-            return t.render(Context(context, autoescape=self.autoescape))
+            return t.render(Context(context, autoescape=not self.autoescape))
 
     def select_template(self, template_name_list):
         """
```

**变异语义**：将 `autoescape=self.autoescape` 改为 `autoescape=not self.autoescape`，使渲染时的转义设置与引擎设置完全相反。当 `Engine(autoescape=False)` 时，`Context` 使用 `autoescape=True` 转义；当 `Engine(autoescape=True)`（默认）时，`Context` 使用 `autoescape=False` 不转义。`test_basic_context` 使用默认引擎（`autoescape=True`），渲染纯文本 `'test'`，即使不转义结果也相同，因此通过。但 `test_autoescape_off` 使用 `Engine(autoescape=False)`，反转后变成 `autoescape=True`，`<script>` 被转义，测试失败。

---

### Group E — 替换（原为不自然的接口设计）

**原 mutation**：

```diff
diff --git a/django/template/engine.py b/django/template/engine.py
...
-    def render_to_string(self, template_name, context=None):
+    def render_to_string(self, template_name, context=None, use_engine_autoescape=False):
...
-            return t.render(Context(context, autoescape=self.autoescape))
+            if use_engine_autoescape:
+                return t.render(Context(context, autoescape=self.autoescape))
+            else:
+                return t.render(Context(context))
```

**分类**：🔴 必须替换 — 不自然，人工痕迹明显（新增从未使用的参数，默认值使修复失效）

**理由**：添加 `use_engine_autoescape=False` 参数是典型的人工注入痕迹，实际代码中不会有开发者设计这样的接口。

**最终 mutation**：

```diff
diff --git a/django/template/engine.py b/django/template/engine.py
index ff9ce58d59..7b0f6ea7b0 100644
--- a/django/template/engine.py
+++ b/django/template/engine.py
@@ -160,7 +160,9 @@ class Engine:
         if isinstance(context, Context):
             return t.render(context)
         else:
-            return t.render(Context(context, autoescape=self.autoescape))
+            ctx = Context(context, autoescape=self.autoescape)
+            ctx.autoescape = Context().autoescape
+            return t.render(ctx)
 
     def select_template(self, template_name_list):
         """
```

**变异语义**：先正确创建 `ctx = Context(context, autoescape=self.autoescape)`，然后立即用 `ctx.autoescape = Context().autoescape` 覆盖 autoescape 属性。`Context()` 不带参数创建时使用默认 `autoescape=True`，因此无论引擎设置如何，最终 `ctx.autoescape` 总被重置为 `True`。此 mutation 在两行代码之间"自我修复再破坏"，看起来像是为了"重用默认值而重置"的合理优化，但实则覆盖了引擎的配置。代码审查者难以察觉这是 bug，因为第一行的写法是正确的。

---

## 新设计 Mutation 说明

**Group A**：基于对 `Engine.__init__` 中 `self.autoescape = autoescape` 赋值点的分析。该位置是引擎 autoescape 状态的唯一存储点，取反后影响整个引擎实例的所有渲染调用。模拟了开发者在实现"反转转义模式"功能时错误地持久化了取反值。

**Group B**：在修复后的 `render_to_string` 中，将动态值替换为硬编码 `True`。模拟了开发者认为"明确写出默认值更清晰"时的疏忽，这类错误在 code review 中极难发现，因为 `True` 看起来就是合理的显式默认值。

**Group C**：利用 `Context.__init__` 中 `self.autoescape = autoescape` 的赋值，用条件表达式使 `False` 值被静默转换为 `True`。模拟了防御性编程风格中对 falsy 参数的"保护性处理"，看似合理但实则破坏了 `autoescape=False` 的语义。跨文件（`context.py` 而非 `engine.py`），使得溯因更困难。

**Group D**：在 `render_to_string` 中将 `autoescape=self.autoescape` 改为 `autoescape=not self.autoescape`。模拟了开发者在修复 bug 时误将逻辑取反（认为需要"翻转"传递方向），产生了与预期完全相反的行为。

**Group E**：先正确创建 Context，再用 `Context().autoescape`（默认值 `True`）覆盖 autoescape 属性。模拟了开发者"规范化/重置上下文状态"时的错误，这种两步赋值模式（先设置后覆盖）在多行代码中很难被察觉。
