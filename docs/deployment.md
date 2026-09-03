# SkillPrism 部署（Ubuntu）

非容器部署：两个 systemd 服务跑在同一台机器上，共用一份代码和一个 SQLite 库。
适用于当前阶段——服务定位是"只展示不拦截"，可用性要求低，量级也小。

已在 Ubuntu 22.04 / 24.04 (amd64) 验证过路径与包的可用性。

## 部署形态

```
skillprism-api      uvicorn，对管理系统提供 HTTP 接口
skillprism-worker   轮询任务表，调用 skillevaluator CLI 跑评测
        ↓ 共用
/var/lib/skillprism/   SQLite 库、报告、物化临时目录
```

两个进程必须能读写同一个 `/var/lib/skillprism`——API 要回读 worker 写的报告。

### 现阶段的硬约束

| 约束 | 原因 | 何时要改 |
| --- | --- | --- |
| **API 无鉴权** | 尚未实现 | 上线前必须补，见文末 |
| **API 与 worker 必须同机** | 报告存本地文件系统，API 靠读同一个目录返回报告 | 换对象存储后可分离 |

因为 API 目前没有鉴权，**不要把它暴露到机器之外**——先绑 `127.0.0.1`，
由前置的网关或反向代理承担鉴权。

## 一、系统准备

```bash
sudo apt update && sudo apt install -y curl ca-certificates
```

不需要 `build-essential`：所有带 C 扩展的依赖（`yara-python`、`cffi`、
`cryptography`）在 amd64/arm64 上都有 manylinux 预编译 wheel。

PostgreSQL。**生产从第一天就用 PG**，不走 SQLite——这样将来不存在把真实数据
从 SQLite 迁到 PG 的问题（那个迁移有几个不好绕的坑：布尔与时间戳的类型表示、
自增序列要重置等）。

**已经有 PG 实例就跳过安装**，只需要拿到五个连接参数（地址、端口、库名、
用户名、密码）并建好库和角色。版本没有下限之外的要求：本服务只用到
`SKIP LOCKED`（PG 9.5+），测试夹具建删临时库时用到
`DROP DATABASE ... WITH (FORCE)`（PG 13+）。驱动侧 `psycopg[binary]` 自带
libpq，`.venv/bin/python -c "import psycopg; print(psycopg.pq.version())"`
可以看到它的版本。

这台机器上还没有 PG 时才装：

```bash
sudo apt install -y postgresql
```

Ubuntu 源里的版本：24.04 是 PG 16，22.04 是 PG 14。要更新的版本走 PGDG 官方源。

建库和角色。下面这两条走的是 apt 包的布局（`postgres` 系统用户 + peer 认证
的本地 socket）；**独立安装或远程实例未必是这个样子**，那就用你自己的管理
账号连上去执行等价的 `CREATE ROLE` / `CREATE DATABASE`：

```bash
sudo -u postgres createuser skillprism --pwprompt --createdb
sudo -u postgres createdb skillprism --owner skillprism
```

`--createdb` 不是给服务用的——服务只读写自己那个库，不建库。它是给
**测试**用的：`tests/conftest.py` 每个用例会建一个随机命名的临时库、跑完即删，
没有这个权限，第九节那趟 PG 测试第一条就会报权限不足。不想给生产账号这个
权限的话，另建一个测试专用账号。

连不上或不确定参数时，这几条能问出来：

```bash
psql "<你的连接串>" -c '\conninfo'          # 地址、端口、库名、用户名
psql "<你的连接串>" -c 'show port'
psql "<你的连接串>" -c 'select version()'
```

建一个专用账号和目录：

```bash
sudo useradd --system --home-dir /var/lib/skillprism --create-home --shell /usr/sbin/nologin skillprism
sudo mkdir -p /opt/skillprism /etc/skillprism
sudo chown -R skillprism:skillprism /var/lib/skillprism
```

