# SkillPrism

基于 [SkillEvaluator](https://github.com/NVIDIA/SkillEvaluator) 的评测编排与结果服务，
把 skill 质量结果回写到公司 skill 管理系统。

**当前范围：M1 / Tier 1。** 定位是参考信息，不拦截发布。
Tier 2（跨 skill 相似度）与 Tier 3（沙箱实跑）在数据模型上预留了位置，尚未实现。

## 快速开始

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
cp .env.example .env
.venv/bin/alembic upgrade head
```

最后一步建库结构。**服务不会自动建表**——自动建表会掩盖"改了模型忘了生成
迁移"这类问题，见下方数据库结构一节。

SkillEvaluator **独立安装**，不要装进本服务的 venv（原因见下）：

```bash
uv tool install --python 3.13 "skillevaluator[security] @ git+https://github.com/NVIDIA/SkillEvaluator.git"
```

Tier 1 的完整安全结论还需要三个外部扫描器：

```bash
uv tool install semgrep
uv tool install "skillspector @ git+https://github.com/NVIDIA/SkillSpector.git@v2.9.6"
brew install gitleaks
```

**SkillSpector 必须 pin 在 v2.9.6，不要装 latest。**

SkillSpector 2.10.0 起对覆盖不完整的扫描 *fail closed*：当
`analysis_completeness.is_complete` 为 false 时，把 `recommendation` 从
`SAFE` 升级为 `CAUTION`，同时保留诚实的 score 与 severity
（见其 `nodes/report.py` 的升级分支）。

而 SkillEvaluator 0.2.1 严格校验 `recommendation` 必须等于 severity 的映射值
（`LOW→SAFE` / `MEDIUM→CAUTION` / `HIGH|CRITICAL→DO_NOT_INSTALL`，见
`validators/security.py:55`）。被升级过的报告对不上映射，SkillEvaluator 判定
报告不可信，把**整个**安全扫描标为 incomplete——注意不是丢弃某一条结论，
而是整个扫描的结果都不算数。

**触发面比想象中大。** SkillSpector 的引用解析会把路径样式的文本当作本地引用，
解析不了就记一条 `reference_unresolved`、把覆盖标为 partial。一个
`### Input/Output Separation` 这样带斜杠的标题就足够触发。文档型 skill
里这类写法非常常见，所以这不是边缘情况。

升级 SkillEvaluator 前不要动这个 pin。`tests/test_e2e_tier1.py` 里的
`test_security_scan_completes` 会在版本回归时立刻变红（已实测：
2.11.0 下该用例失败，2.9.6 下通过）。

装齐后启动：

```bash
.venv/bin/uvicorn skillprism.api.app:app --reload
```

worker 另起一个进程：

```bash
.venv/bin/skillprism-worker
```

部署到服务器见 [docs/deployment.md](docs/deployment.md)（Ubuntu，非容器）。

## 模块

| 模块 | 职责 |
| --- | --- |
| `materialize.py` | 把存储中的内容还原成目录树。**唯一把外部数据写盘的地方**，路径校验在此 |
| `runner.py` | 子进程调用 skillevaluator CLI，启动自检，退出码语义 |
| `adapter.py` | 上游 JSON → 本服务 DTO。**唯一了解上游 schema 的模块** |
| `schemas.py` | 对外契约。管理系统只看这一层 |
| `content.py` | 内容来源协议。已有三种实现：本地目录 / 管理系统 zip / GitLab 归档 |
| `storage.py` | 报告存储协议。生产替换为对象存储 |
| `queue.py` / `worker.py` | 任务队列与处理循环 |
| `repository.py` / `models.py` | 持久化 |

## 关键设计

### 为什么子进程调 CLI 而不是 in-process 调库

上游对外承诺稳定的是 CLI，不是 Python API——它的 `__init__.py` 只导出 `__version__`，
文档中没有任何一处提到 `EvaluationService` 或 `import skillevaluator`。
上游改 `run_validation()` 的签名不算 breaking change，改 JSON schema 才算。

此外：外部扫描器遇到病态输入可能挂起或吃爆内存，子进程可以直接杀掉重来；
上游有 `litellm<1.89`、`harbor==0.13.2` 等硬 pin，独立安装才能避免依赖冲突，
也才能同时保留新旧两个版本做升级灰度。

同栈的价值落在 **adapter 层**：执行用子进程，解析用同栈模型。

### status 有五个值，不是两个

`passed` / `failed` / `incomplete` / `pending` / `error`。两条必须分清：

- **`incomplete` 不是通过。** 外部扫描器缺失时上游输出 `overall_status: incomplete`，
  意思是安全扫描没跑全。若并入 `passed`，界面会给一个没扫过的 skill 发合格徽章。
- **`error` 不是 `failed`。** 前者是评测本身故障（skill 从未被判定，退出码 3，应当重试），
  后者是 skill 不合格（退出码 1，不应重试）。

### `gate_passed` 与 `status` 正交

一个 skill 可以同时"扫描没跑全"和"有 critical 问题"。上游的 `overall_status`
会让 incomplete 盖掉 failed，单看 status 就漏掉了阻断级问题。
因此 DTO 额外给出 `gate_passed`，两个维度分别呈现。

### 路径是不可信输入

存储中记录的路径可能含 `..`、绝对路径、盘符、控制字符。
上游自带的 `path_security` 只服务它自己的扫描逻辑，不会替我们把关，
所以 `materialize.py` 独立完成校验，用例在 `tests/test_materialize.py`。

### 物化布局必须是 `skills/<skill-name>/`

两条 SkillEvaluator 的检查会读目录结构：

- `SCHEMA.name_consistency`（HIGH）比对目录名与 frontmatter 的 `name`
- `SCHEMA.folder_hierarchy`（MEDIUM）要求 skill 位于 `skills/` 或 `team-skills/` 下

所以物化目录名取触发方传来的 `skill_name`、外面套一层 `skills/`
（`skill_name` 落库之前排下的存量任务没这个字段，回落到 `skill_id` 末段）。
用固定名（例如 `skill/`）会让**每个** skill 都平白多出一条 HIGH 加一条 MEDIUM，
全是我们的布局造成的误报。用真实标识名后，`name_consistency` 才回归本来的语义：
“登记的 skill 标识与 frontmatter 声明不一致”。

### 问题定位要归一化

不同 validator 输出的 `file_path` 形式不一致：安全扫描给 `SKILL.md`，
schema 检查给物化目录的绝对路径。后者含任务 UUID，原样传给管理系统
对使用者毫无意义，因此 adapter 统一转成 skill 内的相对路径。

### 数据库结构由 Alembic 管理

服务启动时只**检查**结构是否就绪，不建表。原先用的
`Base.metadata.create_all` 语义是"建出还不存在的表"——它对已存在的表
一个字段都不改，而且不报错。所以模型一改、存量库就会在运行时抛
`no such column`。

改了 `models.py` 之后要生成迁移：

```bash
.venv/bin/alembic revision --autogenerate -m "说明"
```

生成的脚本要**读一遍再提交**，autogenerate 不是万能的（尤其是改列类型、
重命名这类操作，它可能推断成删除加新增，会丢数据）。

部署时执行：

```bash
.venv/bin/alembic upgrade head
```

`tests/test_migrations.py` 是这套机制的保险：它真的跑一遍迁移，再拿结果
和模型比对。改了模型没生成迁移时它会失败并指出缺哪一列——因为其它测试
都从模型 `create_all` 建表，根本走不到迁移那条路，不会发现问题。

SQLite 的 `ALTER TABLE` 能力很弱，所以 `env.py` 里开了
`render_as_batch=True`（建新表、拷数据、换名）。不开的话很多迁移会直接失败。
切到 PostgreSQL 后这个选项是无害的。

### 内容 hash

内容不在 Git 里，没有天然的 commit 标识。`compute_content_hash` 对
(规范化路径, 内容摘要) 排序后整体摘要，与文件顺序无关。
用于缓存命中（内容未变不重跑）与结果版本关联。

### 自定义策略走 `--policy` 而不是 `--profile`

`--profile` 只能选 skillevaluator **包内自带**的 YAML（解析路径是
`skillevaluator/config/profiles/<name>.yaml`），指不到外部文件。
自定义策略必须用 `--policy <路径>`，它 overlay 在基础 profile 之上：
`severity_overrides` 逐键合并，`author_email_regex` 仅在显式出现该键时才覆盖。

策略文件见 `profiles/internal.yaml`，**上线前需替换其中的公司邮箱域名**。

### 调整校验严格度

`internal.yaml` 里的严格度是**起点，不是定论**。当前定位是“只展示不拦截”，
所以可以先按现有配置跑，用真实 skill 库的数据判断哪些检查噪声大、
哪些需要提级，再逐步收紧——这正是不做门禁换来的好处。

覆盖键的格式就是报告里显示的那个：

| 写法 | 含义 |
| --- | --- |
| `SCHEMA.author_missing` | 精确匹配单个检查（`CATEGORY.check_name`） |
| `LICENSE.*` | 通配整个类别 |

精确键优先于通配键。可用等级：`critical` / `high` / `medium` / `low` / `info`。
键名直接从评测结果里抄——DTO 的每条 finding 都带 `category` 与 `check_name`，
拼起来就是覆盖键。

**已知需要观察的点：** 结构类检查（schema、license、secrets）准确度高；
语义安全类有噪声。实测中 SkillSpector 把一篇讲 API 设计的文档里出现的
`DELETE /api/tasks/:id` 判成了 `SECURITY.Tool Parameter Abuse (TM1)`（HIGH）——
那只是散文举例，不是可执行内容。若这类误报在你们的 skill 库里普遍存在，
可以降级该项；但降级前先确认它不是在遮蔽真实问题。

改完用任意一个 skill 验证，结果里的 `policy.profile` 与 `policy.digest`
会记录实际生效的策略：

```bash
skillevaluator validate <skill 目录> --policy ./profiles/internal.yaml -r cli
```

## 对接管理系统

用户上传完 skill 后，管理系统调用触发接口；内容由本服务反向去它那里下载。

### 触发接口

```http
POST /api/evaluations
```

```json
{
  "skill_id": "2000705",
  "skill_name": "skill-file-md5",
  "skill_version": "2.0.0",
  "tier": "tier1",
  "force": false
}
```

回 `202` 与 `{task_id, skill_id, state, deduplicated}`。三条语义要说清：

**`skill_name` 是必填的，不能省。** 物化目录用它命名，SkillEvaluator 的
`SCHEMA.name_consistency`（HIGH）会拿目录名和 frontmatter 的 `name` 比对。
管理系统的 skill_id 是纯数字资源 ID，拿它当目录名会让**每个** skill 都平白
多一条 HIGH。也不能拿包里的目录名或 frontmatter 自己回填——那样这条检查
恒真，等于废掉。要的是管理系统里**登记的**那个名字，它独立于用户打的包，
比对才有意义。`tests/test_e2e_tier1.py` 里的
`test_directory_is_named_by_registered_name_not_skill_id` 两个方向都锁了。

管理系统在**上传口**就校验了"包名与文件内技能名一致"，不一致传不上来。
所以 `name_consistency` 在我们这里正常情况下**不可能报**——它已经不是一个
质量信号，而是一枚**契约探针**：报了就说明对方那道校验被绕过、被放宽，
或者两边的归一化规则不一样（大小写、空格、Unicode 形式）。

正因如此，`skill_name` 仍然要由触发方传，**不能改成我们自己去解包里的
frontmatter**。从包里取等于自己和自己比，探针就废了；从对方的登记记录取，
两边才是两个可以互相印证的来源。这也是不把这条检查在 `internal.yaml` 里
关掉的理由：它不产生噪声（永远不报），却能在集成出问题时立刻出声。

**202 是受理，不是评完，也不代表 skill 存在。** 提交路径上没有任何网络调用：
这个接口挂在用户的上传流程后面，同步下载意味着对方要承担我们的网络耗时
（超时上限 60s、包上限 64MB）和可用性。定位是"只展示不拦截"，我们挂了不该
反映到他们的上传体验上。代价是"取不到这个 skill"要等 worker 真去取才知道，
届时体现为任务的 `error` 状态，而不是一个同步的 4xx——取不到多半是网络抖动
或包还没落盘，长成 4xx 会让调用方以为是自己请求错了。

**重复触发会折叠。** 对方超时重发、用户连点保存都会重复触发，排队中的同一个
skill 收敛成一条任务，`deduplicated: true`。已经在跑的任务不折叠——它已经下载
过内容，跑的是更早的那一份；这种情况由 worker 的缓存判定兜住：内容确实没变
时第二条任务算出同一个 hash，直接复用结论，不会真的重跑评测器。

调用方还要注意两点：**触发要在 zip 落盘可下载之后**，不要和上传放在同一个
事务里；**我们返回非 200 不应该让上传失败**，重试几次仍失败就放弃，另配一个
对账任务扫"有包但没结果"的 skill。

### 只评 Skills 分类

管理系统还托管 Commands / Agents / Hooks，它们的包里没有 `SKILL.md`，
SkillEvaluator 评不了。**由触发方保证只对 Skills 分类调用**，因此契约里
没有 `category` 字段。

万一漏进来，失败是安全的：解归档时找不到 `SKILL.md`，任务报错落在
`error` 上，不会产出结果、更不会发出徽章。报错文案专门和"包损坏"区分开
（`不是一个可评测的 skill`），因为这两种情况的处理方式完全不同——
一个是找触发方，一个是找上传的用户。

### 资源 ID 每次上传都会变，所以结论按内容复用

管理系统里**重新上传会产生一个新的资源 ID**，版本号则是用户在上传表单里
单独填的一个自由文本（和包内 frontmatter 的版本不是一回事）。两件事合起来
意味着"传同一个 zip、只改版本号"是个很自然的操作。

如果缓存按 (skill_id, content_hash) 找，它跨上传永远不命中：同样的字节会被
评第二次，分数可能因为 LLM 或扫描器抖动而不同。用户看到的是"我什么都没改，
分数怎么变了"。

所以复用**按内容找，不看 skill_id**，命中就把结论克隆一份挂到新资源 ID 名下
（报告本来就按 content_hash 寻址，文件不复制）。代价是判据必须严，
三条缺一不可：

| 条件 | 不卡住会怎样 |
| --- | --- |
| `evaluator_version` 相同 | 换了评测器还给旧结论，正是"分数怎么变了"最难查的形态 |
| `policy_file_hash` 相同 | 策略是"起点不是定论"，调完不重评，新策略对存量 skill 不生效 |
| `incomplete_scans` 为空 | 把一次没跑全的扫描永久固化下来 |

`policy_file_hash` 是我们自己算的策略文件指纹。报告里的 `policy.digest` 来自
上游、跑完才知道，判不了"要不要跑"。迁移之前写下的结论没有这个指纹，
一律重跑——安全的方向。

**没**卡住的是外部扫描器自身的版本（semgrep / skillspector / gitleaks）。
它们漂移时复用会给出旧结论，逃生口是 `force=true`。判据全部由
`tests/test_result_reuse.py` 钉住。

### 内容下载

管理系统按**一个 skill 一个 zip** 提供内容。配上 URL 模板即可切换，
留空则退回本地目录（仅开发调试）：

```bash
SKILLPRISM_CONTENT_URL_TEMPLATE=http://<manager>/lingxi-manager/api/resource/{skill_id}/download
SKILLPRISM_CONTENT_TOKEN=<服务令牌>
```

`{skill_id}` 会被整体 URL 编码后替换——skill_id 形如 `team/name` 时不会
改变 URL 的路径结构。令牌作为 `Bearer` 发送。

只有 **worker** 需要能访问管理系统，API 进程不需要。这是刻意的隔离，
部署时可以据此收紧网络策略。

### 内容来源之二：skill 存在 GitLab 上

另一种接入是 skill 文件放在 GitLab 仓库里。配 GitLab 地址即可切换，
与上面的 zip 模板**互斥**（两个都配会在启动内容源时直接报错）：

```bash
SKILLPRISM_GITLAB_BASE_URL=https://gitlab.internal
SKILLPRISM_GITLAB_TOKEN=<只读令牌>
SKILLPRISM_GITLAB_TOKEN_HEADER=PRIVATE-TOKEN   # CI job token 改成 JOB-TOKEN
SKILLPRISM_GITLAB_DEFAULT_REF=main
```

**走归档接口，不 clone。** 取的是
`GET /api/v4/projects/:id/repository/archive.zip?sha=&path=`，拿到的仍然是
一个 zip，`archive.py` 那四道防线（zip slip、解压炸弹、符号链接、重复条目）
原样继续生效。clone 的话它们全部作废，还要额外面对 `.git/hooks`、
`.gitattributes` 的 filter driver、submodule 和没有上限的仓库体积——
物化层的路径校验挡不住其中任何一样，因为它们的路径本身合法。

**触发时怎么指定一个 skill。** 不动对外契约，把两个部分编进现有字段：

| 字段 | GitLab 接入下的含义 | 例 |
| --- | --- | --- |
| `skill_id` | `<项目路径>[:<仓库内子目录>]`，项目位置也接受数字项目 ID | `group/repo`、`group/repo:skills/log-triage`、`42:skills/foo` |
| `skill_version` | git ref（分支 / tag / commit sha），留空用 `DEFAULT_REF` | `v1.2.0`、`main`、40 位 sha |

冒号做分隔不会有歧义：GitLab 的项目路径只允许字母数字与 `_ - . /`。
带子目录时会把它作为 `path=` 传给归档接口，**只取那一个子树**——整仓取档
很容易撞上 512 条目 / 32MB 的上限，还会把同仓其他 skill 算进 `content_hash`。

Git 服务端打的包形如 `<repo>-<ref>-<sha>/<子目录>/SKILL.md`，前缀含 sha、
事先猜不出来，所以 `read_skill_zip` 多了个 `subdir` 参数：调用方声明布局，
对不上就报错，不去猜第二种解读。没有 `subdir` 时仍是原来的推断规则
（根上，或单层顶层目录），埋两层以上照旧拒收。

**Claude plugin 形态的仓库**（`.claude-plugin/` 加 `skills/`、`commands/`、
`agents/`、`hooks/`）不需要额外支持，`skills/` 底下正好就是 bundle 认的
布局。要点是 `skill_id` **指到 `skills` 那一层**，不是仓库根：

| plugin 仓形态 | `skill_id` | `bundle` |
| --- | --- | --- |
| 单 plugin 仓（根上 `.claude-plugin/`） | `group/repo:skills` | `true` |
| marketplace monorepo | `group/repo:plugins/<plugin>/skills` | `true` |
| 只评其中一个 skill | `group/repo:skills/<name>` | `false` |

指到仓库根会失败，因为 skill 埋在二级。这个错误在 plugin 场景下是必然会
撞上的，所以 `_no_members_error` 会把归档里能当 catalog 根的子目录列出来
（识别到 `.claude-plugin/` 时明说这是 plugin 仓）。**只改文案，不自动认
`skills/`**——布局由调用方声明、对不上就报错是解归档这一层的前提，自动推断
会让"`skill_id` 少写一层"重新变成静默评错一批东西。

**别把结论说成"这个 plugin 安全"。** 我们只取 `skills` 子树，`hooks/`、
`commands/`、`agents/` 一个字节都没下载，而 `hooks.json` 恰恰能在工具调用
前后跑任意命令。这和"只评 Skills 分类"是同一条立场，但 plugin 是整体安装
的，展示侧要说清楚徽章覆盖的是哪一部分。

另外预期 `reference_unresolved` 会变多：plugin 的 SKILL.md 里
`${CLAUDE_PLUGIN_ROOT}/scripts/foo.py` 这类写法很常见，它是路径样式文本，
正好撞上前面记的那类 SkillSpector 噪声。

**`skill_version` 的语义在两种接入下不同**，去重键也因此不同。zip 接入下它
是用户手填的标签、内容由 `skill_id` 决定，排队中换个版本号会折叠进同一条
任务并刷新标签；GitLab 接入下它是 ref，两个 ref 是两份内容，折叠等于宣称评
了 v1 却给出 v2 的结论，所以版本进去重键。开关是 `Settings.version_selects_content`，
由 `test_gitlab_mode_does_not_fold_across_refs` 钉住。

**404 的坑。** GitLab 对"有令牌但无权限"的项目也返回 404 而不是 403（防项目
枚举），而 404 在我们这里是不重试的终结态。所以令牌权限配漏了，表现是"这个
skill 不存在"。错误文案已经把两种可能都写上了，排查时先验令牌。
令牌过期是 401，归在可重试一侧——那要靠运维改配置，重试窗口正好留给这个修复。

令牌只需要 `read_repository`，不要给 `api`。**不要**把它放进
`SKILLPRISM_SCANNER_ENV`——那是注给评测子进程的，公司凭据不进那一层。

### 结果怎么回去

先轮询，不做 webhook：详情页渲染时直接调
`GET /api/skills/{skill_id}/evaluation`。webhook 要带重试、退避、签名和幂等，
为一个不拦截的徽章现在上不划算；真需要"评完立刻亮徽章"时，
再在触发 payload 里加可选的 `callback_url`。

这条判断的前提是**触发方和展示方都是管理系统**：用户提交时触发，之后打开
详情页时轮询，有一个"人来看"的时刻承接结果。GitLab 接入没有改变这个前提
（只是内容改从仓库取），所以仍然不需要异步通知。哪天触发方变成 CI 或
push webhook，就没有这个承接时刻了，那时才需要重新算这笔账。

列表页会出现 N 次单查（50 个 skill 打 50 次），需要时补一个批量查询接口。

**查结果必须带 `content_hash`。** 两个查询端点都接受它：

```
GET /api/skills/{skill_id}/evaluation?content_hash=<hash>
GET /api/skills/{skill_id}/report?content_hash=<hash>
```

拿 hash 的路径：提交返回 `task_id` → 轮 `GET /api/tasks/{task_id}`，worker
下载完内容就会把 `content_hash` 填上 → 管理系统把它和这次提交存在一起 →
详情页用它查。

**不带 hash 时退回"最近评完的那条"，这在 GitLab 接入下多半不是你要的。**
zip 接入每次上传换一个资源 ID，一个 `skill_id` 基本只有一条结论，取最近的
就是取那条；GitLab 下 `skill_id` 是仓库路径、长期不变，多个 ref 的结论堆在
同一个 ID 下，取到的是最近评完的那个 ref——详情页展示 v1.0.0，拿回来的可能
是 main 的分数。

**为什么不是按 ref 查。** 结论的身份是 `(skill_id, content_hash)`，
`skill_version` 只是标签、不参与去重，`save_result` 同 `(skill_id, content_hash)`
覆盖写。所以 tag `v1.0.0` 和分支 `main` 指向同一个 commit 时只有一行，标签是
后评的那个，按 ref 查会漏掉一份确实评过的内容。要按 ref 查得准得改结果表的
身份键，那和"结论按内容复用、不看 skill_id"的整个缓存设计冲突。

两个端点走同一条查找逻辑（`service.lookup_result`）。各写一遍的后果是"结论
查准了、点开报告却是另一份"——补 `content_hash` 之前的 `/report` 就是这样，
由 `test_report_follows_content_hash` 钉住。

### HTML 报告链接（`report_url`）

配上承载报告的地址，结论里的 `report_url` 就是一个能直接点开的链接：

```bash
SKILLPRISM_PUBLIC_BASE_URL=https://skillprism.internal
```

```json
{
  "skill_id": "group/repo:skills/log-triage",
  "content_hash": "sha256:9f2c…",
  "report_url": "https://skillprism.internal/api/skills/group/repo:skills/log-triage/report?content_hash=sha256:9f2c…"
}
```

**链接一定带 `content_hash`，而且带的是这条结论自己的那个。** 不带的话它的
含义是"这个 skill 最近评完的那条"，会随后续评测漂走——理由和上面那节完全
一样。区别在于链接会进管理系统的库、长期存在，到那时候"指错版本"比现在难查
得多。`test_report_url_pins_the_hash_of_the_row_it_came_with` 钉住这条。

**没配就是 `null`，不回落到存储地址。** 存储地址形如
`file:///var/lib/skillprism/reports/…`，对方拿到什么也做不了，还把我们的
服务器路径漏了出去。同理，结论没有报告时（例如 `error`）这个字段也是 null：
宁可没有链接，也不给一个点开是 404 的链接——后者会被当成服务坏了。

**地址由前置网关决定，进程无从得知**，所以这是一个显式配置项，不是从请求头
推断的。写错了不会有任何运行时异常，只会让每一条结论都带上一个点不开的链接，
所以启动时就校验它是不是 http(s) 地址。

**用独立域名承载，别和管理系统同域。** 报告是 SkillEvaluator 自生成的 HTML，
内容源头是用户上传的 skill，而它现在由最终用户直接点开。同域意味着它和管理
系统共享 cookie 与 localStorage；独立域名是这里唯一真正起隔离作用的一层。

响应带三个安全头（`api/app.py` 的 `REPORT_SECURITY_HEADERS`）：
`Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; …`、
`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`。要清楚它们
能做什么：CSP 挡住资源加载与请求发起（fetch / img / form），但 `unsafe-inline`
是报告自己的内联脚本要用的，所以**挡不住注入的脚本执行**，也挡不住它靠跳转
把数据带走。

这不是假想的风险。实测 skillevaluator 0.2.1 生成的报告：正文渲染做了 HTML
转义（`'` 输出成 `&#39;`），但报告末尾把整份 JSON **原样**嵌在
`<script id="report-data" type="application/json">` 里，那个位置没有转义——
只要有字面量 `</script` 进去，块就被提前闭合。试过的两条回显通道（死链目标、
代码块正文）都没能把它送进去：前者被 URL 编码成了 `%3C/script%3E`，后者压根
没进报告。**所以没有证明它可利用**，但挡住它的是上游各通道顺手做的归一化，
不是那个位置有转义，换个上游版本就不一定。

报告本身是完全自包含的（无外链资源、无 fetch/XHR，一个内联 style 加一个内联
script），这套 CSP 不影响它渲染——已在浏览器里实跑验证，暗色切换与导出按钮
都正常，无 CSP 违规。

另外报告页眉会显示物化目录的绝对路径（含 work 目录与任务 UUID）。那是上游
生成的，我们只在 DTO 的 finding 里做了归一化。不影响功能，但确实把内部路径
展示给了最终用户。

**链接发出去之后，报告就不能随便删了。** `storage.py` 里已经写了清理必须先做
引用计数；链接进了管理系统的库之后，这条约束从内部正确性变成用户可见的死链。
目前还没有清理机制，做的时候要一起算。

### 一组耦合 skill

一套研发工作流常拆成几个 skill，彼此有跨目录引用。这种情况**必须整套一起评**，
不能一个个来。

单独物化一个成员，它指向兄弟 skill 的相对链接全部变成死链——那是我们的物化
方式造成的误报，不是 skill 的问题，而且耦合越紧分数越难看。实测：单独评
`code-review` 报 `Dead link in SKILL.md: ../test-gen/SKILL.md` 并因此判 fail；
同样的内容整套一起评，`Code Integrity & Hygiene` 通过。两个方向都由
`tests/test_e2e_bundle.py` 钉住——只测后者的话，哪天物化改回单个也没人会发现。

**怎么触发。** 提交时把 `bundle` 置为 true，`skill_id` 指向装着多个 skill 的
父目录：

```json
{
  "skill_id": "group/repo:skills",
  "skill_name": "dev-workflow",
  "skill_version": "v1.2.0",
  "bundle": true
}
```

由触发方声明，我们**不看内容形态推断**：`skill_id` 少写一层子目录就会静默
变成评另一批东西。声明与内容不符时任务直接失败并说明原因（根上有 `SKILL.md`
= 这是单个 skill；没有任何含 `SKILL.md` 的一级子目录 = 不是一组）。

**怎么查结果。** 一次提交产出多条结果，每个成员一条，成员的 `skill_id` 就是
它单独提交时会用的那个：

| bundle | 成员结果的 skill_id |
| --- | --- |
| `group/repo:skills` | `group/repo:skills/code-review`、`group/repo:skills/test-gen` |

管理系统因此不需要第二套查询方式。bundle 的 ID 本身不挂结论——它不是一个 skill。

**底层是 SkillEvaluator 的 catalog 模式。** 对着一个"自身没有 `SKILL.md`、
但含 `*/SKILL.md`"的目录，它自动逐个跑完整 Tier 1，每个成员一份独立报告；
成员的兄弟目录留在盘上，跨 skill 链接因此能解析。不需要额外开关，我们只是
把物化根从单个 skill 目录换成了它们的父目录。不含 `SKILL.md` 的目录
（`shared/` 之类）不算成员，但会一并物化——耦合的典型形态就是几个 skill
共引一份约定。

**复用判据多了一维上下文。** 成员结论不只取决于它自己的字节：一组里改了 A，
B 的字节没变但结论可能变（B 引用 A 的文件，A 改名或删了 B 就多出死链）。所以
每条结论记 `context_hash`（整套的指纹，单独评的为 null），精确相等才复用，
**NULL 与非 NULL 不互认**——同一个 skill 单独评和在一组里评结论本来就不同。
报告地址也带上它，否则后跑的会覆盖先跑的，而先跑那次的结果行还指着这个地址。
这一条错了不会报警，只会给出一个看起来完全正常的过期结论，所以由
`tests/test_result_reuse.py` 单独钉住。

**上限**：一组最多 32 个成员、2048 个文件、128MB；单文件上限仍是 4MB
（一个文件多大和一组里有几个 skill 无关）。

**没有做的：让"调用"本身可见。** SKILL.md 的 frontmatter 里没有依赖字段
（上游 `models/skill.py` 只有 name/description/license/compatibility/metadata/
allowed-tools），SkillEvaluator 也没有跨 skill 依赖的校验器——没有"A 引用的 B
存不存在"、"有没有循环调用"这类检查。当前能看见的耦合只有 **markdown 链接**
（靠 `dead_links` 体现）；写在正文里的自然语言调用（"先跑 code-review"）对
底层完全不可见。要检查调用关系得先和上游定一个声明依赖的约定。

### 解归档是我们的安全边界

物化层防的是**路径**，不是**归档格式**。以下四类风险由
[`archive.py`](src/skillprism/archive.py) 处理，它挡不住：

| 风险 | 防线 |
| --- | --- |
| Zip slip（条目名含 `..` 或绝对路径） | 每个条目名都过 `safe_relative_path`，绝不用 `extractall` |
| 解压炸弹 | 声明大小预筛 + **按实际读出字节数**硬截断 + 压缩比上限（声明值会撒谎） |
| 符号链接条目 | 读 `external_attr` 的文件类型位，拒收非普通文件 |
| 重复条目名 | 后写覆盖前写可藏内容，直接拒绝 |

任何一条被触发就整体拒绝，不做部分解出——**残缺的 skill 评出来的结果比
评测失败更有害**，因为它看起来是有效的。

判定文件类型时注意：很多打包工具只写权限位、不写类型位（例如 `0o600`），
此时不能直接用 `S_ISREG` 判定，否则正常文件会被全部拒收。

### 归档布局

两种都支持，以 `SKILL.md` 的实际位置为准，不靠猜：

```
SKILL.md              ← 文件在根上
scripts/run.sh

my-skill/SKILL.md     ← 带一层顶层目录，会被剥掉
my-skill/scripts/run.sh
```

`SKILL.md` 埋在两层及以上目录下会被拒绝——那不是能安全推断的布局。

### 还要替换的一处

**`storage.py`** — 把 `LocalReportStorage` 换成对象存储实现。其余代码不需要改动。

## 测试

```bash
.venv/bin/python -m pytest
```

数据库地址由 `tests/conftest.py` 的 `db_url` 夹具统一提供，默认 SQLite。
设置 `SKILLPRISM_TEST_DATABASE_URL` 可让整套测试跑在 PostgreSQL 上：

```bash
SKILLPRISM_TEST_DATABASE_URL='postgresql+psycopg://user:pass@host:5432/postgres' \
  .venv/bin/python -m pytest -q
```

**这一步在发版前必须做。** 生产用 PG、开发用 SQLite，两者行为不同——最要紧
的是 `queue.claim_next`：它在 PG 上走 `SKIP LOCKED`、在 SQLite 上不加锁，
也就是说**生产真正执行的那条分支，本地开发一次都跑不到**。
`tests/test_queue_concurrency.py` 里有两条用例专门验证它，只在 PG 上生效。

端到端集成测试标记为 `e2e`，需要 `skillevaluator` 在 PATH 上、三个扫描器齐备，
缺失时自动跳过，因此裸环境下不会让 CI 变红。要显式排除：

```bash
.venv/bin/python -m pytest -m "not e2e"
```

它覆盖契约测试够不到的部分：CLI 的实际行为、外部扫描器的版本漂移、
物化布局造成的误报、问题定位是否泄露内部路径。

`tests/test_upstream_contract.py` 锁定 adapter 依赖的上游 JSON 结构，
用一份真实报告做 fixture。上游一旦改 schema，这里先红，而不是线上解析出错。
其中比对 `Severity` 枚举的用例需要安装上游基础包（`uv pip install -e ".[contract]"`），
未安装时自动跳过。

## Embedding shim（`/embed/v1/embeddings`）

火山方舟 embeddings 接口**单请求最多 10 条输入**，而 SkillEvaluator 把批大小
硬编码为 64（`embedding/registry.py:57` 与 `constants.py:255`，两处都是模块级
常量，没有环境变量或 CLI 参数可调）。直连会稳定失败：

```
InvalidParameter: Embeddings API input limit exceeded: max 10, got 15
```

`embedding_shim.py` 对上游装成一个正常的 OpenAI 端点：接收任意大小的请求，
按 10 条切片、并发调方舟、合并结果。不需要 fork 上游。

Tier 2 的 worker 这样配置——`BASE_URL` 指向 shim 而不是方舟：

```bash
SKILL_EVAL_EMBEDDING_PROVIDER=openai-compatible
SKILL_EVAL_EMBEDDING_BASE_URL=http://127.0.0.1:8000/embed/v1
SKILL_EVAL_EMBEDDING_MODEL=doubao-embedding-vision
SKILL_EVAL_EMBEDDING_API_KEY=<ARK_API_KEY>
```

调用方带来的 `Authorization` 会被转发给方舟，没带时回落到 `SKILLPRISM_ARK_API_KEY`。
**shim 会转发凭据，不要暴露到公网**，只在 worker 可达的内网或本机监听。

两个正确性要点，都有测试覆盖：

- **index 必须跨分片重编号。** SkillEvaluator 严格校验响应的 `index` 为
  0..N-1 的唯一整数，不重复、不缺失，否则直接报错。
- **任一分片失败即整体失败。** 返回数量不符的部分结果，只会让上游在更远的
  地方以更难查的形式报错。

已实测：15 个 skill 直连方舟报 400、catalog 建不出来；经 shim 则
`[PASS]`，catalog 15 条 / 2048 维正常生成。

## 尚未实现

- **Tier 2 编排**：shim 已就绪，但 catalog 分片构建与夜间重建调度尚未实现。
  单个 catalog 建库上限 256 个 skill，实测单条记录约 51 KB。
  `queue.py` 的 `index` 队列与 DTO 的 `tiers.tier2` 已预留。
- **Tier 3**：需要 Docker/K8s 沙箱、agent 凭据、评测预算。`sandbox` 队列与 `tiers.tier3` 已预留。
- **扫描器版本未纳入复用判据**：见上面「结论按内容复用」。
- **批量查询结果**：列表页按 skill_id 逐个查会打 N 次，需要时补。
- **跨 skill 调用关系的校验**：见上面「一组耦合 skill」的最后一段，
  需要先和上游定一个声明依赖的约定。
- **结果回调**：当前只支持轮询。前提是触发方与展示方都是管理系统；触发方
  改成 CI / push webhook 时需要重新评估，见上面「结果怎么回去」。
- **鉴权**：API 尚无认证，接入前需补。配了 `PUBLIC_BASE_URL` 之后
  `/api/skills/*/report` 需要被最终用户的浏览器访问到，网关上要单独放行
  这一条路径，其余端点仍然只对管理系统开放。
