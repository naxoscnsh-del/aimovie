# AI 短片工作台

团队内部工具：输入场景描述，经 **DeepSeek 脚本 → Seedream 场景图 → 人工选图 → Seedance 图生视频 → FFmpeg 合成** 产出短片。
每部短片花费硬上限 100 USD。

## 先试一下：演示模式（不需要任何 Key，不花钱）

```bash
cp .env.example .env && sed -i 's/^DEMO_MODE=0/DEMO_MODE=1/' .env
docker compose up -d --build      # 打开 http://localhost:8000
```

演示模式下脚本、场景图、视频、配音都用本地生成的占位素材，可以把新建 → 改脚本 → 选图 → 生成视频 → 合成 → 导出的完整流程点一遍。
正式使用前把 `DEMO_MODE` 改回 0 并重启。

## 服务器部署（https://ainewimage.com/movie/）

首次部署（服务器需已装 Docker）：

```bash
cd /data && tar xzf aimovie-<提交号>.tar.gz && cd /data/aimovie
bash deploy.sh --port 8010 --base /movie --cn     # --cn：基础镜像、apt、pip 都走国内源
```

`--cn` 会依次尝试 docker.1ms.run、docker.m.daocloud.io、docker.1panel.live 拉基础镜像，用第一个能用的；
apt 和 pip 用阿里云镜像。这些设置写进 `.env`，之后更新时 `docker compose up -d --build app` 自动沿用。
如果三个入口都拉不下来，在 `/etc/docker/daemon.json` 配置 `registry-mirrors` 后 `systemctl restart docker` 再试。

然后把 `deploy/nginx-subpath.conf` 里的两段 `location` 加进 ainewimage.com 的 Nginx 配置，`nginx -t && nginx -s reload`。

更新版本（`.env` 和 `data/` 不在包里，不会被覆盖）：

```bash
cd /data/aimovie && rm -rf videogen web tests deploy
cd /data && tar xzf aimovie-<提交号>.tar.gz && cd /data/aimovie
docker compose up -d --build app
```

静态文件地址带内容哈希，更新后浏览器会自动取新版本。备份：`bash backup.sh`（可加到 crontab 每天执行）。

## 快速开始（Docker，推荐）

```bash
cp .env.example .env     # 填 API Key、ADMIN_USER/ADMIN_PASSWORD、模型单价
docker compose up -d --build
# 浏览器打开 http://服务器IP:8000 ，用管理员账号登录，在右上角“成员”里给同事开账号
```

数据（SQLite 任务库、参考图、场景图、视频、成片）都在 `./data`，备份这个目录即可。

## 本地运行（不用 Docker）

需要 Python 3.10+ 和 FFmpeg：

```bash
pip install -r requirements-dev.txt
cp .env.example .env
python -m web            # Web 服务，默认 8000 端口
python -m pytest -q      # 测试（用假模型，不花钱）
```

## 第一次接真实接口前必须核对

先运行 `python -m videogen.doctor --ping`（Docker 里：`docker compose exec app python -m videogen.doctor --ping`），按提示补齐。

1. **单价**：`PRICE_SEEDREAM_PER_IMAGE`（元/张）、`PRICE_SEEDANCE_PER_SECOND`（元/秒，对应所选分辨率）。
   不填时预估为 0，100 USD 熔断不生效，页面会有提示。
2. **TTS**（可选）：`TTS_APPID`、`TTS_TOKEN`、`PRICE_TTS_PER_10K_CHARS`（元/万字）。
3. **模型 ID**：`SEEDREAM_MODEL`、`SEEDANCE_MODEL` 以火山方舟控制台实际开通的为准（也可填接入点 `ep-xxxx`）。
4. **单段时长**：`SEEDANCE_DURATIONS` 填所选 Seedance 版本允许的秒数。
5. 建议先用命令行跑 1–2 个镜头验证参数，再让团队用 Web。

## 使用流程（Web）

