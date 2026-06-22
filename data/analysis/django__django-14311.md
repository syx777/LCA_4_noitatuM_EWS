# django__django-14311 Mutation 分析

## 问题背景

该 issue 要求 autoreloader 正确支持 `python -m custom_module.sub runserver` 这类启动方式。
原始修复仅处理了 `-m foo.bar`（`bar` 为包且存在 `foo/bar/__main__.py`）的情况。当以
`-m foo.bar.baz`（`baz.py` 位于 `foo/bar/` 下，是一个普通模块而非包）方式启动时，旧逻辑
通过 `__main__.__spec__.parent` 推导出的子进程参数变成了 `-m foo.bar`，丢失了末级模块 `baz`，
导致自动重载时重启了错误的模块。

涉及源码：`django/utils/autoreload.py` 的 `get_child_arguments()`。
F2P 测试：`tests/utils_tests/test_autoreload.py::TestChildArguments::test_run_as_non_django_module_non_package`
（依赖新增的 `tests/utils_tests/test_module/main_module.py`）。

## Golden Patch 语义分析

Golden 把原先单一判断：

```python
if getattr(__main__, '__spec__', None) is not None and __main__.__spec__.parent:
    args += ['-m', __main__.__spec__.parent]
```

改写为基于 `spec.name` 形态的分支：

```python
if getattr(__main__, '__spec__', None) is not None:
    spec = __main__.__spec__
    if (spec.name == '__main__' or spec.name.endswith('.__main__')) and spec.parent:
        name = spec.parent          # 包形式 -m foo.bar （__main__.py）→ 用父包
    else:
        name = spec.name            # 普通点分模块 -m foo.bar.baz → 直接用全名
    args += ['-m', name]
```

核心语义：只有当 `__main__` 模块本身是某个包的 `__main__`（`spec.name` 为 `__main__` 或以
`.__main__` 结尾）时，才回退到父包名 `spec.parent`；否则使用完整模块名 `spec.name`。
这恰好区分了「包启动」与「点分普通模块启动」两种场景。

## 调用链分析

`restart_with_reloader()` → `get_child_arguments()` 构造子进程命令行 → `subprocess.run(args)`。
`get_child_arguments()` 读取 `__main__.__spec__`（由 Python 的 `-m` 启动机制设置）的
`name` / `parent`，结合 `sys.argv` / `sys.warnoptions` 组装 `[executable, -m, <module>, *argv]`。
F2P 测试通过 mock `sys.modules['__main__']` 为 `main_module`（其 spec.name 为
`utils_tests.test_module.main_module`、parent 为 `utils_tests.test_module`）来验证输出应为
`-m utils_tests.test_module.main_module`，即必须走 `else: name = spec.name` 分支。

## 替换决策总览

| 组 | 原 diff 语义 | 分类 | 决策 | 最终变异落点 |
|----|--------------|------|------|--------------|
| A | `... and spec.parent` → `if spec.parent:`（删除 name 形态判断） | 🔴 golden 反向（buggy 原状） | 替换 | else 分支值 `name = spec.name.rsplit('.',1)[0]` |
| B | `and spec.parent` → `or spec.parent`（单 token 运算符交换） | 🟡 语义浅层（控制流边界） | 保留 | 同原 diff |
| E | 与 A 去除 index 行后逐字节相同 | 🔴 重复 + golden 反向 | 替换 | args 构造 `'-m', spec.parent or name` |

M(浅层)=1（仅 B），floor(1/2)=0，浅层不需替换。两个 🔴（A、E）全部替换为经验证的
正交变异。B 作为建模真实边界错误的关键控制流浅层变异保留。

## 各组 Mutation 分析

### Group A
- 原 diff：将 `if (spec.name == '__main__' or spec.name.endswith('.__main__')) and spec.parent:`
  简化为 `if spec.parent:`。这正是 golden 修复前的 buggy 行为（任何有 parent 的 spec 都回退父包），
  属于 golden→buggy 的直接逆向，🔴 必须替换。
- 最终 diff（已验证）：

```diff
@@ -228,7 +228,7 @@ def get_child_arguments():
         if (spec.name == '__main__' or spec.name.endswith('.__main__')) and spec.parent:
             name = spec.parent
         else:
-            name = spec.name
+            name = spec.name.rsplit('.', 1)[0]
```
- 变异语义：保留 golden 的分支结构不变，只在 else 分支对完整模块名做 `rsplit('.',1)[0]`，
  即丢弃末级子模块段。包启动场景（走 if 分支）完全不受影响，仅在点分普通模块输入时把
  `foo.bar.baz` 误截为 `foo.bar`，与 buggy 原状结果一致但成因隐蔽（像一处 off-by-one 路径处理）。

### Group B（保留）
- 原 diff：`and spec.parent` → `or spec.parent`，单 token 运算符交换，作用在 `__main__` 检测
  的关键控制流守卫上。属语义浅层但建模真实边界判断错误，🟡 保留。
- 最终 diff（同原）：

```diff
-        if (spec.name == '__main__' or spec.name.endswith('.__main__')) and spec.parent:
+        if (spec.name == '__main__' or spec.name.endswith('.__main__')) or spec.parent:
```
- 变异语义：对点分普通模块 `spec.parent` 为真，条件被错误满足，走入 `name = spec.parent`，
  输出父包名而非全名，命中 F2P；包启动场景行为不变，P2P 全通过。

### Group E
- 原 diff：去除 `index` 行后与 A 逐字节相同（byte-identical 重复 + golden 反向），🔴 必须替换。
- 最终 diff（已验证）：

```diff
@@ -229,7 +229,7 @@ def get_child_arguments():
             name = spec.parent
         else:
             name = spec.name
-        args += ['-m', name]
+        args += ['-m', spec.parent or name]
```
- 变异语义：把故障点下移到参数拼接处。`name` 计算保持 golden 正确逻辑，但拼接时用
  `spec.parent or name` 覆盖。对包启动（spec.parent 真）行为不变；对点分普通模块，
  spec.parent 为父包名（真值），覆盖掉 else 算出的正确全名，导致重载目标错误。这是一个
  与 A/B 不同层次（接口拼接 vs 取值/控制流）的正交失败模式。

## 新设计 Mutation 说明

- A1（替换 A）：else 分支取值截断 `spec.name.rsplit('.',1)[0]`，模拟路径段处理的 off-by-one。
- E1（替换 E）：args 拼接处 `spec.parent or name` 短路覆盖，模拟「优先用 parent」的错误兜底。
- 三者落点互不相同：B 在条件判断、A1 在取值表达式、E1 在下游拼接，覆盖三种正交故障注入点，
  且均只对「点分普通模块」这一特定输入组合失效，对包启动等常见输入保持正确，难以被泛化测试捕获。

## 真实验证结果

- Harness：`cp -r` 基线仓库 → `patch -p1` 应用 golden + test_patch（均 rc0）→ `git commit`。
- BASELINE：golden 无变异，`utils_tests.test_autoreload` 80 tests `OK (skipped=20)`；
  F2P `test_run_as_non_django_module_non_package` 单独运行 PASS。
- F2P module：`utils_tests.test_autoreload`，命令
  `python3 tests/runtests.py utils_tests.test_autoreload --parallel 1 -v 1`，
  `PYTHONPATH=<tmp>:$PYTHONPATH`。
- 每个最终变异（A1/B1/E1）：`git apply` rc0、`py_compile` OK、全模块运行
  `FAILED (failures=1, skipped=20)`，且唯一失败者为
  `test_run_as_non_django_module_non_package`（F2P），其余 79 个测试（含 P2P）全通过。
