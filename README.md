# quark-plus

夸克网盘分享链接追更：**一部剧挂多个分享链接，按顺序检查，只转存 Emby 里缺少的集数。**
界面与功能参考 gygo-plus（光鸭云盘），后端换成夸克。

## 部署（Docker）

```bash
# 1. 编辑 docker-compose.yml，把 WEBUI_PASSWORD 改成你自己的密码
# 2.
docker compose up -d --build
# 3. 打开 http://<你的IP>:5005   用户名 admin
```

- 数据都在 `./config`（`config.json` 含夸克 Cookie 与各类密钥，请勿公开；`run.log` 为日志）。
- 忘了密码：改 compose 里的 `WEBUI_PASSWORD` 后重启即可。未设置时会随机生成并打印到 `docker logs quark-plus`。
- 从 quark-auto-save 迁移：把旧的 `quark_config.json` 放进 `./config`，首次启动会自动导入 Cookie 和任务，
  也可在「账号登录」页点「导入旧版配置」。

## 访问不了 TMDB？改 hosts

首次启动会在 `./config/hosts` 生成一个模板。按系统 hosts 的格式写 `IP 域名`（去掉示例行首的 `#`），保存后约 10 秒自动生效，
容器每次启动也会自动写入，不需要重启。需要指定的域名通常是 `api.themoviedb.org` 和 `image.tmdb.org`。
海报图片由本服务代理下载，所以你的电脑 / 手机不需要能访问 TMDB。

## 它怎么决定"转哪些集"

1. 先算出**已有集数** = Emby 里真实存在的集 ∪ 夸克目标目录里已有的集（并集，避免"刚转存、Emby 还没入库"时重复转）。
2. 按链接顺序逐个检查，**同一集只转一次**；后面的链接只补前面没覆盖的。
3. 转存后统一整理为 `S01E05.mp4`（可关），触发 SmartStrm → 通知 Emby 刷新 → 钉钉。
4. 订阅了 TMDB 的剧，已播出的集都入库后会跳过链接检查（省请求）；也可手填总集数。
5. 追更时单轮最多转 30 个（防风控，剩下的下一轮接着补）；首次回填不限。

集数解析先用内置规则（含中文「第12集」「第十二集」），解析不出再交给可选的
[GuessIt](https://github.com/guessit-io/guessit)（LGPLv3）兜底；没装 GuessIt 也能正常用，设 `QP_NO_GUESSIT=1` 可关闭。

文件名里解析不出集数的（花絮、电影），按"目录里没有同名文件"判断，不会重复转。

## 开发 / 测试

```bash
pip install -r requirements.txt
python tests/run_minimal.py        # 或 pytest tests/
python tests/dev_server.py         # 假夸克 + 假 TMDB 的离线预览，http://127.0.0.1:5099 (admin/dev)
```

## 许可

夸克接口封装改写自 [Cp0204/quark-auto-save](https://github.com/Cp0204/quark-auto-save)（AGPL-3.0），
因此本项目整体按 AGPL-3.0 发布。
