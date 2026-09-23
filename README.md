# llm-top-news

LLM 生态热榜聚合：GitHub 新晋 star 榜 × HN 高分帖 × Reddit 热帖，跨源去重后按热度聚合。
与 [llm-watch](../llm-watch) 互补：llm-watch 按产品跟踪增量（release × 社区反应），本工具按热度抓快照（今天 LLM 圈什么最火）。

## 目录结构

```
config.yaml             # 三个数据源的关键词/阈值/子版（核心配置）
pipeline/
  collect.py            # 采集: GitHub Search + HN Algolia + Reddit top → data/raw/YYYY-MM-DD.json
  digest.py             # 聚合: 跨源去重 + 🔥 标记 + LLM 摘要 → log/daily/ 或 log/weekly/
data/                   # 原始数据与状态（gitignore）
log/
  daily/YYYY-MM-DD.md   # 日报
  weekly/YYYY-Www.md    # 周报
```

## 快速开始

```bash
conda activate llm-watch        # 与 llm-watch 共用环境（Python 3.12, 仅 pyyaml）
pip install -r requirements.txt

# 可选: 环境变量写入项目根 .env（已 gitignore, 脚本自动加载）
# GITHUB_TOKEN=xxx      # 可选, 无 token 也能跑（每次仅 2 个 Search 查询）
# REDDIT_CLIENT_ID/SECRET  # 可选, Reddit 源需要（公开端点被 403, 走 OAuth）
# LLM_API_BASE/KEY/MODEL  # 可选, OpenAI 兼容端点; 不配则摘要退化为截断

python pipeline/collect.py              # 采集当日快照
python pipeline/digest.py --mode daily  # 生成日报
python pipeline/digest.py --mode weekly # 生成周报（合并近 7 天）
```

## 数据源

| 源 | 取法 | 说明 |
|---|---|---|
| GitHub | Search API: 近 7 天新建 + `topic:llm` 或 `"LLM" in:name,description`（两次查询合并）, 按 star 排序 | 新晋 star 榜 |
| HN | Algolia `search_by_date` + 标题短语精确匹配 | 日榜 ≥30 分 / 周榜 ≥100 分 |
| Reddit | `r/<sub>/top.json?t=day`（OAuth, 需 `REDDIT_CLIENT_ID/SECRET`） | 默认仅 LocalLLaMA, 在 config.yaml 里按需加; 未配置凭据则跳过 |

## 聚合规则

- 标题归一化（lowercase + 去标点）后相似度 ≥0.75 或 URL 相同 → 判同题
- 同题出现在 ≥2 个源 → 🔥 跨源热点, 置顶展示
- 无 seen 状态: top 榜是快照, 当日 raw 文件即当日快照, 重跑覆盖

## 纪律

- 手动跑, 不接定时任务（需要时再加 Windows 计划任务）
- 输出是草稿, 人工终审后再发布
