"""Local MiniLM semantic ranking for news candidates."""

from __future__ import annotations

import re

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_QUERY = (
    "News about large language models, generative AI, AI agents, RAG, MCP, "
    "embeddings, model training, inference, coding assistants, and AI developer tools."
)


def rank_records(
    records: list[dict],
    top_k: int,
    model_name: str = DEFAULT_MODEL,
    query: str = DEFAULT_QUERY,
) -> list[dict]:
    """Score all records and return them with rank and Top-K classification."""
    if not records:
        return []
    if top_k < 1:
        raise ValueError("top_k must be greater than 0")

    import torch
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name)
    model.eval()
    texts = []
    for item in records:
        story_text = re.sub(r"<[^>]+>", " ", str(item.get("story_text") or ""))
        texts.append(
            f"{item.get('title') or ''} {story_text} {item.get('url') or ''}"
        )

    with torch.no_grad():
        encoded = tokenizer(
            texts + [query],
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        output = model(**encoded)
        mask = encoded["attention_mask"].unsqueeze(-1).to(output.last_hidden_state.dtype)
        pooled = (output.last_hidden_state * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
        embeddings = torch.nn.functional.normalize(pooled, p=2, dim=1)
        scores = (embeddings[:-1] @ embeddings[-1]).tolist()

    ranked_indexes = sorted(range(len(scores)), key=scores.__getitem__, reverse=True)
    ranks = {record_index: rank for rank, record_index in enumerate(ranked_indexes, 1)}
    selected_indexes = set(ranked_indexes[:min(top_k, len(records))])

    ranked_records = []
    for record_index, (item, score) in enumerate(zip(records, scores)):
        enriched = dict(item)
        enriched["minilm_score"] = round(float(score), 6)
        enriched["minilm_rank"] = ranks[record_index]
        enriched["minilm_relevant"] = record_index in selected_indexes
        enriched["classification"] = (
            "llm_related" if record_index in selected_indexes else "other"
        )
        ranked_records.append(enriched)
    return ranked_records