## 二、安装 uv

服务和各个工具都用 uv 装，不依赖系统 Python 的版本——本项目要求 3.12/3.13，
而 Ubuntu 22.04 自带的是 3.10。uv 会自己管理 Python，省掉 deadsnakes 之类的源。

```bash
sudo -u skillprism sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
```

装到 `/var/lib/skillprism/.local/bin/uv`。下文统一用绝对路径，避免 PATH 问题。

## 三、安装评测工具链

以 `skillprism` 身份安装，四个工具都会落在
`/var/lib/skillprism/.local/bin/` 下。

```bash
UV=/var/lib/skillprism/.local/bin/uv

sudo -u skillprism $UV tool install --python 3.13 \
  "skillevaluator[security] @ git+https://github.com/NVIDIA/SkillEvaluator.git"

sudo -u skillprism $UV tool install semgrep

sudo -u skillprism $UV tool install \
  "skillspector @ git+https://github.com/NVIDIA/SkillSpector.git@v2.9.6"
```

> **SkillSpector 必须 pin 在 v2.9.6。** 装 latest 会让安全扫描静默降级为
> incomplete。原因见 [README](../README.md)
> 与 [docs/upstream/pr112-comment.md](upstream/pr112-comment.md)。

gitleaks 只发布二进制，按架构选：

```bash
GITLEAKS_VERSION=8.30.1
ARCH=x64                     # arm64 机器改成 arm64
curl -sSL -o /tmp/gitleaks.tar.gz \
  "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${ARCH}.tar.gz"
sudo tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks
sudo chmod +x /usr/local/bin/gitleaks
rm /tmp/gitleaks.tar.gz
```

验证四个都在：

```bash
sudo -u skillprism env PATH=/var/lib/skillprism/.local/bin:/usr/local/bin:/usr/bin:/bin \
  sh -c 'skillevaluator --version && semgrep --version && gitleaks version && skillspector --version'
```

## 四、部署服务代码

```bash
sudo git clone <仓库地址> /opt/skillprism
sudo chown -R skillprism:skillprism /opt/skillprism

cd /opt/skillprism
sudo -u skillprism /var/lib/skillprism/.local/bin/uv venv --python 3.13 .venv
sudo -u skillprism /var/lib/skillprism/.local/bin/uv pip install --python .venv/bin/python -e ".[pg]"
```

## 五、配置

配置走 systemd 的 `EnvironmentFile`，不用项目里的 `.env`——服务由 systemd
启动，环境变量直接注入即可。

```bash
sudo tee /etc/skillprism/service.env > /dev/null <<'EOF'
SKILLPRISM_DATABASE_URL=postgresql+psycopg://skillprism:<密码>@127.0.0.1:5432/skillprism
SKILLPRISM_REPORT_ROOT=/var/lib/skillprism/reports
SKILLPRISM_WORK_ROOT=/var/lib/skillprism/work
SKILLPRISM_LOCAL_SKILLS_ROOT=/var/lib/skillprism/skills
SKILLPRISM_POLICY_FILE=/opt/skillprism/profiles/internal.yaml
SKILLPRISM_SKILLEVALUATOR_BIN=/var/lib/skillprism/.local/bin/skillevaluator
SKILLPRISM_EVAL_TIMEOUT_SECONDS=600
SKILLPRISM_REQUIRE_SCANNERS=true
# 额外传给评测子进程的环境变量，K=V 逗号分隔。留空即可，见下文说明
SKILLPRISM_SCANNER_ENV=
SKILLPRISM_POLL_INTERVAL_SECONDS=2
SKILLPRISM_MAX_ATTEMPTS=3
SKILLPRISM_RETRY_BACKOFF_SECONDS=30
SKILLPRISM_RETRY_BACKOFF_MAX_SECONDS=300

# 内容来源：管理系统的 zip 下载接口（待对方提供后填写）
SKILLPRISM_CONTENT_URL_TEMPLATE=
SKILLPRISM_CONTENT_TOKEN=
SKILLPRISM_CONTENT_TIMEOUT_SECONDS=60
SKILLPRISM_MAX_DOWNLOAD_BYTES=67108864
EOF

sudo chown root:skillprism /etc/skillprism/service.env
sudo chmod 640 /etc/skillprism/service.env
```

