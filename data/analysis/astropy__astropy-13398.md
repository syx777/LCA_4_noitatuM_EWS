# astropy__astropy-13398

## 问题背景

该 issue 提出了一种直接在 ITRS 坐标系内进行 ITRS↔AltAz、ITRS↔HADec 坐标变换的新方法，绕过了原来经过 CIRS 的路径。原有路径在处理近地天体（卫星、飞机等）时会引入不正确的地心差（geocentric vs. topocentric stellar aberration），而新方法通过将 ITRS 位置视为时间不变量，直接用旋转矩阵完成变换。

Golden patch 主要做了以下几件事：
1. 新增 `itrs_observed_transforms.py`，实现 ITRS↔AltAz/HADec 的直接变换
2. 在 `itrs.py` 中为 `ITRS` 帧添加 `location` 属性（支持 topocentric ITRS）
3. 修改 `intermediate_rotation_transforms.py` 中 TETE/CIRS↔ITRS 的变换，使其正确传递 `location`

## Golden Patch 语义分析

核心修复逻辑：

1. **新增直接变换路径**：`itrs_to_observed` 函数先检查 ITRS 的 location/obstime 是否与目标 observed frame 一致，若不一致则先做 ITRS→ITRS 同步（走 CIRS 路径，含 stellar aberration 修正）；再用旋转矩阵直接将 ITRS 笛卡尔坐标转为 AltAz 或 HADec。

2. **旋转矩阵设计**：
   - `itrs_to_altaz_mat(lon, lat)`：先绕 z 轴旋转经度，再绕 y 轴旋转 `(90° - lat)`，最后翻转 x 轴（左手系）
   - `itrs_to_hadec_mat(lon)`：先绕 z 轴旋转经度，再翻转 y 轴（左手系）
   - `altaz_to_hadec_mat(lat)`：翻转 x、y 轴，再绕 y 轴旋转 `(90° - lat)`

3. **大气折射**：`add_refraction`/`remove_refraction` 实现了 ERFA 风格的折射修正（A·tan(z)+B·tan³(z) 模型）

4. **location 属性传递**：`itrs_to_tete`、`itrs_to_cirs` 等函数在创建中间帧时传入 `location=itrs_coo.location`，使 topocentric 信息不丢失

## 调用链分析

```
ITRS → itrs_to_observed() → AltAz/HADec
  ├─ 若 location/obstime 不匹配：ITRS→ITRS(via CIRS)
  ├─ itrs_to_altaz_mat(lon, lat) → 旋转矩阵
  └─ add_refraction() → 折射修正（若 pressure > 0）

AltAz/HADec → observed_to_itrs() → ITRS
  ├─ itrs_to_altaz_mat(lon, lat) 的转置
  ├─ remove_refraction() → 去折射（若 pressure > 0）
  └─ ITRS(crepr, obstime=observed_coo.obstime, location=...) → itrs_frame
       └─ 最终 ITRS→ITRS 变换（可能 no-op）
```

上游调用：测试文件 `test_intermediate_transformations.py` 中的 `test_itrs_topo_to_altaz_with_refraction()` 等函数通过 `transform_to` 调用这些变换。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 替换 | 修改孤立常量（NORTH_POLE），效果等同于简单精度偏差，最弱的语义浅层 |
| B | 语义浅层 | 保留 | 关键逻辑节点（or→and），改变了 location/obstime 同步触发条件，能模拟真实边界判断失误 |
| C | 必须替换 | 替换 | 添加 `to_value(u.deg)` 后 `NORTH_POLE - lat` 会 TypeError，不自然且立即崩溃 |
| D | 必须替换 | 替换 | 注释直接写 "Removed synchronization check - breaks idempotency"，代码审查立即发现 |
| E | 语义浅层 | 保留 | 关键位置（旋转角度 90°-lat → lat），两处同时修改，影响 AltAz 和 HADec 的旋转矩阵 |

语义浅层共 3 个（A、B、E），替换其中最弱的 floor(3/2) = 1 个：[A]

必须替换 2 个（C、D）

**总替换数量：3 个（A、C、D）**

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030..d32d66146 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -13,7 +13,7 @@ from .itrs import ITRS
 CELMIN = 1e-6
 SELMIN = 0.05
 # Latitude of the north pole.
