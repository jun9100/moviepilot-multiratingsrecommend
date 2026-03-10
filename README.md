# MultiRatingsRecommend（MoviePilot 插件）

`MultiRatingsRecommend` 会统一接管 MoviePilot 2.0 里的媒体评分，不再只改单个 TMDB 推荐源。

当前版本覆盖：

- 推荐页所有内置分类
- 媒体搜索结果
- 单个媒体详情识别结果
- 工作流里的“获取媒体数据 / 过滤媒体数据”评分链路

插件的行为是：

- 卡片右上角直接显示可获取平台中的最低分
- 默认参与比较的平台是 `TMDB / IMDb / 豆瓣`
- `Bangumi` 来源的条目会额外保留 `Bangumi` 自身评分一起参与比较
- 不再往 `overview` 里写“当前分/全部评分”提示文案
- 详情页会在 `tagline` 位置显示各平台评分串，例如：

```text
TMDB 7.2 / IMDb 8.8 / 豆瓣 6.4
```

## 仓库结构

- `package.v2.json`
- `plugins.v2/multiratingsrecommend/__init__.py`
- `icons/Moviepilot_A.png`

## 安装方式

1. 在 MoviePilot 中通过插件仓库地址安装本仓库。
2. 安装插件 `MultiRatingsRecommend`。
3. 在插件配置页启用插件，并按需填写 `OMDb API Key` 以启用 IMDb 评分。

> 当前文档对应版本：`v0.3.0`

## 配置项

- `enable`: 启用插件
- `enable_douban`: 是否让豆瓣参与最低分计算
- `enable_imdb`: 是否让 IMDb 参与最低分计算
- `omdb_api_key`: OMDb API Key
- `max_items`: 每次列表接口最大补分条数

## 展示说明

- 推荐页、搜索页、工作流中的评分过滤都基于改写后的 `vote_average`。
- 详情页不新增前端组件，而是复用现有 `tagline` 区域展示各平台评分。
- 如果没有拿到 IMDb 或豆瓣评分，会自动回退到其它已获取的平台分数。

## 说明

- 插件升级或配置变化时，会自动清理推荐缓存，避免页面继续显示旧分数。
- 由于需要做跨平台匹配，首次打开新列表时会比原生 TMDB / 豆瓣稍慢。