**这个文件里的环境变量到不了扫描器那一层。** 评测跑在子进程里，为了不把
公司凭据带进去，它只拿到 `PATH`、`HOME` 和一份写死的扫描器环境
（`runner.SCANNER_ENV_DEFAULTS`）。要给 semgrep 之类加开关，只能写
`SKILLPRISM_SCANNER_ENV`，例如 `SEMGREP_VERSION_CHECK_TIMEOUT=1`。
格式写错会在启动时直接报错，不会静默丢掉。

出网受限的机器不需要额外配置：semgrep 的版本检查与 metrics 上报已经默认关掉
（`SEMGREP_ENABLE_VERSION_CHECK=0`、`SEMGREP_SEND_METRICS=off`）。这两件事
对内网批量评测只有坏处——拖慢甚至挂住评测，还把被扫代码的相关数据发到外部。

可重试的失败（下载的网络/5xx 故障、评测器退出码 3 或超时）按
`RETRY_BACKOFF_SECONDS * 2^(n-1)` 退避，封顶 `RETRY_BACKOFF_MAX_SECONDS`，
`MAX_ATTEMPTS` 次之后终结。默认值下三次尝试摊在约 90 秒里——**别把退避调成 0**：
管理系统重启一次就不止几秒，没有退避的话三次尝试会在一个轮询周期内烧光，
上游的短暂故障会变成任务的永久失败。

`SKILLPRISM_LOCAL_SKILLS_ROOT` 只在 `SKILLPRISM_CONTENT_URL_TEMPLATE` 为空时生效，
是第七节冒烟测试走的那条路。**必须显式给**：它的默认值是相对路径 `./var/skills`，
会跟着 unit 里的 `WorkingDirectory` 落到 `/opt/skillprism/var/skills`，而数据都在
`/var/lib/skillprism` 下——不设它，第七节建的 demo 目录 worker 根本看不到。

`SKILLPRISM_CONTENT_TOKEN` 是凭据，所以这个文件是 `0640 root:skillprism`——
服务读得到，其他账号读不到。

连接串里有数据库密码，这也是这个文件必须是 `0640` 的原因之一。

上线前**必须**改 `profiles/internal.yaml` 里的作者邮箱域名，
默认是 `example.com` 占位，不改会让所有真实 skill 都报作者检查失败。

配置就绪后初始化数据库结构。**服务不会自动建表**，这一步不做的话
两个进程都会拒绝启动：

```bash
cd /opt/skillprism
sudo -u skillprism env $(grep SKILLPRISM_DATABASE_URL /etc/skillprism/service.env) \
  .venv/bin/alembic upgrade head
```

## 六、systemd

两个 unit 共用同一份环境配置。

```bash
sudo tee /etc/systemd/system/skillprism-api.service > /dev/null <<'EOF'
[Unit]
Description=SkillPrism API
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=skillprism
Group=skillprism
WorkingDirectory=/opt/skillprism
EnvironmentFile=/etc/skillprism/service.env
Environment=PATH=/var/lib/skillprism/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/skillprism/.venv/bin/uvicorn skillprism.api.app:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

# 服务会解开外部提交的归档，收紧运行环境
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/skillprism
PrivateDevices=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/skillprism-worker.service > /dev/null <<'EOF'
[Unit]
Description=SkillPrism Worker
After=network-online.target skillprism-api.service
Wants=network-online.target

[Service]
Type=exec
User=skillprism
Group=skillprism
WorkingDirectory=/opt/skillprism
EnvironmentFile=/etc/skillprism/service.env
Environment=PATH=/var/lib/skillprism/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/skillprism/.venv/bin/skillprism-worker
Restart=on-failure
RestartSec=10

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/skillprism
PrivateDevices=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now skillprism-api skillprism-worker
```

