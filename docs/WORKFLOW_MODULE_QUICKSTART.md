# 工作流模组快速配置（调用插件版）

适用目标：让其他用户在 MP 工作流中用“调用插件”节点，稳定完成关键词拦截 + 新片评分稳定性闸门。

## 标准链路

```text
获取媒体 -> 过滤媒体 -> 调用插件(过滤媒体关键词) -> 添加订阅
```

## 第一步：安装并启用插件

1. 插件仓库安装 `MultiRatingsRecommend`
2. 在插件配置页启用插件

## 第二步：设置默认参数（推荐）

在插件配置页设置这些默认值（工作流 `action_params` 为空时会自动使用）：

- `workflow_auto_exclude`
- `workflow_auto_min_vote_count`
- `workflow_auto_min_days_since_release`

推荐值：

- `workflow_auto_exclude`:
  `同性|男同|女同|女童|LGBT|LGBTQ|Gay|Lesbian|BL|GL|Queer|耽美|百合|杜比|Dolby|Dolby\s*Vision|DOVI|DoVi|\bDV\b|HDR10\+`
- `workflow_auto_min_vote_count`: `100`
- `workflow_auto_min_days_since_release`: `14`

## 第三步：工作流插入调用插件节点

在 `过滤媒体` 和 `添加订阅` 之间插入 `调用插件` 节点：

- 插件：`全平台低分保护`
- 动作：`过滤媒体关键词`
- `action_params`：可以直接留空 `{}`（使用插件默认值）

## 可选：按单条工作流覆盖参数

如果某条工作流想用不同阈值，可在该节点 `action_params` 填：

```json
{
  "exclude": "同性|男同|女同|女童|LGBT|LGBTQ|Gay|Lesbian|BL|GL|Queer|耽美|百合|杜比|Dolby|Dolby\\s*Vision|DOVI|DoVi|\\bDV\\b|HDR10\\+",
  "min_vote_count": 100,
  "min_days_since_release": 14
}
```

## 兼容说明

如果某些旧版前端不显示 `调用插件` 节点，可临时启用插件配置里的：

- `workflow_auto_filter_enable = true`

它会在“获取媒体数据”阶段自动执行同样规则，作为兜底方案。
