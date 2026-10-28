# SafeGate-RoboGuard

医院场景下「自然语言 → 安全授权 → 语义规划 → 路线执行」的完整链路，由三个可独立运行的子项目组成。

| 子项目 | 角色 | 关键实现 |
| --- | --- | --- |
| **LSG**（`LSG/LSG`） | 语义理解与安全门控 | 冻结的 Instruct Splitter（Qwen3-0.6B 微调）+ Embedding Gate A（Qwen3-Embedding-0.6B 微调），输出 `AUTHORIZE / DEFER / REJECT` |
| **RoboGuard-new** | 医院 1F 导航规划与统一安全屏障 | Dijkstra 基线、风险约束 A\*（`joint`）、Pareto 多目标规划（`pareto`）、D\* Lite 局部修复、LTLf 时序审查 |
| **SPINE-main** | 上游语义规划框架（第三方） | ICRA 2025 SPINE 框架及其 ROS 封装，作为高层任务解析与规划器被 RoboGuard 调用 |

## 数据流

```text
用户自然语言
     │
     ▼
LSG · Instruct Splitter ──► 可信地点候选 / 正式科室目录
     │
     ▼
LSG · Embedding Gate A ──► REJECT > DEFER > AUTHORIZE（fail-closed）
     │ AUTHORIZE
     ▼
RoboGuard adapter 二次校验：authorization schema、上下文哈希、
runtime candidate 哈希、模型 provenance、allow-list、obligation
     │
     ▼
SPINE 高层语义规划 ──► RoboGuard 路线规划 + UnifiedSafetyBarrier
     │
     ▼
dry-run 计划 · 执行许可证 · SHA-256 安全证书
```

关键约束：任一 `REJECT` 整轮不进入 Response/Robot；任一 `DEFER` 整轮不执行、只保存 pending intent 并追问；RoboGuard 不直接信任 LSG 返回的 `authorized_task`，必须本地重新校验后才交给 SPINE。

## 目录结构

```text
safegate-RoboGuard/
├── LSG/
│   └── LSG/                     # LSG 运行包根目录（注意是双层同名目录）
│       ├── artifacts/           # 冻结的 adapter / checkpoint / prompt / manifest
│       ├── configs/             # 路径、基座模型哈希、runtime、可信 context
│       ├── integrations/        # LSG → RoboGuard 合同适配器
│       ├── response/            # CARE / MEDICATION / DEPARTMENT / PUBLIC_CARE 四个分支
│       ├── runtime/             # Splitter、Gate、Pipeline、Orchestrator、session
│       ├── schemas/             # authorization / orchestration / response 的 JSON Schema
│       ├── scripts/             # 环境、模型准备、CLI、HTTP 服务、包校验
│       └── tests/               # CPU Mock 测试，不加载真实模型
├── RoboGuard-new/
│   ├── src/roboguard/           # 路线校验、joint/pareto 规划器、D* Lite、安全屏障
│   ├── navigation_sandbox/      # 只读基准地图（医院 1F 节点/边/区域权限）
│   ├── config/                  # 医疗导航规则、路线画像、校验规则
│   ├── benchmarks/              # 120 个离线场景与示例事件
│   ├── runtime/events/          # 动态事件 JSON 示例
│   └── scripts/                 # 单模块入口、三模块联跑入口、基准生成/运行
└── SPINE-main/
    ├── src/spine/               # SPINE 核心（语义图、frontiers、prompts、可视化）
    └── ros/spine_ros/           # ROS 封装（launch、srv、节点脚本）
```

> `LSG/LSG` 是双层同名目录，所有 LSG 命令都需要在**内层** `LSG/LSG` 中执行。

## 快速开始

**三个子项目使用彼此独立的 Python 环境**，不要把 LSG 的 PyTorch 依赖和 RoboGuard 混装。下面以仓库根目录为起点，Python 3.10。

### 1) LSG：环境、基座模型、启动服务

```bash
cd LSG/LSG

# Linux / WSL（推荐，torch 依赖在此验证）
./scripts/install_env.sh

# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
```

基座模型**不在本仓库**（`Qwen/Qwen3-0.6B` 与 `Qwen/Qwen3-Embedding-0.6B`），需按固定 SHA-256 自行准备：

