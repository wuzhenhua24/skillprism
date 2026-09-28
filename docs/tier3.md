# Tier 3 接入：选型与验证记录

Tier 3 在 SkillPrism 里**还没有实现**（见 README「尚未实现」）。这篇记下接入前
定下的选型、在 SkillEvaluator v0.3.0 上实测过的事实，以及 SkillPrism 这边要改的
地方。动手实现时以这里为起点，实测结论变了就改这里。

## 选型

| 项 | 选择 | 理由 |
| --- | --- | --- |
| agent | `claude-code` | 先只做一个 agent |
| 模型 | 火山方舟的 Anthropic 兼容接口 | 公司已有方舟；打分和 agent 共用一套凭据 |
| 运行环境 | `--env-mode docker` | local 模式有两个问题，见下文 |
| skill 范围 | 企业内部 skill | 不按不可信代码设防 |

## 配置

评测器选 `anthropic`，打分器与 claude-code **共用同一套凭据**——上游在
「评测器是 anthropic、agent 是 claude-code」时直接把评测器的 key 和 base URL
交给 agent（`tier3/harbor/runner.py` 的 `_agent_credentials`）：

```bash
SKILL_EVAL_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=<方舟 API key>
ANTHROPIC_BASE_URL=https://ark.cn-beijing.volces.com/api/coding
SKILL_EVAL_LLM_MODEL=<方舟上的模型名>
```

- **只认 `ANTHROPIC_API_KEY`**，以 `x-api-key` 头发出；不读 `ANTHROPIC_AUTH_TOKEN`。
  方舟两条 Anthropic 兼容路径（`/api/coding`、`/api/compatible`）对 `x-api-key`
  都会走到它的鉴权层（无效 key 返回 401 "The API key format is incorrect"），
  用哪条跟着 key 的开通方式走。
- **base URL 写根，不带 `/v1/messages`**。上游自己拼 `/v1/messages`，写全了会在
  启动前被拒。
- 配了 base URL 之后，Claude Code 的 sonnet / opus / haiku 与子 agent 都会被钉到
  同一个模型（Harbor 设 `ANTHROPIC_DEFAULT_*_MODEL`），不会有请求漏到别的模型名上。

跑之前先自检，再小规模跑一次：

```bash
skillevaluator doctor --agents claude-code --env-mode docker
skillevaluator tier3 evaluate <skill 目录> --agents claude-code --env-mode docker \
  --n-attempts 1 --results-dir <skill 目录之外的输出目录>
```

## 为什么不用 local 模式

**权限模式被改成了 `auto`。** local 模式下上游把 Harbor 给 Claude Code 的
`--permission-mode=bypassPermissions` 改写成 `auto`（`tier3/harbor/local_agents.py`
的 `SkillEvaluatorLocalClaudeCode`）。auto 模式在每次执行 Bash 前调用一个安全
分类器，**用的是同一个模型**。实测分类器拿不到能解析的判定时，这次 Bash 被拦下，
而这一轮 agent 仍然记为 success，只在 `permission_denials` 里留一条。结果是
skill 里的脚本根本没跑，分数却照常产出——量到的是分类器，不是 skill。接非 Claude
模型时这是现实风险。

docker 模式用 Harbor 原生的 claude-code agent，在容器里以 `bypassPermissions`
运行。实测 0 条 permission denial，也没有任何分类器请求。

**打分脚本依赖宿主 Python。** local 模式的打分脚本（`templates/eval.py`）用宿主
PATH 上的 `python3` 执行，要求 Python ≥ 3.12（用了 3.12 才允许的 f-string 写法）
且装有第三方包 `idna`。宿主是 3.11 时直接语法错误，表现为 `RewardFileNotFoundError`、
整次运行判为未完成。docker 模式在容器里打分，不依赖宿主。

## 实测记录

SkillEvaluator v0.3.0，上游自带的参考 skill `calculator`（2 条用例），docker 模式，
模型端点是一个本地 mock（按 Anthropic Messages API 应答、记录每个请求）。**分数
没有意义**，验证的是流程和接口。

- **全流程跑通**：凭据检查 → 运行前冒烟 → 有 / 无 skill 两臂各 2 条 → 三个 judge
  → 报告，退出码 0，共 318 秒。冒烟一步就占 3 分 38 秒，主要是构建镜像和在 trial
  里现装 Claude Code；mock 是瞬时应答，真实模型只会更慢。
- **两臂隔离正确**：有 skill 一臂在 `/workspace/skills`、`~/.claude/skills`、
  `$CLAUDE_CONFIG_DIR/skills` 都能看到 skill，并实际跑出了 `calc.py` 的结果；
  无 skill 一臂三处都是空的。
- **网关要支持的接口**：
  - `GET /v1/models`：凭据检查用。拿不到可信结果时记为 degraded、继续跑，不阻断。
  - `POST /v1/messages?beta=true`：agent 走流式（SSE），judge 走非流式。
  - `x-api-key` 鉴权。
- **调用量**：每条用例每一臂 = 1 次 agent 会话 + 3 次 judge（accuracy / goal /
  behavior）。另有每个 agent 1 次冒烟会话。
