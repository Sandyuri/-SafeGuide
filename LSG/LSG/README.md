# Learned SafeGate Runtime 1.0.0

这是可独立交付的 Learned SafeGate（LSG）运行包。正式安全判断只来自冻结的 Instruct Splitter 与 Embedding Gate A；不调用旧 SafeGate Core，也不接入 Instruct Gate B。

## 1. 冻结模型

| 组件 | 冻结版本 | 训练选择 | 包内文件 |
| --- | --- | --- | --- |
| Splitter | `lsg_natural_splitter_v0.1` | seed 20261025 / epoch 5 | `artifacts/lsg_natural_splitter_v0.1/best/adapter_model.safetensors` |
| Gate | `lsg_single_intent_v0.1` | seed 20261023 / epoch 8 | `artifacts/lsg_single_intent_v0.1/model.pt` |
| Splitter base | `Qwen/Qwen3-0.6B` | 逐文件 SHA-256 固定 | 默认放到 `models/Qwen3-0.6B/`，不随源码包分发 |
| Gate base | `Qwen/Qwen3-Embedding-0.6B` | 逐文件 SHA-256 固定 | 默认放到 `models/Qwen3-Embedding-0.6B/`，不随源码包分发 |

`configs/base_models.json` 中的逐文件 SHA-256 是基础模型版本的最终约束。即使线上仓库的 `main` 后续变化，哈希不一致也会拒绝启动。

## 2. 目录

```text
LSG/
├── artifacts/                 # 两个冻结 adapter/checkpoint、prompt、标签和 manifest
├── configs/                   # 相对路径、base model 哈希、runtime 和可信 context
├── integrations/roboguard/    # LSG→SPINE/RoboGuard 合同适配器与 allow-list 来源
├── models/                    # 用户准备；默认压缩包排除
├── reports/                   # 冻结验证结果
├── response/                  # 四个 Response 分支
├── runtime/                   # Splitter、Gate、Pipeline、Orchestrator、合同与 session
├── schemas/                   # LSG authorization/orchestration/response schema
├── scripts/                   # 环境、模型准备、服务、CLI 和验证入口
├── tests/                     # Fake/Mock CPU 测试，不加载真实模型
├── FREEZE_NOTES.md
├── MANIFEST.json
├── requirements.lock
├── ROBOGUARD_SPINE_HANDOFF.md
├── SHA256SUMS
└── VERSION
```

运行时会创建 `runs/sessions/`；其中可能包含原始用户输入，不应放进交付压缩包。

## 3. 决策与输出流程

```text
自然语言
  → Instruct Splitter（严格解析 1–3 条 intent）
  → 正式科室目录和可信地点候选
  → 必要时地点澄清；候选必须来自 allow-list
  → 每条 intent 批量进入 Embedding Gate A
  → 校验标签 mask、confidence、candidate 和 schema
  → REJECT > DEFER > AUTHORIZE
  → AUTHORIZE 才进入 Response 或 Robot dry-run
```

- 任一 `REJECT`：整轮不调用 Response 或 Robot。
- 否则任一 `DEFER`：整轮不执行，只保存 pending intent 并追问；默认两次后人工升级。
- 全部 `AUTHORIZE`：按原顺序 fan-out；Response 按原顺序 fan-in，Robot 生成 dry-run plan。
- Splitter 协议、候选、哈希、标签或合同异常：fail-closed 为 DEFER。
- Response 生成失败不会把已经形成的 LSG A/D/R 改写成别的决定。

多轮补充必须关联原 session 和 intent，重新经过 Splitter/Gate，不继承上一轮授权。

## 4. 独立 Python 环境

建议 Python 3.10、Linux/WSL。当前验证环境使用 PyTorch 2.11.0+cu128、CUDA 12.8、Transformers 4.57.6。LSG 不与 RoboGuard 共用虚拟环境。

```bash
cd LSG
./scripts/install_env.sh
```

默认生成 `LSG/.venv`。锁定版本见 `requirements.lock`。如果目标机器的 PyTorch CUDA wheel 需要从专用 index 安装，先按 PyTorch 官方说明安装与驱动匹配的 `torch==2.11.0`，再安装其余锁定依赖。CPU 可运行：设置 `LSG_DEVICE=cpu`；两个 0.6B 模型均在 CPU 上推理会明显更慢。本冻结没有声明一个未经测量的最低显存值；部署前以 `/health` 的 CUDA 峰值字段在目标 GPU 上实测。

## 5. 准备基础模型

### 已有本地权重

```bash
.venv/bin/python scripts/prepare_models.py --model splitter --source /path/to/Qwen3-0.6B
.venv/bin/python scripts/prepare_models.py --model gate --source /path/to/Qwen3-Embedding-0.6B
```

脚本只复制哈希清单中的文件到默认 `models/`，并立即验证。

也可以不复制，通过环境变量直接引用只读目录：

