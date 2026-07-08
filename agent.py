"""
agent.py
--------
The "intelligence" layer on top of opener.py. Instead of you typing exact
keywords, you type a plain-English request ("find that stock photo of a
beach sunset") and the model figures out which tool(s) to call:

    search_files  -> searches the loaded index by keyword
    open_file     -> opens a specific indexed file by its relative path
    recent_files  -> looks at what you recently searched for / opened

You never call these functions yourself. The model reads your sentence,
decides which function to call and with what argument, your code actually
runs it, and the result gets fed back to the model so it can decide the
next step (or just answer you). That loop is the entire trick -- see the
chat explanation for the full breakdown.

TWO BACKENDS, ZERO REQUIRED SPEND:

  1. Local / free (default): Ollama running on your own machine.
     - Install: https://ollama.com
     - Pull a tool-calling-capable model, e.g.:  ollama pull llama3.1:8b
     - Just run this script -- no API key needed, no cost per call.
     - Quality note: smaller local models are noticeably less reliable at
       picking the right tool than Claude. Fine for "find X and open it."
       Don't expect it to nail complex multi-step reasoning every time.

  2. Claude API (for when a client is paying and wants better reliability):
     - set the ANTHROPIC_API_KEY environment variable before running.
     - Each client/user brings their own key -- this script never bakes
       one in, so your 3-10 test users can each use their own billing.

The script auto-detects which backend to use: if ANTHROPIC_API_KEY is
set, it uses Claude. Otherwise it assumes Ollama is running locally.
"""

import os
import sys
import json
import requests

import opener
import memory

ANTHROPIC_MODEL = "claude-sonnet-4-6"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

SYSTEM_PROMPT = (
    "You are a local file-finding assistant. You have access to an index "
    "of files on the user's machine. When the user describes what they "
    "want (even vaguely, e.g. 'that beach photo'), call search_files with "
    "your best-guess keyword(s) first. If there are multiple plausible "
    "matches, you may call search_files more than once with different "
    "keywords, and you can call recent_files to see what the user recently "
    "worked with as a tiebreaker. Once you're confident which single file "
    "the user means, call open_file with its exact rel_path from the "
    "search results. If it's genuinely ambiguous after searching, stop and "
    "ask the user to clarify in plain text instead of guessing."
)

# ---------------------------------------------------------------------------
# Tool schemas -- Anthropic format. Converted to Ollama/OpenAI format below.
# ---------------------------------------------------------------------------

ANTHROPIC_TOOLS = [
    {
        "name": "search_files",
        "description": "Search the loaded file index by keyword. Matches against filename and relative path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Keyword or partial filename to search for."}
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "open_file",
        "description": "Open a specific file by its exact relative path (as returned by search_files).",
        "input_schema": {
            "type": "object",
            "properties": {
                "rel_path": {"type": "string", "description": "The exact rel_path value from a search_files result."}
            },
            "required": ["rel_path"],
        },
    },
    {
        "name": "recent_files",
        "description": "Get recently searched-for or opened files from history, most recent first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entries to return.", "default": 10},
                "event_type": {"type": "string", "enum": ["search", "open", "any"], "default": "any"},
            },
        },
    },
]


def _to_openai_tools(anthropic_tools):
    """Ollama's tool-calling API uses the OpenAI function-calling shape."""
    converted = []
    for t in anthropic_tools:
        converted.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        })
    return converted


# ---------------------------------------------------------------------------
# Tool dispatch -- shared by both backends
# ---------------------------------------------------------------------------

def dispatch_tool(name, tool_input, index_records):
    if name == "search_files":
        keyword = tool_input.get("keyword", "")
        matches = opener.search(index_records, keyword)
        memory.record_event("search", keyword=keyword)
        return [
            {"name": r["name"], "rel_path": r["rel_path"], "size_bytes": r["size_bytes"], "modified": r["modified"]}
            for r in matches[:20]
        ]

    if name == "open_file":
        target = tool_input.get("rel_path")
        match = next((r for r in index_records if r["rel_path"] == target), None)
        if not match:
            return {"error": f"No indexed file with rel_path '{target}'. Did you use the exact value from search_files?"}
        opener.open_record(match)
        memory.record_event("open", record=match)
        return {"status": "opened", "rel_path": target}

    if name == "recent_files":
        event_type = tool_input.get("event_type", "any")
        event_type = None if event_type == "any" else event_type
        return memory.get_recent(limit=tool_input.get("limit", 10), event_type=event_type)

    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Backend: Claude API
# ---------------------------------------------------------------------------

def run_turn_anthropic(conversation, index_records, api_key):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": conversation,
            "tools": ANTHROPIC_TOOLS,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["content"]
    conversation.append({"role": "assistant", "content": content})

    final_text_parts = []
    tool_results = []
    for block in content:
        if block["type"] == "text":
            final_text_parts.append(block["text"])
        elif block["type"] == "tool_use":
            result = dispatch_tool(block["name"], block["input"], index_records)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result),
            })

    if tool_results:
        conversation.append({"role": "user", "content": tool_results})
        return run_turn_anthropic(conversation, index_records, api_key)  # continue the loop

    return "\n".join(final_text_parts) if final_text_parts else "(no response text)"


# ---------------------------------------------------------------------------
# Backend: local Ollama (OpenAI-compatible tool calling)
# ---------------------------------------------------------------------------

def run_turn_ollama(conversation, index_records):
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": conversation,
            "tools": _to_openai_tools(ANTHROPIC_TOOLS),
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    message = data["message"]
    conversation.append(message)

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        for call in tool_calls:
            fn = call["function"]
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            result = dispatch_tool(fn["name"], args, index_records)
            conversation.append({
                "role": "tool",
                "content": json.dumps(result),
            })
        return run_turn_ollama(conversation, index_records)  # continue the loop

    return message.get("content", "(no response text)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    backend = "anthropic" if api_key else "ollama"

    print(f"Backend: {'Claude API' if backend == 'anthropic' else f'Local Ollama ({OLLAMA_MODEL}, free)'}")
    if backend == "ollama":
        print(f"(Set ANTHROPIC_API_KEY to use Claude instead. Make sure Ollama is running at {OLLAMA_URL}.)")

    try:
        source = opener.choose_source()
    except Exception as e:
        print(f"Something went wrong choosing a file: {e}")
        return
    if source is None:
        print("No file selected. Exiting.")
        return

    try:
        records, root_folder, note = opener.load_index(source)
    except Exception as e:
        print(f"Couldn't load that file: {e}")
        return
    if note:
        print(f"\n{note}")
        return

    print(f"\nLoaded {len(records)} indexed files. Root folder: {root_folder or '(not recorded)'}")
    print("Describe what you want in plain English. Type 'quit' to exit.\n")

    if backend == "anthropic":
        conversation = []
    else:
        conversation = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye.")
            break
        if not user_input:
            continue

        conversation.append({"role": "user", "content": user_input})

        try:
            if backend == "anthropic":
                reply = run_turn_anthropic(conversation, records, api_key)
            else:
                reply = run_turn_ollama(conversation, records)
        except requests.exceptions.ConnectionError:
            print("Couldn't reach the model backend. If using Ollama, make sure it's running (`ollama serve`).\n")
            conversation.pop()  # drop the unanswered user turn
            continue
        except Exception as e:
            print(f"Agent error: {e}\n")
            conversation.pop()
            continue

        print(f"\nAgent: {reply}\n")


if __name__ == "__main__":
    main()
