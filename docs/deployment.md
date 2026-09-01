# 部署（Ubuntu）

非容器部署：两个 systemd 服务跑在同一台机器上，共用一份代码和一个 SQLite 库。
适用于当前阶段——服务定位是"只展示不拦截"，可用性要求低，量级也小。

已在 Ubuntu 22.04 / 24.04 (amd64) 验证过路径与包的可用性。

## 部署形态

```
skill-eval-api      uvicorn，对管理系统提供 HTTP 接口
skill-eval-worker   轮询任务表，调用 skillevaluator CLI 跑评测
        ↓ 共用
/var/lib/skill-eval/   SQLite 库、报告、物化临时目录
```

两个进程必须能读写同一个 `/var/lib/skill-eval`——API 要回读 worker 写的报告。

### 三条现阶段的硬约束

| 约束 | 原因 | 何时要改 |
| --- | --- | --- |
| **只能跑一个 worker 实例** | SQLite 不支持 `SELECT ... FOR UPDATE SKIP LOCKED`，多实例会抢同一个任务 | 换 PostgreSQL 后可多开，判据见第九节 |
| **API 与 worker 必须同机** | 报告存本地文件系统，API 靠读同一个目录返回报告 | 换对象存储后可分离 |
| **API 无鉴权** | 尚未实现 | 上线前必须补，见文末 |

因为 API 目前没有鉴权，**不要把它暴露到机器之外**——先绑 `127.0.0.1`，
由前置的网关或反向代理承担鉴权。

## 一、系统准备

```bash
sudo apt update && sudo apt install -y curl ca-certificates
```

不需要 `build-essential`：所有带 C 扩展的依赖（`yara-python`、`cffi`、
`cryptography`）在 amd64/arm64 上都有 manylinux 预编译 wheel。

建一个专用账号和目录：

```bash
sudo useradd --system --home-dir /var/lib/skill-eval --create-home --shell /usr/sbin/nologin skilleval
sudo mkdir -p /opt/skill-eval-service /etc/skill-eval
sudo chown -R skilleval:skilleval /var/lib/skill-eval
```

## 二、安装 uv

服务和各个工具都用 uv 装，不依赖系统 Python 的版本——本项目要求 3.12/3.13，
而 Ubuntu 22.04 自带的是 3.10。uv 会自己管理 Python，省掉 deadsnakes 之类的源。

```bash
sudo -u skilleval sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
```

装到 `/var/lib/skill-eval/.local/bin/uv`。下文统一用绝对路径，避免 PATH 问题。

## 三、安装评测工具链

以 `skilleval` 身份安装，四个工具都会落在
`/var/lib/skill-eval/.local/bin/` 下。

```bash
UV=/var/lib/skill-eval/.local/bin/uv

sudo -u skilleval $UV tool install --python 3.13 \
  "skillevaluator[security] @ git+https://github.com/NVIDIA/SkillEvaluator.git"

sudo -u skilleval $UV tool install semgrep

sudo -u skilleval $UV tool install \
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
sudo -u skilleval env PATH=/var/lib/skill-eval/.local/bin:/usr/local/bin:/usr/bin:/bin \
  sh -c 'skillevaluator --version && semgrep --version && gitleaks version && skillspector --version'
```

## 四、部署服务代码

```bash
sudo git clone <仓库地址> /opt/skill-eval-service
sudo chown -R skilleval:skilleval /opt/skill-eval-service

cd /opt/skill-eval-service
sudo -u skilleval /var/lib/skill-eval/.local/bin/uv venv --python 3.13 .venv
sudo -u skilleval /var/lib/skill-eval/.local/bin/uv pip install --python .venv/bin/python -e .
```

初始化数据库结构。**服务不会自动建表**，这一步不做的话两个进程都会拒绝启动：

```bash
cd /opt/skill-eval-service
sudo -u skilleval env SES_DATABASE_URL=sqlite:////var/lib/skill-eval/skill-eval.db \
  .venv/bin/alembic upgrade head
```

## 五、配置

配置走 systemd 的 `EnvironmentFile`，不用项目里的 `.env`——服务由 systemd
启动，环境变量直接注入即可。

```bash
sudo tee /etc/skill-eval/service.env > /dev/null <<'EOF'
SES_DATABASE_URL=sqlite:////var/lib/skill-eval/skill-eval.db
SES_REPORT_ROOT=/var/lib/skill-eval/reports
SES_WORK_ROOT=/var/lib/skill-eval/work
SES_POLICY_FILE=/opt/skill-eval-service/profiles/internal.yaml
SES_SKILLEVALUATOR_BIN=/var/lib/skill-eval/.local/bin/skillevaluator
SES_EVAL_TIMEOUT_SECONDS=600
SES_REQUIRE_SCANNERS=true
SES_POLL_INTERVAL_SECONDS=2
SES_MAX_ATTEMPTS=3

# 内容来源：管理系统的 zip 下载接口（待对方提供后填写）
SES_CONTENT_URL_TEMPLATE=
SES_CONTENT_TOKEN=
SES_CONTENT_TIMEOUT_SECONDS=60
SES_MAX_DOWNLOAD_BYTES=67108864
EOF

sudo chown root:skilleval /etc/skill-eval/service.env
sudo chmod 640 /etc/skill-eval/service.env
```

