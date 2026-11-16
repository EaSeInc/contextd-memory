#!/usr/bin/env python3
"""contextd_mcp - stdio MCP server exposing contextd memory to AI agents.

No third-party dependencies: JSON-RPC 2.0 over stdin/stdout plus urllib.
Works on Linux and Windows.

Environment:
  CONTEXTD_URL    default http://127.0.0.1:8090
  CONTEXTD_TOKEN  token from /etc/contextd/contextd.env
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

URL = os.environ.get("CONTEXTD_URL", "http://127.0.0.1:8090").rstrip("/")
TOKEN = os.environ.get("CONTEXTD_TOKEN", "").strip()
PROTOCOL = "2025-06-18"

TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Search long-term memory for facts relevant to the current task. "
            "Call this before answering questions about past decisions, "
            "project conventions, printer settings or model configuration."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for"},
                "limit": {"type": "integer", "description": "Max facts, default 6"},
                "budget": {"type": "integer", "description": "Token budget, default 800"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_write",
        "description": (
            "Store one atomic fact (1-2 sentences) in long-term memory. "
            "Use for decisions, constraints, settings and paths worth remembering."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The fact itself"},
                "topic": {"type": "string", "description": "e.g. solidcraft/bracket"},
                "kind": {"type": "string", "description": "fact|decision|constraint|settings|path"},
                "tags": {"type": "string", "description": "Space separated tags"},
                "pinned": {"type": "boolean", "description": "Always include in context"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_context",
        "description": "Return the pinned, always-on memory block for this project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "budget": {"type": "integer", "description": "Token budget, default 800"},
            },
        },
    },
    {
        "name": "memory_forget",
        "description": "Delete a fact by id, or every fact of a topic.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "topic": {"type": "string"},
            },
        },
    },
]


def _call(method: str, path: str, body=None, params=None):
    url = URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if TOKEN:
        req.add_header("X-Context-Token", TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _dispatch(name: str, args: dict) -> str:
    if name == "memory_search":
        res = _call(
            "GET",
            "/recall",
            params={
                "q": args["query"],
                "limit": int(args.get("limit", 6)),
                "budget": int(args.get("budget", 800)),
            },
        )
        block = res.get("context") or "(nothing found)"
        return f"{block}\n\n[used ~{res.get('tokens_est', 0)} tokens, vectors={res.get('vectors')}]"
    if name == "memory_write":
        body = {
            "text": args["text"],
            "topic": args.get("topic", "general"),
            "kind": args.get("kind", "fact"),
            "tags": args.get("tags", ""),
            "source": "agent",
            "pinned": bool(args.get("pinned", False)),
        }
        res = _call("POST", "/remember", body=body)
        return f"stored id={res['id']} ({res['action']})"
    if name == "memory_context":
        res = _call("GET", "/context", params={"budget": int(args.get("budget", 800))})
        return res.get("context") or "(memory is empty)"
    if name == "memory_forget":
        params = {}
        if "id" in args:
            params["id"] = int(args["id"])
        if "topic" in args:
            params["topic"] = args["topic"]
        if not params:
            raise ValueError("give id or topic")
        res = _call("DELETE", "/forget", params=params)
        return f"deleted {res.get('deleted', 0)} fact(s)"
    raise ValueError(f"unknown tool: {name}")


def handle(req: dict):
    mid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "contextd", "version": "0.1.0"},
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text = _dispatch(name, args)
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except Exception as exc:  # tool errors go back as content, not protocol errors
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": f"error: {exc}"}],
                    "isError": True,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"unknown method: {method}"},
    }


def main() -> None:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            print(f"contextd_mcp: bad json ({exc})", file=sys.stderr, flush=True)
            continue
        if isinstance(req, list):
            responses = [handle(r) for r in req]
            responses = [r for r in responses if r]
            if responses:
                print(json.dumps(responses, ensure_ascii=False), flush=True)
            continue
        resp = handle(req)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
