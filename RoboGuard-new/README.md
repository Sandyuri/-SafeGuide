# RoboGuard-new：医院路线独立运行包

这个目录提供医院导航路线规划、确定性路线检查，以及可选的
RoboGuard 医疗自然语言规则/LTL 时序审查。独立路线命令仍会绕过
LSG 和 SPINE；`fixed` 模式不需要 OpenAI API，`hybrid` 模式通过
OpenAI 兼容接口生成补充 LTL。输出路线使用医院 1F 平面图上的人工走廊折点。

路径选择使用 Dijkstra／一致代价搜索，严格按照
`hospital_1f.navigation.json` 每条边的 `distance_m` 累计排序。节点更少不代表
路线更短；RoboGuard 会先审查累计米数最短的候选路线，如果该路线因为权限、
区域状态或携带物限制不可用，再按距离从短到长检查后续候选路线。

## Windows PowerShell

```powershell
cd "C:\Users\10597\Desktop\vscode\RoboGuard-main\RoboGuard-new"

python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m pip install -e .

& ".\.venv\Scripts\python.exe" scripts\show_navigation_route.py `
  --from "功能检查科候诊" `
  --to "心电" `
  --output artifacts\route_functional_waiting_to_ecg.svg
```

如果已在包含 `jsonschema` 的 Python 环境中，也可以直接运行：

```powershell
python scripts/show_navigation_route.py --from "功能检查科候诊" --to "心电" --output artifacts/route_functional_waiting_to_ecg.svg
```

没有模型配置时，使用固定强制规则模式：

```powershell
python scripts/show_navigation_route.py --from "门诊大厅" --to "药房" `
  --temporal-mode fixed --show-ltl `
  --output artifacts/route_lobby_to_pharmacy.svg
```

完整的自然语言规则模式需要先从 conda-forge 安装 Spot，并配置一个
OpenAI 兼容接口：

```powershell
conda install -c conda-forge spot
$env:OPENAI_API_KEY = "..."
$env:OPENAI_BASE_URL = "https://your-compatible-endpoint/v1"
$env:ROBOGUARD_MODEL = "your-model"

python scripts/show_navigation_route.py --from "门诊大厅" --to "药房" `
  --temporal-mode hybrid --show-ltl
```

时序模式：

- `hybrid`：强制规则、固定 LTL 和 LLM 补充 LTL；任何生成或编译失败返回 `PAUSE`。
- `fixed`：强制规则和配置中的固定 LTL，不调用 LLM。
- `off`：只用于调试对照；跳过补充时序检查。

自然语言规则位于 `config/medical_navigation_rules.json`，节点级医疗路线
语义位于 `navigation_sandbox/semantics/route_semantics.json`。LLM 只会看到
`task-region-*` 形式的临时引用，不会看到真实内部节点 ID。

## LSG → SPINE → RoboGuard-new

三模块入口是 `scripts/run_spine_roboguard.py`，默认通过 HTTP 连接同级目录中的
Learned SafeGate（`../LSG/LSG`）。LSG 与 RoboGuard 使用各自独立的 Python 环境；
LSG 模型只在服务启动时加载一次。

RoboGuard 不直接信任 HTTP 返回的 `authorized_task`。本地 adapter 会再次校验
LSG orchestration/authorization schema、可信上下文哈希、runtime candidate 哈希、
冻结模型 provenance、请求/评估 ID、唯一 allow-list 地点以及
`REQUIRE_ROBOGUARD_EXECUTION_CHECK` obligation。全部一致后，任务才会绑定到当前
RoboGuard scene snapshot 并进入 SPINE。`DEFER`、`REJECT`、响应类授权、多个
intent 或任何字段篡改都会在规划前 fail closed。

先在一个 PowerShell 终端启动 LSG：

```powershell
cd ..\LSG\LSG
& ".\.venv\Scripts\python.exe" scripts\serve_lsg.py --config configs\runtime.json
```