`PATH` 必须显式给：worker 用它找 skillevaluator，skillevaluator 用它找三个扫描器。
systemd 默认的 PATH 不含 `/var/lib/skillprism/.local/bin`。

**worker 只能起一个实例**，别做多副本，原因见开头的约束表。

## 七、验证

```bash
systemctl status skillprism-api skillprism-worker
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
```

期望：

```json
{
  "status": "ok",
  "skillevaluator": "/var/lib/skillprism/.local/bin/skillevaluator",
  "version": "skillevaluator, version 0.2.1",
  "missing_scanners": []
}
```

`missing_scanners` 非空就说明扫描器没装全，此时评测结果的安全部分是空的。
worker 在 `SKILLPRISM_REQUIRE_SCANNERS=true` 下会**直接拒绝启动**——这是有意为之，
带病运行会产出看起来合格、实际没扫过的结果。

跑一次真实评测（内容来源接上之前，可以先用本地目录验证链路）。
下面的目录必须与第五节的 `SKILLPRISM_LOCAL_SKILLS_ROOT` 一致：

```bash
sudo -u skillprism mkdir -p /var/lib/skillprism/skills/demo
sudo -u skillprism tee /var/lib/skillprism/skills/demo/SKILL.md > /dev/null <<'EOF'
---
name: demo
description: A deployment smoke test skill. Use when verifying the evaluation pipeline after deployment.
---

# Demo
EOF

curl -s -X POST http://127.0.0.1:8000/api/evaluations \
  -H 'Content-Type: application/json' \
  -d '{"skill_id":"demo","skill_name":"demo","skill_version":"1.0.0"}'

sleep 20
curl -s http://127.0.0.1:8000/api/skills/demo/evaluation | python3 -m json.tool
```

`skill_name` 必填，且要与 `SKILL.md` 里 frontmatter 的 `name` 一致——物化目录用它
命名，对不上会多出一条 `SCHEMA.name_consistency`。

`status` 只要不是 `error` 就说明链路通了。若为 `incomplete`，
看 `evaluator.incomplete_scans` 里是谁。

## 八、日常运维

**看日志**

```bash
journalctl -u skillprism-worker -f
journalctl -u skillprism-api --since "1 hour ago"
```

**升级服务代码**

```bash
cd /opt/skillprism
sudo -u skillprism git pull
sudo -u skillprism /var/lib/skillprism/.local/bin/uv pip install --python .venv/bin/python -e .
sudo -u skillprism env $(grep SKILLPRISM_DATABASE_URL /etc/skillprism/service.env) \
  .venv/bin/alembic upgrade head
sudo systemctl restart skillprism-api skillprism-worker
```

`alembic upgrade head` 这一步不能漏。升级前先备份数据库文件——
迁移可能改表结构，出问题时需要能退回去。

**升级 skillevaluator**——先在非生产环境跑 e2e 测试，尤其确认
`test_security_scan_completes` 仍然通过；升级后存量 skill 的评分可能整体漂移，
建议先跑一批做对比。

**备份**：PostgreSQL 库 `skillprism` 是全部结果数据，用 `pg_dump` 备份。
`reports/` 可按 `content_hash` 重新生成，丢了不致命。

**报告清理：当前有意不做。** 实测单次评测产出约 271 KB（HTML 240 KB +
JSON 31 KB）。按 2000 个 skill、每个每月评测 4 次估算，一年约 26 GB——
在当前规模下不值得为它引入一套 GC。等实际体积接近磁盘容量再处理。

将来实现清理时有三个坑，先记在这里：