```bash
# 联网下载并校验哈希
.venv/bin/python scripts/prepare_models.py --model all --download
# 或从本地权重复制
.venv/bin/python scripts/prepare_models.py --model splitter --source /path/to/Qwen3-0.6B
.venv/bin/python scripts/prepare_models.py --model gate --source /path/to/Qwen3-Embedding-0.6B

.venv/bin/python scripts/verify_package.py --with-base-models
```

配置外部模型 API（只填变量，不要提交真实值）：

```bash
cp .env.example .env.local
export MODEL_API_KEY='...'
export MODEL_NAME='...'
export MODEL_BASE_URL='...'
```

启动常驻服务（默认 `127.0.0.1:8765`）：

```bash
.venv/bin/python scripts/serve_lsg.py --config configs/runtime.json
# 另一个终端
.venv/bin/python scripts/check_service.py          # 期望 ready: true
.venv/bin/python scripts/run_one_request.py --text '头疼应该挂什么科' --dispatch
```

单次命令（不启服务）：`scripts/run_lsg.py --text '...'`；单终端常驻聊天：`scripts/chat_lsg_local.py`。

### 2) RoboGuard-new：装环境、单模块跑通

```powershell
cd RoboGuard-new
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m pip install -e .
```

不需要任何模型即可验证路线规划：

```powershell
& ".\.venv\Scripts\python.exe" scripts\show_navigation_route.py `
  --from "门诊大厅" --to "药房" --temporal-mode fixed --show-ltl `
  --output artifacts\route_lobby_to_pharmacy.svg
```

`--temporal-mode`：`fixed`（强制规则 + 配置中固定 LTL，不调 LLM）、`hybrid`（额外用 LLM 补充 LTL，需 `conda install -c conda-forge spot` 与 OpenAI 兼容接口）、`off`（仅调试对照）。

### 3) SPINE-main：作为库安装

```powershell
# 装进 RoboGuard 的虚拟环境即可，供三模块联跑调用
& ".\.venv\Scripts\python.exe" -m pip install -e ..\SPINE-main
```

离线示例：`python SPINE-main/scripts/test_spine.py --help`。

### 4) 三模块联跑（LSG → SPINE → RoboGuard）

终端 A 启动 LSG 服务；终端 B：

```powershell
cd RoboGuard-new
$env:OPENAI_API_KEY  = "..."
$env:OPENAI_BASE_URL = "https://your-compatible-endpoint/v1"
$env:SPINE_MODEL     = "your-planner-model"
$env:ROBOGUARD_MODEL = "your-guard-model"

# 先用合成 goto 验证 LSG 与 RoboGuard 的连接（不调用 SPINE 模型）
& ".\.venv\Scripts\python.exe" scripts\run_spine_roboguard.py `
  --text "请引导我从门诊大厅前往药房。" --synthetic-goto --temporal-mode fixed

# 验证真实 SPINE：去掉 --synthetic-goto
& ".\.venv\Scripts\python.exe" scripts\run_spine_roboguard.py `
  --text "请引导我从门诊大厅前往药房。" --planner joint --temporal-mode fixed --show-ltl
```

成功标志：控制台出现 `RoboGuard: ALLOW / NAVIGATION_PLAN_ALLOWED`，并生成 `artifacts/*.svg` 与 `--audit-output` 指定的审计 JSON。

## 入口脚本速查

| 目的 | 命令（位于 `RoboGuard-new/` 下执行） |
| --- | --- |
| 纯路线规划（不经 LSG/SPINE） | `scripts/show_navigation_route.py --from ... --to ...` |
| 三模块正式入口 | `scripts/run_spine_roboguard.py --text "..."` |
| 选择规划器 | 追加 `--planner dijkstra \| generate_then_check \| joint \| pareto` |
| 注入动态事件 | 追加 `--events runtime/events/example_events.json` |
| 局部修复 | 追加 `--replanner dstar_lite` |
| 确定性全流程验证 | `scripts/run_full_pipeline_demo.py` |
| 生成 / 运行 120 场景基准 | `scripts/generate_planner_benchmark.py` → `scripts/run_planner_benchmark.py` |

基准输出：`artifacts/benchmark/planner_comparison.csv`（每个场景的完整流水线结果、验证凭证、执行许可证）与 `planner_comparison.summary.json`（可执行成功率、许可证率、动态任务成功率、P95 耗时、相对 Dijkstra 的风险/距离变化）。

## 规划器一览（RoboGuard）

