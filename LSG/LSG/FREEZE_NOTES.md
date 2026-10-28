# LSG Runtime 1.0.0 Freeze Notes

冻结日期：2026-10-26（Asia/Shanghai）。

正式运行组合固定为：

- Instruct Splitter：`lsg_natural_splitter_v0.1`，seed 20261025，epoch 5。
- Embedding Gate A：`lsg_single_intent_v0.1`，seed 20261023，epoch 8。
- 聚合规则：`REJECT > DEFER > AUTHORIZE`。
- Robot：仅生成通过 LSG 合同验证的 dry-run 任务；不执行物理动作。

本冻结包不包含基础模型大权重，不包含 API key，不包含训练数据、Test 数据、训练缓存或用户 session。基础模型按 `configs/base_models.json` 中的 repo ID、目标目录和逐文件 SHA-256 准备。两个正式 adapter/checkpoint 已包含在 `artifacts/`。

本次冻结没有训练、调参或读取正式 Test。CPU Mock 测试、包内 artifact 哈希、相对路径、敏感信息扫描和全新临时目录 startup dry-run 均在冻结时验证。GPU 正式模型加载没有在本次冻结中重跑；此前本机真实链路已完成 Splitter、Gate、Response 与 Robot dry-run smoke，交接方仍应在目标 GPU 上执行 README 中的模型校验和一次非 Test smoke。
