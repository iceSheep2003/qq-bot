<!-- code-quality-gate:start -->
## Code Quality Gate code-change workflow
For code changes, read .agents/skills/code-quality-gate/SKILL.md before editing implementation. Write acceptance tests, record a real red phase, implement and rerun green until all required checks pass, then run cqg gate before completion or any authorized merge. Keep tests, thresholds, adapters and evidence intact. Respect the user's scope and stop request. Reports are saved in .quality/code-quality-gate/. After each implementation iteration, supply a short --note and read history.md to compare measured metrics; never edit historical numeric facts.
<!-- code-quality-gate:end -->

## 两套测试，别混用

改代码时跑**回归集**，交付或合并前才跑**全量**。

```bash
# TDD 内循环：19 个文件、约 400+ 用例、十几秒
.venv/bin/python -m tools.quality.fast

# 只是想知道集合里有什么 / 有没有失效的路径
.venv/bin/python -m tools.quality.fast --list
.venv/bin/python -m tools.quality.fast --check
```

回归集的名单在 `tests/regression.txt`，一行一个测试文件。放进来的标准是：改坏它会让线上行为出错，或者它守着别的文件依赖的边界。名单改动会出现在 diff 里，评审时看得见；`regression-set` 这项门禁会跑一遍并卡住用例数下限，防止有人为了让门禁变快偷偷删文件。

全量套件（`tools.quality.checks regression`，1600+ 用例）是发布门禁，只在提交前、改架构、动公共契约、升级依赖时跑。它慢一个数量级，但覆盖面才是它的职责。

`.pytest_tmp` 下的临时目录在本机沙箱里会撞 `EEXIST`，跑 pytest 时用新的临时目录：

```bash
export TMPDIR="$(mktemp -d "$PWD/.pytest_tmp/run-XXXXXX")"
```

