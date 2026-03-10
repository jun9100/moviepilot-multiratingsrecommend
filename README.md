# MultiRatingsRecommend（MoviePilot 插件）

`MultiRatingsRecommend` 会统一接管 MoviePilot 2.0 里的媒体评分，不再只改单个 TMDB 推荐源。

当前版本覆盖：

- 推荐页所有内置分类
- 媒体搜索结果
- 单个媒体详情识别结果
- 工作流里的“获取媒体数据 / 过滤媒体数据”评分链路

插件的行为是：

- 卡片右上角优先显示 `TMDB / 豆瓣` 两者中的低分
- 如果 `TMDB` 和 `豆瓣` 都没有分数，则回退显示 `IMDb`
- 如果前三者都缺失，而条目本身带有 `Bangumi` 评分，则继续回退为 `Bangumi`
- 不再往 `overview` 里写“当前分/全部评分”提示文案
- 详情页会在 `tagline` 位置显示各平台评分串，顺序固定为 `TMDB / 豆瓣 / IMDb / Bangumi`，例如：

```text
TMDB 7.2 / 豆瓣 6.4 / IMDb 8.8
```

## 仓库结构

- `package.v2.json`
- `plugins.v2/multiratingsrecommend/__init__.py`
- `icons/Moviepilot_A.png`

## 安装方式

1. 在 MoviePilot 中通过插件仓库地址安装本仓库。
2. 安装插件 `MultiRatingsRecommend`。
3. 在插件配置页启用插件，并按需填写 `OMDb API Key` 以启用 IMDb 评分。

> 当前文档对应版本：`v0.3.1`

## 配置项

- `enable`: 启用插件
- `enable_douban`: 是否让豆瓣参与主评分计算
- `enable_imdb`: 是否启用 IMDb 回退评分
- `omdb_api_key`: OMDb API Key
- `max_items`: 每次列表接口最大补分条数

## 展示说明

- 推荐页、搜索页、工作流中的评分过滤都基于改写后的 `vote_average`。
- 详情页不新增前端组件，而是复用现有 `tagline` 区域展示各平台评分。
- 豆瓣榜单、豆瓣搜索结果会额外尝试做 TMDB 匹配，尽量避免点击条目后落到空详情页。
- 如果没有拿到 `TMDB / 豆瓣`，会自动回退到 `IMDb`；再缺失时才使用 `Bangumi`。

## 说明

- 插件升级或配置变化时，会自动清理推荐缓存，避免页面继续显示旧分数。
- 由于需要做跨平台匹配，首次打开新列表时会比原生 TMDB / 豆瓣稍慢。