- **已知告警**：`LLM suggestion generation failed: Messages.create() got an
  unexpected keyword argument 'temperature'`。上游 anthropic 路径上生成改进建议
  时的问题，只影响报告里的建议文字，不影响分数与判定。

## 服务器上的前提

- Docker 与 Compose v2，worker 的运行用户能访问 `docker.sock`。能访问 docker.sock
  基本等于 root 权限；另外 `deployment.md` 里 worker 的 systemd 加固项
  （`ProtectSystem=strict`、`PrivateDevices` 等）与调用 docker 是否兼容要实测。
- **容器里要能访问这些地址**，国内网络下前几项可能不通，上线前逐个确认：

  | 地址 | 用途 | 时机 |
  | --- | --- | --- |
  | Docker Hub `python:3.12-slim` | 评测镜像的基础镜像 | 首次构建 |
  | `deb.debian.org` / `pypi.org` | 镜像里装 apt 包与打分脚本依赖 | 首次构建 |
  | `downloads.claude.ai` | Harbor 在**每个 trial** 里现装 Claude Code，没有镜像源开关 | 每次运行 |
  | 方舟端点 | agent 与 judge | 每次运行 |

  评测镜像 `FROM python:3.12-slim`，本地已有同名镜像时不再拉取——需要换源或加
  CA 时，可以预先构建一个配好的同名镜像（下面的云端脚本就是这么做的）。

## SkillPrism 要改的

1. **入口与队列**：`service.py` 的 `IMPLEMENTED_TIERS` 只有 Tier 1，tier3 返回 501；
   worker 只领 `fast` 队列，`sandbox` 队列需要单独的 worker 进程。
2. **凭据注入**：`runner._subprocess_env` 刻意只给子进程 PATH/HOME 和扫描器变量。
   Tier 3 必须把上面那四个变量交给子进程，要一条专门的、显式的注入通道，不能复用
   `SCANNER_ENV`（它的约定就是公司凭据不进那一层）。
3. **结果判定看 `result.json`，不看退出码**：运行本身失败（冒烟没过、judge 调不通）
   时 `tier3 evaluate` 也退出 1，和"skill 不合格"同码。要读顶层的
   `execution_status`（`succeeded` / `failed`）与 `execution_errors` 区分，否则端点
   抖一下就会被记成 skill 的终态结论。
4. **DTO**：Tier 3 产出的是 `agents.<agent>` 下的 `dimensions_with_skill` /
   `dimensions_without_skill`、`lift`、`pass_at_k`，不是 validator 列表，`TierResult`
   装不下，需要扩展对外契约。
5. **超时**：`EVAL_TIMEOUT_SECONDS=600` 是按 Tier 1 定的，Tier 3 要单独配置。
6. **复用键**：除内容与评测器版本外，还取决于 agent、模型、数据集
   （`result.json` 有 `dataset_digest`）与尝试次数，且结果本身不确定。
7. **输出目录**：用 `--results-dir` 指到任务工作目录之外，默认会写进 skill 自己的
   `evals/results`。
8. **数据集**：每个 skill 需要 `evals/evals.json`，来源待定——作者自带、
   `create-eval-dataset --full` 生成后人工审，或 `--autopilot`（只生成 1 条）。

## 附：在 Claude Code 云端环境里复现

云端容器自带 `dockerd` 但不启动，出网经过做 TLS 拦截的代理，容器里默认不信任它
的 CA。会话开始后执行一次（可重复执行）：

```bash
#!/bin/bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

command -v skillevaluator >/dev/null || uv tool install --python 3.13 \
  "skillevaluator[security,tier3] @ git+https://github.com/NVIDIA/SkillEvaluator.git@v0.3.0"

if ! docker info >/dev/null 2>&1; then
  nohup dockerd >/tmp/dockerd.log 2>&1 &
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
  docker info >/dev/null
fi

# 评测镜像 FROM python:3.12-slim：换成信任代理 CA 的同名本地镜像
if ! docker image inspect python:3.12-slim-upstream >/dev/null 2>&1; then
  docker pull -q python:3.12-slim
  docker tag python:3.12-slim python:3.12-slim-upstream
fi
ctx=$(mktemp -d)
cp /root/.ccr/ca-bundle.crt "$ctx/proxy-ca.crt"
cat > "$ctx/Dockerfile" <<'EOF'
FROM python:3.12-slim-upstream
COPY proxy-ca.crt /usr/local/share/ca-certificates/proxy-ca.crt
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/proxy-ca.crt
EOF
docker build -q -t python:3.12-slim "$ctx" >/dev/null
rm -rf "$ctx"
```

环境变量在环境设置里添加后，**只对新会话生效**。方舟的 key 与地址**不要**直接存成
`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`——云端会话自己的 Claude Code 也读这两个
名字，可能被一起改道到方舟。换个名字存（例如 `ARK_API_KEY`、`ARK_ANTHROPIC_BASE_URL`），
只在调用 skillevaluator 时映射过去：

```bash
env SKILL_EVAL_LLM_PROVIDER=anthropic SKILL_EVAL_LLM_MODEL=<模型名> \
  ANTHROPIC_API_KEY="$ARK_API_KEY" ANTHROPIC_BASE_URL="$ARK_ANTHROPIC_BASE_URL" \
  skillevaluator tier3 evaluate <skill 目录> --agents claude-code --env-mode docker ...
```
