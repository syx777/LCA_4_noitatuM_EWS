# django__django-14376 Mutation 分析

## 问题背景

MySQL backend 使用了被 mysqlclient 标记为弃用的 `db` 与 `passwd` 关键字参数，应改用
`database` 与 `password`。Golden patch 修改两处：

- `django/db/backends/mysql/base.py::get_connection_params` 中把 `kwargs['db']` /
  `kwargs['passwd']` 改为 `kwargs['database']` / `kwargs['password']`。
- `django/db/backends/mysql/client.py::settings_to_cmd_args_env` 中，dbshell 在读取
  `OPTIONS` 时优先取新键 `database`/`password`，并保留对旧键 `db`/`passwd` 的回退，
  最终向后兼容。

F2P 测试位于 `tests/dbshell/test_mysql.py`，模块路径 `dbshell.test_mysql`。

## Golden Patch 语义分析

client.py 的核心改动是把单键查找：

```python
db = settings_dict['OPTIONS'].get('db', settings_dict['NAME'])
```

替换为新键优先、旧键回退、最后落到 `NAME` 的三级查找：

```python
database = settings_dict['OPTIONS'].get(
    'database',
    settings_dict['OPTIONS'].get('db', settings_dict['NAME']),
)
```

`password` 的查找原本已经是两级（`password` -> `passwd`），未改动；末尾把 `if db:`/
`args += [db]` 改名为 `database`。语义契约有两点：(1) 新键优先于旧键；(2) 旧键仍作为
回退被支持。其余字段（user/host/port/ssl/charset）均是 `OPTIONS.get(key, settings)`
的 override 模式。

## 调用链分析

`settings_to_cmd_args_env` 是 `@classmethod`，由 dbshell 管理命令调用，将
`settings_dict` 翻译为 `mysql` 命令行参数与环境变量。测试直接调用该方法并对
`(args, env)` 做精确相等断言。`get_connection_params` 走的是运行时连接路径，无
对应单测，故所有变异都集中在 client.py 上才能被 F2P 捕获。

两个关键 F2P 断言：
- `test_options_override_settings_proper_values`：`OPTIONS` 同时给出 db/passwd/
  user/host/port 的 override，并通过 subTest 同时覆盖 `(database,password)` 与
  `(db,passwd)` 两组键，期望 OPTIONS 值覆盖 settings 值。
- `test_options_non_deprecated_keys_preferred`：`OPTIONS` 同时给出新旧键，期望
  新键 `database`/`password` 胜出。

## 替换决策总览

| 组 | 类别 | 决策 | 原因 |
|----|------|------|------|
| A | 🟢 高质量 | KEEP | database+password 双字段键优先级反转，多行跨两字段语义改动 |
| B | 🔴 冗余 | REPLACE | 与 A 逐字节相同（重复 diff） |
| C | 🔴 冗余 | REPLACE | 与 A 逐字节相同（重复 diff） |
| D | 🟢 高质量 | KEEP | 删除两条弃用键回退链，机制不同于 A（丢失向后兼容路径） |
| E | 🔴 非自然 | REPLACE | 新增 dead 参数 `prefer_new_keys=False`，仅用于关闭修复 |

判定 M（shallow）此处不主导：A/B/C 互为逐字节副本，B、C 直接判 🔴 重复；E 为非自然
artifact 判 🔴。共替换 3 个，保留 A、D。

## 各组 Mutation 分析

### A — KEEP（🟢）
原 diff：把 `database` 与 `password` 的键查找优先级反转（`database`<->`db`、
`password`<->`passwd`）。这是跨两个字段的多行改动，普通单键配置仍正确，只有同时给
出新旧键时失败。F2P 验证：`test_options_non_deprecated_keys_preferred` FAIL，无
P2P 破坏。保留原 diff。strategy_code A2。

### B — REPLACE（🔴 → 新设计）
原 diff 与 A 逐字节相同，属重复，必须替换。
新 diff：删除 `port` 的 OPTIONS override 回退，硬绑定到 `settings_dict['PORT']`。

```python
-        port = settings_dict['OPTIONS'].get('port', settings_dict['PORT'])
+        port = settings_dict['PORT']
```

变异语义：丢失按连接覆盖端口的能力。普通仅 settings 配置仍正确；override 测试期望
`--port=555`（OPTIONS 值）失败。F2P 验证：`test_options_override_settings_proper_values`
FAIL，无 P2P 破坏。strategy_code B2。

### C — REPLACE（🔴 → 新设计）
原 diff 与 A 逐字节相同，重复，必须替换。
新 diff：删除 `host` 的 OPTIONS override，硬绑定到 `settings_dict['HOST']`。

```python
-        host = settings_dict['OPTIONS'].get('host', settings_dict['HOST'])
+        host = settings_dict['HOST']
```

变异语义：socket/host 分支对正常配置仍工作，仅 override 场景期望 `optionhost` 失败，
与 db/password 字段正交。F2P 验证：`test_options_override_settings_proper_values`
FAIL，无 P2P 破坏。strategy_code C1。

### D — KEEP（🟢）
原 diff：同时删除 `database` 与 `password` 的弃用键回退链，使
`OPTIONS['db']`/`OPTIONS['passwd']` 被静默忽略，只剩新键与 settings 默认。这与 A 的
"优先级反转" 机制不同——D 丢失向后兼容路径。现代非弃用配置通过；F2P 中使用弃用键的
subtest 失败。F2P 验证：`test_options_override_settings_proper_values (db,passwd)`
FAIL，无 P2P 破坏。保留原 diff。strategy_code B2。

### E — REPLACE（🔴 → 新设计）
原 diff 新增形参 `prefer_new_keys=False` 并据此分支选择键优先级，默认值即关闭修复，
属典型 dead-guard 非自然 artifact，必须替换。
新 diff：删除 `user` 的 OPTIONS override 回退，固定为 `settings_dict['USER']`。

```python
-        user = settings_dict['OPTIONS'].get('user', settings_dict['USER'])
+        user = settings_dict['USER']
```

变异语义：看似普通简化，单一来源配置全部通过；只有 override 测试期望 `optionuser`
失败，构成第三个正交字段。F2P 验证：`test_options_override_settings_proper_values`
FAIL，无 P2P 破坏。strategy_code B2。

## 新设计 Mutation 说明

三个替换（B/C/E）分别删除 port/host/user 的 OPTIONS override 回退，与保留下来的
A（database+password 键优先级反转）、D（弃用键回退删除）形成五个相互正交的失败面：
A 命中 `test_options_non_deprecated_keys_preferred`；B/C/E 各命中
`test_options_override_settings_proper_values` 中不同的命令行参数（--port / --host /
--user）；D 命中该测试的弃用键 subtest。

每个变异均通过：(1) 基线 golden 全模块 9 测试 PASS；(2) py_compile OK；(3) 仅对应
F2P 断言 FAIL，无 P2P 回归。

## 验证结论

- BASELINE：golden+test_patch 状态下 `dbshell.test_mysql` 9 tests OK。
- A: F2P FAIL（test_options_non_deprecated_keys_preferred），P2P clean。
- B: F2P FAIL（override --port），P2P clean。
- C: F2P FAIL（override --host），P2P clean。
- D: F2P FAIL（override db/passwd subtest），P2P clean。
- E: F2P FAIL（override --user），P2P clean。