| 模式 | 说明 |
| --- | --- |
| `dijkstra` | 只按 `distance_m` 求最短路线，作为旧基线；计算风险但不参与选路 |
| `generate_then_check` | 先生成最短路，再检查封路/权限/携带物/无障碍/区域策略，违规直接拒绝 |
| `joint` | 默认的风险约束 A\*，搜索状态 = 地图节点 × 工作流阶段 × 计划版本；风险（急救通道 20、关键出入口 30、禁止停留区 5、受控资源区 4、拥堵 = 严重度 × 10）优先于距离与时间 |
| `pareto` | 在乘积图中保留非支配候选，按普通/无障碍/紧急/携带物画像选路；局部变化用 D\* Lite 修复，重大变化全量重规划，最终必须取得 `UnifiedSafetyBarrier` 执行许可证 |

## 测试

LSG（Mock，不加载真实模型）：

```bash
cd LSG/LSG
PYTHONPATH="$(pwd)/.." CUDA_VISIBLE_DEVICES="" .venv/bin/python -m pytest tests -q
sha256sum -c SHA256SUMS
.venv/bin/python scripts/verify_package.py
.venv/bin/python scripts/startup_dry_run.py
```

RoboGuard：

```powershell
cd RoboGuard-new
& ".\.venv\Scripts\python.exe" -m pytest tests -q
& ".\.venv\Scripts\python.exe" scripts\run_full_pipeline_demo.py
```

## 仓库包含与不包含的内容

**已包含**：两个冻结的 LSG 权重（`adapter_model.safetensors` 约 18 MB、`model.pt` 约 5.7 MB）、schema、配置、测试、只读基准地图、示例事件、基准脚本。

**未包含（需自行准备）**：

- 基座模型 `Qwen/Qwen3-0.6B`、`Qwen/Qwen3-Embedding-0.6B`（按 `configs/base_models.json` 的哈希校验，`scripts/prepare_models.py` 可下载）
- 真实 `.env`（仓库只有 `.env.example` 模板）
- `runs/` 会话日志（可能含原始用户输入）
- Isaac Sim 医院场景资产（`医院一楼_AA机器人_20261004/`，USD + 贴图约 200 MB），本地留存，`.gitignore` 已将该目录排除
- **医院一层平面图原图**（`RoboGuard-new/navigation_sandbox/artifacts/floorplan/source/hospital_1f_floorplan.jpg`，由医院方提供的建筑出图）以及内嵌该图的路线叠加 SVG（`*.e2e-*.svg`）：出于保密要求**不入库**，仅本地留存。`show_navigation_route.py`、`generate_navigation_drawio.py`、`generate_pareto_case_visualizations.py`、`generate_workflow_dynamic_visualizations.py` 和 `run_spine_roboguard.py --output *.svg` 的绘图输出需要把原图放回上述路径；纯文本/JSON 输出与全部测试不受影响
- `*.egg-info`、`__pycache__`、`.venv` 等构建与缓存产物

## 环境与依赖

| 组件 | 版本 / 说明 |
| --- | --- |
| Python | 3.10+（LSG 建议 3.10） |
| LSG | `torch==2.11.0`（+cu128 验证）、`transformers==4.57.6`、`peft==0.21.2`、`safetensors==0.8.0`，完整锁定见 `LSG/LSG/requirements.lock`；CPU 可跑（设 `LSG_DEVICE=cpu`，速度明显下降） |
| RoboGuard | `jsonschema>=4.0,<5.0`、`openai>=3.6,<4.0`、`tiktoken>=0.5`；`hybrid` 时序模式额外需要 Spot（conda-forge） |
| SPINE | 以 `pip install -e SPINE-main` 安装；ROS 封装需对应 ROS 环境 |

## 许可证

- `RoboGuard-new`：MIT（见 `RoboGuard-new/LICENSE`）
- `SPINE-main`：BSD 3-Clause（见 `SPINE-main/LICENSE`），来源为 KumarRobotics/SPINE
- `LSG/LSG`：交付包未附许可证文件

## 引用

SPINE 上游工作：

```bibtex
@article{ravichandran_spine,
  title={SPINE: Online Semantic Planning for Missions with Incomplete Natural Language Specifications in Unstructured Environments},
  author={Zachary Ravichandran and Varun Murali and Mariliza Tzes and George J. Pappas and Vijay Kumar},
  year={2025},
  journal={International Conference on Robotics and Automation (ICRA)},
  url={https://arxiv.org/abs/2410.03035},
}
```

各子项目更详细的说明见 `LSG/LSG/README.md`、`RoboGuard-new/README.md`、`SPINE-main/README.md`。
