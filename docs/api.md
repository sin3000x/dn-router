# 入参与出参

调用方式有三种，入参相同：

- Python：`solve(payload, time_limit_ms=200)`，返回完整报告（见[完整报告](#完整报告)）。
- CLI：`dn-router input.json [--time-limit-ms N] [--output FILE] [--report] [--html FILE]`。
  默认 stdout 只输出路线数组，诊断写到 stderr；`--report` 时 stdout 输出完整报告。
- HTTP（`dn-router serve`）：`POST /api/solve?time_limit_ms=`，请求体为请求 JSON，响应中 `output` 为完整报告。

`time_limit_ms` 是整体计算的尽力预算（正整数，默认 200），含义见[算法设计](algorithm.md#时间预算)。

JSON Schema 见 `schemas/request.schema.json`（由 `models.Request` 生成）。

## 请求

请求及嵌套对象允许携带额外字段，解析时忽略。日期格式 `YYYY-MM-DD HH:mm` 或 `YYYY-MM-DD HH:mm:ss`，
均为不带时区的业务当地时间，不允许小数秒。

### 顶层字段

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `sourceLocation` | string | 是 | | 仓库 ID，即矩阵中仓库的 `from` 键；不能与任何 DN ID 相同 |
| `sourceLatitude` / `sourceLongitude` | number | 是 | | 仓库坐标，仅用于初解扫描排序和可视化 |
| `earliestShippingDate` | datetime | 是 | | 最早出仓时间，也是全部时间计算的零点 |
| `latestShippingDate` | datetime \| null | 否 | null | 最晚出仓时间；不得早于 `earliestShippingDate` |
| `unloadingTime` | number ≥ 0 | 否 | 0 | 每次停靠的卸货时长，**单位小时**，换算秒后向上取整 |
| `enableTimeWindows` | bool | 否 | true | false 时忽略全部 DN 时间窗 |
| `dnSiteVoList` | DN[] | 是 | | 待配送 DN，可为空列表 |
| `vehicleTypeVoList` | Vehicle[] | 是 | | 车型，至少一个；每种车型数量不限 |
| `allPathDistanceVoList` | Leg[] | 是 | | 唯一权威的有向距离/耗时矩阵 |
| `pathDistanceVoList` | Leg[] | 否 | [] | 兼容字段，只接收不使用；非空时给出警告 |
| `maxSiteQty` | int ≥ 0 | 否 | 0 | 每车去重站点数上限，0 表示不限 |
| `lonAndLatCombination` | 0 \| 1 \| 2 \| null | 否 | null | 站点计数口径：0 按 siteCode，1 按坐标，2/null 按 DN |
| `siteDistanceLimit` | number | 否 | 0 | 相邻配送点之间的单段距离上限，≤0 表示不限 |
| `distanceLessThanFirstLeg` | bool | 否 | false | 是否要求站间距离和 ≤ 仓库到首站距离 |
| `quotationModel` | `distance` \| `city` \| `zipCode` \| `province` \| `""` \| null | 否 | null | 选择优化目标，见[业务规则](business-rules.md#优化目标) |
| `taskNo` | string | 否 | `""` | 原样回传 |
| `destCountryCode3` | string \| null | 否 | null | 只接收，不参与计算（不用于推断时区） |

### DN（`dnSiteVoList[]`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `orderReleaseId` | string | 是 | DN 号，非空且唯一；也是矩阵键 |
| `siteCode` | string | 是 | 实际卸货地点；同 siteCode 必须是同一物理地点 |
| `deliveryLatitude` / `deliveryLongitude` | number | 是 | 范围 [-90, 90] / [-180, 180] |
| `totalGrossVolume` / `totalGrossWeight` | number ≥ 0 | 是 | 最多 6 位小数 |
| `timeWindow` | [start, end] \| [] \| null | 否 | **开始卸货**时间窗。null/缺省：不限；`[]` 或 start>end：空窗，剔除；start=end：合法单点 |

### 车型（`vehicleTypeVoList[]`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `vehicleTypeName` | string | 是 | 车型唯一 ID，非空且唯一；输出的 `vehicle` 就是它 |
| `totalVolume` / `totalWeight` | number > 0 | 是 | 容积、载重，最多 6 位小数 |
| `vehicleTypeCode` / `themeCode` | string | 否 | 只接收 |

### 矩阵边（`allPathDistanceVoList[]`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `from` / `to` | string | 是 | 仓库 ID 或 DN 号（`orderReleaseId`），**不是** siteCode |
| `planDistance` | number ≥ 0 | 是 | 业务距离单位，最多 3 位小数 |
| `duration` | int ≥ 0 | 求解所需边必填 | 行驶耗时，整数秒 |
| `beginSiteCode` / `endSiteCode` | string | 否 | 附带标签，不作索引 |

必须提供：仓库 → 每个保留 DN、每个保留 DN → 其他保留 DN 的有向边，且带 `duration`。
不需要返仓边和对角线。同一 `(from, to)` 重复出现时取值必须完全一致。完整规则见
[业务规则 · 矩阵](business-rules.md#矩阵)。

## 输出

### 路线数组（CLI 默认 stdout）

即 `solve(payload)["routes"]`。出错时为空数组，因此不能只凭空数组判断成功，需同时看退出码或 `status`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `dist` | number | 路线总距离：仓库到首站 + 站间，不含返仓；单位同输入 |
| `siteAddresses` | string[] | 按配送顺序的 **DN 号**（不是 siteCode） |
| `departure_date` | string | 出仓时间，恒为 `earliestShippingDate` |
| `siteDetails` | {siteAddress, eta}[] | 每个 DN 一项，顺序同 `siteAddresses`；`eta` 为**开始卸货时间** |
| `vehicle` | string | 车型名称（`vehicleTypeName`），取装得下本路线的最小车型 |
| `volume` / `weight` | number | 本车 DN 的体积、重量之和 |

时间统一为 `YYYY-MM-DD HH:mm:ss`，保留行驶耗时带来的秒数。同站多个 DN 逐单输出，共用一个 `eta`。
完整示例见 `examples/output-multi.json`。

### 完整报告

`solve()` 返回值，也是 CLI `--report` 的输出：

| 字段 | 出现条件 | 说明 |
|---|---|---|
| `status` | 总是 | 见下表 |
| `routes` | 总是 | 路线数组；非成功状态为空 |
| `elapsedMs` | 总是 | `solve()` 内部耗时，不含解释器启动、JSON 解析、网络 |
| `errors` | 失败时 | `[{code, message?, orderReleaseId?}]` |
| `taskNo` | 输入校验通过后 | 原样回传 |
| `rejectedOrders` | 输入校验通过后 | 被剔除的 DN：`[{orderReleaseId, code}]` |
| `warnings` | 输入校验通过后 | 矩阵归一化等提示，字符串数组 |
| `solutionSource` | 成功时 | `ortools` / `insertion_fallback` / `direct_fallback`，见[算法设计](algorithm.md#求解流程) |
| `vehicleCount` | 成功时 | 路线数 |
| `servedOrderCount` | 成功时 | 已安排 DN 数，等于保留 DN 数 |
| `totalDistance` | 成功时 | 全部路线 `dist` 之和 |
| `objectivePolicy` | 成功时 | `vehicles_distance_type`（车辆数优先）或 `distance_type`（距离优先） |
| `optimalityProven` | 成功时 | 恒为 false：有限时间搜索不证明最优 |
| `solverStatus` | 成功时 | OR-Tools `RoutingSearchStatus` 数值，未进入搜索时为 null（1 = SUCCESS，4 = FAIL_TIMEOUT 等） |
| `timingsMs` | 成功时 | `{buildAndSeed, search}`：建模与初解导入、局部搜索耗时 |

### 状态与退出码

| `status` | 含义 | CLI 退出码 |
|---|---|---|
| `FEASIBLE` | 全部保留 DN 已安排，路线通过审计 | 0 |
| `NO_ELIGIBLE_ORDERS` | 请求为空，或全部 DN 都被剔除；`routes` 为空 | 0 |
| `INVALID_INPUT` | 结构/字段校验失败、矩阵缺失或冲突、精度超限、目标值溢出、JSON 无法解析 | 2 |
| `INFEASIBLE_INPUT` | 某个有效 DN 单独装不进任何车型，整批不交付 | 2 |
| `SOLVER_ERROR` | 输出审计失败，说明求解流程有缺陷；不交付路线 | 1 |

### 错误码

| `code` | 所属状态 | 说明 |
|---|---|---|
| `INVALID_INPUT` | INVALID_INPUT | 校验或预处理失败，`message` 给出原因 |
| `OBJECTIVE_OVERFLOW` | INVALID_INPUT | 距离量级过大，目标权重超出 int64 安全范围 |
| `DN_EXCEEDS_ALL_VEHICLES` | INFEASIBLE_INPUT | 每个超限 DN 一条，带 `orderReleaseId` |
| `ROUTE_AUDIT_FAILED` | SOLVER_ERROR | 审计失败原因见 `message` |

CLI 读文件或解析 JSON 失败时，`errors` 只有 `message`，没有 `code`。

### 剔除码（`rejectedOrders[].code`）

| `code` | 条件 |
|---|---|
| `EMPTY_TIME_WINDOW` | `timeWindow` 为 `[]` 或 start > end（`enableTimeWindows=false` 时不检查） |
| `DIRECT_TIME_WINDOW_UNREACHABLE` | 最早出仓直送，到达时间仍晚于时间窗上限 |

### 警告（`warnings[]`）

| 前缀 | 说明 |
|---|---|
| `DIAGONAL_NORMALIZED: <id>` | 对角线边给了非零距离/耗时，已按 0 处理 |
| `SAME_SITE_TRAVEL_NORMALIZED: <a> -> <b>` | 同 siteCode 的 DN 之间给了非零距离/耗时，已按 0 处理 |
| `pathDistanceVoList is compatibility-only; ...` | `pathDistanceVoList` 非空，已忽略 |

## 示例

`examples/` 下：

- `input-original.json`：原始示例。DN 体积 100 超过最大容积 65，返回 `INFEASIBLE_INPUT`。
- `input-feasible.json`：同上，DN 体积改为 1。
- `input-multi.json` / `output-multi.json`：三个 DN、两个实际站点（两个 DN 同站共用 eta）。
