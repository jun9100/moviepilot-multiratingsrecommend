# MultiRatingsRecommend（MoviePilot 插件）

`MultiRatingsRecommend` 会直接接管 MoviePilot 2.0 现有的 TMDB 推荐行，不再新增推荐分类。

它的行为是：

- 保留原来的 `流行趋势 / TMDB热门电影 / TMDB热门电视剧` 位置不变
- 补充 `IMDb` 和 `豆瓣` 评分
- 从 `TMDB / IMDb / 豆瓣` 中选出最低分，回填到卡片右上角的 `vote_average`
- 在卡片简介首行写明当前分数来源，并列出全部已获取评分

这样做的目的，是避免单独使用 TMDB 分数误导判断。

## 仓库结构

- `package.v2.json`
- `plugins.v2/multiratingsrecommend/__init__.py`
- `icons/Moviepilot_A.png`

## 安装方式

1. 在 MoviePilot 中通过插件仓库地址安装本仓库。
2. 安装插件 `MultiRatingsRecommend`。
3. 在插件配置页启用插件，并按需填写 `OMDb API Key` 以启用 IMDb 评分。

> 当前文档对应版本：`v0.2.0`

## 配置项

- `enable`: 启用插件
- `enable_douban`: 是否让豆瓣参与最低分计算
- `enable_imdb`: 是否让 IMDb 参与最低分计算
- `omdb_api_key`: OMDb API Key
- `max_items`: 每页最多补分条数，超出部分保留原始 TMDB 分数

## 展示说明

- 右上角只支持显示一个数字，因此插件会直接写入最低分。
- 右上角当前无法同时显示“分数来源站点名称”，这是 MoviePilot 前端卡片组件的限制。
- 卡片简介首行会写成类似：

```text
当前分：豆瓣 6.4（取最低分）
全部评分：豆瓣 6.4 / TMDB 7.2 / IMDb 8.8
```

## 说明

- 插件升级或配置变化时，会自动清理推荐缓存，避免页面继续显示旧的 TMDB 分数。
- 如果没有拿到 IMDb 或豆瓣评分，插件会回退为 TMDB 分数。
