import argparse
import datetime
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests
import dotenv


GITHUB_API = "https://api.github.com"

DEFAULT_QUERIES = [
    # pip + venv
    'ModuleNotFoundError "No module named" language:Python is:issue is:closed',
    '"No matching distribution found" pip is:issue is:closed',
    '"Could not build wheels for" pip is:issue is:closed',
    '"subprocess-exited-with-error" pip is:issue is:closed',
    '"ResolutionImpossible" pip is:issue is:closed',
    '"CERTIFICATE_VERIFY_FAILED" pip is:issue is:closed',
    # docker compose
    '"port is already allocated" "docker compose" is:issue is:closed',
    '"Cannot connect to the Docker daemon" is:issue is:closed',
    '"Temporary failure in name resolution" docker is:issue is:closed',
    '"pull access denied" docker is:issue is:closed',
    '"manifest unknown" docker is:issue is:closed',
    # fastapi + nginx
    '"502 Bad Gateway" nginx upstream is:issue is:closed',
    '"upstream timed out" nginx is:issue is:closed',
    '"422 Unprocessable Entity" FastAPI is:issue is:closed',
    '"client intended to send too large body" nginx is:issue is:closed',
]

# fenced code blocks
CODE_FENCE_RE = re.compile(r"```(?:[\w.+-]+)?\n(.*?)```", re.S)

# 关键词：判断一个代码块是不是“错误日志/堆栈/关键报错”
ERROR_HINT_RE = re.compile(
    r"(?i)(Traceback \(most recent call last\)|"
    r"ModuleNotFoundError|ImportError|"
    r"No matching distribution found|Could not build wheels|subprocess-exited-with-error|ResolutionImpossible|CERTIFICATE_VERIFY_FAILED|"
    r"502 Bad Gateway|upstream timed out|Unprocessable Entity|client intended to send too large body|"
    r"Cannot connect to the Docker daemon|port is already allocated|Temporary failure in name resolution|pull access denied|manifest unknown)"
)

def utc_now_iso() -> str:
    return datetime.now(datetime.timezone.utc).isoformat()

def build_session(token: Optional[str]) -> requests.Session:
    s = requests.Session()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "error-assistant-dataset-builder",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    s.headers.update(headers)
    return s

def build_session(token: Optional[str]) -> requests.Session:
    s = requests.Session()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "error-assistant-dataset-builder",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    s.headers.update(headers)
    return s

def maybe_sleep_rate_limit(headers: Dict[str, str]) -> None:
    remaining = headers.get("X-RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset")
    if remaining == "0" and reset:
        reset_epoch = int(reset)
        wait = max(0, reset_epoch - int(time.time()) + 2)
        if wait > 0:
            print(f"[rate-limit] sleeping {wait}s until reset...")
            time.sleep(wait)

def github_get_json(session: requests.Session, url: str, params=None, max_retries: int = 4):
    for attempt in range(max_retries):
        r = session.get(url, params=params, timeout=30)

        # 处理限流（Search 很容易触发）
        if r.status_code == 403:
            maybe_sleep_rate_limit(r.headers)

        # 处理临时错误
        if r.status_code in (429, 500, 502, 503, 504):
            backoff = 2 ** attempt
            print(f"[retry] {r.status_code} {url} backoff={backoff}s")
            time.sleep(backoff)
            continue

        r.raise_for_status()
        return r.json(), r.headers

    raise RuntimeError(f"Failed after retries: {url}")

def parse_next_link(headers: Dict[str, str]) -> Optional[str]:
    link = headers.get("Link", "")
    if not link:
        return None
    parts = [p.strip() for p in link.split(",")]
    for p in parts:
        if 'rel="next"' in p:
            return p[p.find("<")+1:p.find(">")]
    return None

def extract_error_blocks(text: str, max_blocks: int = 8, max_chars: int = 4000) -> List[str]:
    text = text or ""
    blocks: List[str] = []

    # 优先抓 fenced code blocks
    for b in CODE_FENCE_RE.findall(text):
        b = b.strip()
        if not b:
            continue
        if ERROR_HINT_RE.search(b):
            blocks.append(b[:max_chars])
        if len(blocks) >= max_blocks:
            return blocks

    # 如果没有 fenced，但正文里明显含错误提示，则保留前一段（轻度兜底）
    if not blocks and ERROR_HINT_RE.search(text):
        blocks.append(text[:max_chars])

    return blocks

