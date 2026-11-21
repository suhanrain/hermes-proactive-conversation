# 运行测试

[English](testing.en.md) | **简体中文**

测试用的是**假模型**（LLM 响应是桩/模拟的，**不会真的调用大模型、不联网、不烧 token**），但会用到真实的 hermes loader、真实 `SessionStore` 与真实 `config.yaml`（都放在临时隔离目录里，不读写你的 profile、不发消息）。需要「装有 `pytest` 且能 `import hermes`」的解释器。

用例分两类，靠 marker `integration` 区分，**不引入任何外部插件**：

- **单元测试**（`tests/*.py`）——不需要宿主，任何环境都该跑；
- **集成测试**（`tests/host_e2e/`）——需要**真实 Hermes 宿主**（`HERMES_HOME` 下有 `state.db`）。marker 由 `tests/conftest.py` 按路径**自动打上**。

集成测试是**显式 opt-in**：即使机器上有宿主，默认也不跑。

| 环境 | 行为 |
|---|---|
| 未设 `PCHAT_RUN_INTEGRATION=1` | 集成测试**全部 SKIPPED**（无论宿主是否存在），单元照跑 |
| 设了 `PCHAT_RUN_INTEGRATION=1`，且 `HERMES_HOME` 下有 `state.db` | 集成测试**真跑** |
| 设了 `PCHAT_RUN_INTEGRATION=1`，但没有宿主 | 集成测试 SKIPPED，并说明原因 |
| 设了 `HERMES_HOST_REQUIRED=1`，但没有宿主 | **硬失败**（`pytest.exit` + 明确原因），绝不静默 skip 变绿 |

推荐宿主版本：**Hermes Agent v0.23.0**。宿主 API 可能变化，其他版本未验证。

```bash
# 本地 / 无宿主：默认只该看到单元结果，host_e2e 明确 SKIPPED
<python> -m pytest -q -p no:cacheprovider
# 只跑单元
<python> -m pytest -q -m "not integration" -p no:cacheprovider
# 只在确有宿主时跑集成（缺宿主会明确报错，而不是假装通过）
HERMES_HOME=/path/to/live/profile HERMES_HOST_REQUIRED=1 \
    <python> -m pytest -q -m integration -p no:cacheprovider
```
