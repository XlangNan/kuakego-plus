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

**在原项目基础上，kuakego-plus 新增 / 重做了：**

- 一个任务可挂**多个分享链接**，按顺序检查，同一集只转存一次，后面的链接只补前面没有的集数
- 转存前查询 Emby，**只转存库里缺少的集数**（Emby 连不上时本轮不转存，避免整批重复转存）
- 全新的 Web 界面：海报墙、剧集推荐、订阅进度、转存记录、右侧「最近入库」面板、暗色主题、手机适配
- 「试运行」：只看不转存，逐个文件说明会不会转、为什么
- 钉钉、SmartStrm、Emby、TMDB 联动；`config/hosts` 文件，国内访问不了 TMDB 时可自行指定 IP

## 功能一览

| 页面 | 作用 |
|---|---|
| 📡 **分享监控** | 粘贴分享链接（整段粘贴自动识别提取码，可一次粘多个），定时检查并转存新内容 |
| 🍿 **剧集推荐** | 基于 TMDB 的热门 / 国产 / 日韩 / 欧美剧集，可搜索、按热度/评分/最新排序，详情里可看分季集数、Emby 已有数、演员，一键订阅 |
| 📺 **订阅追更** | 海报墙。点开看进度、缺哪几集、分享链接（可调整顺序）、转存记录 |
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
  xlangnan/kuakego-plus:latest
```

### 方式二：docker compose

```yaml
services:
  kuakego-plus:
    image: xlangnan/kuakego-plus:latest
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
4. 到「订阅追更」搜索一部剧订阅，粘贴一个或多个分享链接；或者在「分享监控」直接粘贴链接。

## 工作方式

每次扫描：

1. 算出**已有集数** = Emby 里真实存在的集 ∪ 夸克目标目录里已有的集（取并集，避免「刚转存、Emby 还没入库」时重复转）。
2. 按链接顺序逐个检查，**同一集只转一次**；后面的链接只补前面没覆盖的集。链接顺序就是优先级——想优先高画质，就把高画质的链接放前面。
3. 转存后统一整理为 `S01E05.mp4`（可关闭），触发 SmartStrm → 通知 Emby 刷新 → 钉钉推送。
4. 订阅了 TMDB 剧集的，已播出的集都入库后自动跳过链接检查（省请求）；也可手填总集数。

**安全设计：**

- 扫描间隔最低 **60 分钟**，追更时单轮最多转存 **30 个**（剩下的下一轮接着补），避免触发夸克风控
- Emby 连接失败时，本轮**不转存**（而不是退回去整批转存）
- 订阅追更里**解析不出第几集**的视频默认**跳过**（分享监控默认转存，可在「编辑」里改）
- 链接失效不会误判：网络异常不计失败，其他错误连续 3 次才标记失效，「分享已取消」等明确原因立即标记
- 「试运行」只看不转存，也不会创建目录

**文件名识别：** 内置规则支持 `S01E05`、`S01E05E06`、`1x05`、`第12集`、`第十二集`、`01集`、`EP03`、`某某剧01.mp4`、`[Group] 标题 - 05 [1080p]` 等；识别不出时交给 GuessIt 兜底。季号优先取文件名里的季，其次是所在文件夹名（如「第二季」），最后才用任务设置的季号。

## 常见问题

**访问不了 TMDB？** 编辑 `config/hosts`（首次启动自动生成），按系统 hosts 格式写 `IP 域名`，保存后约 10 秒自动生效，容器每次启动也会写入。需要指定的域名通常是 `api.themoviedb.org` 和 `image.tmdb.org`。也可以在「TMDB 设置 → 高级」里填反代地址。海报由本服务代理下载，所以你的电脑和手机不需要能访问 TMDB。

**已有的集又被转存了一遍？** 先对这部剧点「试运行」：它会显示本任务的季号、Emby 已有哪些集、夸克目录已有哪些集，以及每个文件被识别成第几集。最常见的原因是**季号对不上**（Emby 里是第 2 季，任务季号是 1）、Emby 联动没启用、或剧名没匹配到 Emby。在「编辑」里填「Emby 里的剧名」或「Emby 剧集 ID」可精确指定。

**从 quark-auto-save 迁移？** 把旧的 `quark_config.json` 放进 `config` 目录，首次启动自动导入 Cookie 和任务；也可在「账号登录」页点「导入旧版配置」。

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
python tests/dev_server.py                   # 假夸克 + 假 TMDB 的离线预览：http://127.0.0.1:5099 （admin / dev）
```

## 许可与声明

- 本项目以 **GNU AGPL-3.0** 发布（因改写自 quark-auto-save）。如果你修改后通过网络提供服务，需按 AGPL 向用户提供对应源码。
- 本项目是**非官方**工具，与夸克 / UC、Emby、TMDB、钉钉均无关联。
- 本工具只是自动化你**自己账号**里的转存操作，请遵守相关法律法规和各服务的使用条款，尊重版权；使用产生的账号风险和责任由使用者自行承担。请保持低频使用。
- **本产品使用 TMDB API，但未经 TMDB 认可或认证。**（This product uses the TMDB API but is not endorsed or certified by TMDB.）
- Web 界面带有登录保护，但**不建议直接暴露到公网**；如需远程访问，请使用反向代理加 HTTPS 或 VPN。