`SES_CONTENT_TOKEN` 是凭据，所以这个文件是 `0640 root:skilleval`——
服务读得到，其他账号读不到。

注意 SQLite 的绝对路径是**四个斜杠**：`sqlite:////var/lib/...`
（`sqlite://` + `/var/...`）。写成三个会被当成相对路径。

上线前**必须**改 `profiles/internal.yaml` 里的作者邮箱域名，
默认是 `example.com` 占位，不改会让所有真实 skill 都报作者检查失败。

## 六、systemd

两个 unit 共用同一份环境配置。

```bash
sudo tee /etc/systemd/system/skill-eval-api.service > /dev/null <<'EOF'
[Unit]
Description=Skill 评测服务 API
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=skilleval
Group=skilleval
WorkingDirectory=/opt/skill-eval-service
EnvironmentFile=/etc/skill-eval/service.env
Environment=PATH=/var/lib/skill-eval/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/skill-eval-service/.venv/bin/uvicorn skill_eval_service.api.app:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

# 服务会解开外部提交的归档，收紧运行环境
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/skill-eval
PrivateDevices=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/skill-eval-worker.service > /dev/null <<'EOF'
[Unit]
Description=Skill 评测服务 Worker
After=network-online.target skill-eval-api.service
Wants=network-online.target

[Service]
Type=exec
User=skilleval
Group=skilleval
WorkingDirectory=/opt/skill-eval-service
EnvironmentFile=/etc/skill-eval/service.env
Environment=PATH=/var/lib/skill-eval/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/skill-eval-service/.venv/bin/skill-eval-worker
Restart=on-failure
RestartSec=10

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/skill-eval
PrivateDevices=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now skill-eval-api skill-eval-worker
```

`PATH` 必须显式给：worker 用它找 skillevaluator，skillevaluator 用它找三个扫描器。
systemd 默认的 PATH 不含 `/var/lib/skill-eval/.local/bin`。

**worker 只能起一个实例**，别做多副本，原因见开头的约束表。

## 七、验证

```bash
systemctl status skill-eval-api skill-eval-worker
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
```

期望：

```json
{
  "status": "ok",
  "skillevaluator": "/var/lib/skill-eval/.local/bin/skillevaluator",
  "version": "skillevaluator, version 0.2.1",
  "missing_scanners": []
}
```

`missing_scanners` 非空就说明扫描器没装全，此时评测结果的安全部分是空的。
worker 在 `SES_REQUIRE_SCANNERS=true` 下会**直接拒绝启动**——这是有意为之，
带病运行会产出看起来合格、实际没扫过的结果。

跑一次真实评测（内容来源接上之前，可以先用本地目录验证链路）：

```bash
sudo -u skilleval mkdir -p /var/lib/skill-eval/skills/demo
sudo -u skilleval tee /var/lib/skill-eval/skills/demo/SKILL.md > /dev/null <<'EOF'
---
name: demo
description: A deployment smoke test skill. Use when verifying the evaluation pipeline after deployment.
---

# Demo
EOF

curl -s -X POST http://127.0.0.1:8000/api/evaluations \
  -H 'Content-Type: application/json' -d '{"skill_id":"demo"}'

sleep 20
curl -s http://127.0.0.1:8000/api/skills/demo/evaluation | python3 -m json.tool
```

`status` 只要不是 `error` 就说明链路通了。若为 `incomplete`，
看 `evaluator.incomplete_scans` 里是谁。

## 八、日常运维

**看日志**

```bash
journalctl -u skill-eval-worker -f
journalctl -u skill-eval-api --since "1 hour ago"
```

**升级服务代码**

```bash
cd /opt/skill-eval-service
sudo -u skilleval git pull
sudo -u skilleval /var/lib/skill-eval/.local/bin/uv pip install --python .venv/bin/python -e .
sudo -u skilleval env SES_DATABASE_URL=sqlite:////var/lib/skill-eval/skill-eval.db \
  .venv/bin/alembic upgrade head
sudo systemctl restart skill-eval-api skill-eval-worker
```

`alembic upgrade head` 这一步不能漏。升级前先备份数据库文件——
迁移可能改表结构，出问题时需要能退回去。

**升级 skillevaluator**——先在非生产环境跑 e2e 测试，尤其确认
`test_security_scan_completes` 仍然通过；升级后存量 skill 的评分可能整体漂移，
建议先跑一批做对比。

**备份**：`/var/lib/skill-eval/skill-eval.db` 是全部结果数据。
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

## 九、什么时候切换 PostgreSQL