| 步骤 | 页面 | 说明 |
| --- | --- | --- |
| 1 | 首页 · 新建短片 | 场景描述、画幅、时长、风格、最多 4 张参考图（角色/产品） |
| 2 | ① 脚本 | 编辑全片风格、角色外观、每镜的场景图提示词与视频提示词、时长、旁白；可增删和调整镜头顺序 |
| 3 | ② 场景图 | 每镜出 2 张候选，点选一张；不满意单镜重出 |
| 4 | ③ 视频 | 选好的图作为首帧生成视频；单镜重生成 |
| 5 | ④ 成片 | 可上传 BGM，合成后下载 mp4 和 srt 字幕 |

**P6 新增**

- **演示模式**（`DEMO_MODE=1`）：不调用任何接口、不花钱，用占位素材跑通全流程，适合部署前让团队熟悉操作。
- **实时进度**：任务进度和日志通过服务器推送（SSE）即时刷新，连接不上时自动退回 2 秒轮询。经 Nginx 反代时请关闭缓冲（已带 `X-Accel-Buffering: no`）。
- **取消任务**：任务横幅上的“取消”。排队中的直接取消；运行中的在当前步骤结束后停下，已完成的图和视频保留，
  正在生成的视频会尝试在方舟侧取消（已开始生成的可能仍会计费）。
- **淡入淡出转场**：合成时可选镜头间 0.5 秒淡入淡出，字幕和配音时间轴自动对齐。

**P5 新增**

- **多角色配音**：脚本里每个角色可设配音音色（voice_type），每镜可选“说话人”；合成时台词用该角色音色，其余用旁白音色。
  模板会一并保存角色音色。
- **字幕烧录**：合成时可勾选“把字幕烧进画面”（抖音、视频号等不显示软字幕的平台需要），字号按画面自动适配，使用 Noto Sans CJK 字体（Docker 镜像已内置）。
- **导出其他画幅**：成片一键转 9:16 / 16:9 / 1:1 / 3:4 / 4:3，原画面完整保留、空白处模糊填充，不重新生成、不花钱；重新合成后旧导出自动作废。
- **自动测试**：推送到 main 或提 PR 时，GitHub Actions 自动安装 FFmpeg 跑全部测试。

**P4 新增（上线前加固与提效）**

- **系统自检**：`python -m videogen.doctor [--ping]`，或管理员在“成员”页底部查看。检查 FFmpeg、Key、单价、模型、时长、磁盘；
  “测试接口连通”只读取 DeepSeek 模型列表和方舟任务列表，不产生生成费用。**第一次部署前先跑一遍。**
- **基于选中图修改**：在「② 场景图」输入修改说明（如“背景加一盏暖色吊灯”），以选中图为底图再出一张，追加为新候选，原图和选择不变。
- **项目复制 / 删除**：复制需求和脚本做变体（不复制图和视频）；删除移入 `data/trash`，管理员可找回。
- **列表筛选**：全部 / 我的、按状态、按标题或成员搜索。
- **安全**：拒绝其他网站发起的写请求（防跨站伪造花钱）；上传按文件头校验是否真的是图片；加了常见安全响应头。
  如果前面有反向代理改写了 Host，把浏览器访问的域名加到 `ALLOWED_ORIGIN_HOSTS`。
- 合成完成后自动清理中间文件；默认视频模型改为 Seedance 2.0（`doubao-seedance-2-0-260128`），首尾帧过渡需要它。

**P3 新增**

- **成员账号与权限**：每人独立账号密码，分管理员和成员。成员能看所有短片，只能修改自己建的；
  管理员可改全部、添加/停用成员、重置密码。最后一名管理员不能被降级或停用。仍兼容 P1 的共用口令模式。
- **风格模板**：项目「① 脚本」里“另存为模板”，保存全片风格、角色外观、画幅时长和参考素材。
  新建短片时选模板即可沿用；生成的脚本里同名角色的外观强制使用模板版本，保证系列片角色一致。
- **素材库**：团队共享的角色/产品/场景/风格图。可上传，也可把选中的场景图一键存入；新建短片时直接勾选作为参考图（与上传合计最多 4 张）。
- **团队月度预算**：`TEAM_MONTHLY_BUDGET_USD`，看板显示本月花费，超过 80% 提醒，用完后拒绝新建短片和提交新任务
  （已在运行的任务不会被打断，单片 100 USD 熔断照常生效）。

