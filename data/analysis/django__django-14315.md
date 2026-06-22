# django__django-14315 Mutation 分析

## 问题背景

PostgreSQL 的数据库客户端 `runshell` 在某些情况下不尊重 `os.environ` 的值。
`settings_to_cmd_args_env` 在没有任何特殊设置时返回空字典 `{}` 作为 `env`。
基类 `BaseDatabaseClient.runshell` 原始逻辑为 `if env: env = {**os.environ, **env}`：
当 `env` 为空字典时分支不进入，`env` 保持为 `{}` 并直接传给
`subprocess.run(args, env={})`，导致子进程拿到一个**空环境**而不是继承父进程的
`os.environ`。期望行为：当没有需要覆盖的变量时应传 `env=None`，让 subprocess 继承
当前进程环境。

## Golden Patch 语义分析

Golden patch 修改两处：

1. `django/db/backends/base/client.py`:
   `if env: env = {**os.environ, **env}` 改为
   `env = {**os.environ, **env} if env else None`。
   核心契约：**有自定义 env 时与 os.environ 合并；否则显式置为 None**（让 subprocess 继承环境）。
2. `django/db/backends/postgresql/client.py`:
   `return args, env` 改为 `return args, (env or None)`，把空字典规整为 None。

二者协同保证：无覆盖变量时最终传给 `subprocess.run` 的 `env` 是 `None`。

## 调用链分析

- `DatabaseClient.settings_to_cmd_args_env`（postgresql）构造 `args` 与 `env`（初始 `env={}`）。
- `DatabaseClient.runshell` -> `super().runshell()`（base）-> `subprocess.run(args, env=env)`。
- F2P 测试 `tests/backends/base/test_client.py::test_runshell_use_environ` 直接 mock
  `settings_to_cmd_args_env` 返回 `([], None)` 与 `([], {})` 两种情况，断言
  `subprocess.run` 被以 `env=None` 调用。这是判定核心契约的关键测试，**位于 base 层**。
- `tests/dbshell/test_postgresql.py` 的 `test_nopass` / `test_parameters` 改为期望 `None`，
  验证 postgresql 层空字典被规整为 None；其余选项测试（test_basic 等）仍期望具体非空 dict。

重要约束：postgresql `env` 始终是 dict（初始 `{}`），任何针对 postgresql 层
对**非空**情况的改动都会破坏 P2P（具体 dict 断言）。因此非冗余替换只能放在 base 层
`runshell` 的返回值/合并语义上。

## 替换决策总览

| 组 | 原类别 | 决策 | 原因 |
|----|--------|------|------|
| B | 🔴 MUST REPLACE | 替换 | 直接 revert postgresql golden（`(env or None)`→`env`），且与 D 字节级重复 |
| C | 🟡 SEMANTIC-SHALLOW | 保留 | 关键控制流边界突变（`if env`→`if env is not None`），建模真实边界错误 |
| D | 🔴 MUST REPLACE | 替换 | 与 B 字节级完全相同的重复 diff |
| E | 🔴 MUST REPLACE | 替换 | 新增 `merge_environ=False` 守卫参数，默认关闭整个修复=人为禁用 fix 的不自然伪迹 |

共替换 3 个（B、D、E），保留 C。所有替换落在 base 层，互相正交且与 C 正交。

## 各组 Mutation 分析

### Group B
- 原 diff：postgresql `return args, (env or None)` → `return args, env`。
- 分类：🔴。直接把 golden 的 postgresql 改动还原回 buggy 原状（reverse of golden），同时与
  Group D 完全字节相同。属直接冗余 + 重复。
- 理由：是 golden 的逆操作，且重复。
- 最终 diff（base 层重设计）：
  `env = {**os.environ, **env} if env else None` → `env = {**os.environ, **(env or {})}`
