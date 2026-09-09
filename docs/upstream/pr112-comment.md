# 上游 PR #112：SkillSpector 完整性契约不匹配

**这是什么：** 给 [NVIDIA/SkillEvaluator#112](https://github.com/NVIDIA/SkillEvaluator/pull/112)
准备过的独立复现确认评论。下半部分是当时写好的原文，保留下来当证据。

**状态（2026-09-09）：** PR #112 已合并——`ff349e0`，2026-09-08 进 main，上游
尚未打 tag（最新 tag 仍是 `v0.1.0`，版本号仍写 0.2.1）。评论最终没有发出，
现在也不必发了。

**结论：pin 不能解。** 合并版比原 PR 大得多，按版本分了契约（2.9.5/2.9.6
statusless、2.10+ 带 `status`、2.11+ 要 `bundled_execution_surface`、2.11.1+ 用
finding ID）。下表那条 `recommendation` 不匹配的报错确实没有了——校验改成了
"incomplete 且 LOW 时期望 CAUTION"——但 partial 报告只是从"整份丢弃"变成
"保留 findings、扫描仍记为 incomplete"。触发源没变：2.11.1 对同一份 skill 仍然
在 `SKILL.md:12` 记一条非致命的 `reference_unresolved`，`status` 仍是 `partial`。

用 `tests/test_e2e_tier1.py` 的 fixture 实测（evaluator 为 `ff349e0`）：

| SkillSpector | Security Scan | `incomplete_scans` | 说明 |
| --- | --- | --- | --- |
| 2.9.6 | passed | `[]` | pin 保持有效，升 evaluator 不退化 |
| 2.10.0 | incomplete | `["skillspector"]` | `analysis_completeness reports incomplete analysis (status 'partial')` |
| 2.11.1 | incomplete | `["skillspector"]` | 同上 |

`incomplete_scans` 非空在本服务里就是 INCOMPLETE 且不复用，所以升 SkillSpector
对文档型 skill 没有任何改善。解 pin 的前提与后续选项见
[README](../../README.md) 的安装一节。

---

Independent reproduction — this matches the root cause described here exactly, and I bisected the SkillSpector side.

**Minimal reproducer.** A skill with no findings at all is enough; the only trigger is a `/` in a heading, which the reference resolver records as an unresolved local reference:

```markdown
---
name: repro
description: Minimal reproducer for the SkillSpector completeness contract mismatch. Use when reproducing the reported validation failure.
---

# Repro

## Overview

Prose only. No executable content.

### Input/Output Separation

Keep inputs and outputs distinct.
```

**Behaviour across SkillSpector versions** (same skill, same SkillEvaluator commit, `--no-llm`):

| SkillSpector | score | severity | recommendation | `is_complete` | `skillevaluator security-scan` |
| --- | --- | --- | --- | --- | --- |
| 2.9.6 | 0 | LOW | SAFE | false | PASS |
| 2.10.0 | 0 | LOW | CAUTION | false (`status: partial`) | INCOMPLETE |
| 2.11.0 | 0 | LOW | CAUTION | false (`status: partial`) | INCOMPLETE |

Failure message on 2.10.0+:

```
skillspector JSON field 'risk_assessment.recommendation' does not match the risk severity;
security scan did not complete
```

Three things worth noting:

1. **The skill has zero findings and `score: 0`**, yet the entire security scan result is discarded. The rejection is not scoped to one questionable claim in the report.
2. **2.9.6 already reports `is_complete: false`** for this skill — it just doesn't escalate the recommendation. So the behavioural change in 2.10.0 is the fail-closed escalation, not completeness detection. That supports reading `analysis_completeness` before the recommendation/severity invariant, as this PR does.
3. **The trigger surface is wide.** `ledger_exceptions` is a single `reference_unresolved` from the `reference_resolution` phase, raised by a slash inside a Markdown heading. Prose-heavy skills hit this readily — the first real skill where I saw it had two such exceptions, one from a heading and one from a table cell.

Environment: SkillEvaluator `3bfba44` (main, reports 0.2.1), SkillSpector installed via `uv tool install`, macOS / Python 3.13.

Until this lands, we are pinning SkillSpector to `v2.9.6` downstream, with a regression test that fails on 2.10.0+ so we can tell when the pin is safe to drop.

---

*以上为 2026-09-01 写就的原文，未发出。它描述的失效形式（recommendation 与
severity 不匹配、整份报告被丢弃）已被 `ff349e0` 修复；残留的 partial→incomplete
问题见本文开头。*