-NORTH_POLE = 90.0*u.deg
+NORTH_POLE = 89.9*u.deg
```
**分类**：🟡 语义浅层（替换）

**理由**：修改的是一个模块级常量定义，改动位置孤立（不在任何函数内部），效果是引入 0.1° 的系统性偏差。这个偏差在任何精度测试中都会被检测到（因为旋转矩阵的误差会直接反映在坐标值上），是同组语义浅层中最弱的。

**最终 mutation**：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030a..69cc7e99bc 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -31,7 +31,7 @@ def itrs_to_hadec_mat(lon):
     # form ITRS to HADec matrix
     # HADec frame is left handed
     minus_y = np.eye(3)
-    minus_y[1][1] = -1.0
+    minus_y[1][1] = 1.0
     mat = (minus_y
            @ rotation_matrix(lon, 'z'))
     return mat
```
**变异语义**：去掉了 `itrs_to_hadec_mat` 中 y 轴的符号翻转，使 HADec 坐标系变为右手系而非左手系。这会导致 HADec 的 HA（Hour Angle）方向反转。对于 AltAz 变换完全无影响，只在 ITRS↔HADec 的变换中失败。模拟了开发者在实现左手坐标系时忘记翻转某一轴符号的真实错误。

---

### Group B — 保留
**原 mutation**：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030..054764ceb 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -103,7 +103,7 @@ def remove_refraction(aa_crepr, observed_frame):
 @frame_transform_graph.transform(FunctionTransformWithFiniteDifference, ITRS, AltAz)
 @frame_transform_graph.transform(FunctionTransformWithFiniteDifference, ITRS, HADec)
 def itrs_to_observed(itrs_coo, observed_frame):
