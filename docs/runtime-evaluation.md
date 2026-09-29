# 运行时评测（Tier 3）设计

> 状态：**设计稿，尚未实现**（2026-09-29）。落地后，其中讲"为什么"的部分并入
> README，对接方要照着做的部分并入 [api.md](api.md)，部署部分并入
> [deployment.md](deployment.md)。

Tier 1 是静态检查：看 skill 写得对不对。Tier 3 是实跑：把 skill 装进一个真实
的 agent，喂作者写好的用例，看它做得对不对。执行器用
[skill-up](https://github.com/alibaba/skill-up)（Go 单二进制），**不用**
SkillEvaluator 自带的 Tier 3（harbor + autopilot 生成用例）。

---

## 1. 范围

**做：**

- 单个 skill 的运行时评测，用例由作者随 skill 提供（`evals/cases/*.yaml`）。
- zip 与 GitLab 两种接入都支持，沿用现有的取内容、物化、结论复用、报告链路。
- agent 用 Claude Code，模型走公司内部网关的 Anthropic 兼容接口。

**不做（本期）：**

- **隔离。** agent 直接在 worker 机器上跑（skill-up 的 `environment: none`）。
  这是一个明确接受的风险，理由和边界见 §10。docker / sandbox 是后续工作。
- 一组耦合 skill（`bundle: true`）的实跑：提交时直接拒绝，见 §8.1。
- 平台自动生成用例：生成用例和判分用的是同一类模型，自己出题自己判，结论没有意义。
- 有无 skill 对比（skill-up 的 `--baseline`）：token 翻倍，留作可选项，见 §11。

---

## 2. POC 结论

2026-09-29 在本机实测：skill-up v0.12.0（从 tag 编译），Claude Code 2.1.284，
火山方舟 Anthropic 兼容端点，模型 `deepseek-v4-flash`。用的是一个模拟 skill
（`ticket-formatter`），带 4 个用例，覆盖 `rule_based`、写文件检查、`agent_judge`，
另有一个故意判错的用例。

| 验证项 | 结果 |
| --- | --- |
| 端到端 | 3 个正常用例全部 PASS，三种判法都能用 |
| agent 真的用了 skill | transcript 里有 `Skill(ticket-formatter)` 调用 |
| 稳定性 | `--iteration 3`：正常用例 3/3 PASS，故意判错的用例 3/3 FAIL |
| 用例能否区分有无 skill | `--baseline`：有 skill 100%，无 skill 22% |
| 单个用例的开销 | 16～19s；约 1.6 万输入 token + 几百输出 token，大头是 Claude Code 自己的系统提示 |
| `agent_judge` 额外开销 | 约 7s + 1.6 万 token |
| 吞吐 | 12 次运行、并发 3，共 1 分 42 秒 |
| 凭据泄漏 | 所有输出文件和 HOME 目录扫过，没有 key |

实测中发现的坑，全部写进了下面的设计：

1. **环境变量会静默覆盖 `eval.yaml`**（§5.3）。
2. **`agent_judge` 的用例必须带 `judge.model`**，否则校验不过（§4.2）。
3. **退出码和 `run_finished.status` 都不是结论**：用例全部出错时，事件里仍是
   `COMPLETED`（§6.2）。
4. **网关连不上时不会马上失败**：每个用例都会等到超时（§6.4）。
5. **skill-up 不报告实际用的模型**（`observed_model` 为空），但 transcript 里有：
   请求的是 `deepseek-v4-flash`，实际应答的是 `deepseek-v4-flash-ga-260731`（§7.2）。
6. **Claude Code 不认识非 Claude 的模型名**，会退回按 200k 上下文窗口做自动压缩（§5.2）。

另外验证了一件**不需要改**的事：SkillEvaluator v0.3.0 在 Tier 1 遍历文件时，会跳过
任意层级的 `evals/`（`constants.SCAN_EXCLUDED_DIRS`）。把含假 token、注入漏洞代码
和一张 PNG 的 fixture 放进 `evals/`，Tier 1 的结论、分数、问题清单都和没有
`evals/` 时**完全一致**。同样的文件放到 `evals/` 外面，就会多出 1 条 critical 和
十几条其他问题，放 PNG 的那份还会变成 INCOMPLETE。所以作者补用例不影响 Tier 1，
前提是所有测试材料都放在 `evals/` 下（见附录 A）。这是上游的行为，不是我们保证的，
由一条哨兵测试钉住（§12）。

---

## 3. 整体流程

```
管理系统 POST /api/evaluations/{zip|gitlab}  tier=tier3
   → 入队，queue = sandbox（已预留）
sandbox worker（独立进程）领任务
   → 取内容 → 算 content_hash（与 Tier 1 同一套）
   → 查复用：content_hash + 运行时指纹（§7）
   → 物化到 skills/<skill_name>/
   → 找用例：evals/cases/*.yaml；没有则任务以"无用例"结束（§8.2）
   → 改写用例、生成平台自己的 eval.yaml（§4、§5）
   → 网关探活（§6.4）
   → skill-up validate → skill-up run --event-log … --format html
   → 解析事件流和 result.json → 定状态（§6）
   → 报告入存储，结论写入 runtime_result 表（§8）
   → 清理工作目录（含临时 HOME）
```

Tier 1 和 Tier 3 是**两个任务**。触发方按需分别提交，两者互不折叠：
`find_queued` 的去重键里本来就有 tier。

---

## 4. 用例从哪来

### 4.1 作者提供什么

只认一个位置：**skill 目录下的 `evals/cases/*.yaml`**，文件按名字排序，
case ID 就是文件名。fixture、判分脚本、mock MCP 的配置都放在 `evals/` 下，
用例里按**相对 skill 根**的路径引用（例如 `evals/fixtures/repos/sample`）。
给作者的约定见附录 A。

**作者的 `evals/eval.yaml` 整个忽略。** 它里面能写的东西——运行环境、engine、
模型、真实 MCP、并发、`cases.defaults`——都应该由平台决定：

- engine、模型、judge 模型不统一，不同 skill 的结论就没法比，也没法复用（§7）；
- 运行环境和真实 MCP 由作者决定，等于由作者决定在我们机器上执行什么；
- 用例列表用约定位置的 glob 取，比读作者的清单更好预测。一个用例写了但没列进
  清单，是作者最容易犯、也最难发现的错。

代价：作者在 `cases.defaults.expect` 里写的公共断言不会生效。附录 A 里写明
"每个用例自己写全"。

### 4.2 平台接管的字段

用例拷进工作目录后，逐个改写以下字段再交给 skill-up（改的是我们物化出来的那份，
不是作者的源文件）：

| 字段 | 处理 | 理由 |
| --- | --- | --- |
| `judge.model`（`agent_judge`） | 强制改成平台配置的 judge 模型 | 不写会校验失败（POC 实测）；作者写了也不应该生效 |
| `constraints.timeout_seconds` | 不超过平台上限 | 一个用例写 3600s 就能拖住整个 worker |
| `constraints.max_turns` | 不超过平台上限 | 同上，还关系到 token |
| `mcp.servers`（用例级） | 只允许 `mode: mocked` | skill-up 本身也只允许用例级 mocked；eval 级的 MCP 由平台生成，本期为空 |

用例总数设上限（默认 20），超出时任务报错、**不截断**：只跑前 N 个会给出一个
覆盖不全、看起来却完整的结论。

---

## 5. 执行

### 5.1 目录布局

```
<work_root>/<task_id>/
  skill/skills/<skill_name>/      ← 物化结果，与 Tier 1 相同
    SKILL.md
    evals/
      cases/*.yaml                ← 作者的用例，已按 §4.2 改写
      fixtures/…                  ← 作者的 fixture，原样
      .skillprism-eval.yaml       ← 平台生成
  home/                           ← 本次任务专用的 HOME，跑完删除
  out/                            ← skill-up 的 --output-dir
  events.jsonl                    ← --event-log
```

平台的 `eval.yaml` **放在物化后的 skill 的 `evals/` 下**，这是 POC 里试出来的：

- skill-up 从配置文件所在位置向上找 `SKILL.md` 来确定根目录。放在这里，根就是
  skill 目录，作者按"相对 skill 根"写的 fixture 路径原样可用，不用改写。
- 用例路径必须写相对路径。写绝对路径会被拼到根目录后面（POC 实测）。
- 报告里的 `skill_name` 取的是根目录名，放在这里正好是登记名。放在别处时，
  它是工作目录的名字。
- skill-up 装 skill 时会排除 `evals/`，这个文件不会被装进 agent 能看到的
  skill 目录。

### 5.2 生成的 eval.yaml

```yaml
schema_version: v1alpha1
environment:
  type: none
skills:
  - source: local_path
    path: .
engine:
  name: claude_code
  version: 2.1.284             # 平台钉死，预检时核对本机一致
  model:
    provider: gateway          # 固定的自定义名，见 §5.3
    name: <SKILLPRISM_RUNTIME_MODEL>
cases:
  files: [evals/cases/a.yaml, …]   # 按 §4.1 取到的列表
  defaults:
    timeout_seconds: <上限>
    max_turns: <上限>
  parallelism: <SKILLPRISM_RUNTIME_PARALLELISM>
```

`skills` 显式写出，不靠 skill-up 的"找到 `SKILL.md` 就自动装"。
`base_url` 和 key 不写进文件，由环境变量给（§5.3）。

非 Claude 模型会触发 Claude Code 的"未知模型"警告，它会退回按 200k 上下文窗口
自动压缩对话。短用例没有影响。模型的真实窗口不是 200k 时，用
`SKILLPRISM_RUNTIME_CONTEXT_TOKENS` 传 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`。

### 5.3 子进程环境：只给白名单

沿用 Tier 1 的做法：子进程不继承 worker 的环境，只拿到显式列出的变量。
在这里这不只是整洁问题，**是正确性问题**：

skill-up 按 provider 名读 `<PROVIDER>_BASE_URL`、`<PROVIDER>_MODEL`、
`<PROVIDER>_API_KEY`，**优先级高于 `eval.yaml`**
（`internal/credential/agent_init.go` 的 `lookupProviderEnv`）。provider 如果写成
`anthropic`，环境里只要有一个 `ANTHROPIC_BASE_URL`，所有调用就改道了，而且不报错。
POC 所在的会话里就有这么一个变量，指向 api.anthropic.com。

所以：

- provider 固定叫 `gateway`，不用 `anthropic`，避开 Claude Code 自己和运维环境里的
  同名变量。POC 里故意把 `ANTHROPIC_BASE_URL` 设成一个不存在的地址，用例照常
  通过，说明调用确实走的是 provider 配置。
- 子进程的环境**完整列出**如下：

| 变量 | 值 |
| --- | --- |
| `PATH` | `SKILLPRISM_RUNTIME_PATH`，必须能找到 `claude`、`bash`、`git` |
| `HOME` | `<work_root>/<task_id>/home`，本次任务专用 |
| `GATEWAY_BASE_URL` | `SKILLPRISM_RUNTIME_BASE_URL` |
| `GATEWAY_API_KEY` | `SKILLPRISM_RUNTIME_API_KEY` |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | 仅在配置了 `SKILLPRISM_RUNTIME_CONTEXT_TOKENS` 时给 |
| 其余 | `SKILLPRISM_RUNTIME_ENV`（`K=V` 逗号分隔），给部署者补漏，同 `SCANNER_ENV` |

**不给** `<PROVIDER>_MODEL`：模型名只从 `eval.yaml` 来，一处定义。

**HOME 每个任务一个，跑完删掉。** 理由有两条：

- 共用的 HOME 下，`~/.claude` 里的用户级 skills、CLAUDE.md、settings 都会被
  agent 加载，干扰评测。开发机上尤其如此。
- Claude Code 会在 HOME 下积累会话记录（POC 里 12 次运行攒了 4.6MB）。

**子进程的工作目录设成 `<work_root>/<task_id>/`，不设成 skill 目录。** skill-up 会
读 `$PWD/.skill-up.yaml`，它能注入环境变量和运行时参数。skill 里要是带了这个文件，
工作目录设在 skill 目录下就会被读进去。临时 HOME 也同时挡住了
`~/.config/skill-up/config.yaml`。

### 5.4 调用

```
skill-up validate <skill>/evals/.skillprism-eval.yaml
skill-up run      <skill>/evals/.skillprism-eval.yaml \
    --output-dir <out> --event-log <events.jsonl> --format html \
    --iteration <N>
```

先单独跑一次 `validate`：它失败说明作者的用例写错了，这是**不该重试**的错误，
要和 `run` 期间的故障分开（§6.3）。

整体超时 = 用例数 × 迭代次数 × 单用例上限 ÷ 并发 + 余量，超时直接杀进程。
`--iteration` 默认 1，见 §13 待定项。

---

## 6. 解释结果

### 6.1 读哪些文件

| 来源 | 取什么 | 稳定性 |
| --- | --- | --- |
| `--event-log` 的 `run_finished` / `case_completed` | 计数（`passed` `failed` `errored` `skipped`）、每个用例每次运行的状态 | **有版本号**：`schemas/evalevent/v1` 带 JSON Schema，`protocol_version` / `event_version` 均为 1 |
| `iteration-N/result.json` | 每个用例的 grading 明细、失败原因、token、耗时、Claude Code 版本 | **没有稳定性承诺**：它是 `internal/report.Input` 的序列化，项目在 0.x、约两周一个 minor |
| transcript（`outputs/agent/run/*.jsonl`） | 实际应答的模型名 | Claude Code 自己的格式，**只作尽力而为的展示**，取不到就留空 |
| `report.html` | 原样入存储 | — |

判定状态只依赖事件流，明细才读 `result.json`。skill-up 只装 tag 版本（v0.12.0），
和 SkillEvaluator 同一个理由。新增的 adapter 是唯一了解 skill-up 输出格式的模块，
由一份真实输出做 fixture 的契约测试钉住（§12）。

### 6.2 状态映射

退出码只有 0/1，失败和出错不区分。`run_finished.status` 在用例全部出错时仍是
`COMPLETED`（POC 实测）。两者都不能当结论。按计数判：

| 条件（对全部用例的全部运行） | 状态 | 说明 |
| --- | --- | --- |
| `failed > 0` | `failed` | 有明确的判定不通过。出错数照样展示 |
| `failed == 0`，`errored + skipped > 0`，`passed > 0` | `incomplete` | 有用例没被判定。**不是通过**，与 Tier 1 同一条立场 |
| `passed == 0`，全部出错或跳过 | `error` | skill 从未被判定，不写结论，按 §6.3 处理 |
| 全部 `PASS` | `passed` | |

迭代次数大于 1 时，一个用例只要有一次 FAIL 就算没通过。每个用例的通过率照样
展示，不稳定的用例一眼能看出来。

### 6.3 失败分类

| 情况 | 结局 | 理由 |
| --- | --- | --- |
| 没有 `evals/cases/*.yaml` | 任务结束，不重试，`error` = `无运行时用例：…` | 作者还没补，重试多少次都一样 |
| 用例数超上限 / `skill-up validate` 失败 | 任务结束，不重试，错误信息原样带出 | 作者的问题，信息要能直接转给作者 |
| 网关探活失败 | 重新排队（`_requeue`，计入次数、退避） | 运维问题，重试窗口留给修复 |
| 进程超时 / 崩溃 / 没有 `run_finished` 事件 | 重新排队 | 评测本身的故障 |
| 用例全部 ERROR（§6.2 的 `error`） | 重新排队，到 `max_attempts` 后结束 | 最常见的原因是网关限流或抖动 |
| 其余 | 写结论 | |

"无用例"要和其他错误分得开：管理系统据此提示作者"补用例"，而不是"评测失败"。
本期用 `error` 文案的固定前缀区分，是否升级成独立状态见 §13。

### 6.4 网关探活

网关连不上时，Claude Code 会一直重试，每个用例都要耗满超时才判 ERROR。POC 里
一个用例在 60s 超时上耗了整整 60s。10 个用例、并发 2、超时 300s 时，就是 25 分钟
白等，之后还要重试 `max_attempts` 轮。

所以每个任务开跑前，先向 `GATEWAY_BASE_URL/v1/messages` 发一个 `max_tokens=1` 的
请求，10s 超时。失败就直接重新排队，不启动 skill-up。每个任务多花几十个 token。

---

## 7. 结论身份与复用

### 7.1 运行时指纹

Tier 1 的复用前提是"同样的字节 + 同样的评测器 + 同样的策略 → 同样的结论"。
Tier 3 的结论还取决于执行配置，所以另算一个**运行时指纹**：

```
runtime_fingerprint = sha256(规范化 JSON {
  skillup_version,            # skill-up --version
  engine, engine_version,     # claude_code, 2.1.284
  model, judge_model,         # 请求的模型名
  iterations,
  case_timeout_cap, max_turns_cap,
  template_version,           # 常量，改写规则或 eval.yaml 模板变了就升
  context_tokens,             # 配了才有
})
```

**不进指纹：**

- `base_url`：换网关地址不该让所有结论失效。换了后端就该换模型名。
- `parallelism`：影响快慢，不影响判定。
- key。

`content_hash` 与 Tier 1 共用同一个算法：用例属于内容，改用例就会让 Tier 3 重跑，
这正是想要的。Tier 1 也会跟着重跑一次，但结论不变（§2），只是多跑一遍静态检查。

### 7.2 复用判据

`content_hash` 相同、`runtime_fingerprint` 相同、`errored + skipped == 0` 时复用。
和 Tier 1 一样不看 `skill_id` 与 `source`，命中就克隆一份挂到本次的身份下。

第三条和 Tier 1 的"`incomplete_scans` 为空"是同一个意思：没判定完的结论不固化。

**没卡住的是网关背后的模型版本。** 同一个模型名，网关可能换了后端
（POC 里 `deepseek-v4-flash` 实际是 `deepseek-v4-flash-ga-260731`）。
跑之前拿不到这个信息，所以它进不了指纹。这和 Tier 1 的 `policy.digest`、
外部扫描器版本是同一类问题：结论里记下从 transcript 取到的实际模型名
（`served_models`），界面能展示；漂移了用 `force=true` 重跑。

---

## 8. 持久化与对外接口

### 8.1 为什么单独建表

不能把 Tier 3 结论写进 `evaluation_result`：

- **会删掉 Tier 1 的结论。** 那张表的身份是 `(source, skill_id, content_hash, context_hash)`，
  没有 tier（`tier` 只在 `evaluation_detail` 上）。`save_result` 是先删后插：同一份
  内容的 Tier 3 结论一落库，Tier 1 那条就被删了，全程不报错。
- **字段对不上。** 结果行上的 `score`、`grade`、`gate_passed`、`severity_counts`、
  `policy_*`、`incomplete_scans` 全是 Tier 1 的概念；Tier 3 要的是通过率、用例明细、
  模型和 token。硬塞会让每个字段都有两种含义。

新表 `runtime_result`：

| 列 | 说明 |
| --- | --- |
| `id`, `source`, `skill_id`, `skill_version`, `content_hash` | 同 `evaluation_result` |
| `runtime_fingerprint` | §7.1 |
| `status` | §6.2 |
| `passed` / `failed` / `errored` / `skipped` | 全部运行的计数 |
| `case_count`, `iterations` | |
| `skillup_version`, `engine`, `engine_version`, `model`, `judge_model` | 指纹的原料，单独存以便展示和排查 |
| `served_models` | JSON，transcript 里取到的实际模型，尽力而为 |
| `input_tokens` / `output_tokens` / `judge_tokens`, `duration_ms` | 成本 |
| `cases` | JSON：每个用例的 ID、标题、每次运行的状态、通过率、失败原因（已归一化，不含工作目录路径） |
| `report_json_uri` / `report_html_uri` / `events_uri` | 存储地址 |
| `evaluated_at`, `created_at` | |

唯一性：`UNIQUE (source, skill_id, content_hash, runtime_fingerprint)`。本期没有
bundle，所以不需要 `context_hash`，也就没有 NULL 的问题，一条普通唯一索引就够了。
将来支持 bundle 时再照 `evaluation_result` 拆成两条部分唯一索引。

**不存 transcript。** 它包含 Claude Code 的完整系统提示和工具定义，体积大，
每个用例的 prompt 和 response 已经在 `result.json` 里了。

报告存储路径需要多一层命名空间：`LocalReportStorage.put` 现在按
`(content_hash, context_hash)` 分目录，Tier 3 的文件会和 Tier 1 的
`report.html` 重名。加一个 `runtime/<fingerprint 前 16 位>/` 前缀。

### 8.2 对外接口的变化

**触发**（`POST /api/evaluations/{zip|gitlab}`）：

- `tier: "tier3"` 开始被接受（`IMPLEMENTED_TIERS` 加入 `TIER3`），任务进
  `sandbox` 队列。
- `tier3` 和 `bundle: true` 同时出现时返回 `422`，写明本期不支持。
- 其余语义（202 是受理、重复触发会折叠、`force`）不变。

**任务**（`GET /api/tasks/{task_id}`）：结构不变。`tier3` 任务的 `results[]` 从
`runtime_result` 反查，`report_url` 指向运行时报告。`task_results` 按 `task.tier` 分支。

**查询**，新增两个端点：

```
GET /api/skills/{skill_id}/runtime-evaluation?source=&content_hash=
GET /api/skills/{skill_id}/runtime-report?source=&content_hash=
```

规则照搬 `/evaluation`：多来源时必须带 `source`；带 `content_hash` 时精确取，
不带时退回最近一条（GitLab 接入下同样多半不是你要的）。同一份内容在多个指纹下
都评过时，取最近一条。返回的 `RuntimeEvaluationDTO` 包含状态、计数、通过率、
每个用例的明细、执行配置（engine、模型、实际应答的模型）、token、耗时、`report_url`。

**`EvaluationDTO.tiers.tier3`**（已预留）：`/evaluation` 返回 Tier 1 结论时，
顺带查同一 `(source, skill_id, content_hash)` 下最近的一条运行时结论，摘要填进
`tiers.tier3`：`status`，每个用例映射成一个 `ValidatorOutcome`
（`validator` = 用例 ID，`description` = 标题，`passed`，`errors` = 失败原因）。
这样详情页不用改接口就能展示一个分区。明细和成本信息只在新端点里给。
这条是否要做见 §13。

**报告**：skill-up 的 `report.html` 是自己生成的 HTML，里面有用户 prompt 和
agent 的完整回复，和 Tier 1 的报告一样只在独立域名下、带同一组安全头提供。
上线前要在浏览器里实测它在 `REPORT_SECURITY_HEADERS` 的 CSP 下能正常渲染，
Tier 1 的报告当时就是这么验证的。

---

## 9. 部署与配置

sandbox worker 是**单独的进程**：`skillprism-worker --queue sandbox`。它的预检和
Tier 1 不同，不查扫描器，改为检查：

- `skill-up --version` 能跑，且版本等于钉住的那个；
- `claude --version` 等于 `SKILLPRISM_RUNTIME_ENGINE_VERSION`；
- 模型相关配置齐全。

预检不过就拒绝启动，同 Tier 1。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `SKILLPRISM_SKILLUP_BIN` | `skill-up` | |
| `SKILLPRISM_RUNTIME_PATH` | — | 子进程的 PATH，必填 |
| `SKILLPRISM_RUNTIME_ENGINE_VERSION` | — | 必填，例 `2.1.284` |
| `SKILLPRISM_RUNTIME_BASE_URL` | — | 网关的 Anthropic 兼容地址，必填，启动时校验是否为 http(s) |
| `SKILLPRISM_RUNTIME_API_KEY` | — | 必填。**用专门的 key，给它设额度**，见 §10 |
| `SKILLPRISM_RUNTIME_MODEL` | — | 必填 |
| `SKILLPRISM_RUNTIME_JUDGE_MODEL` | 同上 | |
| `SKILLPRISM_RUNTIME_CONTEXT_TOKENS` | 空 | §5.2 |
| `SKILLPRISM_RUNTIME_ITERATIONS` | `1` | |
| `SKILLPRISM_RUNTIME_PARALLELISM` | `2` | skill-up 的用例并发 |
| `SKILLPRISM_RUNTIME_CASE_TIMEOUT` | `300` | 单用例上限（秒） |
| `SKILLPRISM_RUNTIME_MAX_TURNS` | `12` | |
| `SKILLPRISM_RUNTIME_MAX_CASES` | `20` | |
| `SKILLPRISM_RUNTIME_ENV` | 空 | §5.3 |

安装（服务器出网受限，两样东西都要离线带进去）：

- skill-up：在能联网的机器上从 tag 编译（`go build -ldflags "-X main.version=0.12.0"`），
  拷二进制过去。不用 `install.sh`，它从 GitHub 下载。
- Claude Code：按钉住的版本装到服务账号下。它开跑时会尝试访问 Anthropic 的非必要
  服务，skill-up 已经设了 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`。服务器上
  仍要确认：在出网受限的环境里它不会卡在某个联网调用上。这和当初 semgrep 版本检查
  挂 30 秒是同一类坑。

成本估算（按 POC 数据）：10 个用例、迭代 1 次，约 16 万～32 万 token（看有多少
`agent_judge`），并发 2 时 2～3 分钟。

---

## 10. 接受的风险：本期不隔离

需要说清楚接受的是什么。`environment: none` 下：

- agent 以 **worker 的服务账号**在宿主机上跑，Claude Code 固定带
  `--permission-mode=bypassPermissions`，执行任何命令都不需要确认；
- 它能读服务账号能读的一切：`/opt/skillprism/.env`（数据库密码、GitLab 令牌、
  这个模型 key）、`/var/lib/skillprism` 下其他 skill 的内容与报告、同机其他任务的
  工作目录；
- 作者的 `script` 判分脚本同样在宿主机上执行；
- 驱动 agent 的是 SKILL.md 和用例里的文字，也就是上传者写的内容。

接受它的前提是**所有 skill 都是公司内部的，上传者都是内部员工**。这个前提变了，
比如开放外部上传、或者把第三方 skill 导进来评，就必须先做隔离，再开放 Tier 3。

本期能做、而且成本很低的三件事：

1. 模型 key 用专门的一个，设额度上限。泄漏了损失有上限，也能单独吊销。
2. sandbox worker 和 API 分开部署。最好放另一台机器，不和 PG 同机，这样上面
   那几类文件在那台机器上本来就不存在。
3. 每个任务的 HOME 和工作目录跑完就删（§5.3）。

---

## 11. 后续

- **隔离**：docker 运行时的 `network_policy: deny_all` 会连模型也访问不到，
  `allow_declared` 还没实现。要"只能访问模型网关"，得自己配 docker 网络加出口规则，
  或者用 opensandbox。
- **bundle**：skill-up 支持装多个 skill，但用例怎么组织、结论挂给谁要另外设计。
- **有无 skill 对比**：`--baseline` 能回答"这个 skill 到底有没有用"（POC：100% 对 22%），
  代价是 token 翻倍。可以作为触发参数按需开。
- **自动触发**：Tier 1 发现内容里有 `evals/cases/` 时自动排一个 Tier 3 任务。
  等成本有了实际数据再决定。
- **模型版本漂移**：网关能暴露模型版本时，把它放进指纹。

---

## 12. 测试

| 测试 | 内容 |
| --- | --- |
| 单测：布局与改写 | 用例收集、`judge.model` 强制、上限裁剪、用例级真实 MCP 拒收、生成的 eval.yaml |
| 单测：子进程环境 | 环境变量**完整等于**白名单，worker 环境里的 `ANTHROPIC_*`、`GATEWAY_MODEL` 不会漏进去；HOME 和工作目录位置正确 |
| 单测：runner | 用一个假的 `skill-up` 脚本回放 POC 抓到的输出，覆盖 PASS / FAIL / ERROR / 混合 / 无 `run_finished` / 超时 |
| 契约测试 | 用 POC 的真实 event-log 和 `result.json` 做 fixture，锁住 adapter 读的字段，同 `test_upstream_contract.py` |
| 复用 | 指纹每个组成部分变化都不复用；有 ERROR 的结论不复用；跨 `skill_id` / `source` 克隆 |
| 表结构 | 同一份内容的 Tier 1 与 Tier 3 结论共存，互不删除（§8.1 那个坑的回归测试） |
| 迁移 | `test_migrations.py` 自动覆盖新表 |
| 哨兵：Tier 1 忽略 `evals/` | e2e：`evals/` 里放假 token、注入代码和一张 PNG，断言 Tier 1 结论和没有 `evals/` 时一致（§2） |
| e2e：实跑 | 标记 `e2e`，用 POC 的模拟 skill。需要 `skill-up`、`claude` 和网关凭据，缺一个就跳过 |

---

## 13. 待定项

| 问题 | 建议 | 备注 |
| --- | --- | --- |
| 无用例用什么表达 | 本期：任务 `error` + 固定前缀，不写结论 | 另一个方案是新增状态值，但那会改对外契约的枚举 |
| 默认迭代次数 | 1 | POC 里模拟 skill 三次结果一致，真实 skill 未知。先用 1，攒一批数据看不稳定率再调 |
| `tiers.tier3` 摘要做不做 | 做 | 这个位置本来就是为此预留的；不做的话，详情页要多调一个端点 |
| 谁来触发 Tier 3 | 管理系统显式触发 | 不在上传时自动触发，先把成本控制在人手里 |

---

## 附录 A：给作者的用例约定（草案）

正式模板另出。要点：

1. 用例放在 `evals/cases/<case-id>.yaml`，一个文件一个用例，文件名就是 ID。
2. **所有测试材料都放在 `evals/` 下**，包括样例仓库、图片、判分脚本、mock 配置。
   放在别处会被 Tier 1 当作 skill 的一部分扫描：样例代码里故意写的漏洞、假 token
   都会算到 skill 头上，任何二进制文件都会让安全扫描判为不完整（§2 实测）。
3. 引用 fixture 用**相对 skill 根**的路径，例如 `evals/fixtures/repos/sample`。
4. `evals/eval.yaml` 不生效：engine、模型、运行环境、并发由平台决定。
   `cases.defaults` 里的公共断言也不生效，每个用例把断言写全。
5. `agent_judge` 的 `model` 写了也会被替换成平台的 judge 模型。
6. 用例级 MCP 只能用 `mode: mocked`。
7. 能用 `expect` / `rule_based` 判的就不要用 `agent_judge`：前者确定、免费，后者
   每个用例多花约 1.6 万 token，结论还有随机性。
8. 至少写一个"没有这个 skill 就过不了"的用例，否则通过率说明不了 skill 本身
   有没有用。
