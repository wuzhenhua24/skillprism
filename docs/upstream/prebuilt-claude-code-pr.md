# 上游 PR 草稿：docker 模式使用镜像里预装的 Claude Code

**这是什么：** 准备提给 [NVIDIA/SkillEvaluator](https://github.com/NVIDIA/SkillEvaluator)
的 PR 说明草稿，下半部分是英文原文，按上游 PR 模板组织。改动本身是
[`prebuilt-claude-code-pr.patch`](prebuilt-claude-code-pr.patch)，基于上游 main
`a2636b2`，6 个文件 +221 / −5 行。背景见 [../tier3.md](../tier3.md)「预装 Claude Code」。

**状态（2026-09-28）：** 未提交。提交前要做的：

1. fork 上游，在 main 上 `git apply` 这份 patch，用 `git commit -s` 提交。上游 CI 有
   DCO 检查，`Signed-off-by` 必须是提交人本人。
2. 再查一遍上游有没有同类 issue / PR。这次只查了代码：main 上没有类似机制。
3. main 如果又前进了，重新确认 patch 能应用，并重跑 `make lint && make test && make build`。

**和 v0.3.0 补丁的区别：** [`skillevaluator-prebuilt-claude-code.patch`](skillevaluator-prebuilt-claude-code.patch)
是给 v0.3.0 fork 用的最小版本。main 已经把选类逻辑收进 `_agent_import_path`，
这份 PR 在它之上另外做了：未知 agent 名、与 provider 包装类冲突时在运行前直接报错；
镜像里缺 CLI 时错误信息点名这个变量；接受 `claude` 别名；补测试、文档和 CHANGELOG。

**验证记录：** 全部在 Claude Code 云端环境里跑，端点是本地 mock，分数没有意义。

- 完整测试 7056 passed / 49 skipped；lint（`src tests` 与 CI 的 `ruff check .`）、build
  都通过。最后一处文档措辞改动之后，重跑了读文档的 13 个测试文件（1036 passed）。
- 端到端四项见英文部分的表格。
- main 上 agent runtime preflight **默认关闭**（v0.3.0 默认开启），所以文档写的是
  "缺 CLI 时 agent setup 失败，加 `--agent-runtime-preflight` 可以提前拦住"，两种情况
  都实测过。

---

**Title:** `feat(tier3): opt-in prebuilt Claude Code CLI for Docker-mode trials`

## Summary

In `--env-mode docker`, Harbor's stock Claude Code agent installs itself in
every trial container: `apt-get update && apt-get install -y curl procps`, then
`curl -fsSL https://downloads.claude.ai/claude-code-releases/bootstrap.sh | bash`.
`bootstrap.sh` hard-codes its download base URL and always fetches the full
native binary (241.6 MB for linux-x64 2.1.283). Every trial repeats that
download, and hosts that cannot reach `downloads.claude.ai` (restricted egress,
some regions) cannot run Claude Code trials at all. Baking the CLI into the
image does not help: the install step still runs under `set -o pipefail`, and
Docker mode always dispatches `-a claude-code`, with no CLI or
`evals/config.yml` route to a different agent class.

This PR adds an operator opt-in, `SKILLEVALUATOR_DOCKER_PREBUILT_AGENTS=claude-code`.
With it, Docker-mode Claude Code is dispatched via `--agent-import-path` to
`SkillEvaluatorPrebuiltClaudeCode`, whose install only verifies the CLI
(`claude --version`) and exits 127 with a message naming the variable when the
image lacks it. Launch, credentials, permission mode, and verification are
Harbor's stock agent; dispatch reuses the import-path route the NVIDIA Build and
gateway wrappers already use.

- Scope: `claude-code` (and the `claude` alias) in `--env-mode docker` only;
  other modes ignore the variable.
- Fails closed: unsupported names, and a provider whose compatibility wrapper
  installs the agent itself (`nv_build`), fail at credential validation before
  any image or trial work. Import-path resolution moved into the existing `try`
  block, so these report like runtime-plan errors (`{"error": [...]}`, exit 1)
  instead of an unhandled exception.
- Unset (the default): behavior is unchanged, and the existing selection tests
  pass untouched.

Operators supply the CLI through the image: generated task images start
`FROM python:3.12-slim`, and Docker uses a local image with that tag when one
exists, so an operator-built image with `claude` and `procps` covers every
generated task. This is documented under *Prebuilt agent CLIs*.

End-to-end on this branch: `claude-code`, Docker, the reference skill
`calculator`, both arms, `--n-attempts 1`, with a local mock Anthropic Messages
endpoint standing in for the provider.

| Run | Result |
| --- | --- |
| Opt-in + image with `claude` | `succeeded`, 4/4 attempts scored; install ran only `claude --version` |
| Same, with container egress to the internet dropped (only the host-side mock reachable) | `succeeded`, 4/4 scored |
| `SKILLEVALUATOR_DOCKER_PREBUILT_AGENTS=claude-code,codex` | exit 1 at credential validation: `supports only claude-code; unsupported: codex` |
| Opt-in, image without `claude`, `--agent-runtime-preflight` | preflight fails at 40 s with exit 127, naming the variable |

With an equivalent patch on v0.3.0, the same comparison took 318 s with the
stock install and 245 s with the prebuilt CLI.

Open questions for maintainers:

1. **Env var vs. flag/config key.** Modeled on the operator-owned
   `SKILLEVALUATOR_LOCAL_*` knobs, since image contents are an operator concern
   and `evals/config.yml` is skill-owned. Happy to switch to a CLI flag.
2. **Supplying the image.** Shadowing the local `python:3.12-slim` tag works
   without further changes but affects other builds on the host. A base-image
   override (e.g. `SKILLEVALUATOR_DOCKER_BASE_IMAGE`) may be a cleaner follow-up.
3. **Other agents.** Codex and OpenCode could get the same treatment; this PR
   keeps to Claude Code.

## Verification

- [x] I am familiar with the [Contributing Guidelines](../CONTRIBUTING.md)
- [x] Added or updated focused tests: `tests/tier3/test_prebuilt_agents.py`
  covers selection per provider and alias, unchanged routes in other modes,
  both fail-closed cases, Harbor command dispatch, and install through Harbor's
  real `AgentFactory` / `_exec` path (only `claude --version`; the exit-127 path
  raises with the variable named).
- [x] Updated documentation for user-visible changes:
  `docs/agents-and-sandboxes.mdx` (*Prebuilt agent CLIs*) and
  `docs/environment-variables.mdx`.
- [x] Ran `make lint` (plus `ruff check .` as CI runs it)
- [x] Ran `make test`: 7056 passed, 49 skipped
- [x] Ran `make build`
- [x] Did not add credentials, private datasets, or proprietary benchmark content

## Release Impact

- [ ] No user-visible release note needed
- [x] Updated `CHANGELOG.md`