**P2 新增**

- **镜头过渡**：勾选“平滑过渡到下一镜”后，用下一镜选中的场景图做尾帧（Seedance 首尾帧模式），连续动作不再硬切。
  下一镜换图、改顺序或取消勾选时，受影响的视频自动作废。DeepSeek 写脚本时也会给同场景连续动作预先勾选。
- **旁白配音**：合成时可用火山引擎语音合成为每镜旁白配音，按镜头起点对齐，有配音时 BGM 自动压低；
  同样的文字和音色只合成一次。旁白比镜头长时会在日志里提醒。
- **失败自动重试**：视频任务失败自动重提一次（`VIDEO_AUTO_RETRY`），失败的那次不计费、不记账。
- **成本看板**（右上角）：累计花费、每部成片平均花费、按月/按成员/按模型、各类任务成功率与耗时、视频重生成率、花费最多的短片。

素材不会因为改脚本而被删除：改了画面提示词只会标记“建议重出”，改了视频提示词或时长才会作废该镜视频；换图会作废该镜视频。

## 成本控制

| 时机 | 规则 |
| --- | --- |
| 脚本生成后 | 显示整片预估（含 50% 重生成余量） |
| 提交整片场景图前 | 预估 + 已花 > 上限 → 拒绝 |
| 每次提交模型任务前 | 已花 + 进行中任务 + 本次 > 上限 → 暂停 |
| 累计到 80 USD | 任务日志提醒，页面花费条变黄 |

每笔花费和接口返回的 usage 都记在项目 `state.json` 的 `ledger` 里，用来和方舟账单对账、校准单价。

## 命令行（P0 验证用，仍可用）

```bash
python -m videogen run "场景描述" --duration 30 --ratio 9:16 --ref 角色.png   # 一键跑完
python -m videogen new / images / select 1=2 / videos --shots 2 / compose / status
```

命令行项目放在 `./runs`，Web 项目放在 `./data/runs`，两边互不影响。

## 架构

```
浏览器（原生 JS 单页，web/static）
   │  JSON API + 轮询进度
Starlette 服务（web/app.py）── HTTP Basic 登录（成员账号 / 共用口令），按项目归属控制修改权限
   │  web/team.py：成员（PBKDF2 密码哈希）、模板、素材库
   │  入队
SQLite 任务队列 + 后台线程（web/jobs.py）── 每个项目同一时间只跑一个任务；重启时中断任务标记失败
   │
生成内核（videogen/）
   script.py   DeepSeek 结构化脚本 + 校验 + 场景图提示词拼接（风格 + 角色外观 + 画面）
   models.py   Seedream 文生图（带参考图）/ Seedance 图生视频（首帧或首尾帧，异步任务 + 轮询）/ 火山 TTS
   cost.py     预估、记账、提醒、熔断
   compose.py  FFmpeg 统一规格、拼接、BGM、软字幕
   pipeline.py 分阶段状态机，镜头有稳定 id
```

为什么不是计划里的 FastAPI + Celery + Redis + Next.js：5 人内部工具用不上这些组件的扩展能力，
精简后单个容器就能部署，没有 Redis 要运维，前端也不需要构建。API 的分层与计划一致，以后用户变多可以平滑迁移：
任务队列换 Celery，前端换 Next.js，`videogen/` 内核不用改。

## 已知限制

- 未用真实 API Key 联调过，所有测试基于模拟接口（FFmpeg 合成为真实执行）
- 登录用浏览器自带的 HTTP Basic 弹窗，退出需关闭浏览器；对外网开放时请在前面加 HTTPS（如 Nginx / Caddy）
- 进度用 2 秒轮询；视频生成时长取决于方舟排队
- 方舟返回的图片/视频链接 24 小时过期，系统会在生成后立即下载到本地
- 首尾帧模式需所选 Seedance 版本支持（2.0 起支持 first_frame + last_frame）；不支持时请不要勾选过渡
- TTS 音色 ID 需在火山引擎控制台确认已开通
