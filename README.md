# dn-router

基于 OR-Tools Routing 的单仓开放路线配送初始代码仓。Python 3.11+。
输入兼容示例业务字段；车型不限数量；DN 不拆单；除明确时间窗异常外，所有 DN 必须恰好安排一次。

## 快速启动

```bash
uv sync --locked
uv run dn-router examples/input-multi.json --time-limit-ms 1000
uv run pytest -q
uv run python scripts/benchmark.py --dn 100 --runs 5 --time-limit-ms 200
```

也支持 `uv run python -m dn_router input.json`、stdin (`input` 为 `-`) 和 `--output result.json`。
Python 调用：

```python
from dn_router import solve
result = solve(payload, time_limit_ms=1000)
```

`examples/input-original.json` 保留原始示例数据（去掉注释并修正 JSON 标点）：
DN 体积 100 > 最大容积 65，应返回 `INFEASIBLE_INPUT`，不是成功漏单。
`input-feasible.json` 仅将 DN 体积改成 1；`input-multi.json` 演示三个 DN、两个实际站点。

## 业务语义

- 路线从 `sourceLocation` 出发，最后一个站点卸完结束，不返仓。
- 体积和重量分别求和，必须同时适配**同一种**车型。不要求车型容量互相嵌套。
- 以 `vehicleTypeName` 为唯一车型 ID，输出为 `vehicle`。
- 同车同 `siteCode` 的 DN 形成**一次连续停靠、一个共同开始卸货时间**，只计一次 `unloadingTime`。
  同站 DN 可分给不同车；每辆车各计一次卸货。不同 DN 时间窗不相交时不能同车同次交付。
  不允许同一车辆离开后再次访问该站点。
- 站点数口径与卸货身份独立：0 按 siteCode，1 按经纬度六位小数 ROUND_HALF_UP，2/null 按 DN。
  因此 DN 口径可能计 3 站，但实际只卸货 2 次。`maxSiteQty=0` 表示不限。
- `siteDistanceLimit<=0` 表示不限；只限制配送点之间，不限制仓库到首站。
- `distanceLessThanFirstLeg=true`：站间距离之和 <= 仓库到首站距离；单站路线自然满足。
- `quotationModel=distance/null/""`：按 **总距离、车型等级和** 的严格优先顺序优化。
- `quotationModel=city/zipCode/province`：按 **车辆数、总距离、车型等级和** 优化。
  当前三个值只是目标策略，不读取城市/邮编/省份报价（入参也不包含这些价格）。
- “小车型”等级按容积、载重、名称升序；业务若有独立车型偏好，应后续扩展显式等级字段。

## 时间与单位

- `duration`：整数秒；`unloadingTime`：小时，换算秒后向上取整。
- 距离单位不臆定：`planDistance`、`siteDistanceLimit`、输出距离使用相同业务单位。
  距离最多 3 位小数，体积/重量最多 6 位小数；整数化是精确的，更多精度返回输入错误。
- 日期均为**同一业务当地时间**，不带时区；国家码不能唯一确定时区（例如巴西）。不自动换成 UTC。
- `earliestShippingDate` 为出发下限。新增可选 `latestShippingDate` 为出发上限；未给不补造上限。
- 允许提前到达并等待。`eta` **表示开始卸货时间**，必须位于每个 DN 的时间窗内；不要求窗内卸完。
  不输出物理到达时间。下一站开始卸货不得早于本站 eta + 卸货时间 + 行驶时间。
  同车同站多个 DN 共用 eta，卸货时间只计一次。
  输出选择最早出仓的可行时间表，不优化等待时间。
- `timeWindow=null`/未给：该 DN 无时间窗；`[]` 或 start>end：空窗口，允许剔除；相等是合法单点窗口。
- 新增可选 `enableTimeWindows=false`：忽略所有 DN 时间窗，保留出发时间与卸货时间。

## 矩阵与错误

`allPathDistanceVoList` 是唯一权威矩阵，按 `from/to` 的仓库 ID / DN ID 索引，支持非对称距离和耗时。
必须提供保留 DN 的仓库->DN 和 DN->DN 有向边（含 duration）；不要求返仓边或对角线。
不从坐标估算缺失矩阵。缺项/冲突是 `INVALID_INPUT`，不能把缺失项当零。
对角线归零并告警；同 siteCode 的 DN 之间视为同一次物理停靠，内部边距离/时间归零并告警。
因此同 siteCode 必须确实代表同一物理卸货地点。
`beginSiteCode/endSiteCode` 是附带标签，不作矩阵键。
`pathDistanceVoList` 仅兼容接收，不补齐/覆盖权威矩阵；非空时返回提示。

只允许以下两种逐 DN 剔除：

1. `EMPTY_TIME_WINDOW`：窗口为空。
2. `DIRECT_TIME_WINDOW_UNREACHABLE`：最早出仓直送，也晚于开始卸货时间窗上限。

