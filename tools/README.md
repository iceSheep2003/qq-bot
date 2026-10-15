# 开发工具

从项目根目录运行，先安装项目开发环境。

| 目录 | 用途 | 示例 |
| --- | --- | --- |
| `experiments/` | 离线图谱、检索、验证集与评估 | `.venv/bin/python -m tools.experiments.bench_behavior_retrieval --help` |
| `operations/` | 控制台、模型配置与语音联调 | `.venv/bin/python -m tools.operations.serve_console --help` |
| `corpus/` | 导出后的本地语料整理 | `.venv/bin/python -m tools.corpus --help` |
| `quality/` | 测试报告、静态检查和覆盖率适配 | `.venv/bin/python -m tools.quality.checks regression` |

工具不随 Bot wheel 分发，也不是用户能力扩展入口。实验输出保存在本地
`experiments/`，私有输入仍保存在本地 `data/`；目录整理不会自动运行导出、模型请求或 QQ 动作。

质量门禁的入口仍是 `.code-quality-gate/toolchain/` 中锁定版本的 Code Quality Gate。
