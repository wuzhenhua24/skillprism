# SkillPrism 接口文档

面向对接方（管理系统）的完整 HTTP 契约，联调时按这一份对。**为什么这样设计**写在
[README「对接管理系统」](../README.md#对接管理系统)，**怎么部署**写在
[deployment.md](deployment.md)，这里只讲接口本身。

服务定位是**只展示不拦截**：评测结果用于展示徽章，我们不可用不应该影响用户的上传流程。

---

## 1. 总体流程

```
①  用户上传完 skill（zip 已落盘可下载 / 代码已推到 GitLab）
②  管理系统 POST /api/evaluations/{zip|gitlab}      → 202 {task_id}
③  管理系统轮询 GET /api/tasks/{task_id}            → state=done 时 results[] 出结果
④  管理系统把 results[] 里的寻址键和这次提交存在一起
⑤  详情页 GET /api/skills/{skill_id}/evaluation     → 结论 JSON（含 report_url）
    用户点开 report_url                              → HTML 报告
```

三条贯穿全文的规则，联调时先记住：

1. **`202` 是受理，不是评完，也不代表这个 skill 存在。** 提交路径上没有任何网络调用，
   内容由 worker 异步反向下载。取不到内容体现为任务的 `error`，不是同步 4xx。
2. **查结论的钥匙只从 `GET /api/tasks/{task_id}` 的 `results[]` 里拿**，不要自己拼，
   更不要拿任务对象上的 hash 直接去查（见 §5.3）。
3. **两种接入是两个命名空间。** 同时启用 zip 与 GitLab 时，查询必须带 `?source=`。

---

## 2. 通用约定

| 项 | 约定 |
| --- | --- |
| Base URL | 由部署决定，例 `http://127.0.0.1:8000`（进程只绑本机，对外由前置网关承载） |
| 认证 | **当前没有**。见 §11.1，联调期直连内网地址即可 |
| 请求体 | `Content-Type: application/json`，UTF-8 |
| 时间 | ISO 8601，UTC，例 `2026-08-27T10:22:37.113621Z` |
| hash | 形如 `sha256:9f2c1b…`（`sha256:` 前缀 + 64 位十六进制）。**当不透明字符串用**，不要解析、不要截断存储 |
| 在线 schema | `GET /openapi.json`；交互式调试页 `GET /docs` |
| 错误体 | `{"detail": ...}`，两种形状，见 §10 |

---

## 3. 端点一览

| 方法 | 路径 | 用途 | 成功码 |
| --- | --- | --- | --- |
| POST | `/api/evaluations/zip` | 按管理系统 zip 下载接口触发 | `202` |
| POST | `/api/evaluations/gitlab` | 按 GitLab 仓库触发 | `202` |
| POST | `/api/evaluations` | 保留通道，不指明接入 | `202` |
| GET | `/api/tasks/{task_id}` | 轮询任务状态、取结论寻址键 | `200` |
| GET | `/api/skills/{skill_id}/evaluation` | 取一条结论 | `200` |
| GET | `/api/skills/{skill_id}/report` | 取 HTML 报告 | `200` |
| GET | `/healthz` | 健康检查 + 扫描器齐备情况 | `200` |

`POST /embed/v1/embeddings` 也在同一个进程里，但它是给评测子进程用的内部 shim，
**不属于对接契约**，见 §11.4。

---

## 4. 触发评测

### 4.1 `POST /api/evaluations/zip`

内容从管理系统的 zip 下载接口取。

**请求体**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_id` | string | 是 | 管理系统里的资源 ID，也是拼下载地址用的那个 ID |
| `skill_name` | string ≤255 | **单 skill 必填** | 管理系统里**登记的**技能名。物化目录用它命名，会和包内 frontmatter 的 `name` 比对。必须是单段名字，不能含 `/`、`..` |
| `skill_version` | string ≤128 | 否 | 用户上传时手填的自由文本标签，与包内版本无关。**不决定取到哪份内容** |
| `tier` | string | 否 | `tier1`（默认）。`tier2` / `tier3` 返回 `501` |
| `force` | bool | 否 | 默认 `false`（内容未变时复用已有结论）。`true` 强制重跑 |
| `bundle` | bool | 否 | 默认 `false`。`true` 表示 `skill_id` 指向装着多个 skill 的父目录，见 §4.4 |

```bash
curl -X POST http://127.0.0.1:8000/api/evaluations/zip \
  -H 'Content-Type: application/json' \
  -d '{
    "skill_id": "2000705",
    "skill_name": "skill-file-md5",
    "skill_version": "2.0.0",
    "tier": "tier1",
    "force": false
  }'
