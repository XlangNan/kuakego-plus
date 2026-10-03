<p align="center"><img src="assets/logo.png" width="112" alt="kuakego-plus"></p>

<h1 align="center">kuakego-plus</h1>

<p align="center">夸克网盘追更工具：一部剧挂多个分享链接，按顺序检查，<b>只转存 Emby 里缺少的集数</b>。<br>
自带 Web 管理界面，Docker 一键部署。</p>

---

## 来源与致谢

kuakego-plus 是在下面这些项目的基础上修改、参考而来，**不是原创的从零实现**：

| 项目 | 关系 |
|---|---|
| [**Cp0204/quark-auto-save**](https://github.com/Cp0204/quark-auto-save)（AGPL-3.0） | **主要来源。** 夸克网盘接口的封装（分享解析、转存、目录操作、签到）改写自该项目，转存追更的整体思路也来自它。因此本项目同样以 **AGPL-3.0** 发布。 |
| [**XlangNan/gygo-plus**](https://github.com/XlangNan/gygo-plus)（光鸭云盘监控转存） | **界面与功能设计参考。** 侧栏布局，以及「分享监控 / 剧集推荐 / 订阅追更 / 钉钉 / SmartStrm / TMDB / Emby」这套功能划分参考自它。本项目的代码为独立编写，没有复制其源码。 |
| [GuessIt](https://github.com/guessit-io/guessit)（LGPL-3.0） | 可选依赖：内置规则识别不出集数时兜底。镜像默认安装。 |
| [TMDB](https://www.themoviedb.org/) | 剧集海报、简介、季集数、播出日期的数据来源。 |
| [fish2018/pansou](https://github.com/fish2018/pansou)（PanSou） | **可选的外部服务。** 「资源搜索」功能通过它的 HTTP 接口搜索夸克资源。**不内置、不包含其代码**，需要你自己部署一个。请遵守其「仅供学习研究」的声明。 |

**在原项目基础上，kuakego-plus 新增 / 重做了：**

- 一个任务可挂**多个分享链接**，按顺序检查，同一集只转存一次，后面的链接只补前面没有的集数
- 转存前查询 Emby，**只转存库里缺少的集数**（Emby 连不上时本轮不转存，避免整批重复转存）
- 全新的 Web 界面：海报墙、剧集推荐、订阅进度、转存记录、右侧「最近入库」面板、暗色主题、手机适配
- 「试运行」：只看不转存，逐个文件说明会不会转、为什么
- 「资源搜索」：添加订阅时直接搜索夸克资源，先检测内容（集数范围 / 画质 / 体积 / 是否失效）再勾选（对接 PanSou）
- 「自动找资源」：订阅后自动搜索、检测、筛选并添加链接，之后定时检查有没有新资源，不用再管；每次判断都有记录
- 「画质筛选」：按分辨率（4K / 1080P / 720P）和是否 HDR 筛选，自动选资源和转存文件时都生效
- 钉钉、SmartStrm、Emby、TMDB 联动；`config/hosts` 文件，国内访问不了 TMDB 时可自行指定 IP

## 功能一览

| 页面 | 作用 |
|---|---|
| 📡 **分享监控** | 粘贴分享链接（整段粘贴自动识别提取码，可一次粘多个），定时检查并转存新内容 |
| 🔎 **资源搜索** | 对接 PanSou：在添加订阅 / 监控时搜索夸克资源，**检测**每个分享里有多少集、什么画质、是否已失效，勾选后直接填入链接 |
| 🍿 **剧集推荐** | 基于 TMDB 的热门 / 国产 / 日韩 / 欧美剧集，可搜索、按热度/评分/最新排序，详情里可看分季集数、Emby 已有数、演员，一键订阅 |
| 📺 **订阅追更** | 海报墙。点开看进度、缺哪几集、分享链接（可调整顺序）、转存记录；可开启 🤖 自动找资源，设置画质要求 |
| 🔔 **钉钉通知** | 新增订阅 / 有新入库 / 链接失效时推送，支持加签 |
| 🎬 **SmartStrm 联动** | 转存后自动触发 SmartStrm 生成 STRM |
| 🎯 **TMDB 设置** | API Key、语言、地区；可配置反代地址 |
| 📹 **Emby 联动** | 只转存缺少的集数、转存后通知 Emby 刷新 |
| 🔐 **账号登录** | 夸克 Cookie 登录、每日自动签到、导入旧版 quark-auto-save 配置 |
| 📜 **运行日志** | 每次检查的详细过程，可只看警告 / 错误 |
| 📥 **最近入库**（右侧面板，窗口 ≥ 1200px 时显示） | 今天 / 近 7 天 / 累计入库数，最近入库列表（点击直达详情），订阅、监控、失效链接、出错任务概况 |

## 快速开始

### 方式一：docker run

```bash
docker run -d \
  --name kuakego-plus \
  --restart unless-stopped \
  -p 5005:5005 \
  -e TZ=Asia/Shanghai \
  -e WEBUI_USERNAME=admin \
  -e WEBUI_PASSWORD=请改成你自己的密码 \
  -v /你的路径/kuakego-plus/config:/app/config \
  <你的DockerHub用户名>/kuakego-plus:latest
```

### 方式二：docker compose

```yaml
services:
  kuakego-plus:
    image: <你的DockerHub用户名>/kuakego-plus:latest
    container_name: kuakego-plus
    restart: unless-stopped
    ports:
      - "5005:5005"
    environment:
      - TZ=Asia/Shanghai
      - WEBUI_USERNAME=admin
      - WEBUI_PASSWORD=请改成你自己的密码
    volumes:
      - ./config:/app/config
```

```bash
docker compose up -d
```

打开 `http://服务器IP:5005`，用户名 `admin`，密码是你设置的 `WEBUI_PASSWORD`。
没设置密码时，程序会随机生成一个并打印在日志里：`docker logs kuakego-plus`。

> **群晖 DSM**：在 Container Manager 的「项目」里新建项目，把上面的 compose 内容粘进去即可；
> 或者 SSH 登录后直接执行 docker run（路径例：`/volume1/docker/kuakego-plus/config`）。

### 参数说明

| 类型 | 名称 | 默认值 | 说明 |
|---|---|---|---|
| 端口 | `5005` | — | Web 界面。可改左边的宿主机端口，如 `-p 8080:5005` |
| 挂载 | `/app/config` | — | **必须挂载**。存放 `config.json`（含夸克 Cookie 和各类密钥，请勿公开）、`run.log`、`hosts` |
| 环境变量 | `WEBUI_USERNAME` | `admin` | 登录用户名 |
| 环境变量 | `WEBUI_PASSWORD` | 随机生成 | 登录密码。**务必设置** |
| 环境变量 | `TZ` | `Asia/Shanghai` | 时区。影响「运行星期」、每日签到 |
| 环境变量 | `PORT` | `5005` | 容器内监听端口 |
| 环境变量 | `CONFIG_DIR` | `/app/config` | 配置目录 |
| 环境变量 | `QP_NO_GUESSIT` | 空 | 设为 `1` 关闭 GuessIt 兜底识别 |

### 升级

```bash
docker pull <你的DockerHub用户名>/kuakego-plus:latest
docker rm -f kuakego-plus
# 再执行一遍上面的 docker run；config 目录里的数据不会丢
# compose 用户：docker compose pull && docker compose up -d
```

## 第一次使用

1. **账号登录**（必须）：浏览器登录 `pan.quark.cn` → F12 → 网络(Network) → 刷新 → 点任意请求 → 复制请求头里的 `Cookie`，粘贴到「账号登录」页，点「验证并保存」。
2. **Emby 联动**（想要「只转存缺失集数」就必须）：填 Emby 地址和 API Key（Emby 控制台 → 高级 → API 密钥），勾选启用和「只转存 Emby 里缺少的集数」，点「测试连接」。
3. **TMDB 设置**（使用剧集推荐和订阅追更时）：填 API Key（themoviedb.org 免费申请）。
4. （可选）**资源搜索**：部署 PanSou 后，在「资源搜索」页填它的地址，见下面的章节。
5. 到「订阅追更」搜索一部剧订阅，点「🔎 搜索资源」选择资源（或自己粘贴一个或多个分享链接）；也可以在「分享监控」直接粘贴链接。

## 资源搜索（PanSou）

添加订阅时不用再去别处找链接：在添加订阅的弹窗、订阅详情的「分享链接」页、分享监控的表单里，都有 **🔎 搜索资源** 按钮。

1. 搜索：关键词默认是剧名，可以改（比如去掉「第二季」）。只显示夸克网盘的结果。
2. 检测：点每一条的「检测」（或「检测前 10 个」），会读取分享内容并显示：集数范围（如 `S01E01-E16`）、集数、画质（4K / 1080P / HDR…）、体积，以及是否已失效；订阅的剧还会标出「✓ 覆盖全季」。**检测只读取，不会转存任何文件。**
3. 勾选并添加：可以勾选多个，从上到下就是检查优先级，添加后还能在订阅详情里调整顺序。

**部署 PanSou**（本项目不内置，只调用它的接口）：

```bash
docker run -d --name pansou -p 8888:8888 \
  -e ENABLED_PLUGINS=hunhepan,pansearch,quark4k,quarksoo,sousou,labi,zhizhen,duoduo,muou,wanou \
  ghcr.io/fish2018/pansou:latest
```

然后到「资源搜索」页填 `http://服务器IP:8888`，勾选启用，点「测试连接」。如果和 kuakego-plus 写在同一个 compose 里：

```yaml
services:
  kuakego-plus:
    image: <你的DockerHub用户名>/kuakego-plus:latest
    container_name: kuakego-plus
    restart: unless-stopped
    ports:
      - "5005:5005"
    environment:
      - TZ=Asia/Shanghai
      - WEBUI_USERNAME=admin
      - WEBUI_PASSWORD=请改成你自己的密码
    volumes:
      - ./config:/app/config

  # 可选：资源搜索。启动后在 kuakego-plus 的「资源搜索」页填 http://pansou:8888
  pansou:
    image: ghcr.io/fish2018/pansou:latest
    container_name: pansou
    restart: unless-stopped
    environment:
      - PORT=8888
      - ENABLED_PLUGINS=hunhepan,pansearch,quark4k,quarksoo,sousou,labi,zhizhen,duoduo,muou,wanou
      # - CHANNELS=频道名1,频道名2          # 要搜索的 Telegram 频道
      # - PROXY=socks5://代理地址:1080       # 搜索 Telegram 频道在国内需要代理
    volumes:
      - pansou-cache:/app/cache

volumes:
  pansou-cache:
```

> - 填**只有接口**的版本（`ghcr.io/fish2018/pansou`，端口 8888），不是带网页的 `pansou-web`。
> - 搜索 **Telegram 频道**在国内需要给 PanSou 配代理（`PROXY`）；只用网站插件的话不需要，在「资源搜索」页把来源选成「仅网站插件」即可。
> - PanSou 开启了认证的话，在页面里填用户名和密码（程序会自动登录获取令牌）。
> - 可用的插件、频道、认证等更多配置，请看 [PanSou 的文档](https://github.com/fish2018/pansou)。

## 自动找资源与画质筛选

**订阅后不用再管**：配置好 PanSou 后，订阅时默认勾选「🤖 自动找资源」。点一下订阅，程序会自动搜索、检测、筛选、添加链接并立刻转存；之后每隔几小时（默认 6 小时，可在「资源搜索」页调整）再搜一次，有新资源就自动加进来。本季已播出的集都齐了就不再搜。

**怎么保证不加错东西？** PanSou 是关键词搜索，会混进别的剧、别的季、花絮、预告。所以搜到之后会**逐个检测内容**，下面这些**全部满足**才会添加：

1. 标题里有剧名（先去掉名字结尾的「第二季」再比对）；标题明确写了别的季的直接跳过
2. 必须是这一季：文件名 / 文件夹里写了季号的，要包含本季；**完全没写季号的，只接受第 1 季**（第 2 季及以后一律不要，避免把第 1 季当成第 2 季转进来）
3. 画质符合你选的要求（见下）
4. 平均每个视频不小于 100 MB（排除花絮、预告）
5. 最大集号不能远超本季总集数（排除合集和同名的别的剧）
6. 能补上你**缺的**集数

**数量限制**（避免触发风控、避免越加越多）：每个订阅最多同时保留 5 个有效链接；每轮最多自动添加 3 个、最多检测 8 个候选。被拒绝的候选 24 小时内不会重复检测（作者可能更新了，之后会再看一次）；已失效的 3 天内不再检测。自动添加后又失效的链接会被清理，**你自己添加的链接不会被动，并且优先级永远排在前面**。

**每一次判断都有记录**：订阅详情 → 「分享链接」→「判断记录」，能看到每个候选为什么被添加或被拒绝。也可以点「立即搜索一次」，或随时取消勾选关闭自动找资源。

**画质筛选**（订阅、分享监控、编辑里都可以设置）：

- 分辨率：4K / 1080P / 720P 可多选，不选 = 不限
- HDR：不限 / 只要 HDR（含杜比视界）/ 不要 HDR
- 两处生效：**自动找资源时只选画质符合的分享**；**转存时也会跳过画质不符的文件**（「试运行」里能看到原因）
- 识别规则：`2160p` / `4K` / `UHD` 算 4K，`1080p` / `1080i` / `FHD` 算 1080P，`720p` 算 720P；`HDR` / `HDR10` / `杜比视界` / `DV` 算 HDR（「杜比全景声」是音效，不算）
- **文件名里没写分辨率的无法判断，照常转存**；选了「只要 HDR」时，文件名里没有 HDR 标记的会被当成 SDR 跳过（HDR 版基本都会标出来）

**只转存本季**：订阅追更默认开启，文件名或文件夹里写了别的季的文件会被跳过（比如从合集里只取第 2 季）。可在「编辑」里关闭。

> 自动判断依赖文件名，文件名乱写时可能判断不准。第一次自动添加后，建议看一眼「判断记录」和转存记录；不放心的订阅可以关掉自动找资源，自己在「🔎 搜索资源」里选。


## 工作方式

每次扫描：

1. 算出**已有集数** = Emby 里真实存在的集 ∪ 夸克目标目录里已有的集（取并集，避免「刚转存、Emby 还没入库」时重复转）。
2. 按链接顺序逐个检查，**同一集只转一次**；后面的链接只补前面没覆盖的集。链接顺序就是优先级——想优先高画质，就把高画质的链接放前面。
3. 转存后统一整理为 `S01E05.mp4`（可关闭），触发 SmartStrm → 通知 Emby 刷新 → 钉钉推送。
4. 订阅了 TMDB 剧集的，已播出的集都入库后自动跳过链接检查（省请求）；也可手填总集数。

**安全设计：**

- 扫描间隔最低 **60 分钟**，追更时单轮最多转存 **30 个**（剩下的下一轮接着补），避免触发夸克风控
- Emby 连接失败时，本轮**不转存**（而不是退回去整批转存）
- 订阅追更里**解析不出第几集**的视频默认**跳过**；写了别的季的文件默认跳过（只转存本季）（分享监控默认转存，可在「编辑」里改）
- 链接失效不会误判：网络异常不计失败，其他错误连续 3 次才标记失效，「分享已取消」等明确原因立即标记
- 「试运行」只看不转存，也不会创建目录

**文件名识别：** 内置规则支持 `S01E05`、`S01E05E06`、`1x05`、`第12集`、`第十二集`、`01集`、`EP03`、`某某剧01.mp4`、`[Group] 标题 - 05 [1080p]` 等；识别不出时交给 GuessIt 兜底。季号优先取文件名里的季，其次是所在文件夹名（如「第二季」），最后才用任务设置的季号。

## 常见问题

**访问不了 TMDB？** 编辑 `config/hosts`（首次启动自动生成），按系统 hosts 格式写 `IP 域名`，保存后约 10 秒自动生效，容器每次启动也会写入。需要指定的域名通常是 `api.themoviedb.org` 和 `image.tmdb.org`。也可以在「TMDB 设置 → 高级」里填反代地址。海报由本服务代理下载，所以你的电脑和手机不需要能访问 TMDB。

**已有的集又被转存了一遍？** 先对这部剧点「试运行」：它会显示本任务的季号、Emby 已有哪些集、夸克目录已有哪些集，以及每个文件被识别成第几集。最常见的原因是**季号对不上**（Emby 里是第 2 季，任务季号是 1）、Emby 联动没启用、或剧名没匹配到 Emby。在「编辑」里填「Emby 里的剧名」或「Emby 剧集 ID」可精确指定。

**从 quark-auto-save 迁移？** 把旧的 `quark_config.json` 放进 `config` 目录，首次启动自动导入 Cookie 和任务；也可在「账号登录」页点「导入旧版配置」。

**资源搜索没结果 / 连不上？** 先在「资源搜索」页点「测试连接」。常见原因：填成了带网页版本的地址（要填接口版，端口 8888）；PanSou 没有启用任何插件（`ENABLED_PLUGINS` 必须显式指定）；只搜 Telegram 频道但 PanSou 没配代理。检测提示「已失效」的链接说明分享已被取消或删除。

**Cookie 失效？** 页面顶部会出现黄色提示，重新粘贴即可。自动签到需要 Cookie 里含 `kps` / `sign` / `vcode`（需从手机端抓取）。

**国内自己构建镜像时 pip 超时？** Dockerfile 默认使用清华镜像源。可覆盖：`docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple -t kuakego-plus .`

## 自行构建

```bash
git clone https://github.com/<你的GitHub用户名>/kuakego-plus.git
cd kuakego-plus
# 编辑 docker-compose.yml 里的 WEBUI_PASSWORD
docker compose up -d --build
```

## 发布到 GitHub 与 Docker Hub

仓库内置了 GitHub Actions（`.github/workflows/docker.yml`），推送代码后自动构建 `linux/amd64` + `linux/arm64` 镜像并推送到 Docker Hub，同时把 `DOCKERHUB.md` 同步为 Docker Hub 的仓库简介。

1. 在 GitHub 新建仓库 `kuakego-plus`，上传本项目所有文件。**创建 `LICENSE` 文件时选 AGPL-3.0 模板**（Add file → Create new file → 文件名填 `LICENSE` → Choose a license template → GNU Affero General Public License v3.0）。
2. 在 [Docker Hub](https://hub.docker.com/) 创建仓库 `kuakego-plus`，并在 Account Settings → Security 里生成一个 Access Token。
3. 在 GitHub 仓库 Settings → Secrets and variables → Actions 里添加：
   - `DOCKERHUB_USERNAME`：你的 Docker Hub 用户名
   - `DOCKERHUB_TOKEN`：上一步生成的令牌
4. 推送到 `main` 分支会生成 `latest` 镜像；打版本标签（如 `git tag v1.0.0 && git push origin v1.0.0`）会额外生成 `1.0.0`、`1.0` 标签。

## 开发与测试

```bash
pip install -r requirements.txt
python tests/run_minimal.py                  # 引擎测试（也可 pytest tests/）
python tests/run_minimal.py tests.test_diagnose
python tests/run_minimal.py tests.test_autofind   # 自动找资源 / 画质筛选
python tests/dev_server.py                   # 假夸克 + 假 TMDB 的离线预览：http://127.0.0.1:5099 （admin / dev）
```

## 许可与声明

- 本项目以 **GNU AGPL-3.0** 发布（因改写自 quark-auto-save）。如果你修改后通过网络提供服务，需按 AGPL 向用户提供对应源码。
- 本项目是**非官方**工具，与夸克 / UC、Emby、TMDB、钉钉均无关联。
- 「资源搜索」只是调用你自己部署的 PanSou 的接口，搜到什么、能不能用都取决于它，本项目不对搜索结果的内容负责。
- 本工具只是自动化你**自己账号**里的转存操作，请遵守相关法律法规和各服务的使用条款，尊重版权；使用产生的账号风险和责任由使用者自行承担。请保持低频使用。
- **本产品使用 TMDB API，但未经 TMDB 认可或认证。**（This product uses the TMDB API but is not endorsed or certified by TMDB.）
- Web 界面带有登录保护，但**不建议直接暴露到公网**；如需远程访问，请使用反向代理加 HTTPS 或 VPN。
