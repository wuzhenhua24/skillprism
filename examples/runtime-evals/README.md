# 运行时用例示例

一个完整、能跑通的 skill 加它的 `evals/` 目录。给自己的 skill 补运行时用例时，
**把 `ticket-formatter/evals/` 整个拷过去，照着改**。约定与原因见
[docs/runtime-cases.md](../../docs/runtime-cases.md)。

```text
ticket-formatter/
  SKILL.md                                   被评的 skill
  evals/
    cases/                                   ← 平台只读这里的 *.yaml，一个文件一个用例
      basic-format.yaml                      回答符合格式（最常用）
      writes-file.yaml                       产出了文件
      from-log-file.yaml                     用 fixture 准备工作区
      ticket-structure-script.yaml           脚本判分
      no-invented-steps.yaml                 agent_judge 语义判断
      not-a-bug-report.yaml                  不该触发的边界情况
    fixtures/                                ← 用例用到的文件都放这里
      repos/service-logs/incident.log
      scripts/check-ticket.sh
    eval.yaml                                只给本地调试用，平台不读
```

每个用例文件开头的注释说明了这种写法适合测什么。

## 这套用例的实测

2026-09-29 在 SkillPrism 的运行时评测链路上跑过（Claude Code 2.1.284 + 火山方舟
`deepseek-v4-flash`）：

| 跑法 | 结果 | 耗时 | token |
| --- | --- | --- | --- |
| 每个用例 1 次 | 6/6 通过 | 51 秒 | 约 11.7 万 |
| 每个用例 2 次 | 12/12 通过 | 88 秒 | 约 23.7 万 |

token 的大头是 agent 自己的系统提示，每个用例约 1.6 万起；`agent_judge` 的用例再加约
1.6 万。用例写多少个，心里要有这笔账。

## 改成你自己的

1. 把 `cases/` 里的示例换成测你的 skill 的用例。文件名就是用例 ID，扩展名必须是
   `.yaml`。
2. 每条 SKILL.md 里写明的关键规则，至少有一个用例覆盖；再加一两个边界情况。
3. 至少一个用例要「没装这个 skill 就过不了」——例如 skill 规定的特殊格式或标记。
   示例里的 `TICKET-V1` 就是这个作用：没装 skill 时模型不会写出它。
4. 用例需要的文件放进 `evals/fixtures/`，按相对 skill 根目录的路径引用。**别放到
   `evals/` 外面**：静态检查会把它们当成 skill 的一部分扫，样例代码里的漏洞、假
   token、图片都会算到你的 skill 头上。
5. 本地先跑通：改好 `evals/eval.yaml` 里的模型，
   `skill-up validate ./evals/eval.yaml && skill-up run ./evals/eval.yaml`。