```

**响应 `202`**

```json
{
  "task_id": "5f0c8a3e-...",
  "skill_id": "2000705",
  "state": "queued",
  "deduplicated": false
}
```

回执里**刻意没有 `content_hash`**：此刻还没下载内容，给不出真实的 hash。

### 4.2 `POST /api/evaluations/gitlab`

内容从 GitLab 归档接口取（`archive.zip`，不 clone）。

**请求体**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `project` | string 1–255 | 是 | 项目路径 `group/repo`，也接受数字项目 ID |
| `subdir` | string ≤255 | 否 | 仓库内子目录，例 `skills/log-triage`。留空表示 `SKILL.md` 在仓库根 |
| `ref` | string ≤128 | 否 | 分支 / tag / commit sha。留空用服务端的 `GITLAB_DEFAULT_REF`。**它决定取到哪份内容** |
| `skill_name` | string ≤255 | 单 skill 必填 | 同 §4.1 |
| `tier` / `force` / `bundle` | | 否 | 同 §4.1 |

```bash
curl -X POST http://127.0.0.1:8000/api/evaluations/gitlab \
  -H 'Content-Type: application/json' \
  -d '{
    "project": "group/repo",
    "subdir": "skills/log-triage",
    "ref": "v1.2.0",
    "skill_name": "log-triage",
    "bundle": false
  }'
