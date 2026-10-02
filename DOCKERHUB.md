# kuakego-plus

**夸克网盘追更工具：一部剧挂多个分享链接，按顺序检查，只转存 Emby 里缺少的集数。** 自带 Web 管理界面（海报墙、剧集推荐、最近入库）。

*English: a self-hosted Quark Drive auto-save tool with a web UI. One show can have several share links, checked in order; only the episodes missing from your Emby library are saved. Supports TMDB recommendations, Emby, SmartStrm and DingTalk integration.*

源码与完整文档：<https://github.com/你的GitHub用户名/kuakego-plus>

## 来源

本项目是在下面这些项目的基础上修改、参考而来：

- **[Cp0204/quark-auto-save](https://github.com/Cp0204/quark-auto-save)**（AGPL-3.0）— 夸克接口封装改写自该项目，转存追更的整体思路来自它。因此本项目同样以 **AGPL-3.0** 发布。
- **[XlangNan/gygo-plus](https://github.com/XlangNan/gygo-plus)**（光鸭云盘监控转存）— 界面布局与功能划分参考自它，代码为独立编写。
- [GuessIt](https://github.com/guessit-io/guessit)（LGPL-3.0，文件名识别兜底）、[TMDB](https://www.themoviedb.org/)（剧集数据）。

在原项目基础上新增：多分享链接按序检查、Emby 缺集补转、全新 Web 界面（海报墙 / 剧集推荐 / 最近入库 / 暗色主题）、试运行、钉钉 / SmartStrm / Emby / TMDB 联动。

## 快速开始（docker run）

```bash
docker run -d \
  --name kuakego-plus \
  --restart unless-stopped \
  -p 5005:5005 \
  -e TZ=Asia/Shanghai \
  -e WEBUI_USERNAME=admin \
  -e WEBUI_PASSWORD=请改成你自己的密码 \
  -v /你的路径/kuakego-plus/config:/app/config \
  你的DockerHub用户名/kuakego-plus:latest
```

打开 `http://服务器IP:5005`，用户名 `admin`，密码为你设置的 `WEBUI_PASSWORD`。未设置密码时会随机生成并打印在日志里：`docker logs kuakego-plus`。

### docker compose

```yaml
services:
  kuakego-plus:
    image: 你的DockerHub用户名/kuakego-plus:latest
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

## 参数

| 类型 | 名称 | 默认值 | 说明 |
|---|---|---|---|
| 端口 | `5005` | — | Web 界面，可改宿主机端口如 `-p 8080:5005` |
| 挂载 | `/app/config` | — | **必须挂载。** 存放 `config.json`（含夸克 Cookie 与各类密钥，请勿公开）、`run.log`、`hosts` |
| 环境变量 | `WEBUI_USERNAME` | `admin` | 登录用户名 |
| 环境变量 | `WEBUI_PASSWORD` | 随机生成 | 登录密码，**务必设置** |
| 环境变量 | `TZ` | `Asia/Shanghai` | 时区，影响「运行星期」和每日签到 |
| 环境变量 | `PORT` | `5005` | 容器内监听端口 |
| 环境变量 | `QP_NO_GUESSIT` | 空 | 设为 `1` 关闭 GuessIt 兜底识别 |

**镜像标签：** `latest`（最新）、`x.y.z` / `x.y`（版本号）。支持 `linux/amd64` 和 `linux/arm64`。

**升级：** `docker pull 你的DockerHub用户名/kuakego-plus:latest`，删除旧容器后用同样的命令重新运行；`config` 里的数据不会丢。

## 功能介绍

| 页面 | 作用 |
|---|---|
| 📡 分享监控 | 粘贴分享链接（整段粘贴自动识别提取码，可一次粘多个），定时检查并转存新内容；可选「只追新」 |
| 🍿 剧集推荐 | 基于 TMDB 的热门 / 国产 / 日韩 / 欧美剧集；搜索、排序；详情含分季集数、Emby 已有数、演员；一键订阅 |
| 📺 订阅追更 | 海报墙；点开看进度、缺哪几集、分享链接（可调整优先级）、转存记录；支持「试运行」 |
| 🔔 钉钉通知 | 新增订阅 / 有新入库 / 链接失效时推送，支持加签 |
| 🎬 SmartStrm 联动 | 转存后自动触发 SmartStrm 生成 STRM |
| 🎯 TMDB 设置 | API Key、语言、地区、反代地址 |
| 📹 Emby 联动 | 只转存缺少的集数；转存后通知 Emby 刷新 |
| 🔐 账号登录 | 夸克 Cookie 登录、每日自动签到、导入旧版 quark-auto-save 配置 |
| 📜 运行日志 | 每次检查的详细过程 |
| 📥 最近入库（右侧面板） | 今天 / 近 7 天 / 累计入库数、最近入库列表、订阅与异常概况 |

## 第一次使用

1. **账号登录**：浏览器登录 `pan.quark.cn` → F12 → 网络 → 刷新 → 点任意请求 → 复制请求头里的 `Cookie` → 粘贴到「账号登录」页并保存。
2. **Emby 联动**（想要「只转存缺失集数」就必须）：填 Emby 地址和 API Key，勾选启用和「只转存 Emby 里缺少的集数」。
3. **TMDB 设置**：填 API Key（themoviedb.org 免费申请）。
4. 去「订阅追更」搜索一部剧订阅，粘贴一个或多个分享链接；或在「分享监控」直接粘贴链接。

## 工作方式与安全设计

- **已有集数** = Emby 里的集 ∪ 夸克目标目录里的集；按链接顺序检查，**同一集只转一次**，后面的链接只补前面没有的集数。
- 扫描间隔最低 **60 分钟**；追更时单轮最多转存 **30 个**，避免触发风控。
- Emby 连接失败时本轮**不转存**，不会整批重复转存。
- 订阅追更里**解析不出第几集**的视频默认跳过。
- 转存后统一整理为 `S01E05.mp4`（可关闭）。

## 常见问题

- **访问不了 TMDB**：编辑 `config/hosts`（首次启动自动生成），按系统 hosts 格式写 `IP 域名`，保存后约 10 秒生效；也可在 TMDB 设置里填反代地址。
- **已有的集又被转存**：先点该剧的「试运行」，会显示季号、Emby 已有集数、每个文件的识别结果。最常见原因是季号对不上。
- **从 quark-auto-save 迁移**：把旧的 `quark_config.json` 放进 `config` 目录，首次启动自动导入。

## 声明

- 采用 **AGPL-3.0** 许可。非官方工具，与夸克 / UC、Emby、TMDB、钉钉均无关联。
- 仅自动化你**自己账号**里的转存操作，请遵守相关法律与服务条款、尊重版权，使用风险自负，请保持低频。
- 建议不要直接暴露到公网，远程访问请使用反向代理加 HTTPS 或 VPN。
- **本产品使用 TMDB API，但未经 TMDB 认可或认证。**
