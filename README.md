# llm-top-news

LLM 生态热榜聚合：GitHub 新晋 star 榜 × HN 高分帖 × Reddit 热帖，跨源去重后按热度聚合。
与 [llm-watch](../llm-watch) 互补：llm-watch 按产品跟踪增量（release × 社区反应），本工具按热度抓快照（今天 LLM 圈什么最火）。

## 目录结构

```
config.yaml             # 三个数据源的关键词/阈值/子版（核心配置）
pipeline/
  collect.py            # 采集: GitHub Search + HN Top Stories/MiniLM + Reddit top → data/raw/YYYY-MM-DD.json
  semantic_filter.py    # 本地 MiniLM 语义排序
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
# GITHUB_TOKEN=xxx      # 推荐, 提高 GitHub Search API 配额
# REDDIT_CLIENT_ID/SECRET  # 可选, Reddit 源需要（公开端点被 403, 走 OAuth）
# LLM_API_BASE/KEY/MODEL  # 可选, OpenAI 兼容端点; 不配则摘要退化为截断

python pipeline/collect.py              # 采集当日快照
python pipeline/digest.py --mode daily  # 生成日报
python pipeline/digest.py --mode weekly # 生成周报（合并近 7 天）
```

HN 从 Firebase Top Stories 取前 100 条，经时间/分数过滤后，先用 `hn.pre_filter_keywords`（139 个 LLM 相关词，小写子串匹配）预过滤，再用本地 `all-MiniLM-L6-v2` 按语义相关性排序，入选需同时满足「通过预过滤 + 分数 ≥ `min_score`」，默认取 Top 10。完整候选（含分数/排名/预过滤标记）另存 `data/raw/YYYY-MM-DD.hn_candidates.json` 供人工审查召回质量。配置位于 `config.yaml` 的 `hn.semantic_filter`；临时关闭可设置 `enabled: false`。GitHub Actions 会缓存 Hugging Face 模型目录，首次运行需要下载模型，后续运行复用缓存。

## 数据源

| 源 | 取法 | 说明 |
|---|---|---|
| GitHub | Search API: 10 个 topic 分别查询新建项目和活跃存量项目（共约 20 次请求）, 按 star 排序 | 新建榜与存量活跃榜分开展示 |
| HN | Firebase Top Stories 前 100 → 时间/分数过滤 → 关键词预过滤 → 本地 MiniLM 语义排序 | 候选 ≥30 分，入选需通过预过滤且分数 ≥0.20，默认取 Top 10 |
| Reddit | `r/<sub>/top.json?t=day`（OAuth, 需 `REDDIT_CLIENT_ID/SECRET`） | 默认仅 LocalLLaMA, 在 config.yaml 里按需加; 未配置凭据则跳过 |

## 聚合规则

- 标题归一化（lowercase + 去标点）后相似度 ≥0.75 或 URL 相同 → 判同题
- 同题出现在 ≥2 个源 → 🔥 跨源热点, 置顶展示
- 无 seen 状态: top 榜是快照, 当日 raw 文件即当日快照, 重跑覆盖

## 纪律

- 手动跑, 不接定时任务（需要时再加 Windows 计划任务）
- 输出是草稿, 人工终审后再发布
