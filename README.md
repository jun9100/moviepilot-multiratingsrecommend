# MultiRatingsRecommend（MoviePilot 插件）

`MultiRatingsRecommend` 为 MoviePilot 2.0 推荐页新增三条基于 TMDB 的多评分推荐源：

- `TMDB流行趋势+IMDb/豆瓣`
- `TMDB热门电影+IMDb/豆瓣`
- `TMDB热门电视剧+IMDb/豆瓣`

插件会保留卡片右上角的 `TMDB vote_average`，并补充：

- `IMDb` 评分
- `豆瓣` 评分

当前 MoviePilot 2.0 推荐卡片右上角只能直接显示 `vote_average`，因此 IMDb 和豆瓣评分会写入卡片悬浮详情中的简介前缀。

## 仓库结构

- `package.v2.json`
- `plugins.v2/multiratingsrecommend/__init__.py`
- `icons/Moviepilot_A.png`

## 安装方式

1. 在 MoviePilot 中通过插件仓库地址安装本仓库。
2. 安装插件 `MultiRatingsRecommend`。
3. 在插件配置页启用插件，并按需填写 `OMDb API Key` 以启用 IMDb 评分补充。

## 配置项

- `enable`: 启用插件
- `enable_douban`: 补充豆瓣评分
- `enable_imdb`: 补充 IMDb 评分
- `omdb_api_key`: OMDb API Key
- `max_items`: 每个推荐源每页补充分数的最大条数

## 插件 API

- `/api/v1/plugin/MultiRatingsRecommend/recommend/tmdb_trending`
- `/api/v1/plugin/MultiRatingsRecommend/recommend/tmdb_movies`
- `/api/v1/plugin/MultiRatingsRecommend/recommend/tmdb_tvs`

认证方式：

- `X-API-KEY: <MoviePilot API_TOKEN>`
- 或浏览器登录态 / Bearer Token

## 说明

- 推荐卡右上角仍显示 `TMDB` 分数，这是 MoviePilot 当前卡片组件的限制。
- `IMDb` 和 `豆瓣` 评分会追加到卡片 hover 详情里的简介首行。
- 如果你需要把卡片右上角直接改为 IMDb，必须同时修改 MoviePilot 前端卡片组件。