-    if (np.any(itrs_coo.location != observed_frame.location) or
+    if (np.any(itrs_coo.location != observed_frame.location) and
             np.any(itrs_coo.obstime != observed_frame.obstime)):
```
**分类**：🟡 语义浅层（保留）

**理由**：修改位置在关键逻辑节点——`itrs_to_observed` 的入口条件判断。`or` → `and` 改变了触发 ITRS→ITRS 同步的条件：原来只要 location 或 obstime 其中一个不同就同步，改后需要两者都不同才触发。当只有 obstime 不同（location 相同）时，不会同步，导致 stellar aberration 修正被跳过。能通过简单的同地点同时刻测试，只在跨时间或跨地点的特定场景下失败。

**最终 mutation**（保留，与原相同）：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030..054764ceb 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -103,7 +103,7 @@ def remove_refraction(aa_crepr, observed_frame):
 @frame_transform_graph.transform(FunctionTransformWithFiniteDifference, ITRS, AltAz)
 @frame_transform_graph.transform(FunctionTransformWithFiniteDifference, ITRS, HADec)
 def itrs_to_observed(itrs_coo, observed_frame):
-    if (np.any(itrs_coo.location != observed_frame.location) or
+    if (np.any(itrs_coo.location != observed_frame.location) and
             np.any(itrs_coo.obstime != observed_frame.obstime)):
```
**变异语义**：ITRS→Observed 的 location/obstime 同步条件从"任一不同即同步"变为"两者都不同才同步"，使得仅 obstime 不同或仅 location 不同时跳过同步步骤，产生错误的 stellar aberration 处理。

---

### Group C — 替换
**原 mutation**：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030..e40fd630a 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -19,6 +19,8 @@ NORTH_POLE = 90.0*u.deg
 def itrs_to_altaz_mat(lon, lat):
     # form ITRS to AltAz matrix
     # AltAz frame is left handed
+    lon = lon.to_value(u.deg)
+    lat = lat.to_value(u.deg)
     minus_x = np.eye(3)
```
**分类**：🔴 必须替换

**理由**：将 `lon` 和 `lat` 从 Quantity 转为纯浮点数（degrees）后，后续 `rotation_matrix(NORTH_POLE - lat, 'y')` 会因为 `NORTH_POLE`（Quantity）减去 `lat`（float）而抛出 TypeError。这是一个会立即崩溃的 bug，不需要任何特定测试场景就能发现，且代码风格非常不自然（在函数入口随意转换单位）。

**最终 mutation**：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030a..f699f01c8f 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -72,7 +72,7 @@ def add_refraction(aa_crepr, observed_frame):
     uv[..., 2] = cosdel * uv[..., 2] + delta_el * cel
     # Need to renormalize to get agreement with CIRS->Observed on distance
     norm2, uv = erfa.pn(uv)
-    uv = erfa.sxp(norm, uv)
+    uv = erfa.sxp(norm2, uv)
     return CartesianRepresentation(uv, xyz_axis=-1, unit=aa_crepr.x.unit, copy=False)
```
**变异语义**：`add_refraction` 中最终重新缩放向量时，用 `norm2`（归一化后的模，≈1.0）替换了 `norm`（原始距离）。这导致折射修正后返回的坐标距离被归一化为单位长度，而非保持原始距离。对于无折射的测试（`pressure=0`）完全无影响，只在有大气折射时（`pressure > 0`）导致距离错误。模拟了开发者混淆了两个 norm 变量（归一化前后的模）的真实错误。

---

### Group D — 替换
**原 mutation**：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030..fd3d1fb74 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -103,11 +103,12 @@ def remove_refraction(aa_crepr, observed_frame):
 @frame_transform_graph.transform(FunctionTransformWithFiniteDifference, ITRS, AltAz)
 @frame_transform_graph.transform(FunctionTransformWithFiniteDifference, ITRS, HADec)
 def itrs_to_observed(itrs_coo, observed_frame):
-    if (np.any(itrs_coo.location != observed_frame.location) or
-            np.any(itrs_coo.obstime != observed_frame.obstime)):
-        # This transform will go through the CIRS and alter stellar aberration.
-        itrs_coo = itrs_coo.transform_to(ITRS(obstime=observed_frame.obstime,
-                                              location=observed_frame.location))
+    # Removed synchronization check - breaks idempotency
+    # if (np.any(itrs_coo.location != observed_frame.location) or
+    #         np.any(itrs_coo.obstime != observed_frame.obstime)):
+    #     # This transform will go through the CIRS and alter stellar aberration.
+    #     itrs_coo = itrs_coo.transform_to(ITRS(obstime=observed_frame.obstime,
+    #                                           location=observed_frame.location))
```
**分类**：🔴 必须替换

**理由**：注释直接写明 "Removed synchronization check - breaks idempotency"，代码审查者会立即注意到这个注释，且注释本身就暗示了这是一个已知的 bug。这是一个明显的人工痕迹，违反了"不自然"原则。

**最终 mutation**：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030a..1992ab57b7 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -138,7 +138,7 @@ def observed_to_itrs(observed_coo, itrs_frame):
     else:
         crepr = observed_coo.cartesian.transform(matrix_transpose(itrs_to_hadec_mat(lon)))
 
-    itrs_at_obs_time = ITRS(crepr, obstime=observed_coo.obstime,
+    itrs_at_obs_time = ITRS(crepr, obstime=itrs_frame.obstime,
                             location=observed_coo.location)
     # This final transform may be a no-op if the obstimes and locations are the same.
     # Otherwise, this transform will go through the CIRS and alter stellar aberration.
```
**变异语义**：`observed_to_itrs` 中，创建中间 ITRS 帧时使用了 `itrs_frame.obstime` 而非 `observed_coo.obstime`。当 `observed_coo.obstime` 与 `itrs_frame.obstime` 不同时，中间帧的时间戳错误，导致最终 ITRS→ITRS 变换在错误的时间点进行，stellar aberration 修正错误。若两个 obstime 相同则完全无影响（no-op）。模拟了开发者混淆了应该用哪个帧的 obstime 来初始化中间 ITRS 帧的真实错误，代码看起来完全合理。

---

### Group E — 保留
**原 mutation**：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030..1e55cdbcf 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -22,7 +22,7 @@ def itrs_to_altaz_mat(lon, lat):
     minus_x = np.eye(3)
     minus_x[0][0] = -1.0
     mat = (minus_x
-           @ rotation_matrix(NORTH_POLE - lat, 'y')
+           @ rotation_matrix(lat, "y")
            @ rotation_matrix(lon, 'z'))
     return mat
 
@@ -43,7 +43,7 @@ def altaz_to_hadec_mat(lat):
     z180[0][0] = -1.0
     z180[1][1] = -1.0
     mat = (z180
-           @ rotation_matrix(NORTH_POLE - lat, 'y'))
+           @ rotation_matrix(lat, "y"))
     return mat
