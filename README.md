# dn-router

基于 OR-Tools Routing 的单仓开放路线配送初始代码仓。Python 3.11+。
输入兼容示例业务字段；车型不限数量；DN 不拆单；除明确时间窗异常外，所有 DN 必须恰好安排一次。
请求及嵌套对象允许携带额外字段，解析时忽略，不参与计算。

## 快速启动

```bash
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -e . "pytest>=8,<10"                     # 只运行求解时不需要 pytest
dn-router examples/input-multi.json --time-limit-ms 1000
pytest -q
python scripts/benchmark.py --dn 100 --runs 5 --time-limit-ms 200
```

也支持 `python -m dn_router input.json`、stdin (`input` 为 `-`) 和 `--output result.json`。
用 uv 的话，把上面的安装换成 `uv sync`，命令前加 `uv run` 即可；仓库不提交 `uv.lock`。
Python 调用：

```python
from dn_router import solve

result = solve(payload, time_limit_ms=1000)
```

`examples/input-original.json` 保留原始示例数据（去掉注释并修正 JSON 标点）：
DN 体积 100 > 最大容积 65，应返回 `INFEASIBLE_INPUT`，不是成功漏单。
`input-feasible.json` 仅将 DN 体积改成 1；`input-multi.json` 演示三个 DN、两个实际站点。

## 结果可视化

### 在线服务（推荐远程开发使用）

在服务器上启动可视化服务，浏览器里选择请求文件即可求解并查看：

```bash
dn-router serve --dir examples          # 默认监听 0.0.0.0:8787
```

- 页面列出 `--dir` 下的全部 `*.json`（最近修改的在前，跳过隐藏目录），也可上传、粘贴或直接拖入 JSON。
- 地址栏会记录 `?file=...&t=...`，刷新页面即重新求解该文件，修改请求后刷新即可看到新结果。
- “导出 JSON” 下载求解器原始结果，与 `--report` 输出一致。
- 默认监听所有网卡，启动时会打印可直接访问的地址（主机名、出口 IP），在本地浏览器打开
  `http://<服务器>:8787` 即可。服务无鉴权，只读取 `--dir` 内的 JSON；服务器暴露在公网时，
  改用 `--host 127.0.0.1`，再在本地执行 `ssh -L 8787:localhost:8787 <服务器>` 访问。
- 其他参数：`--port`、`--time-limit-ms`（页面可逐次修改，上限 120000）。

接口：`GET /api/files`，`GET /api/solve?file=<相对路径>&time_limit_ms=`，
`POST /api/solve?time_limit_ms=`（请求体为请求 JSON）；求解接口返回报告数据，`output` 为原始结果。

### 报告内容

页面浅色 / 深色跟随系统。报告包含汇总指标、可切换的车辆路线、体积/重量装载率、停靠顺序、到离站时间、等待与卸货时长，以及异常订单和告警。路线图支持以指针为中心的滚轮缩放、拖动和重置。

- **时间轴播放**：播放条紧贴地图下方，与地图同屏；播放/暂停（空格键）、回到起点、拖动时间，以及每秒推进 1/5/15/60 个时间单位的速度选择。时间范围覆盖所有车辆；筛选路线保留当前时间。
- **车辆甘特图**：每车一行，按行驶、等待、卸货分段；点击色块暂停播放并切换到该路线及该阶段起点。色块悬浮提示包含起止时间和持续分钟数。
- **地图联动**：车辆方形标记随时间移动，当前行驶路段加粗，已走实线、未走虚线；等待和卸货时显示状态，并高亮停靠明细。路线结束自动显示“已完成”，全局播放到终点自动暂停，再次播放从起点开始。悬停路线列表或甘特图中的车辆时，地图突出该路线、淡化其余路线。

行驶位置按矩阵耗时沿直线插值；为演示采用“先行驶后等待”的分段方式，把等待标记放在目的站附近并注明“位置示意”。这不是实际等待地点或 GPS 轨迹。零时长阶段不扩造持续时间；无有效时间表时禁用播放。跨日时间仍采用业务当地时刻，不按浏览器时区换算。
经纬度按直线连接，仅作位置与顺序示意，不代表真实道路；总距离保留业务矩阵单位，不自动标成公里。重合坐标不偏移，停靠明细仍逐项展示；缺失或同站不一致的坐标不连线。无路线或输入不可行时也会显示状态报告。

报告依据 `siteDetails[].eta`（开始卸货时间）和输入矩阵重建显示用停靠与时间分段，同站只计一次卸货；公开 JSON 输出协议保持不变。

Python 调用（保留原始输入以关联坐标与车型容量）：

```python
from dn_router import report_data, solve

result = solve(payload, time_limit_ms=1000)
data = report_data(payload, result)  # 页面所用的 JSON 数据
```

## 文档

- [入参与出参](docs/api.md)：请求字段、路线与完整报告字段、状态码、错误码、CLI 退出码。
- [业务规则](docs/business-rules.md)：容量、停靠与站点计数、距离和时间约束、剔除规则、矩阵要求、优化目标。
- [算法设计](docs/algorithm.md)：求解流程、初解与路线消除、OR-Tools 建模、时间预算。
- [Benchmark](docs/benchmark.md)：合成数据实测记录，原始结果在 `docs/benchmarks/`。

## 性能边界

默认 `time_limit_ms=200` 是**尽力而为的整体计算预算**，不是硬实时承诺。
本版本尚未达到 100 DN / 200ms 的原始性能目标；需要更好的距离建议给 1–3 秒。详见
[算法设计 · 时间预算](docs/algorithm.md#时间预算) 和 [Benchmark](docs/benchmark.md)。

`scripts/benchmark.py` 是固定种子的合成数据冒烟测试，不代表真实业务分布，也不提供最优性证明。
生产验收需固定硬件和并发，检查 P95/P99、车辆数、距离和较长预算基线的差距。

## 文件结构

- `src/dn_router/models.py`：输入类型与结构校验。
- `problem.py`：矩阵、单位、预检查、独立路线审计。
- `initial.py`：有界可行初解构造。
- `arcs.py`：求解器回调用的距离、时间、首段预算矩阵，用 numpy 整体计算。
- `reduce.py`：车辆数优先时的路线消除阶段；阶段列表见 `solver._seed_stages`，新增阶段只需在此挂接。
- `solver.py`：OR-Tools 模型与输出。
- `visualization.py` + `report.html`：可视化报告的数据组装与页面模板。
- `cli.py`：命令行；`server.py`：在线可视化服务。
- `schemas/request.schema.json`：生成的 JSON Schema。
- `scripts/benchmark.py`：合成数据基准。
- `docs/`：接口、业务规则、算法设计与 benchmark 记录。
- `tests/`：业务回归测试。

## Git

仓库： https://github.com/sin3000x/dn-router （私有）。

```bash
git clone https://github.com/sin3000x/dn-router.git
cd dn-router
python3 -m venv .venv && source .venv/bin/activate
pip install -e . "pytest>=8,<10"
pytest -q
```

依赖声明在 `pyproject.toml`：运行依赖在 `dependencies`，开发依赖（pytest）在 `[dependency-groups] dev`。
新增依赖直接改 `pyproject.toml`。生产环境用 `pip install .`。`.python-version` 是开发默认的 Python 版本。
`uv.lock` 和 `uv.toml` 在 `.gitignore` 中（内网镜像会改写锁文件里的源地址），不作为依赖管理依据。

