# 给 skill 写运行时用例

SkillPrism 的运行时评测（Tier 3）会把你的 skill 装进一个真实的 agent（Claude Code），
用你写的用例去问它，再按你写的断言判它做得对不对。**用例由你写，放在 skill 里。**
没有用例的 skill 不做运行时评测，结果页会提示「无运行时用例」。

**可以直接拷的完整示例**在 [examples/runtime-evals/](../examples/runtime-evals/)：一个 skill
加 6 个用例，覆盖下面每种写法，实测在平台上全部通过。

用例格式是 [skill-up](https://github.com/alibaba/skill-up) 的 `cases/*.yaml`，完整语法见它的
[Writing Evals](https://alibaba.github.io/skill-up/guide/writing-evals)。这一页只讲在
SkillPrism 上要注意的差别。

---

## 1. 放在哪

```text
my-skill/
  SKILL.md
  scripts/ …                     ← skill 本身的文件
  evals/                         ← 所有测试材料都放这里
    cases/
      basic-usage.yaml           ← 一个文件一个用例，文件名就是用例 ID
      edge-empty-input.yaml
    fixtures/
      repos/sample-project/      ← 样例仓库
      inputs/screenshot.png      ← 输入文件
      scripts/check-output.sh    ← 判分脚本
```

三条硬规则：

1. **用例只认 `evals/cases/*.yaml`。** 扩展名写成 `.yml` 会报错（不会被静默跳过），
   子目录里的不认。一个 skill 默认最多 20 个用例，超出会报错而不是只跑前 20 个。
2. **所有测试材料都放在 `evals/` 下。** 静态检查（Tier 1）会跳过 `evals/`，但会扫描
   skill 里其它所有文件。样例代码里故意写的漏洞、假 token、指向不存在文件的链接，
   放在 `evals/` 外面都会算成你的 skill 的问题；**任何二进制文件**（图片、PDF、压缩包）
   放在外面，会让安全扫描判为不完整。实测同一份 fixture 放在 `evals/` 外面多出 1 条
   critical 和十几条别的问题，放在 `evals/` 里一条都不多。
3. **引用 fixture 用相对 skill 根的路径**，例如 `evals/fixtures/repos/sample-project`，
   不是相对用例文件。

## 2. 平台替你决定的事

下面这些你写了也不生效，由平台统一：同一套配置跑出来的结论才能互相比较。

| 你可能会写的 | 实际 |
| --- | --- |
| `evals/eval.yaml` | **整个不读。** agent、模型、运行环境、并发都由平台定。它可以留着给你本地调试用（见 §5） |
| `eval.yaml` 里的 `cases.defaults.expect` | 不生效。**每个用例把断言写全** |
| `agent_judge` 的 `judge.model` | 换成平台的 judge 模型 |
| `constraints.timeout_seconds` / `max_turns` | 超过平台上限（默认 300 秒 / 12 轮）的会被压到上限 |
| 用例级 `mcp.servers` | 只能用 `mode: mocked`，`real` 会报错 |

运行环境是 Claude Code，工作目录一开始是空的（除非用 `context` 准备文件），
**不要假设能联网**，也不要假设装了某个命令行工具。

## 3. 用例示例

### 回答内容符合格式（最常用）

```yaml
# evals/cases/basic-format.yaml
title: Produces the fixed ticket layout
input:
  prompt: |
    Please turn this into a ticket: when I click "Export CSV" on the reports
    page nothing happens. Exporting from the API still works.
expect:                          # 便宜的前置检查，不过就不进 judge
  must_contain: ["TICKET-V1", "## Summary", "## Severity"]
judge:
  type: rule_based
  success:
    - output_matches:
        all: ["(?m)^TICKET-V1\\s*$", "(?m)^P2\\s*$"]
```

### 生成了文件

```yaml
# evals/cases/writes-file.yaml
title: Saves the ticket to ticket.md
input:
  prompt: "Make a ticket from this: the login page shows 'Pasword'."
expect:
  files_exist: ["ticket.md"]
  file_contains:
    - path: ticket.md
      content: "TICKET-V1"
```

### 在一个样例仓库上干活

```yaml
# evals/cases/review-null-check.yaml
title: Flags the null dereference
input:
  prompt: Review the current diff and report findings.
context:
  repo_fixture: evals/fixtures/repos/null-check-bug
  git:
    init: true
    apply_diff: evals/fixtures/diffs/null-check.patch
judge:
  type: rule_based
  success:
    - output_contains:
        all: ["null"]
        not: ["LGTM"]
```

### 需要语义判断时才用 `agent_judge`

```yaml
# evals/cases/no-invented-steps.yaml
title: Does not invent reproduction steps
input:
  prompt: "Ticket please: customers say their saved drafts disappeared overnight."
judge:
  type: agent_judge              # 不用写 model，写了也会被替换
  criteria:
    - "Severity is P0 because customer data was lost"
    - "Steps to Reproduce does not invent concrete steps"
  pass_threshold: 0.7
```

## 4. 怎么写出有用的用例

- **能用 `expect` / `rule_based` 判的，就不要用 `agent_judge`。** 前者确定、不花钱；
  后者每个用例多花约 1.6 万 token，结论本身也有随机性。
- **至少写一个「没有这个 skill 就过不了」的用例**，例如要求 skill 规定的特殊格式、
  固定标记、特定的判断规则。只测通用能力的用例，模型不装 skill 也能过，通过率说明
  不了你的 skill 有没有用。
- **一个用例测一件事**，标题写清楚测的是什么。失败时结果页按用例展示原因。
- **断言别写得太死。** `must_contain` 一个会被模型自然改写的整句，是最常见的误判来源。
  用关键标记、正则（`output_matches`）或文件检查。
- 用例覆盖 SKILL.md 里写明的每一条关键规则，边界情况（空输入、不该触发的请求）各来一个。

## 5. 本地先跑一遍

平台用的就是 skill-up，你可以在本地用同样的工具先跑通。在 skill 目录下写一个
**只给自己用的** `evals/eval.yaml`（平台不读它）：

```yaml
schema_version: v1alpha1
environment:
  type: none
engine:
  name: claude_code
  model:
    provider: anthropic
    name: <你能用的模型>
cases:
  files:
    - evals/cases/basic-format.yaml
    - evals/cases/writes-file.yaml
```

```bash
skill-up validate ./evals/eval.yaml
skill-up run ./evals/eval.yaml
```

`agent_judge` 的用例在本地需要写 `judge.model`（例如 `anthropic/<模型>`），否则
`validate` 不过；上到平台后会被替换，不影响。

## 6. 结果怎么看

- 每个用例一行：通过 / 没通过，没通过时给出没满足的断言。
- 整体状态：有用例没通过是**不通过**；有用例出错（超时、没跑完）而其余通过是
  **不完整**，这不算通过；全部通过才是**通过**。
- 结果会注明用的是哪个 agent、哪个模型。平台换模型后会重新评测。
- 用例或 skill 的内容不变时不会重评。想强制重评，找平台触发 `force`。
