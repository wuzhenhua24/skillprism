# Skill 评测服务

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
.venv/bin/uvicorn skill_eval_service.api.app:app --reload
```

worker 另起一个进程：

```bash
.venv/bin/skill-eval-worker
```

部署到服务器见 [docs/deployment.md](docs/deployment.md)（Ubuntu，非容器）。

## 模块

| 模块 | 职责 |
| --- | --- |
| `materialize.py` | 把存储中的内容还原成目录树。**唯一把外部数据写盘的地方**，路径校验在此 |
| `runner.py` | 子进程调用 skillevaluator CLI，启动自检，退出码语义 |
| `adapter.py` | 上游 JSON → 本服务 DTO。**唯一了解上游 schema 的模块** |
| `schemas.py` | 对外契约。管理系统只看这一层 |
| `content.py` | 内容来源协议。接入时替换实现 |
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

所以物化目录名取 `skill_id` 的最后一段、外面套一层 `skills/`。
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

管理系统按**一个 skill 一个 zip** 的方式提供内容。配上 URL 模板即可切换，
留空则退回本地目录（仅开发调试）：

```bash
SES_CONTENT_URL_TEMPLATE=https://skills.internal/api/skills/{skill_id}/download
SES_CONTENT_TOKEN=<服务令牌>
```

`{skill_id}` 会被整体 URL 编码后替换——skill_id 形如 `team/name` 时不会
改变 URL 的路径结构。令牌作为 `Bearer` 发送。

### 解归档是我们的安全边界

物化层防的是**路径**，不是**归档格式**。以下四类风险由
[`archive.py`](src/skill_eval_service/archive.py) 处理，它挡不住：

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

调用方带来的 `Authorization` 会被转发给方舟，没带时回落到 `SES_ARK_API_KEY`。
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
- **迁移**：当前用 `Base.metadata.create_all`，接正式库前换 Alembic。
- **鉴权**：API 尚无认证，接入前需补。