```
**分类**：🟡 语义浅层（保留）

**理由**：修改位置在两个关键函数的核心旋转计算中，将旋转角度从 `(90° - lat)` 改为 `lat`。这两处修改协同作用，影响 AltAz 坐标变换和 AltAz↔HADec 的中间变换。修改位置重要，能模拟开发者在推导旋转角度时犯的余角错误（混淆了纬度和余纬度），且两处同时修改使 bug 更隐蔽。

**最终 mutation**（保留，与原相同）：
```diff
diff --git a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
index 4aa61e030..1e55cdbcf 100644
--- a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
+++ b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py
@@ -22,7 +22,7 @@ def itrs_to_altaz_mat(lon, lat):
     minus_x = np.eye(3)
     minus_x[0][0] = -1.0
     mat = (minus_x
-           @ rotation_matrix(NORTH_POLE - lat, 'y')
+           @ rotation_matrix(lat, "y")
            @ rotation_matrix(lon, 'z'))
     return mat
 
@@ -43,7 +43,7 @@ def altaz_to_hadec_mat(lat):
     z180[0][0] = -1.0
     z180[1][1] = -1.0
     mat = (z180
-           @ rotation_matrix(NORTH_POLE - lat, 'y'))
+           @ rotation_matrix(lat, "y"))
     return mat
```
**变异语义**：将 AltAz 旋转矩阵中的 `NORTH_POLE - lat`（余纬度，即从北极到观测点的角距离）改为 `lat`（纬度），使旋转角度完全错误。在赤道（lat=0）时，错误最大（原本旋转90°，现在旋转0°）；在极点（lat=90°）时，两者相同（均为0°），无影响。这能通过极点附近的简单测试，只在中低纬度的精度测试中失败。

---

## 新设计 Mutation 说明

### Group A 替换说明
**分析基础**：`itrs_to_hadec_mat` 函数构建 ITRS→HADec 的旋转矩阵，其中 `minus_y[1][1] = -1.0` 实现了 HADec 坐标系的左手性（y 轴翻转）。这是 HADec 坐标系定义的核心——Hour Angle 从东向西增加，与通常的右手系方向相反。

**选择位置的理由**：该修改位于 `itrs_to_hadec_mat` 函数内部，与 Group B（`itrs_to_observed` 入口条件）和 Group E（`itrs_to_altaz_mat` 和 `altaz_to_hadec_mat`）的修改位置不重叠。修改只影响 HADec 路径，不影响 AltAz 路径，使 bug 只在 HADec 相关测试中暴露。

**模拟的真实错误**：开发者在实现左手坐标系时，可能忘记了需要翻转 y 轴（而不是 x 轴），或者误以为右手系就足够了。

### Group C 替换说明
**分析基础**：`add_refraction` 函数末尾有一个归一化步骤：先用 `erfa.pn` 对折射修正后的向量归一化（得到 `norm2` 和单位向量 `uv`），再用 `erfa.sxp(norm, uv)` 将原始距离 `norm` 乘回去，保持距离不变。`norm` 是输入向量的原始模，`norm2` 是折射修正后向量的模（理论上应接近1，因为已归一化）。

**选择位置的理由**：`add_refraction` 函数独立于其他 mutation 的修改位置。使用 `norm2` 替换 `norm` 的错误非常隐蔽——两个变量名只差一个数字，且 `norm2` 在同一行就被赋值，看起来像是"更新后的 norm"。只有在有大气折射（`pressure > 0`）的测试中才会失败，且距离误差可能很小（取决于折射量），使 bug 难以察觉。

**模拟的真实错误**：开发者在重构或阅读代码时，可能将 `norm2`（归一化后的新模）误认为是"更新后的正确 norm"，从而用它替换了原始的 `norm`。

### Group D 替换说明
**分析基础**：`observed_to_itrs` 函数末尾创建 `itrs_at_obs_time` 中间帧，然后调用 `transform_to(itrs_frame)` 做最终的 ITRS→ITRS 变换（含 stellar aberration 修正）。中间帧的 `obstime` 应该是 `observed_coo.obstime`（观测时间），这样后续的 ITRS→ITRS 变换才能正确地从观测时间变换到目标 ITRS 帧的时间。

**选择位置的理由**：`observed_to_itrs` 是逆变换函数，与 Group B 修改的 `itrs_to_observed` 是对称的，但修改点不同（B 改入口条件，D 改出口的 obstime）。当 `itrs_frame` 没有指定 obstime（使用默认值）时，`itrs_frame.obstime` 与 `observed_coo.obstime` 可能相同，bug 不显现；只有在显式指定不同 obstime 的 `itrs_frame` 时才会暴露。

**模拟的真实错误**：开发者在编写 `observed_to_itrs` 时，可能认为"中间 ITRS 帧应该用目标帧的 obstime"（因为最终目标是转到 `itrs_frame`），而忽略了应该先保存观测时刻的状态，再做时间变换。
