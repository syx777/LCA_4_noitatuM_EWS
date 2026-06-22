# django__django-14855 Mutation 分析

## 问题背景

当一个包含 `ForeignKey` 字段的模型在**自定义 Admin Site**（custom Admin Site）中以只读方式查看/编辑，且该外键被列入 `readonly_fields` 时，`AdminReadonlyField.get_admin_url` 生成的链接 URL 会错误地指向默认的 `/admin/...`，而不是自定义站点的 `/custom-admin/...`。

根因在于 `django/contrib/admin/helpers.py` 的 `get_admin_url` 调用 `reverse()` 时没有传入 `current_app` 参数。Admin 的其他部分（如 `ModelAdmin.response_add`）都使用 `current_app=self.admin_site.name` 来标识当前站点命名空间。

## Golden Patch 语义分析

```python
url = reverse(
    url_name,
    args=[quote(remote_obj.pk)],
    current_app=self.model_admin.admin_site.name,
)
```

Golden patch 仅新增了 `current_app=self.model_admin.admin_site.name` 关键字参数。`reverse()` 在解析带命名空间的 URL 时依据 `current_app` 选择正确的站点实例，从而生成属于当前 Admin Site 的 URL。

## 调用链分析

1. 自定义站点 `site2 = AdminSite(name="namespaced_admin")` 注册了 `ReadOnlyRelatedField` 与 `Language`。
2. 访问 change 页面 → 渲染只读外键字段 → `AdminReadonlyField.contents()` → `get_admin_url(remote_field, remote_obj)`。
3. `url_name = 'admin:%s_%s_change' % (app_label, model_name)`，再 `reverse(url_name, args=[quote(pk)], current_app=...)`。
4. 返回 `<a href="{url}">{remote_obj}</a>`，若 `NoReverseMatch` 则降级为 `str(remote_obj)` 纯文本。

F2P 测试（`tests/admin_views/tests.py::ReadonlyTest`）将原 `test_readonly_foreignkey_links` 改造为参数化的 `_test_readonly_foreignkey_links(admin_site)`，并派生出：
- `test_readonly_foreignkey_links_default_admin_site('admin')`
- `test_readonly_foreignkey_links_custom_admin_site('namespaced_admin')`

它们分别断言生成的链接 href 与可见文本（`super`、`_40`）完全匹配，并验证 `Language` 的字符串主键 `_40` 经 `quote()` 编码为 `_5F40`。

## 替换决策总览

| 槽位 | 原策略 | 原始判定 | 决策 | 新 strategy_code | 失败机制 |
|------|--------|----------|------|------------------|----------|
| A | 删除 `current_app` 行 | 🔴 直接 golden-revert 冗余 | 替换 | A1 | 硬编码 `current_app='admin'`，仅自定义站点失败 |
| B | 调换 url_name 顺序 | 🔴 破坏 P2P `test_readonly_onetoone_backwards_ref`（两站点皆降级纯文本，连默认站点的 onetoone 测试也挂） | 重设计 | C1 | 用错属性 `admin_site.site_url`（恒为 `/`）取代 `.name`，仅自定义站点失败 |
| C | 去掉 `quote()` | 🟢 正交语义改动 | 保留 | C3 | 字符串主键未编码（`_40` vs `_5F40`） |
| D | 锚文本 `remote_obj.pk` | 🔴 破坏 P2P `test_readonly_onetoone_backwards_ref`（该测试断言默认站点锚文本，pk 改写令其失败） | 重设计 | B2 | 条件分支仅对 `name=='admin'` 传 current_app，仅自定义站点失败 |
| E | 新增 `use_current_app=False` 形参门控 | 🟢 API 契约改动 | 保留 | A1 | 默认不传参 → 自定义站点退回 bug 行为 |

