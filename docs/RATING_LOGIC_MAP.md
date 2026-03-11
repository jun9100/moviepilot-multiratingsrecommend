# 评分数据出现位置与取值逻辑梳理（v0.6.24）

本文按“数据来源 -> 改写 -> 展示/过滤”的链路，梳理 MoviePilot + 插件里所有关键评分位置，便于排障。

## 1. 一眼看懂：哪些位置在用评分

- 推荐/搜索卡片右上角主分：前端读取 `media.vote_average`。
- 详情页星级：前端读取 `media.vote_average`。
- 详情页评分串（tagline 位置）：前端读取 `media.tagline`。
- 工作流“过滤媒体数据”的评分条件：后端用 `media.vote_average < params.vote` 判断。

结论：`vote_average` 是“主评分唯一入口”；`tagline` 只负责展示各平台评分串。

## 2. 插件接管范围（哪些接口会被改写）

插件通过 `get_module()` 动态接管媒体列表/单条识别相关方法：

- 列表类（推荐/搜索）：`search_medias`、`tmdb_trending`、`movie_hot`、`bangumi_calendar` 等
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:41`
- 单条识别类（详情链路）：`recognize_media` / `async_recognize_media`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:35`
- 接管入口：`get_module()`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:302`

这意味着推荐页、搜索结果、详情识别、工作流获取媒体都会吃到插件改写后的评分。

## 3. 主评分计算规则（当前 v0.6.13）

主评分函数：`_select_primary_rating()`

- 代码位置：
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:2324`
- 规则顺序：
  1. 若豆瓣和 TMDB 同时存在：取 `min(豆瓣, TMDB)`
  2. 仅有豆瓣：取豆瓣
  3. 仅有 TMDB：取 TMDB
  4. 前两者缺失：回退 IMDb
  5. 再缺失：回退 Bangumi

## 4. 字段写回规则（决定前端/工作流看到什么）

在 `_enrich_media()` 里统一写回：

- `media.vote_average = primary_rating`（主评分）
- `media.tagline = _merge_rating_tagline(display_ratings)`（评分串）

代码位置：
- `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:1018`
- `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:1021`
- `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:1022`

评分串平台顺序固定：`TMDB / 豆瓣 / IMDb / Bangumi`
- `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:2342`
- `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:2449`

## 5. overview/tagline 清洗逻辑

- 不再往 `overview` 写“当前分/全部评分”提示。
- 进入补分前会清理历史评分前缀，并清空原 `tagline`。

代码位置：
- `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:943`
- `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:944`
- `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:2442`

## 6. 前端各显示位实际取值

### 6.1 卡片右上角（推荐/搜索列表）

当前前端逻辑：
- 优先取 `overview` 第一行里旧格式 `评分：...` 的解析值（若存在）
- 否则回退 `props.media.vote_average`

代码位置：
- `/Volumes/Data/vscode/MoviePilot-Frontend/src/components/cards/MediaCard.vue:423`
- `/Volumes/Data/vscode/MoviePilot-Frontend/src/components/cards/MediaCard.vue:444`
- `/Volumes/Data/vscode/MoviePilot-Frontend/src/components/cards/MediaCard.vue:543`

说明：插件已清理评分前缀并不再写入该格式，因此正常情况下会走 `vote_average`（即主评分规则）。

### 6.2 详情页评分串（tagline 位置）

- 前端直接渲染 `mediaDetail.tagline`
- `/Volumes/Data/vscode/MoviePilot-Frontend/src/views/discover/MediaDetailView.vue:667`

### 6.3 详情页星级（右侧评分）

- 前端直接渲染 `mediaDetail.vote_average`
- `/Volumes/Data/vscode/MoviePilot-Frontend/src/views/discover/MediaDetailView.vue:823`
- `/Volumes/Data/vscode/MoviePilot-Frontend/src/views/discover/MediaDetailView.vue:898`
- `/Volumes/Data/vscode/MoviePilot-Frontend/src/views/discover/MediaDetailView.vue:931`

## 7. 工作流过滤评分使用的是哪个平台

工作流“过滤媒体数据”动作只看 `media.vote_average`：
- `/Volumes/Data/vscode/MoviePilot/app/workflow/actions/filter_medias.py:58`

工作流“获取媒体数据”动作从推荐接口拉取 `MediaInfo` 列表：
- `/Volumes/Data/vscode/MoviePilot/app/workflow/actions/fetch_medias.py:166`
- 推荐接口入口示例：`/api/v1/recommend/*`
  - `/Volumes/Data/vscode/MoviePilot/app/api/endpoints/recommend.py:31`

由于推荐/识别链路已被插件接管并改写 `vote_average`，所以工作流过滤评分 = 插件主评分（即豆瓣/TMDB取低分后再回退）。

如果工作流中接了插件动作 `filter_medias_keywords`，还可额外叠加两条“评分稳定性闸门”：
- `min_vote_count`：最低投票人数
- `min_days_since_release`：上映最短天数

当前判定（v0.6.24）：
- 仅当 `vote_count > min_vote_count` 且 `上映天数 > min_days_since_release` 才放行。
- 若上映日期缺失，则自动降级为仅判断 `vote_count > min_vote_count`。
- 不满足上述条件会在“添加订阅”前被剔除，后续由下一次工作流再评估。

兼容说明（v0.6.20）：
- 对于前端不渲染 `InvokePluginAction` 节点的 MP 版本，可启用插件配置中的“工作流自动过滤”，无需在画布插入该节点。
- 自动过滤会在工作流“获取媒体数据”阶段生效，使用同样的关键词排除与稳定性闸门参数。

工作流模组推荐（v0.6.24）：
- 推荐链路：`获取媒体 -> 过滤媒体 -> 调用插件(过滤媒体关键词) -> 添加订阅`
- 当 `调用插件` 节点的 `action_params` 为空时，插件会自动使用配置页里的默认参数（关键词排除/最低投票人数/上映最短天数）。
- 因此其他用户只需要插入该节点并选择动作，不需要再手工写 JSON 参数。
- 插件会追加 `媒体过滤结果` 到执行历史；默认 `发送消息` 动作会在 TG 消息中显示过滤后名称列表。

## 8. 豆瓣缓存与防风控（你重点关心）

目标：尽量减少豆瓣网页抓取，优先使用本地缓存/内置详情。

关键点：
- 豆瓣评分持久缓存读写：
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:1998`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:2048`
- 按 ID 获取豆瓣详情时，先用缓存信息，再尝试内置 API，再按条件网页兜底：
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:1219`
- 网页兜底入口（含每日配额、近期 miss 冷却、Cookie）：
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:1293`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:1304`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:1313`
- 默认可仅详情页触发网页兜底（列表尽量不触网）：
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:152`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:2041`

## 9. 为什么你会看到“不是新规则”的分数（常见原因）

- 列表补分超时，回退原始分：
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:924`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:929`
- 详情补分超时，回退原始详情：
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:795`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:800`
- 列表结果缓存未过期（TTL 内看到旧结果）：
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:724`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:733`
  - `/Volumes/Data/vscode/moviepilot-multiratingsrecommend/plugins.v2/multiratingsrecommend/__init__.py:739`

## 10. 快速排障步骤（建议）

1. 先看同一条目的 `vote_average` 与 `tagline` 是否同步更新（详情页）。
2. 若列表不对、详情正确，优先怀疑列表缓存/列表补分超时。
3. 若豆瓣长期缺失，检查网页兜底是否被限额、是否命中近期 miss 冷却。
4. 工作流过滤异常时，先确认输入媒体的 `vote_average`，再看 `filter_medias` 的 `vote` 阈值。