如果尚未准备 LSG 环境和基础模型，按 `../LSG/LSG/README.md` 的“独立 Python
环境”和“准备基础模型”两节操作。服务就绪后，`http://127.0.0.1:8765/health`
应返回 `ready: true`。

在本目录的虚拟环境中安装 SPINE：

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -e ..\SPINE-main
```

SPINE 和 RoboGuard 的可选模型可以使用同一个 OpenAI 兼容接口：

```powershell
$env:OPENAI_API_KEY = "..."
$env:OPENAI_BASE_URL = "https://your-compatible-endpoint/v1"
$env:SPINE_MODEL = "your-planner-model"
$env:ROBOGUARD_MODEL = "your-guard-model"
```

先用合成 `goto` 验证 LSG 和 RoboGuard 的连接；这一步不调用 SPINE 模型：

```powershell
python scripts\run_spine_roboguard.py `
  --text "请引导我从门诊大厅前往药房。" `
  --synthetic-goto `
  --temporal-mode fixed
```

若 LSG 不在默认位置或端口，追加 `--lsg-root <目录>` 和 `--lsg-url <URL>`。

验证真实 SPINE 时删除 `--synthetic-goto`。`--text` 是传给 LSG 的原始自然
语言请求。文本中包含地点名称时，程序会解析 `从/由` 后的起点和最终出现的目的
地，因此 `--from`、`--to` 可以省略。没有显式起点时使用场景中的机器人当前
位置；`--from` 仍可用于本地场景测试。

## 验证结果

成功时控制台应包含：

```text
RoboGuard: ALLOW / NAVIGATION_PLAN_ALLOWED
```

同时生成：

```text
artifacts/route_functional_waiting_to_ecg.svg
```

## 数据边界

- `hospital_1f.entrance_mapping.json` 保存入口位姿和数据置信度。
- `hospital_1f.coordinate_mapping.json` 是当前运行时使用的同步坐标。
- `hospital_1f.navigation.json` 保存节点、边、区域权限和距离。
- `hospital_1f.edge_routes.json` 保存平面图上的人工走廊折点，仅用于绘图。
- Dijkstra 使用 `hospital_1f.navigation.json` 的 `distance_m`，不会使用 SVG
  折线的像素长度。
- 当前折线路线没有经过 NavMesh、机器人足迹或动态障碍物验证，不能直接作为底盘控制轨迹。
- 当前支持从 JSON 注入动态事件并废止旧计划版本；真实传感器事件流和底盘局部避障尚未接入。
# 医院地图与医疗工作流联合规划（竞赛版）

当前版本在同一份二维 JSON 医院地图上提供四种可重复方法：

- `dijkstra`：只按距离生成最短路线，作为旧基线；输出会计算路线风险，但风险不参与选路。
- `generate_then_check`：先生成最短路线，再检查封路、权限、携带物、无障碍和区域策略；最短路线违规时直接拒绝。
- `joint`：默认的风险约束 A*。搜索状态同时包含地图节点、工作流阶段和计划版本，违规分支不会进入队列；合法路线按医疗风险、距离、预计时间依次比较。
- `pareto`：在地图节点、工作流状态和计划版本的乘积图中保留多条非支配候选，再按照普通、无障碍、紧急或携带物任务画像选择当前路线。局部变化使用 D* Lite 修复；重大变化重新运行 Pareto 搜索；结果必须取得统一安全屏障签发的执行许可证。

风险约束 A* 使用二维直线距离作为启发信息，并根据地图中“边权/几何距离”的最小比例自动缩放，保证启发值不会高估剩余代价。风险分按路径逐节点累加并给出组成：急救通道 20、关键出入口 30、禁止停留区 5、受控资源区 4，拥堵风险为事件严重度乘 10。风险是第一排序目标，不能用缩短距离抵消；在风险相同时才比较距离和预计时间。

`joint` 使用确定性的工作流自动机执行有序目的地、暂停、重规划版本失效和交接规则。LTLf 公式用于展示与审计，运行时判定以自动机为准。DeepSeek/SPINE 只负责语言解析和高层动作，最终路线仍由 RoboGuard 计算。

