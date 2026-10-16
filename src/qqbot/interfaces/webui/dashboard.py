"""Read-only Wiki projection; page identity is never a filesystem path."""
from __future__ import annotations
from dataclasses import asdict


class DashboardQuery:
    def __init__(self, traces, wiki_store=None):
        self.traces, self.wiki_store = traces, wiki_store

    def wiki_graph(self, scope=None, limit=200):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        pages = self.wiki_store.load() if self.wiki_store else []
        if scope:
            kind, _, entity = scope.partition(":")
            prefix = f"groups/{entity}" if kind == "group" else f"private/{entity}"
            roots = {p.page_id for p in pages if p.page_id in {scope, prefix}}
            adjacent = {link.target_page_id for p in pages if p.page_id in roots for link in p.links}
            pages = [p for p in pages if p.page_id in roots or p.page_id in adjacent or p.page_id.startswith(prefix+"/")]
        total = len(pages)
        pages = pages[:limit]
        ids = {p.page_id for p in pages}
        links, dangling = [], 0
        for page in pages:
            for link in page.links:
                if link.source_page_id not in ids or link.target_page_id not in ids:
                    dangling += 1
                else:
                    links.append({"source": link.source_page_id, "target": link.target_page_id,
                                  "kind": link.kind, "label": link.label, "confidence": link.confidence})
        return {"available": self.wiki_store is not None, "nodes": [
            {"id": p.page_id, "title": p.title, "kind": p.scope, "version": p.version,
             "facts": len(p.entries), "topics": len(p.topics), "slang": len(p.slang)} for p in pages
        ], "links": links[:2000], "total_nodes": total, "truncated": total>limit or len(links)>2000,
                "dangling_links": dangling}

    def wiki_page(self, page_id):
        if self.wiki_store is None:
            return None
        pages = self.wiki_store.load()
        page = next((p for p in pages if p.page_id == page_id), None)
        if page is None:
            return None
        data = asdict(page)
        data["entries"] = sorted(data["entries"], key=lambda e: -e["observed_at"])
        data["backlinks"] = [{"page_id": p.page_id, "title": p.title} for p in pages
                             if any(link.target_page_id == page_id for link in p.links)]
        return data
