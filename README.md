# MultiRatingsRecommend（MoviePilot 插件）

`MultiRatingsRecommend` 用来统一 MoviePilot 的评分显示和工作流过滤，核心目标是：

- 推荐/搜索/详情/工作流使用同一套评分逻辑
- 订阅前先做关键词拦截 + 评分稳定性拦截
- 减少豆瓣风控风险（缓存优先、网页兜底可控）

> 当前文档版本：`v0.6.24`

## 核心规则（先看这个）

- 主评分（`vote_average`）：`豆瓣` 与 `TMDB` 取低分，缺失时依次回退 `IMDb`、`Bangumi`
- 详情页评分串（`tagline`）：固定顺序 `TMDB / 豆瓣 / IMDb / Bangumi`
- 不再向 `overview` 写“当前分/全部评分”提示文案
- 工作流“过滤媒体数据”读取的也是改写后的 `vote_average`

## 3 分钟上手（推荐给所有用户）

1. 在 MP 安装并启用插件 `MultiRatingsRecommend`
2. 在插件配置页设置工作流默认值：
   - `workflow_auto_exclude`
   - `workflow_auto_min_vote_count`
   - `workflow_auto_min_days_since_release`
3. 工作流使用标准链路：

```text
获取媒体 -> 过滤媒体 -> 调用插件(过滤媒体关键词) -> 添加订阅
```

4. 在 `调用插件` 节点选择：
   - 插件：`全平台低分保护`
   - 动作：`过滤媒体关键词`
   - `action_params` 可留空 `{}`（自动使用插件默认值）

按工作流单独覆盖参数时，`action_params` 可填：

```json
{
  "exclude": "同性|男同|女同|女童|LGBT|LGBTQ|Gay|Lesbian|BL|GL|Queer|耽美|百合|杜比|Dolby|Dolby\\s*Vision|DOVI|DoVi|\\bDV\\b|HDR10\\+",
  "min_vote_count": 100,
  "min_days_since_release": 14
}
```

## 旧版前端兼容（仅兜底）

如果你的 MP 前端版本不显示 `调用插件` 节点，可临时开启：

- `workflow_auto_filter_enable = true`

开启后会在“获取媒体数据”阶段自动应用同样的过滤规则。
前端升级后，建议回到标准链路并关闭该兜底开关。

## 推荐默认值

- `workflow_auto_exclude`：
  `同性|男同|女同|女童|LGBT|LGBTQ|Gay|Lesbian|BL|GL|Queer|耽美|百合|杜比|Dolby|Dolby\s*Vision|DOVI|DoVi|\bDV\b|HDR10\+`
- `workflow_auto_min_vote_count`：`100`
- `workflow_auto_min_days_since_release`：`14`
- `enable_douban_web_fallback`：建议默认关闭；只在确有需要时开启

## 常用配置项

- 基础开关：`enable`、`enable_douban`、`enable_imdb`
- IMDb：`imdb_source`、`imdb_ratings_path`、`omdb_api_key`
- 豆瓣兜底：`enable_external_douban`、`external_douban_url_template`
- 工作流默认值：
  - `workflow_auto_exclude`
  - `workflow_auto_min_vote_count`
  - `workflow_auto_min_days_since_release`
- 旧版前端兜底：`workflow_auto_filter_enable`

## 插件 API

- `GET /api/v1/plugin/MultiRatingsRecommend/imdb/status`
- `POST /api/v1/plugin/MultiRatingsRecommend/imdb/rebuild`
- `POST /api/v1/plugin/MultiRatingsRecommend/imdb/unblock`

## 文档索引

- 工作流模组快速配置：`docs/WORKFLOW_MODULE_QUICKSTART.md`
- 评分链路排障与字段逻辑：`docs/RATING_LOGIC_MAP.md`
