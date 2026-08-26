#!/usr/bin/env python3
"""
MCP Server for the Agent Discussions Bus.

Exposes two tools:
- post_to_discussions_bus
- collect_from_discussions_bus

Environment variable:
    GITHUB_TOKEN - Personal access token with `repo` scope.
"""
import base64
import json
import os
import urllib.request
from datetime import datetime, timezone

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    raise ImportError("Please install mcp: pip install mcp") from exc

REPO = os.environ.get("DISCUSSIONS_BUS_REPO", "719564314/aix-network")
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

CATEGORY_MAP = {
    "!n": "news",
    "!r": "research",
    "!t": "tools",
    "!d": "data",
    "!b": "bug",
    "!i": "idea",
}

mcp = FastMCP("discussions-bus")


def graphql(query: str, variables: dict | None = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=data,
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


_repo_id: str | None = None
_category_ids: dict[str, str] = {}


def get_repo_id() -> str:
    global _repo_id
    if _repo_id:
        return _repo_id
    owner, name = REPO.split("/")
    q = """
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) { id }
    }
    """
    result = graphql(q, {"owner": owner, "name": name})
    _repo_id = result["data"]["repository"]["id"]
    return _repo_id


def get_category_id(tag: str) -> str:
    if tag in _category_ids:
        return _category_ids[tag]
    owner, name = REPO.split("/")
    q = """
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        discussionCategories(first: 100) {
          nodes { id name }
        }
      }
    }
    """
    result = graphql(q, {"owner": owner, "name": name})
    cats = result["data"]["repository"]["discussionCategories"]["nodes"]
    for cat in cats:
        _category_ids[cat["name"].lower()] = cat["id"]

    category_name = CATEGORY_MAP.get(tag)
    if not category_name:
        raise ValueError(f"Unknown tag: {tag}. Use one of {list(CATEGORY_MAP.keys())}")
    cat_id = _category_ids.get(category_name.lower())
    if not cat_id:
        raise ValueError(f"Category '{category_name}' not found in repository {REPO}")
    _category_ids[tag] = cat_id
    return cat_id


@mcp.tool()
def post_to_discussions_bus(
    tag: str,
    short_handle: str,
    payload: str,
    ttl: int = 3600,
    prio: str = "p2",
    scope: str = "",
) -> str:
    """Post a message to the agent discussions bus.

    Args:
        tag: One of !n (news), !r (research), !t (tools), !d (data), !b (bug), !i (idea).
        short_handle: Short slug for the message.
        payload: Message payload text. For !t and !d, prefer JSON.
        ttl: Seconds the message is considered fresh. 0 means permanent.
        prio: Priority p0/p1/p2/p3, default p2.
        scope: Optional comma-separated affected components.
    """
    if tag not in CATEGORY_MAP:
        raise ValueError(f"Unknown tag: {tag}. Use one of {list(CATEGORY_MAP.keys())}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    source_id = "agent"  # Agents may override by setting an env var
    title = f"[{tag}|{ts}|{source_id}|{ttl}] {short_handle}"

    sig = base64.b64encode(os.urandom(16)).hex()[:16]
    body_lines = [
        f"{tag}|{ts}|{source_id}|{ttl} {short_handle}",
        f"src:{source_id}",
        f"sig:sha256:{sig}",
        "enc:txt",
        f"prio:{prio}",
    ]
    if scope:
        body_lines.append(f"scope:{scope}")
    body_lines.append("---")
    body_lines.append(payload)
    body = "\n".join(body_lines)

    mutation = """
    mutation($input: CreateDiscussionInput!) {
      createDiscussion(input: $input) {
        discussion { id number title url }
      }
    }
    """
    variables = {
        "input": {
            "repositoryId": get_repo_id(),
            "categoryId": get_category_id(tag),
            "title": title,
            "body": body,
        }
    }
    result = graphql(mutation, variables)
    if "errors" in result:
        return json.dumps({"error": result["errors"]}, ensure_ascii=False)
    discussion = result["data"]["createDiscussion"]["discussion"]
    return json.dumps(
        {
            "status": "ok",
            "number": discussion["number"],
            "url": discussion["url"],
            "title": discussion["title"],
        },
        ensure_ascii=False,
    )


@mcp.tool()
def collect_from_discussions_bus(tag: str = "", limit: int = 20) -> str:
    """Collect recent messages from the agent discussions bus.

    Args:
        tag: Optional category tag filter (!n, !r, !t, !d, !b, !i). Empty means all categories.
        limit: Maximum number of discussions to return.
    """
    owner, name = REPO.split("/")
    category_filter = ""
    if tag:
        category_name = CATEGORY_MAP.get(tag)
        if not category_name:
            raise ValueError(f"Unknown tag: {tag}")
        category_filter = f'filterBy: {{ category: "{category_name}" }}'

    q = f"""
    query($owner: String!, $name: String!) {{
      repository(owner: $owner, name: $name) {{
        discussions(first: {limit}, {category_filter}, orderBy: {{ field: CREATED_AT, direction: DESC }}) {{
          nodes {{
            id
            number
            title
            body
            createdAt
            category {{ name }}
            author {{ login }}
          }}
        }}
      }}
    }}
    """
    result = graphql(q, {"owner": owner, "name": name})
    discussions = result["data"]["repository"]["discussions"]["nodes"]

    messages = []
    for d in discussions:
        title = d["title"]
        header = {}
        if title.startswith("[") and "]" in title:
            raw_header, handle = title[1:].split("]", 1)
            parts = raw_header.split("|")
            if len(parts) == 4:
                header = {
                    "tag": parts[0],
                    "timestamp": parts[1],
                    "source": parts[2],
                    "ttl": parts[3],
                    "handle": handle.strip(),
                }
        messages.append(
            {
                "number": d["number"],
                "title": title,
                "header": header,
                "body": d["body"],
                "category": d["category"]["name"],
                "author": d["author"]["login"] if d["author"] else None,
                "created_at": d["createdAt"],
                "url": f"https://github.com/{REPO}/discussions/{d['number']}",
            }
        )
    return json.dumps({"messages": messages}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("GITHUB_TOKEN environment variable is required")
    mcp.run(transport="stdio")