```

响应形状同 §4.1，其中 `skill_id` 回的是服务端拼出来的内部 ID `group/repo:skills/log-triage`
——**查询时用的就是这个字符串**。

`project` / `subdir` / `ref` 的合法性在提交时就校验，写错当场 `422`（例：`ref` 含空格、
`..`、以 `-` 开头）。

> **Claude plugin 形态的仓库**（根上有 `.claude-plugin/`）要把 `subdir` 指到 `skills`
> 那一层、`bundle` 置 `true`；指到仓库根会失败，因为 skill 埋在二级。
> 单 plugin 仓 → `subdir=skills`；monorepo → `subdir=plugins/<plugin>/skills`。

### 4.3 `POST /api/evaluations`（保留通道）

收 §4.1 那个形状的请求体，**只启用了一种接入时**按那一种解释；两种都启用时返回 `400`
要求改用具名入口。GitLab 接入下 `skill_id` 仍是 `项目[:子目录]`、`skill_version` 仍是 ref。

新对接**请直接用具名入口**，这个通道只为已经在用它的调用方保留。

### 4.4 三个入口共同的语义

**`skill_name` 为什么必填。** 物化目录用它命名，SkillEvaluator 的 `SCHEMA.name_consistency`
（HIGH）会拿目录名和 frontmatter 的 `name` 比对。用纯数字资源 ID 当目录名会让**每个**
skill 平白多一条 HIGH。要的是管理系统里登记的那个名字——它独立于用户打的包，比对才有意义。

`bundle: true` 时它**不参与任何计算**，省略即可：一次提交对应 N 个 skill、登记名只有一个。
传了不报错但会被丢弃，任务状态里回显 `null`。

**重复触发会折叠。** 对方超时重发、用户连点保存，排队中的同一个 skill 收敛成一条任务，
回执里 `deduplicated: true`，`task_id` 是**原来那条**。已经在跑的任务不折叠，由 worker 的
内容缓存兜住（内容没变就不会真的重跑评测器）。

zip 接入下换个 `skill_version` 仍会折叠（版本只是标签）；GitLab 接入下换个 `ref` 是两份
内容，**不折叠**。

**只对 Skills 分类调用。** Commands / Agents / Hooks 的包里没有 `SKILL.md`，评不了。
契约里没有 `category` 字段，由触发方保证。漏进来的表现是任务 `error` 里出现
「不是一个可评测的 skill」。

**触发时机与失败处理。** 触发要在 zip 落盘可下载之后，不要和上传放在同一个事务里；
我们返回非 `2xx` **不应该让上传失败**，重试几次仍失败就放弃，另配对账任务扫
「有包但没结果」的 skill。

**状态码**

| 码 | 含义 |
| --- | --- |
| `202` | 已受理 |
| `400` | 保留通道 + 两种接入都启用，说不清是哪一种 |
| `409` | 这个入口对应的接入在本部署没启用（收了也没人跑） |
| `422` | 请求体不合法：缺 `skill_name`、名字含路径分隔符、GitLab 三字段写错 |
| `501` | `tier2` / `tier3` 尚未实现 |

---

## 5. `GET /api/tasks/{task_id}`

轮询任务状态。评完之后，`results[]` 里是这次产出的**每条**结论的寻址键。

**响应字段**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | |
| `source` | string | `zip` / `gitlab` / `local`。也是查询时的 `?source=` |
| `skill_id` | string | 触发时声明的（GitLab 为拼出来的内部 ID） |
| `skill_name` | string \| null | bundle 任务恒为 `null` |
| `skill_version` | string \| null | zip 下是标签，GitLab 下是 ref |
| `bundle` | bool | 这次评的是不是一组耦合 skill。**下面两个 hash 字段的含义由它决定** |
| `content_hash` | string \| null | 单任务：这份内容的指纹，也是结论的寻址键。**bundle 任务恒为 `null`** |
| `context_hash` | string \| null | bundle：整组内容的指纹（= 每条成员结论的 `context_hash`）。**单任务恒为 `null`** |
| `tier` | string | `tier1` |
| `queue` | string | `fast` / `index` / `sandbox`（按 tier 路由） |
| `state` | string | `queued` / `running` / `done` / `failed` |
| `attempts` | int | 已尝试次数（默认上限 3） |
| `error` | string \| null | 失败原因；也可能在 `queued` 时非空（正在退避重试） |
| `next_attempt_at` | datetime \| null | 非空即「正在退避、还没到重试时间」 |
| `results` | array | 见下 |

`results[]` 每项：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `skill_id` | string | 这条结论的 ID（bundle 成员是 `<bundle_id>/<成员目录名>`） |
| `content_hash` | string | 查结论用的钥匙 |
| `context_hash` | string \| null | 上下文，单独评的为 `null`。**也是钥匙的一部分** |
| `status` | string | 见 §9 |
| `report_url` | string \| null | 报告直链，见 §7 |

### 5.1 单 skill 任务

```json
{
  "task_id": "5f0c8a3e-...",
  "source": "zip",
  "skill_id": "2000705",
  "skill_name": "skill-file-md5",
  "skill_version": "2.0.0",
  "bundle": false,
  "content_hash": "sha256:9f2c1b…",
  "context_hash": null,
  "tier": "tier1",
  "queue": "fast",
  "state": "done",
  "attempts": 1,
  "error": null,
  "next_attempt_at": null,
  "results": [
    {
      "skill_id": "2000705",
      "content_hash": "sha256:9f2c1b…",
      "context_hash": null,
      "status": "passed",
      "report_url": "https://skillprism.internal/api/skills/2000705/report?source=zip&content_hash=sha256:9f2c1b…&context_hash="
    }
  ]
}
```

### 5.2 bundle 任务

```json
{
  "task_id": "7b31d0c2-...",
  "source": "gitlab",
  "skill_id": "group/repo:skills",
  "skill_name": null,
  "skill_version": "v1.2.0",
  "bundle": true,
  "content_hash": null,
  "context_hash": "sha256:d4ac66…",
  "state": "done",
  "results": [
    {"skill_id": "group/repo:skills/code-review",
     "content_hash": "sha256:8f3a0b…", "context_hash": "sha256:d4ac66…",
     "status": "passed", "report_url": "…"},
    {"skill_id": "group/repo:skills/test-gen",
     "content_hash": "sha256:1c77e2…", "context_hash": "sha256:d4ac66…",
     "status": "failed", "report_url": "…"}
  ]
}
```

一次提交产出多条结论，每个成员一条；成员的 `skill_id` 就是它单独提交时会用的那个，
所以**不需要第二套查询方式**。bundle 的 ID 本身不挂结论——它不是一个 skill。

### 5.3 ⚠ 最容易踩的坑：别拿任务上的 hash 去查结论

单任务时 `task.content_hash` 确实就是结论的寻址键；**bundle 任务时任务行上存的是整组
内容的指纹**，它是每条成员结论的 `context_hash`，不是任何一条结论的 `content_hash`
——拿它查什么都是 `404`。

所以任务 DTO 按形态把它放到对的字段上（bundle 时 `content_hash` 为 `null`）。
**统一只读 `results[]`**，两种形态一个写法，不用判断这次提交的是什么。

一词之差，整条路上没有任何一步报错：提交 `202`、任务 `done`、查询 `404`。查询侧因此也把
话挑明——拿组指纹当 `content_hash` 传时，`404` 的 `detail` 会说明它其实是 `context_hash`
并点出几个成员的 `skill_id`。

### 5.4 轮询与失败

- `state` 为 `done` 表示这条任务处理完了；**结论本身可能是 `failed` 或 `error`**，那在
  `results[].status` 里，不在 `state` 上。
- `state=queued` 且 `next_attempt_at` 非空 = 正在退避重试，`error` 是上一次的原因。
  默认退避 30s 起、翻倍、上限 300s，最多 3 次尝试。
- `state=failed` 是终结态，`error` 里是原因。常见前缀：`取不到内容：…`（网络 / 权限 /
  包还没落盘）、`物化失败：…`（归档不安全或布局不对）、`评测退出码 …`。
- bundle 里个别成员评失败时，`results[]` 里**少那几条**，原因在 `error` 里，不会给占位结论。
- 建议轮询节奏：前 30s 每 2s 一次，之后退到 10s，5 分钟没完就转后台对账。
  Tier 1 一个 skill 通常几秒到几十秒，一组几十个成员会更久（服务端单次评测超时 600s）。
- `404 {"detail": "任务不存在"}`。

---

## 6. `GET /api/skills/{skill_id}/evaluation`

**查询参数**

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `source` | 视部署 | `zip` / `gitlab` / `local`。只启用一种接入时可省；两种都启用时省了返回 `400` |
| `content_hash` | 强烈建议 | 不带则退回「这个 skill 最近评完的那条」 |
| `context_hash` | 强烈建议 | **三态**，见下 |

`context_hash` 的三态，**空值不等于没给**：

| 传法 | 含义 |
| --- | --- |
| 参数不出现 | 不限上下文，回最近评完的那条 |
| `context_hash=`（空值） | 要**单独评**的那条 |
| `context_hash=sha256:…` | 要那一组上下文下的那条 |

把 `results[]` 里的 `content_hash` 与 `context_hash` **原样回传**即可（`null` 传成空值），
不需要判断这次是哪种形态。

> 为什么两个都要带：同一个 skill 单独评过、又在一组里评过时，两条结论的
> `(skill_id, content_hash)` 一模一样，只有 `context_hash` 不同。
> 只带 `content_hash` 的查询会在两条之间跳。

**`skill_id` 的编码。** 路由是 `{skill_id:path}`，`/` 与 `:` 保持字面量不要编码
（GitLab 的 ID 形如 `group/repo:skills/code-review`），其余字符照常百分号编码。

```bash
curl -G http://127.0.0.1:8000/api/skills/2000705/evaluation \
  --data-urlencode 'source=zip' \
  --data-urlencode 'content_hash=sha256:9f2c1b…' \
  --data-urlencode 'context_hash='