def search_issues(session: requests.Session, query: str, max_items: int) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page = 1
    per_page = 100

    while len(items) < max_items:
        params = {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": per_page,
            "page": page,
        }
        data, _ = github_get_json(session, f"{GITHUB_API}/search/issues", params=params)
        batch = data.get("items", [])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
        time.sleep(0.8)  # 温和节流

    return items[:max_items]

def fetch_comments(session: requests.Session, comments_url: str, max_comments: int) -> List[Dict[str, Any]]:
    comments: List[Dict[str, Any]] = []
    if not comments_url or max_comments <= 0:
        return comments

    url = comments_url
    params = {"per_page": 100}

    while url and len(comments) < max_comments:
        data, headers = github_get_json(session, url, params=params)
        if not isinstance(data, list):
            break

        for c in data:
            comments.append({
                "id": c.get("id"),
                "user": (c.get("user") or {}).get("login"),
                "created_at": c.get("created_at"),
                "body": c.get("body") or "",
            })
            if len(comments) >= max_comments:
                break

        url = parse_next_link(headers)
        params = None
        time.sleep(0.2)

    return comments

def build_raw_record(issue: Dict[str, Any], comments: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
    body = issue.get("body") or ""
    comments_text = "\n\n".join([c.get("body") or "" for c in comments])
    repo = (issue.get("repository_url") or "").replace(f"{GITHUB_API}/repos/", "")

    return {
        "stage": "raw",
        "fetched_at": utc_now_iso(),
        "source": "github",
        "query": query,

        "issue": {
            "id": issue.get("id"),
            "number": issue.get("number"),
            "repo": repo,
            "url": issue.get("html_url"),
            "title": issue.get("title"),
            "state": issue.get("state"),
            "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"),
            "closed_at": issue.get("closed_at"),
            "labels": [lb.get("name") for lb in (issue.get("labels") or []) if isinstance(lb, dict)],
            "comments_count": issue.get("comments"),
            "body": body,
        },

        "comments": comments,
        # 轻度抽取：方便你后续 build_messages 阶段更快定位报错
        "error_blocks": extract_error_blocks(body + "\n\n" + comments_text),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出 raw JSONL 文件路径")
    ap.add_argument("--max-items-per-query", type=int, default=300, help="每个 query 抓取 issue 数上限")
    ap.add_argument("--max-comments", type=int, default=50, help="每个 issue 抓取评论数上限")
    ap.add_argument("--query", action="append", help="额外增加 search query（可重复传入）")
    ap.add_argument("--no-default-queries", action="store_true", help="不使用内置默认 queries")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[warn] 未检测到 GITHUB_TOKEN（强烈建议设置，否则很快限流）")

    session = build_session(token)

    queries: List[str] = []
    if not args.no_default_queries:
        queries.extend(DEFAULT_QUERIES)
    if args.query:
        queries.extend(args.query)
    if not queries:
        raise SystemExit("没有任何 query。请使用默认 queries 或 --query 添加。")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    seen_issue_ids = set()
    total = 0

    with open(args.out, "w", encoding="utf-8") as f:
        for q in queries:
            print(f"\n[search] {q}")
            issues = search_issues(session, q, max_items=args.max_items_per_query)
            print(f"[search] got {len(issues)} issues")

            for it in issues:
                iid = it.get("id")
                if not iid or iid in seen_issue_ids:
                    continue
                seen_issue_ids.add(iid)

                comments: List[Dict[str, Any]] = []
                try:
                    if (it.get("comments") or 0) > 0:
                        comments = fetch_comments(session, it.get("comments_url"), max_comments=args.max_comments)
                except Exception as e:
                    print(f"[warn] comments fetch failed: {it.get('html_url')} err={e}")

                rec = build_raw_record(it, comments, query=q)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                total += 1

                if total % 50 == 0:
                    print(f"[progress] wrote {total} records...")

                time.sleep(0.2)

    print(f"\n[done] wrote {total} records to {args.out}")

if __name__ == "__main__":
    main()