- 变异语义：falsy env 时仍构造合并 dict，结果**永不为 None**。subprocess 仍拿到含
  os.environ 的有效环境，行为看似正确；只有断言"空 env 必须为 None 契约"的测试能抓到。
  两个 subtest（None 与 {}）均失败。

### Group C（保留）
- 原 diff：base `if env` → `if env is not None`。
- 分类：🟡 关键控制流边界突变。
- 理由：建模真实的"空容器边界"错误——空字典 `{}` 现被当作真实 env 与 os.environ 合并，
  不再变 None。只有 `env={}` 这一 subtest 失败，`env=None` 仍正常，部分覆盖的测试会漏检。
  位于关键控制流节点，符合保留标准。
- 最终 diff = 原 diff（无改动）。

### Group D
- 原 diff：与 Group B **字节相同**（postgresql `(env or None)`→`env`）。
- 分类：🔴 重复 diff（B/D 二者保留其一，另一个为 🔴）。
- 理由：与 B 完全重复。
- 最终 diff（base 层重设计）：
  `... if env else None` → `... if env else {}`
- 变异语义：falsy env 时返回**空字典 `{}`** 而非 None。`{}` 与 None 对开发者都"看起来空"，
  但 `subprocess.run(env={})` 会忽略 os.environ，重新引入原始 bug；只验证"返回的是 dict"的
  形状测试会通过。两个 subtest 均失败。

### Group E
- 原 diff：base `def runshell(self, parameters)` → `def runshell(self, parameters, merge_environ=False)`
  并用 `if merge_environ:` 守卫合并逻辑，默认不合并。
- 分类：🔴 不自然伪迹——新增默认 False 的守卫参数把整个修复在默认路径上禁用（dead guard disabling the fix）。
- 理由：人为引入失活参数禁用 fix，非真实开发者写法。
- 最终 diff（base 层重设计）：
  `... if env else None` → `... if env else os.environ`
- 变异语义：falsy env 时返回**活的 `os.environ` 对象本身**而非 None。subprocess 仍继承环境，
  外观正确，但违反 None 契约且泄漏可变全局 environ；只有 identity/None 断言能检出。两个 subtest 均失败。

## 新设计 Mutation 说明

三个替换（B/D/E）全部作用于 `BaseDatabaseClient.runshell` 的 `env` 归一化语义，
但失败模式互相正交：
- B：恒为合并 dict（永不 None）；
- D：falsy 时为空 dict `{}`（重现空环境 bug）；
- E：falsy 时为 `os.environ` 本体（泄漏可变全局）。
C 则作用于条件分支边界（`is not None`），仅 `{}` 子用例失败。四者覆盖不同的"empty/None
契约"误用维度，对 LLM 生成的测试构成多样化检测难度。

## 真实验证结果

- 验证环境：Python 3.8.12，`tests/runtests.py`，POST-PATCH 工作树（golden + test_patch 已 commit 为 HEAD）。
- F2P 模块：`backends.base.test_client`（含新增 `test_runshell_use_environ`），并联跑 `dbshell.test_postgresql`。
- BASELINE（golden 无变异）：`Ran 12 tests ... OK (skipped=1)`，rc=0，通过。✅
- 各最终变异（均 py_compile OK，可 git apply）：

| 组 | F2P 结果 | 失败数 | 仅 F2P 失败 |
|----|----------|--------|-------------|
| B | FAILED rc=1 | 2 subtests (None,{}) | 是，P2P 全过 |
| C | FAILED rc=1 | 1 subtest ({}) | 是，P2P 全过 |
| D | FAILED rc=1 | 2 subtests | 是，P2P 全过 |
| E | FAILED rc=1 | 2 subtests | 是，P2P 全过 |

逐测试核验（以 B 为例，`-v 2`）：仅 `test_runshell_use_environ` 失败，
`dbshell.test_postgresql` 的 test_basic/test_nopass/test_parameters/test_ssl_certificate
等全部 ok，未破坏 P2P。