```

**响应 `200`**（findings 已截断）

```json
{
  "skill_id": "2000705",
  "skill_version": "2.0.0",
  "content_hash": "sha256:9f2c1b…",
  "context_hash": null,
  "status": "passed",
  "gate_passed": true,
  "evaluated_at": "2026-08-27T10:22:37.113621Z",
  "score": 76.5,
  "grade": "C",
  "severity_counts": {"critical": 0, "high": 0, "medium": 5, "low": 7},
  "evaluator": {
    "version": "0.2.1",
    "profile": "external",
    "policy_digest": "sha256:1caeb0bf…",
    "incomplete_scans": []
  },
  "tiers": {
    "tier1": {
      "status": "passed",
      "validators": [
        {
          "validator": "Schema & Repository Governance",
          "description": "",
          "passed": true,
          "status": "passed",
          "findings": [],
          "errors": []
        },
        {
          "validator": "QUALITY",
          "description": "",
          "passed": true,
          "status": "passed",
          "findings": [
            {
              "category": "QUALITY",
              "severity": "medium",
              "check_name": "quality_correctness",
              "message": "SKILL_SPEC recommended field missing: 'version'",
              "file_path": "SKILL.md",
              "line_number": null,
              "suggestion": "Add 'version' to frontmatter — Semantic version (e.g., \"1.0.0\")"
            }
          ],
          "errors": []
        }
      ]
    },
    "tier2": null,
    "tier3": null
  },
  "report_url": "https://skillprism.internal/api/skills/2000705/report?source=zip&content_hash=sha256:9f2c1b…&context_hash=",
  "error": null
}
```

**字段说明**

| 字段 | 说明 |
| --- | --- |
| `status` | 五态，见 §9。**`incomplete` 不是通过** |
| `gate_passed` | 阻断级检查是否全过。**与 `status` 正交**：`status=incomplete` 时仍可能有 critical/high，只看 `status` 会漏 |
| `score` / `grade` | 质量分与等级，上游没给时为 `null` |
| `severity_counts` | 只包含上游报告里出现的级别，缺的按 0 处理 |
| `evaluator.version` | 评测器版本。**必须随结果展示**——分数变化时用户第一个要问的就是「是不是评测器变了」 |
| `evaluator.incomplete_scans` | 非空即安全扫描没跑全，结论不完整 |
| `tiers.tier1.validators[]` | 按 validator 名分组，不拍平成固定列；上游新增 validator 不需要改契约 |
| `validators[].errors[]` | 上游只有一句话、没有 severity 的 legacy 问题（**死链走这个通道**）。`passed=false` 而 `findings` 为空时，原因在这里 |
| `findings[].file_path` | 已归一化成 skill 内部的相对路径，不会暴露我们的临时目录 |
| `tiers.tier2` / `tier3` | 当前恒为 `null`，字段先占位，将来补齐时契约不变 |
| `report_url` | 见 §7。没配公开域名或这条结论没有报告时为 `null` |
| `error` | `status=error` 时说明原因 |

> `validators[].description` 从查询接口读回来**恒为空字符串**（这个字段没有落库），
> 界面不要依赖它。

**`404` 的三种 `detail`**，文案本身就是排查线索：

| detail | 含义 |
| --- | --- |
| `该 skill 尚无评测结果` | 没带 hash，且这个 skill 从没评过——去看有没有触发成功 |
| `content_hash=… 是一组耦合 skill 的整组指纹…（并列出几个成员）` | 踩了 §5.3 的坑，换成 `results[]` 里的成员 hash |
| `该 skill 没有 content_hash=… 的评测结果` | hash 记串了，或内容还没评完 |

**不带 hash 时退回「最近评完的那条」，在 GitLab 接入下多半不是你要的**：`skill_id` 是长期
不变的仓库路径，多个 ref 的结论堆在同一个 ID 下，详情页展示 `v1.0.0`、拿回来的可能是
`main` 的分数。**没有按 ref 查的接口**——`skill_version` 只是标签，不参与结论的身份。

---

## 7. `GET /api/skills/{skill_id}/report`

回传 SkillEvaluator 生成的 HTML 报告（`Content-Type: text/html`）。参数与 §6 **完全同义**，
`source` / `content_hash` / `context_hash` **两边必须一起带**——只在一边带，拿到的结论和
报告可能来自不同版本、不同上下文甚至不同接入。

一般不用自己拼这个 URL：结论与 `results[]` 里的 `report_url` 就是它，**原样存下来、
原样给用户点开**即可，链接里已经钉住了这条结论自己的三个参数。

服务端没配 `SKILLPRISM_PUBLIC_BASE_URL`、或这条结论没有报告（例如 `error`）时
`report_url` 为 `null`——不会回落到内部存储地址。

**承载要求（对接方要做的）**

- **用独立域名承载，别和管理系统同域。** 报告是自生成 HTML，内容源头是用户上传的 skill；
  同域意味着它和管理系统共享 cookie 与 localStorage。
- 要嵌进页面就用**沙箱化 iframe**，不要把 HTML 内联进自身 DOM。
- 我们已经在响应上带了 `Content-Security-Policy`、`X-Content-Type-Options: nosniff`、
  `Referrer-Policy: no-referrer`。要清楚 CSP 的边界：`script-src 'unsafe-inline'` 是报告
  自己的内联脚本要用的，所以它**挡不住注入的脚本执行**，真正起隔离作用的是独立域名。
- 报告地址要对**最终用户的浏览器**开放，而用户带不了服务令牌——网关上单独放行
  `/api/skills/*/report` 这一条路径。
- 链接发出去之后报告就不能随便删（目前也没有清理机制）。

`404`：`报告不存在`（结论在但没有报告文件），或 §6 那三种（结论就没查到）。

---

## 8. `GET /healthz`

```json
{
  "status": "ok",
  "skillevaluator": "/var/lib/skillprism/.local/bin/skillevaluator",
  "version": "skillevaluator, version 0.2.1",
  "missing_scanners": []
}
```

`status` 为 `ok` 的条件是**评测器在位且 `missing_scanners` 为空**，任一不满足即 `degraded`：

- `skillevaluator` 为 `null` → 评测器不在 PATH 上，任何评测都跑不了；
- `missing_scanners` 非空 → 安全扫描跑不全，评出来的结论会是 `incomplete`。

联调开始前先看这一条。（`version` 为 `null` 只是自检取版本超时，不阻塞评测。）

---

## 9. 枚举字典

**结论状态 `status`**（`EvaluationDTO.status`、`results[].status`）

| 值 | 含义 | 界面 |
| --- | --- | --- |
| `passed` | 通过 | 合格徽章 |
| `failed` | 不合格 | 不合格，**不该重试** |
| `incomplete` | 外部扫描器缺失，安全结论不完整 | **不是通过**，不能发合格徽章 |
| `error` | 评测本身失败，skill 从未被判定 | 展示为「评测失败」，可重试 |
| `pending` | 占位，尚未评出 | |

**任务状态 `state`**：`queued` / `running` / `done` / `failed`（`done` 只表示这条任务处理
完了，不代表结论是通过）。

**问题级别 `severity`**：`critical` / `high` / `medium` / `low` / `info`。

**内容来源 `source`**：`zip`（管理系统下载接口）/ `gitlab` / `local`（仅开发调试）。

**层级 `tier`**：`tier1`（唯一已实现）/ `tier2` / `tier3`。

---

## 10. 错误码总表

响应体统一是 `{"detail": ...}`，但有**两种形状**：

- 我们自己抛的业务错误 → `detail` 是**一句中文字符串**，可以直接给运维看；
- 请求体 / 参数不符合 schema（FastAPI 校验）→ `detail` 是**数组**，形如
  `[{"loc": ["body", "skill_name"], "msg": "...", "type": "..."}]`。

解析时要能同时吃下这两种，别假设它一定是字符串。同一个端点上两种都会出现，例如：

```jsonc
// 缺 skill_name —— schema 校验，数组形状
{"detail": [{"type": "value_error", "loc": ["body"], "msg": "Value error, skill_name 必填：…"}]}

// skill_name 含路径分隔符 —— 业务校验，字符串形状
{"detail": "skill_name 必须是单段名字，不能含路径分隔符：'a/b'"}
```

| 码 | 端点 | 典型 `detail` | 怎么办 |
| --- | --- | --- | --- |
| `400` | 保留通道触发、查询 | `本部署同时启用了 zip、gitlab 接入，请用 source= 指明这次要哪一个` | 改用具名入口 / 带上 `?source=` |
| `409` | `/zip`、`/gitlab` | `本部署未启用 gitlab 接入（当前启用：zip）` | 找运维确认部署配置 |
| `422` | 触发 | 缺 `skill_name`、`skill_name` 含路径分隔符、`ref` 不合法 | 改请求，重试没用 |
| `422` | 查询 | `source 只能是 local、zip、gitlab，收到 'x'` | 改参数 |
| `404` | 任务 | `任务不存在` | 确认 `task_id` |
| `404` | 结论 / 报告 | 见 §6 的三种 | 按文案区分 |
| `501` | 触发 | `tier2 尚未实现，当前仅支持 tier1` | 只传 `tier1` |
| `5xx` | 任意 | | **不应影响用户的上传流程**，重试几次后放弃并记账 |

注意「skill 不存在」**不会**是一个同步 4xx——那要等 worker 真去取才知道，届时体现为任务的
`error`。取不到多半是网络抖动或包还没落盘，长成 4xx 会让调用方以为是自己请求错了。

---

## 11. 不在契约里 / 尚未实现

### 11.1 接口鉴权

**当前完全开放。** 进程绑 `127.0.0.1`，靠前置网关鉴权。补鉴权时 `/api/skills/*/report`
需要另一套方案（用户点链接带不了服务令牌，最省事的是签名 URL）。联调期直连内网地址即可，
上生产前这是第一优先级。

### 11.2 结果推送

**只有轮询，没有 webhook。** 前提是触发方和展示方都是管理系统——用户提交时触发、之后打开
详情页时轮询，有一个「人来看」的时刻承接结果。真需要「评完立刻亮徽章」时，再在触发 payload
里加可选的 `callback_url`。

### 11.3 批量查询

没有。列表页按 `skill_id` 逐个查会打 N 次请求（50 个 skill 打 50 次），需要时补一个批量接口
——联调时如果列表页压力明显，提出来我们加。

### 11.4 `POST /embed/v1/embeddings`

OpenAI 兼容的 embeddings 批量拆分 shim，是给 Tier 2 的评测子进程用的（火山方舟单请求最多
10 条输入，而上游把批大小硬编码成 64）。**不是对接契约的一部分，也不要暴露到公网**——它会
转发凭据。

### 11.5 其它

- **Tier 2 / Tier 3 未实现**：`tiers.tier2` / `tier3` 恒为 `null`，`tier=tier2` 触发返回 `501`。
- **按 ref / 按版本号查结论**：没有，见 §6 最后一段。
- **不要把 bundle 的结论说成「这个 plugin 安全」**：我们只取 `skills` 子树，`hooks/`、
  `commands/`、`agents/` 一个字节都没下载，而 `hooks.json` 恰恰能在工具调用前后跑任意命令。
  展示侧要说清楚徽章覆盖的是哪一部分。

---

## 12. 联调 checklist

```bash
BASE=http://127.0.0.1:8000

