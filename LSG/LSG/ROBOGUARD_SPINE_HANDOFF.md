# LSG → SPINE / RoboGuard 交接合同

## 1. 边界

LSG 负责：自然语言拆分、逐 intent Gate 判断、`REJECT > DEFER > AUTHORIZE` 聚合、可信地点候选验证，以及生成受约束 Robot plan。

SPINE/RoboGuard 负责：再次验证 LSG 合同、机器人当前状态、地图、路径可达性、实时障碍、权限和执行策略。它不得绕过 LSG 授权，也不得把 DEFER/REJECT、合同错误或候选越界解释为可执行任务。

当前包只输出 `AUTHORIZED_FOR_PLANNING / DRY_RUN`。没有启用真实执行的配置开关；接入方不能仅通过修改字符串启动机器人。

## 2. ROBOT_TASK 何时产生

必须同时满足：

1. Splitter 输出有效且覆盖原始请求；
2. 地点解析得到唯一 allow-list 候选；
3. Gate A 对该 intent 给出 `AUTHORIZE / ROBOT_TASK`；
4. 本轮所有 intent 聚合结果为 AUTHORIZE；
5. action 固定为 `NAVIGATE_TO_LOCATION`；
6. authorization schema、context hash、candidate hash 和 checkpoint provenance 均有效；
7. obligation 精确为 `REQUIRE_ROBOGUARD_EXECUTION_CHECK`。

任何 intent 为 REJECT 或 DEFER，整轮所有 Response/Robot 都被屏障阻止。

## 3. 下游任务结构

LSG 的 `dispatch_plan.items[]` 中，Robot 项的 route 为 `SPINE_TO_RoboGuard_DRY_RUN`，`authorized_task` 示例：

```json
{
  "schema_version": "lsg.robot-task.v0.1",
  "lsg_version": "0.1",
  "session_id": "lsg-session-UUID",
  "evaluation_id": "lsg-evaluation-UUID",
  "request_id": "lsg-request-UUID",
  "action_type": "NAVIGATE_TO_LOCATION",
  "target": {"kind": "LOCATION", "id": "sg-f1-restroom"},
  "resource": {"kind": "LOCATION", "id": "卫生间"},
  "parameters": {},
  "authorization_scope": "ALLOW_TO_ENTER_PLANNING",
  "obligations": ["REQUIRE_ROBOGUARD_EXECUTION_CHECK"],
  "context_id": "ctx-first-floor-navigation-allowlist",
  "context_hash": "64-hex-sha256",
  "runtime_candidate_hash": "64-hex-sha256",
  "provenance": {
    "splitter_checkpoint_sha256": "64-hex-sha256",
    "gate_checkpoint_sha256": "64-hex-sha256",
    "splitter_manifest_sha256": "64-hex-sha256",
    "gate_manifest_sha256": "64-hex-sha256"
  },
  "raw_text": "带我去卫生间"
}
```

字段含义：

- `request_id`：单 intent 的授权请求 ID；必须与 authorization 中 evaluated/normalized request 一致。
- `session_id`：多轮会话 ID；只用于关联，不代表授权可跨轮复用。
- `evaluation_id`：本次 Gate/合同评估 ID；审计和回传必须保留。
- `target.id`：正式地点 ID。
- `resource.id`：用户可读 alias；必须与 allow-list 中该 ID 对应。
- `context_hash`：整个可信 context 的规范 JSON SHA-256。
- `runtime_candidate_hash`：该 intent 实际候选对象的规范 JSON SHA-256。
- `provenance`：冻结 Splitter/Gate checkpoint 与 manifest 的 SHA-256。
- `authorization_scope`：仅允许进入规划，不等价于允许物理执行。
- `obligations`：要求 RoboGuard 在执行前完成自己的运行时安全检查。

## 4. 适配器校验

包内 `integrations/roboguard/adapter.py` 的 `authorize_for_spine()` 执行：

- LSG authorization schema；
- `AUTHORIZE + ROBOT_TASK + AUTHORIZED_FOR_PLANNING`；
- 固定聚合规则和空 `fail_closed_reason`；
- authorization scope 和 obligation；
- checkpoint provenance；
- context ID/hash；
- candidate hash；
- request ID 和 normalized/evaluated request 一致性；
- 唯一可信地点、alias→ID 对应和 action target 一致性。

任何失败必须返回不可执行错误，不得静默修复为 AUTHORIZE。

## 5. 调用方式

推荐 SPINE 调用 LSG HTTP `POST /evaluate`，请求：

```json
{
  "text": "带我去卫生间",
  "session_id": null,
  "request_id": "caller-request-id",
  "trusted_context": null,
  "dispatch": true
}
```

若需要补充，使用 `POST /continue`：

```json
{
  "session_id": "lsg-session-UUID",
  "request_id": "caller-followup-id",
  "supplements": {"1": "挂号收费窗口"},
  "trusted_context": null,
  "dispatch": true
}
```

补充文本不能作为独立新任务直接执行。终态前不得缓存或提前执行先前的 AUTHORIZE intent。

成功时检查：

- `native_result.aggregate_decision == "AUTHORIZE"`；
- 对应 item route 为 `SPINE_TO_RoboGuard_DRY_RUN`；
- `authorized_task` 通过包内 adapter 复验。

失败或暂停时：

- `REJECT`：终止整轮；
- `DEFER`：读取 `dispatch_plan.followups`，不规划、不执行；
- `fail_closed_reason != null`：记录原因并停止；
- HTTP/模型异常、超时、schema 错误：停止；
- Response 失败也不得让同轮 Robot 绕过聚合屏障。

## 6. 从 dry-run 切到真实执行前

必须全部完成并形成独立批准记录：

- 固定 LSG、SPINE、RoboGuard 三方 schema/version 和允许的 provenance；
- 在 SPINE 入口强制调用 adapter，覆盖所有旁路入口；
- 验证实时定位、地图版本、目标可达性、区域权限和导航图状态；
- 验证急停、速度限制、碰撞/障碍检测、人工接管和超时停止；
- 对 context/candidate 变更执行重新评估，禁止复用旧授权；
- 完成重放攻击、错误 session/request/evaluation ID、候选越界和篡改哈希测试；
- 完成模拟器、封闭场地、监护条件下的逐级验收；
- 确认审计日志不含 API key，并有授权、规划、执行和失败的完整关联；
- 由负责机器人安全的人员显式开启独立执行配置。

在这些条件完成前，`AUTHORIZED_FOR_PLANNING` 只能用于 dry-run。

## 7. 联调检查清单

- [ ] LSG health 显示正确 Splitter/Gate hash。
- [ ] 非 AUTHORIZE 不产生 Robot dispatch item。
- [ ] 多 intent 的 R>D>A 屏障生效。
- [ ] 唯一地点来自 `configs/elevator-hall-context.json`。
- [ ] alias、location ID、context hash、candidate hash 全部匹配。
- [ ] request/session/evaluation ID 原样贯穿 SPINE/RoboGuard 审计。
- [ ] provenance 与 `MANIFEST.json` 一致。
- [ ] 修改任一合同字段会 fail-closed。
- [ ] Robot 仍为 dry-run，没有物理动作调用。