A 原始为直接 golden-revert，必须替换。B、D 的初版（调换 url_name 顺序 / 锚文本改 `remote_obj.pk`）经独立复核被**否决**：它们不针对 current_app 维度，而是改动了默认站点也会走到的 url_name 构造 / 锚文本，导致**预先存在的 P2P 测试 `test_readonly_onetoone_backwards_ref` 回归**（该测试在默认站点上断言精确锚标签）。破坏任何 P2P 测试都会被既有套件捕获，使 mutation 失效。重设计后的 B、D 改为只攻击 current_app/站点命名空间维度——这正是区分自定义站点 F2P 测试与通用 readonly 测试的关键，从而只破坏自定义站点 F2P 测试、零 P2P 回归。C、E 为正交高质量 mutation，保留。

## 各组 Mutation 分析

- **A（原）**：直接删除 golden 新增行 → 直接 golden-revert，必须替换。
- **B（原，否决）**：调换 `url_name` 模板中的 `app_label`/`model_name` → 两站点的 `reverse()` 均抛 `NoReverseMatch`、降级为纯文本。默认站点同样受影响，导致 P2P `test_readonly_onetoone_backwards_ref` 回归（共 3 个失败：2 F2P + 1 P2P），被既有套件捕获，无效，必须重设计。
- **C（原，保留）**：`args=[remote_obj.pk]`，去掉 `quote()`。只有当主键包含 URL 保留字符时（`Language.iso='_40'`）才与编码后 URL 不符。整数主键场景无感知，失败模式独特。
- **D（原，否决）**：锚文本由 `remote_obj` 改为 `remote_obj.pk`，href 正确但可见文本变化。默认站点同样渲染该锚，导致 P2P `test_readonly_onetoone_backwards_ref`（断言 `Brand New Plot`）回归（共 3 个失败：2 F2P + 1 P2P），无效，必须重设计。
- **E（原，保留）**：将 `current_app` 改为受 `use_current_app=False` 形参门控，调用方从不传参，等效于退回 bug。签名看似刻意的可选 API，自然度高，保留。

## 新设计 Mutation 说明

为最大化失败模式正交性，三个替换刻意采用不同机制：

为保证 B、D 仅破坏 F2P 而零 P2P 回归，二者均**专攻 current_app/站点命名空间维度**（默认 `admin` 站点无论 current_app 如何都能正确反解 `/admin/`，故只能影响自定义站点 F2P 测试，绝不波及走默认站点的 onetoone/manytomany 等 readonly 测试）。

- **A → A1（硬编码默认站点名）**：`current_app='admin'`。默认站点解析一致，`test_..._default_admin_site` 通过；自定义站点 `namespaced_admin` 被错误解析到默认命名空间，仅 `test_..._custom_admin_site` 失败（failures=1）。模拟开发者"先写死调通默认情况"的常见疏忽。

- **B → C1（取错属性源）**：把站点身份从 `admin_site.name` 改读 `admin_site.site_url`。`site_url` 在所有站点默认均为 `'/'`，看似站点相关实则恒定，`reverse()` 因此回退到默认命名空间。属性混淆型笔误，自然度高。默认站点测试仍解析到 `/admin/` 通过，仅 `test_..._custom_admin_site` 失败（failures=1）。

- **D → B2（条件分支反转意图）**：`current_app=...name if name=='admin' else None`，读起来像"只为默认 admin 站点加命名空间"的防御式特判，实则令自定义站点拿到 `current_app=None` 退回默认命名空间。默认站点（name=='admin'）通过，仅 `test_..._custom_admin_site` 失败（failures=1）。与 A（硬编码）、B（错属性）、E（形参门控）机制正交。

保留的 C（C3 编码）、E（A1 形参门控）与三个新设计共同覆盖了：站点命名空间多种入口（A 硬编码 / B 错属性 / D 条件分支 / E 形参门控）与主键编码（C）五种正交失败模式。所有 5 个 mutation 均经真实测试验证：编译通过且至少触发 1 个 F2P 失败，无 P2P 回归（B、D 经完整 `admin_views.tests` 模块 345 测试确认唯一失败为 `test_readonly_foreignkey_links_custom_admin_site`，`test_readonly_onetoone_backwards_ref` 通过）。
