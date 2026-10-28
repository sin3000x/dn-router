# 业务规则

字段定义见[入参与出参](api.md)。本文说明这些字段在求解时的含义，以及哪些情况会被拒绝。

## 基本模型

- 单仓、开放路线：车辆从 `sourceLocation` 出发，最后一个站点卸完即结束，**不返仓**。
- 车型数量不限；每辆车选一种车型，输出 `vehicleTypeName`。
- DN 不拆单：一个 DN 只能整单装在一辆车上。
- **除下文两种时间窗异常外，所有 DN 必须恰好安排一次。** 不通过罚分漏单；做不到就整批报错，
  不交付部分路线。

## 容量

- 一辆车上 DN 的体积之和、重量之和必须**同时**不超过**同一种**车型的容积和载重。
- 不假定车型容量互相嵌套（容积大的不一定载重也大）。
- 任一有效 DN 单独都装不进任何车型 → `INFEASIBLE_INPUT`（`DN_EXCEEDS_ALL_VEHICLES`）。
- 输出车型取装得下该路线的**最小车型**。车型大小按（容积, 载重, 名称）升序排定等级；
  业务若有独立的车型偏好，需另加显式等级字段。

## 站点与停靠

两个概念互相独立：

**卸货停靠**（按 `siteCode`）

- 同车同 `siteCode` 的 DN 组成**一次连续停靠**：中间不插入其他站点，共用一个开始卸货时间（eta），
  整组只计一次 `unloadingTime`。
- 同站 DN 可以分给不同车，每辆车各计一次卸货。
- 同一辆车离开某站后不能再回到该站。
- 同站 DN 的时间窗必须有交集才能同车：共同 eta 要同时落在每个 DN 的窗口内。

**站点计数**（`maxSiteQty` 与 `lonAndLatCombination`）

| `lonAndLatCombination` | 计数口径 |
|---|---|
| 0 | 按 `siteCode` 去重 |
| 1 | 按经纬度四舍五入（ROUND_HALF_UP）到 6 位小数后去重 |
| 2 / null | 按 DN，每个 DN 计 1 站 |

每车去重后的站点数 ≤ `maxSiteQty`；0 表示不限。计数口径不影响卸货停靠，
所以按 DN 口径可能计 3 站，但实际只卸 2 次。

## 距离约束

- `siteDistanceLimit > 0`：相邻两个配送点之间的每一段距离不超过该值。仓库到首站不受限。
- `distanceLessThanFirstLeg = true`：站间距离之和 ≤ 仓库到首站距离。单站路线自然满足。
- 距离单位沿用业务矩阵，不假定为公里；`planDistance`、`siteDistanceLimit`、输出距离同一单位。

## 时间

- 时间均为同一业务当地时间，不带时区，不换算 UTC（国家码不能唯一确定时区，例如巴西）。
- 车辆在 `earliestShippingDate` 出仓。`latestShippingDate` 是出仓上限；因为允许提前到达等待，
  最早出仓总是可行，所以输出的 `departure_date` 恒为 `earliestShippingDate`。
- 允许提前到达并等待。时间窗约束的是**开始卸货时间**（eta），不要求在窗口内卸完。
- 下一站 eta ≥ 本站 eta + 卸货时长 + 行驶耗时。
- 不输出物理到达时间。输出采用最早出仓、到站即尽早开工的时间表，不优化等待时长。
- `unloadingTime` 单位为小时，换算秒后向上取整；`duration` 为整数秒。
- `enableTimeWindows = false`：忽略所有 DN 时间窗（包括空窗，不剔除），出仓时间与卸货时长仍生效。
- 旧版请求（缺少 `earliestShippingDate`）完全不考虑时间：耗时与卸货视为零、忽略时间窗（不剔除），
  不要求矩阵带 `duration`，输出不含 `departure_date` 和 `siteDetails`。

## 允许剔除的 DN

只有以下两种情况允许逐单剔除，出现在 `rejectedOrders` 中，不影响其余 DN 成功出线：

1. `EMPTY_TIME_WINDOW`：`timeWindow` 为 `[]` 或 start > end。
2. `DIRECT_TIME_WINDOW_UNREACHABLE`：最早出仓直接送达，到达时间仍晚于窗口上限。

时间窗早于出仓时间不会被剔除（直送可以等待）。全部 DN 都被剔除或请求本身为空时返回
`NO_ELIGIBLE_ORDERS`，这**不表示**被剔除的 DN 已完成。

## 矩阵

- `allPathDistanceVoList` 是唯一权威矩阵，按 `(from, to)` 的仓库 ID / DN 号索引，支持非对称距离和耗时。
- 必须提供保留 DN 所需的全部有向边：仓库 → DN、DN → DN，并带 `duration`。
  不从坐标估算缺失边，也不把缺失当零；缺失或同键冲突 → `INVALID_INPUT`。
- 不需要返仓边和对角线。对角线非零时归零并告警。
- 同 `siteCode` 的 DN 之间视为同一物理停靠，边的距离和耗时归零并告警。
  因此同 `siteCode` 必须确实是同一个卸货地点。
- `beginSiteCode` / `endSiteCode` 只是标签，不作索引。
- `pathDistanceVoList` 只兼容接收，不补齐也不覆盖权威矩阵，非空时告警。

## 数值精度

- 距离最多 3 位小数，体积/重量最多 6 位小数。内部按精确整数缩放，超出精度或范围 → `INVALID_INPUT`，
  不做静默舍入。
- JSON 中的小数按 Decimal 读取（CLI 与服务端），避免浮点误差。

## 优化目标

按严格的词典序比较方案，前一项更优的方案永远胜出，与后面各项差多少无关：

| `quotationModel` | 目标顺序 | `objectivePolicy` |
|---|---|---|
| `distance` / `""` / null | 总距离 → 车型等级和 | `distance_type` |
| `city` / `zipCode` / `province` | 车辆数 → 总距离 → 车型等级和 | `vehicles_distance_type` |

`city` / `zipCode` / `province` 目前只是目标策略的开关，不读取城市、邮编或省份报价（入参中也没有价格）。

结果在给定时限内尽力优化，`FEASIBLE` 不等于全局最优，`optimalityProven` 恒为 false。

## 输出可靠性

所有输出路线在交付前都经过独立审计：容量、站点数、站间距离、首段约束、时间窗、同站连续性，
以及每个保留 DN 恰好出现一次。审计不依赖求解器内部约束；审计失败返回 `SOLVER_ERROR`
（`ROUTE_AUDIT_FAILED`），不交付路线，也不报告为输入错误。
