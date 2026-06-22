# django__django-15814

## 问题背景

对 proxy model 同时用 `select_related()` + `only()` 时崩溃：`ValueError: 'id' is not in list`。原因在 `Query.deferred_to_data` 遍历关系路径时，`cur_model` 可能是 proxy model，其 `_meta` 不含具体字段的完整 pk 信息（proxy 没有自己的字段表），导致后续 `RelatedPopulator` 在 `init_list` 里找不到 pk attname。Golden patch 在取到 `cur_model` 后加一行 `cur_model = cur_model._meta.concrete_model`，把 proxy 规整为其 concrete model，再取 `opts = cur_model._meta`。

## Golden Patch 语义分析

```python
if is_reverse_o2o(source):
    cur_model = source.related_model
else:
    cur_model = source.remote_field.model
cur_model = cur_model._meta.concrete_model   # ← 新增
opts = cur_model._meta
```
核心语义：**沿关系跳到下一个模型后，必须把可能是 proxy 的 `cur_model` 规整为 concrete model**，这样 `opts`、后续 `add_to_dict(must_include, cur_model, opts.pk)`、以及 select 列表构造都基于具体模型的真实字段。proxy 与 concrete 共享数据库表，但只有 concrete model 的 `_meta` 持有完整字段/pk 定义。

F2P 测试 `ProxyModelTests.test_select_related_only`：`Issue.objects.select_related("assignee").only("assignee__status")`，其中 assignee 指向 `ProxyTrackerUser`（proxy），断言 `qs.get()` 不崩溃且返回正确对象。

## 调用链分析

`deferred_to_data` 为 `only()`/`defer()` 计算每个模型要加载的字段集合。遍历 `parts[:-1]` 逐跳关系：`source = opts.get_field(name)` → 更新 `cur_model` → `opts = cur_model._meta` → `add_to_dict(must_include, cur_model, opts.pk)`。结果 `must_include`/`seen` 传给 SQL 编译，最终影响 `RelatedPopulator.init_list`。若 `cur_model` 是 proxy，`opts.pk` 与 select 列表中真实列不对应，`init_list.index(pk.attname)` 抛 ValueError。新增行把 proxy 规整为 concrete 即可。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `.concrete_model`→`.model`，proxy 的 `_meta.model` 仍是 proxy 自身 |
| B | 🟢 高质量 | 保留 | `.concrete_model`→`._meta`，`cur_model` 变成 Options 对象 |
| C | 🟢 高质量 | 保留 | 删除整行规整，直接还原原 bug |
| D | 🔴 必须替换 | 替换 | 原 D 与 C 完全相同（删行）；改为赋值到错误变量名（死赋值） |
| E | 🟢 高质量 | 保留 | 只在非 proxy 时规整（`if not proxy`），proxy 场景反而不规整 |

原 C=D 字节完全相同（均删除规整行）。保留 C，把 D 改为死赋值状态错误。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：`.model` vs `.concrete_model`）
```diff
-                cur_model = cur_model._meta.concrete_model
+                cur_model = cur_model._meta.model
```
**变异语义**：`_meta.model` 返回定义该 `_meta` 的模型本身（proxy 则仍是 proxy），而非 `concrete_model`（穿透到具体表模型）。对非 proxy 二者相同，故普通用例通过；proxy 场景下 `cur_model` 仍是 proxy，pk/字段不全，F2P 崩溃。模拟"`.model` 与 `.concrete_model` 混淆"。保留。

### Group B — 保留（A2 接口契约：少一级属性）
```diff
-                cur_model = cur_model._meta.concrete_model
+                cur_model = cur_model._meta
```
**变异语义**：少写 `.concrete_model`，`cur_model` 被赋成 `Options` 对象而非 model 类。随后 `opts = cur_model._meta`（Options 无 `_meta`）或 `add_to_dict` 用 Options 当 key 都会出错。模拟"属性链少写一级"。保留。

### Group C — 保留（B2 移除规整行）
```diff
-                cur_model = cur_model._meta.concrete_model
                 opts = cur_model._meta
```
**变异语义**：直接删除规整行，`cur_model` 保持 proxy，完全还原原 bug。F2P 崩溃。保留。

### Group D — 替换（D1 状态：死赋值到错误变量）
**原**：与 C 完全相同（删行）。
**最终 mutation**：
```diff
-                cur_model = cur_model._meta.concrete_model
+                concrete_model = cur_model._meta.concrete_model
                 opts = cur_model._meta
```
**变异语义**：规整结果赋给了一个**从未被使用**的新局部变量 `concrete_model`，`cur_model` 本身没被更新仍是 proxy。代码看起来"做了 concrete_model 转换"，实则是死赋值（dead store）——后续 `opts`、`add_to_dict` 全用未变的 proxy `cur_model`。比直接删行更隐蔽：审查者瞥见 `concrete_model = ...` 容易误以为已正确处理。F2P 崩溃。

### Group E — 保留（B3 条件反转语义）
```diff
-                cur_model = cur_model._meta.concrete_model
+                if not cur_model._meta.proxy:
+                    cur_model = cur_model._meta.concrete_model
```
**变异语义**：加守卫"只在**非 proxy** 时才规整"。但 proxy 才是真正需要规整的场景（concrete model 规整后是自身、无副作用），守卫恰好把唯一需要修复的情况排除掉。逻辑看似谨慎实则完全错位，proxy 场景仍崩溃。保留。

## 新设计 Mutation 说明

原 C/D 两组字节完全相同（都删规整行）。保留 A（`.model`）、B（少一级 `_meta`）、C（删行）、E（非 proxy 才规整），把重复的 D 改为 D1 状态错误——把 `concrete_model` 赋给一个未使用的新变量名（死赋值），`cur_model` 实际未更新。五组覆盖"`.model`/`.concrete_model` 混淆 / 少一级属性 / 删行 / 死赋值 / 条件反转"五个角度。全部实测：golden 通过、五个变异均令 F2P（`test_select_related_only`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