# ① 服务与扫描器都在
curl -s $BASE/healthz | python3 -m json.tool

# ② 触发一次（zip 接入）
TASK=$(curl -s -X POST $BASE/api/evaluations/zip \
  -H 'Content-Type: application/json' \
  -d '{"skill_id":"2000705","skill_name":"skill-file-md5","skill_version":"2.0.0"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_id"])')
echo "task=$TASK"

# ③ 轮询到 done，把 results 打出来
curl -s $BASE/api/tasks/$TASK | python3 -m json.tool

# ④ 用 results[0] 的 content_hash / context_hash 查结论
curl -s -G $BASE/api/skills/2000705/evaluation \
  --data-urlencode 'source=zip' \
  --data-urlencode 'content_hash=<从 results 里抄>' \
  --data-urlencode 'context_hash=' | python3 -m json.tool
```

逐项确认：

- [ ] `/healthz` 的 `missing_scanners` 为空（否则结论都是 `incomplete`）
- [ ] 触发返回 `202`，重复触发返回 `deduplicated: true` 且 `task_id` 相同
- [ ] 缺 `skill_name` 时返回 `422`（确认对接方一定会传登记名，不是资源 ID）
- [ ] 任务评完后 `results[]` 非空，且**代码里只从 `results[]` 取 hash**
- [ ] 查结论带齐 `source` + `content_hash` + `context_hash`（空值也要传）
- [ ] `report_url` 能在浏览器里打开，且承载在独立域名下
- [ ] 界面同时展示 `status`、`gate_passed` 和 `evaluator.version`
- [ ] 我们返回 5xx 时，对方的上传流程不受影响
- [ ] bundle 场景单独走一遍（`bundle: true`，确认任务 `content_hash` 为 `null`、
      成员从 `results[]` 取）