长远看一定要切，但**别等"跑不动了"——那一天不会来**。

先排除掉不是理由的：写入量方面，我们每次评测只写几行，即使一万个 skill 每天
全评一遍也只有约 0.2 次写/秒，而 SQLite 在 WAL 模式下每秒几千次写没问题，
差四个数量级。数据量方面，结果行很小，十万行的库也就几十 MB，查询仍是毫秒级。

**真正的触发条件是部署形态，不是性能：**

| 触发条件 | 为什么必须切 |
| --- | --- |
| 要跑第二个 worker | SQLite 不支持 `SELECT ... FOR UPDATE SKIP LOCKED`，两个 worker 会抢同一个任务、重复评测 |
| API 与 worker 分机部署 | SQLite 是文件，两进程需共享文件系统；跨机走 NFS 锁语义不可靠，会损坏库 |
| Tier 3 上线 | 必然同时触发上面两条——任务是分钟到小时级，串行不可行，且需要专门的沙箱节点 |
| 流程要求 | 公司要求数据库纳入统一备份/监控/审计体系时，SQLite 不在那套体系里 |

最后一条**可能比技术需求先到**。Tier 2 是中间情况：catalog 重建是分钟级，
单 worker 也能跑；但若夜间重建窗口拉得太长想并发，就触发第一条。

### 选 PostgreSQL 还是 MySQL

**跟公司现有的 DBA 体系走**，不要为技术偏好单开一套。两者都支持 `SKIP LOCKED`
（MySQL 8.0+）、都有 JSON 类型、SQLAlchemy 都支持。

若两条路都通，选 PostgreSQL——JSONB 可建索引，将来要按 findings 内容查询会有
优势。但这个优势不足以对抗"运维体系里只有 MySQL"。

### 代码基本不用改

- 列类型全是可移植的（`String` / `Integer` / `Float` / `Boolean` / `DateTime` / `Text` / `JSON`），迁移脚本在 PG 上可直接重跑
- [`queue.py`](../src/skill_eval_service/queue.py) 的 `claim_next` **已经先尝试 `SKIP LOCKED`**、失败才退回不加锁的查询。切到 PG 后自动走正确分支，无需改代码
- 代码里没有 SQLite 特有的函数或类型

改一个 `SES_DATABASE_URL` 就能连上去。

### 但切换是两步，Alembic 只解决第一步

1. **建结构**——在新库上跑 `alembic upgrade head`，表就有了
2. **搬数据**——Alembic 不管这个，需要一次性脚本

搬数据有三个坑：

- **用 SQLAlchemy ORM 读写，不要用 SQL dump。** dump 会把 SQLite 的类型表示
  原样带过去——布尔存成 0/1、时间戳存成字符串。走 ORM 则由 SQLAlchemy 负责
  两端的类型转换。
- **自增序列要重置。** `evaluation_detail.id` 是自增主键，在 PG 上对应一个序列。
  带显式 id 批量插入后序列仍停在 1，下一次插入会主键冲突。导完必须
  `setval` 到当前最大值。
- **`sa.JSON` 在 PG 上映射为 `JSON` 而非 `JSONB`。** 若要利用 JSONB 的索引能力，
  那是切换之后另一个迁移的事，不要混在同一步里做。

计划切换时要为搬数据留出工夫，别以为改个 URL 就完事。

## 十、故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| worker 启动即退出，日志写"启动自检未通过" | 扫描器缺失或 PATH 不对 | 检查 unit 里的 `Environment=PATH`，确认四个工具都能找到 |
| `status` 一直是 `incomplete`，`incomplete_scans` 含 `skillspector` | 装了 2.10.0 及以上版本 | 降回 v2.9.6 |
| 所有 skill 都报 `SCHEMA.author_missing` | `internal.yaml` 的邮箱域名还是 `example.com` | 改成公司域名 |
| 每个 skill 都多出 `name_consistency` / `folder_hierarchy` | 物化布局异常 | 应当不会发生，若出现说明 `skill_id` 末段与 frontmatter 的 `name` 系统性不一致，需要判断是真问题还是命名规则差异 |
| 任务卡在 `queued` | worker 没运行 | `systemctl status skill-eval-worker` |
| 报告接口 404 但评测显示成功 | API 与 worker 不在同一文件系统 | 当前形态要求两者同机 |
| 下载内容失败 | 区分两类：`SkillNotFoundError`（404 或归档解不出，不重试）与 `ContentFetchError`（5xx/网络，会重试） | 看 worker 日志里的具体异常 |

## 十一、上生产前必须补的

按优先级：

1. **API 鉴权**——目前完全开放。在此之前只能绑 `127.0.0.1`，靠前置网关鉴权。
2. ~~数据库迁移~~——已引入 Alembic。
3. ~~报告保留策略~~——当前有意不做，见第八节的说明与三个坑。
4. **监控**——目前只有 journald 日志，没有指标。至少要能看到任务失败率和积压量。
