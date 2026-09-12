"""
ima 搜索结果提取器 — 从 search_knowledge 落盘文件中提取指定关键词的命中上下文
用法:
    python utils/kb_extract.py <结果文件路径> 关键词1 关键词2 ...
输出: 每个关键词在哪些文档命中 + 上下文片段
"""
import json
import sys
import os
import re


def extract(filepath, keywords):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("searched_knowledge_list", [])
    print(f"搜索命中 {len(items)} 条文档\n")
    for kw in keywords:
        hits = []
        for it in items:
            k = it.get("knowledge", {})
            title = k.get("title", "?")
            hl = it.get("highlight_content") or ""
            idx = hl.find(kw)
            if idx >= 0:
                snippet = re.sub(r"<[^>]+>", "", hl[max(0, idx - 50):idx + len(kw) + 80])
                snippet = snippet.replace("\n", " ").strip()
                hits.append((title, snippet))
        if hits:
            print(f"◈ 关键词「{kw}」命中 {len(hits)} 条:")
            for title, sn in hits[:5]:
                print(f"   [{title}]")
                print(f"     …{sn}…")
        else:
            print(f"◈ 关键词「{kw}」: 未直接命中")
        print()


if __name__ == "__main__":
    fp = sys.argv[1]
    kws = sys.argv[2:]
    extract(fp, kws)