直送可提前等待，所以仅有出发上限不导致“到货窗口太晚”不可行。
有效订单超出所有车型容量：`INFEASIBLE_INPUT`，整批不交付部分路线。
不使用 `AddDisjunction`，不会通过罚分允许漏单。
返回 `NO_ELIGIBLE_ORDERS` 表示原请求为空或全为允许剔除的异常，不表示完成了这些异常 DN。
CLI 对 INVALID_INPUT / INFEASIBLE_INPUT 返回退出码 2，其他业务结果返回 0。

## 实现

1. Pydantic 校验、Decimal 精确缩放、时间窗预检查。
2. 单单直送保证完整可行兜底；短预算的可行插入构造较好初解。
3. OR-Tools Routing 搜索：虚拟终点、容量维度、时间维度、去重计数、禁用边、首段关联距离约束。
4. 使用原生固定车型车辆容量模型。距离优先时每车型最多 N 个槽位；
   车辆数优先时每车型最多初解车辆数个槽位，足够表示所有不比初解更多车的方案。
   槽位只是数学表示，不是车源上限：N 个 DN 的有效方案本来就不需要超过 N 辆非空车辆。
5. 发车固定成本中包含车型等级；依据全局上界设置词典序权重，并检查 int64 安全范围。
6. 输入初解经 ReadAssignmentFromRoutes 导入，再用局部搜索改进。
7. 独立审计输出的容量、站点数、距离、时间窗、同站连续性和 DN 完整性；校验失败直接抛错，绝不返回无效方案。

`solutionSource` 区分 `ortools`、`insertion_fallback`、`direct_fallback`。
所有这些来源都必须覆盖全部保留 DN。`FEASIBLE` 不等于全局最优，`optimalityProven` 始终为 false。

## 性能边界

默认 `time_limit_ms=200` 是**尽力而为的整体计算预算**，不是硬实时承诺。
预处理、建模、模型关闭、初解导入、结果校验都消耗时间；部分 OR-Tools 调用不能在任意时点中断。
返回 `elapsedMs` 和分阶段耗时；它们不包含解释器启动、JSON 解析、网络和进程排队。

初始化实测记录见 `BENCHMARK.md`。本版本尚未达到 100 DN / 200ms 的原始性能目标。
建议先用 3000ms 调试 100 DN 业务与比较方案，再优化模型/初解导入开销；不能只把搜索限时设为 200ms 就声称达标。

`benchmark.py` 是固定种子的合成数据冒烟测试，不代表真实业务分布，也不提供最优性证明。
生产验收需固定硬件和并发，检查 P95/P99、车辆数、距离和较长预算基线的差距。

## 文件结构

- `src/dn_router/models.py`：输入类型与结构校验。
- `problem.py`：矩阵、单位、预检查、独立路线审计。
- `seed.py`：有界可行初解构造。
- `solver.py`：OR-Tools 模型与输出。
- `cli.py`：命令行。
- `schemas/request.schema.json`：生成的 JSON Schema。
- `tests/`：业务回归测试；`.github/workflows/tests.yml`：CI。

参考：
- https://developers.google.com/optimization/routing/routing_tasks
- https://developers.google.com/optimization/routing/dimensions

## Git

仓库： https://github.com/sin3000x/dn-router （私有）。

```bash
git clone https://github.com/sin3000x/dn-router.git
cd dn-router
uv sync --locked
uv run pytest -q
```

依赖由 `pyproject.toml` 和 `uv.lock` 管理。使用 `uv sync --locked` 安装，生产使用 `uv sync --locked --no-dev`。新增依赖使用 `uv add`，开发依赖使用 `uv add --dev`。Python 默认版本由 `.python-version` 指定。

## 输出协议

CLI 默认输出路线 JSON 数组，与 `solve(payload)["routes"]` 相同；诊断信息（包括逐 DN 的
`rejectedOrders`、全局错误、状态和耗时）输出到 stderr。`--report` 则在 stdout 输出完整报告，
与 Python `solve(payload)` 一致。错误时路线数组为空，CLI 退出码仍为 2；不能仅依据空数组判断成功。

每条路线字段：

- `dist`：路线总距离，含仓库到首站，不含返仓，单位与输入一致。
- `siteAddresses`：按配送顺序排列的 **DN orderReleaseId**，不是 siteCode。
- `departure_date`：仓库出发时间，每条路线都有。
- `siteDetails`：每个 DN 一项，`siteAddress` 为 DN 号，`eta` 为开始卸货时间。
- `vehicle`：车型名称；`volume`、`weight`：该车 DN 的体积、重量之和。

时间统一输出 `YYYY-MM-DD HH:mm:ss`，保留运输耗时产生的秒数，避免截断后 eta 落到窗口外。
同站多个 DN 仍逐单输出，共用一个 eta。完整路线示例见 `examples/output-multi.json`。
