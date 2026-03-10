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
- 详情页会在 `tagline` 位置显示各平台评分串，顺序固定为 `TMDB / 豆瓣 / IMDb / Bangumi`，并且不再拼接原始宣传语，例如：

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
3. 在插件配置页启用插件，并按需填写 `OMDb API Key` 或 IMDb 官方数据集路径。

> 当前文档对应版本：`v0.6.3`

## 配置项

- `enable`: 启用插件
- `enable_douban`: 是否让豆瓣参与主评分计算
- `enable_imdb`: 是否启用 IMDb 回退评分
- `enable_external_douban`: 是否启用外部豆瓣详情 API 兜底
- `external_douban_url_template`: 外部豆瓣详情 URL 模板，支持 `{douban_id} {media_type} {title} {year}`
- `douban_web_cookie`: 豆瓣网页 Cookie（可选），用于网页评分兜底在需要登录态时继续抓取评分
- `enable_diagnostics`: 是否在插件页显示最近补分诊断记录
- `imdb_source`: IMDb 数据来源，可选 `auto / dataset / omdb`
- `imdb_ratings_path`: IMDb 官方 `title.ratings.tsv` 或 `title.ratings.tsv.gz` 的本地路径
- `omdb_api_key`: OMDb API Key
- `max_items`: 每次列表接口最大补分条数

## 新增能力

- 列表补分已增加并发限制，避免一次性打爆外部评分源。
- IMDb 官方数据集支持后台自动建索引。
- 豆瓣可额外接一个外部详情 API，补齐 MP 内置豆瓣详情链路拿不到分数的条目。
- 当豆瓣 `douban_id` 已知但内置详情仍无评分时，插件会尝试从豆瓣网页提取评分作为最后兜底。
- 插件页支持补分诊断模式，可直接查看最近的评分命中来源和失败原因。
- 新增插件 API：
  - `GET /api/v1/plugin/MultiRatingsRecommend/imdb/status`
  - `POST /api/v1/plugin/MultiRatingsRecommend/imdb/rebuild`
- OMDb 限额熔断状态会持久化保存，容器重启后不会马上重新打满额度。
- 豆瓣匹配不再只用单一标题，会同时尝试中文名、原标题、英文名和别名。

## 展示说明

- 推荐页、搜索页、工作流中的评分过滤都基于改写后的 `vote_average`。
- 详情页不新增前端组件，而是复用现有 `tagline` 区域展示各平台评分。
- 豆瓣榜单、豆瓣搜索结果会额外尝试做 TMDB 匹配，尽量避免点击条目后落到空详情页。
- 如果 MP 内置豆瓣详情接口拿不到评分，且你配置了外部豆瓣详情 URL 模板，插件会按 `douban_id` 请求外部 API 继续补分。
- 如果没有拿到 `TMDB / 豆瓣`，会自动回退到 `IMDb`；再缺失时才使用 `Bangumi`。
- IMDb 外部接口触发额度限制后，插件会自动熔断 12 小时，避免持续请求。
- OMDb 熔断状态会持久化保存，直到过期后才自动恢复。
- 如果配置了 IMDb 官方数据集路径，插件会自动在插件数据目录生成本地 SQLite 索引，后续优先本地查分。
- 如果数据集还没建立好索引，插件会先在后台建索引，不阻塞页面请求。

## 说明

- 插件升级或配置变化时，会自动清理推荐缓存，避免页面继续显示旧分数。
- 由于需要做跨平台匹配，首次打开新列表时会比原生 TMDB / 豆瓣稍慢。
- 诊断模式默认关闭；开启后会在插件页展示最近若干条补分记录，方便排查为什么某个条目只剩 TMDB 分。

## 外部豆瓣详情 API

适合你已经单独部署了豆瓣服务，但 MP 内置豆瓣详情链路不稳定的场景。

配置方式：

```text
启用外部豆瓣详情 API 兜底 = 开
外部豆瓣详情 URL 模板 = http://<your-douban-api>/subject/{douban_id}
```

模板支持变量：

- `{douban_id}`: 豆瓣条目 ID
- `{media_type}`: `movie` 或 `tv`
- `{title}`: URL 编码后的标题
- `{year}`: URL 编码后的年份

插件期望外部接口返回 JSON，以下任一格式都可以：

```json
{"id":"35900174","rating":{"value":6.4},"title":"侵略机器"}
```

```json
{"data":{"id":"35900174","rating":6.4,"title":"侵略机器"}}
```

```json
{"result":{"id":"35900174","score":6.4,"title":"侵略机器"}}
```

## IMDb 官方数据集模式

适合自用私有部署。

1. 从 IMDb 官方下载 `title.ratings.tsv.gz`
2. 把文件挂载到 MoviePilot 容器可访问的位置
3. 在插件配置中设置：
   - `IMDb 数据来源 = 本地数据集优先，OMDb 回退（推荐）`
   - `IMDb title.ratings.tsv(.gz) 路径 = 容器内实际路径`

示例挂载：

```yaml
volumes:
  - /data/imdb/title.ratings.tsv.gz:/config/imdb/title.ratings.tsv.gz:ro
```

示例配置路径：

```text
/config/imdb/title.ratings.tsv.gz
```

插件会自动在自身数据目录建立本地 SQLite 索引，无需手工预处理。也可以通过插件 API 主动触发重建：

```bash
curl -X POST \
  -H 'X-Api-Key: <MP_API_KEY>' \
  http://<MP_HOST>/api/v1/plugin/MultiRatingsRecommend/imdb/rebuild
```