不需要复制新的导航沙箱。`navigation_sandbox/` 是只读基准地图，动态状态通过独立事件 JSON 叠加。事件文件支持：

- `NODE_CLOSED` / `NODE_BLOCKED`
- `CONGESTION` / `CROWDING`
- `EMERGENCY_CORRIDOR_ACTIVE`
- `ELEVATOR_UNAVAILABLE`
- `STATUS_EXPIRED`
- `EDGE_COST_CHANGED`
- `PATIENT_STATUS_CHANGED` / `MEDICAL_RISK_CHANGED`
- `TARGET_CHANGED` / `WORKFLOW_CHANGED`
- `PERMISSION_CHANGED` / `CARGO_CHANGED`

完整运行示例：

```bash
python RoboGuard-new/scripts/run_spine_roboguard.py \
  --text "请引导我从门诊大厅前往药房。" \
  --planner joint \
  --events RoboGuard-new/runtime/events/example_events.json \
  --temporal-mode fixed \
  --show-ltl \
  --audit-output RoboGuard-new/artifacts/lobby_to_pharmacy.audit.json \
  --output RoboGuard-new/artifacts/lobby_to_pharmacy.svg
```

复现旧基线只需改成 `--planner dijkstra`。对比“生成后检查”使用 `--planner generate_then_check`。

运行完整的确定性离线流程验证：

```bash
python RoboGuard-new/scripts/run_full_pipeline_demo.py
```

它验证三条路线来源：Pareto候选选择、局部封路后的 D* Lite 修复、患者状态变化后的完整 Pareto 重规划。三种结果都必须通过同一个 `UnifiedSafetyBarrier`，并生成不可省略的执行许可证和 SHA-256 安全证书。输出位于：

- `RoboGuard-new/artifacts/full_pipeline_demo/full_pipeline_results.json`
- `RoboGuard-new/artifacts/full_pipeline_demo/full_pipeline_summary.md`

在实际入口中使用完整规划器：

```bash
python RoboGuard-new/scripts/run_spine_roboguard.py \
  --text "请引导我从门诊大厅前往药房。" \
  --planner pareto \
  --temporal-mode fixed \
  --audit-output RoboGuard-new/artifacts/pareto_route.audit.json \
  --output RoboGuard-new/artifacts/pareto_route.svg
```

动态事件发生后，可在同一正式入口使用 D* Lite 局部修复；如果变化影响医疗风险、任务或权限，系统会自动转入 Pareto 全量重规划：

```bash
python RoboGuard-new/scripts/run_spine_roboguard.py \
  --text "请引导我前往药房。" \
  --planner pareto \
  --replanner dstar_lite \
  --events RoboGuard-new/benchmarks/example_events.json \
  --synthetic-goto \
  --temporal-mode off
```

生成并运行 120 个离线场景（40 普通、20 无障碍、20 多阶段、20 权限、20 动态事件）：

```bash
python RoboGuard-new/scripts/generate_planner_benchmark.py
python RoboGuard-new/scripts/run_planner_benchmark.py
```

结果写入：

- `RoboGuard-new/artifacts/benchmark/planner_comparison.csv`：Dijkstra、A*、生成后检查、联合规划和 Pareto 在每个场景中的完整流水线结果，包括独立验证凭证与执行许可证。
- `RoboGuard-new/artifacts/benchmark/planner_comparison.summary.json`：最终可执行成功率、验证凭证率、许可证率、动态任务成功率、端到端平均/P95 耗时，以及四种算法相对 Dijkstra 的成对风险、距离和耗时变化。
- `--audit-output`：单次任务的授权 ID、场景哈希、地图版本、路径、风险组成、工作流轨迹、规则和拒绝原因。

核心基准不调用 DeepSeek，以免 API 随机性干扰路线算法对比。自然语言模型对比应使用相同输入文本、相同提示词、固定温度和同一场景集，并单独统计目的地解析准确率、结构化任务完整率和平均 token/延迟。
