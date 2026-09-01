# 上游 PR #112：SkillSpector 完整性契约不匹配

**这是什么：** 给 [NVIDIA/SkillEvaluator#112](https://github.com/NVIDIA/SkillEvaluator/pull/112)
准备的独立复现确认评论。该 PR 修的正是我们踩到的问题。

**为什么留着：** 它记录了我们把 SkillSpector pin 在 v2.9.6 的原因和证据。
PR 合并后可以升级 SkillEvaluator、解除 pin——届时用
`tests/test_e2e_tier1.py::test_security_scan_completes` 验证：
把 SkillSpector 装回 latest 跑一遍，绿了就说明 pin 可以去掉。

**状态：** 截至 2026-09-01，PR #112 仍为 open，上游 main 停在 `3bfba44`，未合入。
本评论尚未发出。

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