1. **必须做引用计数。** 报告路径只由 `content_hash` 决定、不含 `skill_id`，
   内容相同的两个 skill 共用同一个目录。按 skill 删会误删别人的报告。
2. **DB 摘要和报告文件要分开对待。** 结果行只有几 KB 且是趋势视图的数据源，
   应当长留；240 KB 的 HTML 只有最近的有人看，可以短留。
3. **孤儿目录需要对账。** worker 写完报告但 DB 事务失败会留下无引用的目录，
   扫描清理时要留宽限期，避免删掉正在写入的那一份。

顺带：报告 gzip 后只有原来的 11%（271 KB → 29 KB）。真到了要省空间那天，
先做压缩比做删除划算得多。

`work/` 由 worker 自己清理，正常情况下应当是空的；持续有残留说明 worker 异常退出过。

## 九、为什么开发用 SQLite、生产用 PostgreSQL

**生产从第一天就是 PostgreSQL**，不经过 SQLite。这样永远不会遇到把真实数据
从 SQLite 迁到 PG 的问题——那个迁移有几个不好绕的坑：SQL dump 会把 SQLite 的
0/1 布尔和字符串时间戳原样带过去、`evaluation_detail` 的自增序列导完要
`setval` 否则主键冲突、`sa.JSON` 在 PG 上是 `JSON` 而非 `JSONB`。绕开这些的
最省事办法就是一开始就用 PG。

**开发仍然用 SQLite**：跑得快、每个测试一个独立文件、无需本地装数据库。

### 但两者会分叉，这是要正视的代价

| | SQLite（开发） | PostgreSQL（生产） |
| --- | --- | --- |
| 任务领取 | 不加锁 | `SELECT ... FOR UPDATE SKIP LOCKED` |
| 类型严格性 | 宽松，Integer 列塞字符串也收 | 严格，直接报错 |
| 并发写 | 串行化 | 真 MVCC |
| Alembic 迁移 | batch 模式（建新表拷数据） | 原生 ALTER |

第一行最要紧：`claim_next` 在 PG 上走加锁分支、在 SQLite 上不加锁，
**生产真正执行的是前者，而本地开发永远跑不到它**。那段代码决定两个 worker
会不会抢到同一个任务。

### 所以发版前必须用 PG 跑一遍测试

测试的数据库地址由 `tests/conftest.py` 的 `db_url` 夹具统一提供，默认 SQLite，
设置 `SKILLPRISM_TEST_DATABASE_URL` 后改用 PostgreSQL。指向的库只用于建/删临时测试库，
本身不会被改动；每个测试用一个随机命名的临时库，跑完即删，因此多人并跑
不会互相污染。

执行这一步的账号需要 **CREATEDB** 权限（夹具要建临时库），见第一节。
连接串指向的库（下面用的是 `postgres`）本身不会被改动。

在测试机上执行：

```bash
cd /opt/skillprism
sudo -u skillprism env \
  SKILLPRISM_TEST_DATABASE_URL='postgresql+psycopg://skillprism:<密码>@127.0.0.1:5432/postgres' \
  .venv/bin/python -m pytest -q
```

关键是 `tests/test_queue_concurrency.py`：其中两条用例在 SQLite 上会跳过，
只有在 PG 上才真正验证"两个 worker 不会抢到同一个任务"。它们跳过时会打印
原因，不会伪装成通过。

同一文件里的 `test_configured_backend_actually_engages` 是另一道防线——
设了 `SKILLPRISM_TEST_DATABASE_URL` 却仍跑在 SQLite 上时它会失败，避免出现
"以为验证过了、其实一直在跑 SQLite"这种最坏的情况。连不上 PG 时测试会
直接 ERROR，也不会静默跳过。

### 多 worker

跑在 PG 上之后，多 worker 实例在数据库层面是安全的（`SKIP LOCKED` 保证不会
重复领取）。但当前仍受另一条约束限制：报告存本地文件系统，API 要能读到
worker 写的报告，所以两者必须同机。要真正横向扩展需要先换对象存储。

