# skill-up 真实输出

`skill-up` v0.12.0 + Claude Code 2.1.284 + 火山方舟 `deepseek-v4-flash` 的实跑产物
（2026-09-29 POC，见 docs/runtime-evaluation.md §2），用作 `runtime_adapter` 的
契约 fixture。被评的是 `../runtime_skill/ticket-formatter`。

| 目录 | 怎么跑出来的 | 内容 |
| --- | --- | --- |
| `passed/` | 3 个用例各 1 次 | 3 PASS；含 `report.html` |
| `iterations/` | 再加一个故意判错的用例，`--iteration 3` | 9 PASS + 3 FAIL，三个迭代目录 |
| `errored/` | 模型地址指向 `127.0.0.1:9`，单用例超时 60s | 1 ERROR；`run_finished.status` 仍是 `COMPLETED` |

`events.jsonl` 与 `result.json` 原样保留。唯一改过的是
`passed/…/agent/run/session.jsonl`：原文是 125KB 的 Claude Code 会话记录，含
工作目录路径，这里只留下每条记录的 `type` 与 `message.role` / `message.model`
——那正是 `runtime_adapter.served_models` 读的全部字段。

换 skill-up 版本时重新抓一份替换这里，契约测试先红就说明解析要跟着改。