```bash
export LSG_SPLITTER_BASE_MODEL=/path/to/Qwen3-0.6B
export LSG_GATE_BASE_MODEL=/path/to/Qwen3-Embedding-0.6B
.venv/bin/python scripts/verify_package.py --with-base-models
```

### 联网下载

```bash
.venv/bin/python scripts/prepare_models.py --model all --download
```

脚本从 `Qwen/Qwen3-0.6B` 和 `Qwen/Qwen3-Embedding-0.6B` 获取需要的文件；下载结果必须匹配冻结 SHA-256，否则失败。

### 离线机器

在联网机器下载后，将两个基础模型目录复制到离线机器的 `models/`，再运行：

```bash
.venv/bin/python scripts/prepare_models.py --model splitter --target models/Qwen3-0.6B
.venv/bin/python scripts/prepare_models.py --model gate --target models/Qwen3-Embedding-0.6B
```

无 `--source`/`--download` 时命令只验证目标目录。

## 6. API 配置

复制变量名，不要把真实值写进包：

```bash
cp .env.example .env.local
export MODEL_API_KEY='...'
export MODEL_NAME='...'
export MODEL_BASE_URL='...'
```

可选 `MODEL_EXTRA_BODY_JSON`。API 未配置时：

- 需要外部生成或官方资料检索的科室、公开服务、一般药品资料可能返回 `BLOCKED_MISSING_API_ENV`；
- 地点语义解析无法调用 API，会 fail-closed；
- 本地固定 Care、部分药物概念及限定答复仍可生成；
- Gate 的 A/D/R 不因 Response API 失败而改变。

日志不得打印 API key。

## 7. 校验与测试

不需要基础模型的包校验和 startup dry-run：

```bash
.venv/bin/python scripts/verify_package.py
.venv/bin/python scripts/startup_dry_run.py
PYTHONPATH="$(pwd)/.." CUDA_VISIBLE_DEVICES="" .venv/bin/python -m pytest tests -q
sha256sum -c SHA256SUMS
```

基础模型准备后再执行：

```bash
.venv/bin/python scripts/verify_package.py --with-base-models
```

上述 Mock 测试不实例化真实 Splitter/Gate。正式 GPU smoke 应使用全新的非 Test 输入，并确认训练或其他 GPU 任务已经结束。

## 8. 运行

### 单终端常驻聊天

模型只在进程启动时加载一次：

```bash
export LSG_DEVICE=auto
.venv/bin/python scripts/chat_lsg_local.py
```

输入 `/取消` 放弃 pending 请求，`/退出` 释放模型并结束。

### 单次命令

```bash
.venv/bin/python scripts/run_lsg.py --text '头疼应该挂什么科'
```

### 常驻 HTTP 服务

```bash
.venv/bin/python scripts/serve_lsg.py --config configs/runtime.json
```

另一个终端：

```bash
.venv/bin/python scripts/check_service.py
.venv/bin/python scripts/chat_lsg.py
.venv/bin/python scripts/run_one_request.py --text '头疼应该挂什么科' --dispatch
```

默认监听 `127.0.0.1:8765`。接口为 `GET /health`、`GET /model-info`、`POST /evaluate`、`POST /continue`、`GET /sessions/{session_id}`。HTTP `dispatch` 默认 `false`。

## 9. Response 分支

| Gate 分支 | 主要 action | 约束 |
| --- | --- | --- |
| `CARE_GUIDANCE` | `GET_GENERAL_CARE_GUIDANCE` | 固定业务流程，不查询个人记录 |
| `MEDICATION_INFORMATION` | 概念、术语、一般资料 | 不做个人选药、剂量或频次决定 |
| `DEPARTMENT_GUIDANCE` | `RECOMMEND_DEPARTMENT` | 只能从包内正式科室目录选择 |
| `PUBLIC_CARE_INFORMATION` | `GET_PUBLIC_CARE_INFORMATION` | 医院信息或一般购药渠道必须满足允许的官方来源 |

地点 API 只负责从可信目录中解析候选，不作 A/D/R 决策。Robot 合同与联调要求见 `ROBOGUARD_SPINE_HANDOFF.md`。

## 10. 常见问题

- **启动提示 base model missing/hash mismatch**：按第 5 节准备权重；不要关闭哈希验证。
- **每次提问都加载模型**：使用 `chat_lsg_local.py` 或常驻 HTTP 服务。
- **API 分支无法回答**：检查三个 `MODEL_*` 环境变量和网络；查看 `response_results[].response.failure_reason`。
- **终端显示 DEFER**：审计 `fail_closed_reason`、`location_resolutions` 和 `per_intent_predictions`；可能是 Gate DEFER，也可能是候选/合同 fail-closed。
- **Robot JSON 没有动作**：这是设计边界；当前只授权进入规划并要求 RoboGuard 再检查。
- **目录被改名后 import 失败**：交付解压后的顶层目录应保留名称 `LSG`。