## 十、故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| worker 启动即退出，日志写"启动自检未通过" | 扫描器缺失或 PATH 不对 | 检查 unit 里的 `Environment=PATH`，确认四个工具都能找到 |
| `status` 一直是 `incomplete`，`incomplete_scans` 含 `skillspector` | 装了 2.10.0 及以上版本 | 降回 v2.9.6 |
| 所有 skill 都报 `SCHEMA.author_missing` | `internal.yaml` 的邮箱域名还是 `example.com` | 改成公司域名 |
| 出现 `name_consistency` | 管理系统的上传校验失效了 | 它在上传口就卡住"包名与文件内技能名一致"，所以这条**正常情况下不可能报**。报了就是那道校验被绕过、被放宽，或两边的归一化规则不同（大小写、空格、Unicode），先查上传侧 |
| 每个 skill 都多出 `folder_hierarchy` | 物化布局异常 | 物化时没套上 `skills/` 那层，见 materialize.py |
| 任务报"不是一个可评测的 skill" | 多半是传了非 Skills 分类的包 | Commands / Agents / Hooks 里没有 `SKILL.md`。触发方应当只对 Skills 分类调用，找对接方查触发侧的过滤；不是用户的包坏了 |
| 任务 error，日志写"取不到内容：找不到 skill" | 走本地目录时 `SKILLPRISM_LOCAL_SKILLS_ROOT` 与实际目录不一致 | 该变量不设会默认成相对路径 `./var/skills`，即 `/opt/skillprism/var/skills`。按第五节显式配成绝对路径 |
| 提交返回 422 `skill_name Field required` | 触发请求少了必填字段 | `skill_name` 是管理系统里登记的技能名，见第七节与 README 的接口说明 |
| 评测长时间不返回、最终超时，机器出网受限 | 扫描器在联网（版本检查、metrics 上报） | 本服务已默认关掉 semgrep 的这两项。仍然卡就用 `SKILLPRISM_SCANNER_ENV` 加开关，**不要**去写 `~/.semgrep/settings.yml`——`disable_version_check` 不是 semgrep 认的键，写了也不生效 |
| `/healthz` 的 `version` 是 `null`，但服务能起 | 取版本的子进程超时或失败 | 看 worker/api 日志里 `skillevaluator --version` 那条 warning |
| 任务卡在 `queued` | worker 没运行 | `systemctl status skillprism-worker` |
| 启动报"数据库结构尚未初始化" | 没跑迁移 | `alembic upgrade head`，见第五节 |
| 连不上数据库 | 连接串、密码或 PG 服务 | `sudo -u skillprism psql "$SKILLPRISM_DATABASE_URL" -c 'select 1'` |
| 报告接口 404 但评测显示成功 | API 与 worker 不在同一文件系统 | 当前形态要求两者同机 |
| 下载内容失败 | 区分两类：`SkillNotFoundError`（404 或归档解不出，不重试，直接 `failed`）与 `ContentFetchError`（5xx/网络，退避重试至 `MAX_ATTEMPTS`） | 看 worker 日志里的具体异常；任务的 `error` 字段也会带上它 |
| 任务在 `queued` 与失败之间来回，`attempts` 在涨 | 正在退避重试 | `GET /api/tasks/{task_id}` 看 `error` 和 `next_attempt_at`——前者是上次失败的原因，后者是下次重试时间 |

## 十一、上生产前必须补的

按优先级：

1. **API 鉴权**——目前完全开放。在此之前只能绑 `127.0.0.1`，靠前置网关鉴权。
2. ~~数据库迁移~~——已引入 Alembic。
3. ~~报告保留策略~~——当前有意不做，见第八节的说明与三个坑。
4. **监控**——目前只有 journald 日志，没有指标。至少要能看到任务失败率和积压量。
