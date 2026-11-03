import httpx

DDG_API = "https://api.duckduckgo.com/"


def search(query: str, max_results=5) -> list[dict]:
    params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
    resp = httpx.get(DDG_API, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    results = []

    if data.get("AbstractText"):
        results.append({
            "title": data.get("Heading", "Summary"),
            "snippet": data["AbstractText"],
            "url": data.get("AbstractURL", ""),
        })

    for item in data.get("Results", []):
        results.append({
            "title": item.get("Text", ""),
            "snippet": item.get("Text", ""),
            "url": item.get("FirstURL", ""),
        })

    for item in data.get("RelatedTopics", []):
        if "Topics" in item:
            for sub in item["Topics"][:2]:
                results.append({
                    "title": sub.get("Text", ""),
                    "snippet": sub.get("Text", ""),
                    "url": sub.get("FirstURL", ""),
                })
        else:
            results.append({
                "title": item.get("Text", ""),
                "snippet": item.get("Text", ""),
                "url": item.get("FirstURL", ""),
            })

    return results[:max_results]


def format_results(results: list[dict]) -> str:
    if not results:
        return ""
    lines = ["[WEB SEARCH RESULTS]"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        url = r.get("url", "")
        lines.append(f"  {i}. {title}")
        if snippet:
            lines.append(f"     {snippet[:300]}")
        if url:
            lines.append(f"     {url}")
    return "\n".join(lines)
