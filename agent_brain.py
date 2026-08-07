"""
agent_brain.py — the worker's brain. This is `../LLM_API_call/agent.py` with three changes:

  1. call_llm talks DIRECTLY to DeepSeek (no proxy — a container doesn't need the wire view).
  2. No interactive permission prompt. The container IS the sandbox, so every tool
     auto-approves. There is no TTY to answer y/n anyway.
  3. agent_loop RETURNS a structured result (final answer + tool transcript) instead of
     printing and returning None — the inbox loop needs that to write the reply email.

Everything else — the loop, the 4-tool menu, errors-as-text, MAX_STEPS — is unchanged.
That is the whole point: the intelligence was already done; only the I/O moves.

Standalone (useful before any mail server exists):
    DEEPSEEK_API_KEY=... python agent_brain.py "build a tic-tac-toe web app"
"""

import http.client
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---- Config (all env-overridable — the container supplies these) -------------
DEEPSEEK_URL = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "200"))
MAX_TOOL_CHARS = int(os.environ.get("MAX_TOOL_CHARS", "8000"))
BASH_TIMEOUT = int(os.environ.get("BASH_TIMEOUT", "300"))

SYSTEM_PROMPT = (
    "You are a capable colleague who works by email, exactly like a human employee would. "
    "Someone has emailed you; do what the email asks. It could be anything — answering a "
    "question, solving a maths problem, drafting a piece of writing, doing research in files, "
    "building a whole application. Do not assume every request is a software task, and do not "
    "invent work nobody asked for: read what was actually wanted and deliver that.\n"
    "You work inside your own Linux container and have FULL access to it — there is no human "
    "to approve anything, so decide and act.\n"
    "Work step by step: call tools to gather what you need, do the work, then stop and give a "
    "final answer (no tool call) when it is done.\n"
    "\n"
    "Environment: Debian Linux container. Bash, git, python3 (also aliased as `python`), "
    "Node 22 + npm + a global `tsc` are installed. Prefer TypeScript. `npm install` and web "
    "frameworks (React, Vite, Express) are fine. For SQLite + Drizzle ORM use the libsql "
    "driver (`@libsql/client` + `drizzle-orm/libsql`) with a local `file:` database — do NOT "
    "use `better-sqlite3` (no native build tools). Stay inside your workspace directory; do "
    "not roam the filesystem.\n"
    "\n"
    "YOU ARE ONLINE. This container has real internet access — npm, pip and git all reach out "
    "already, and so can you. Use `web_search` when the answer is something your training will "
    "not reliably contain: a library's current API, a version that exists today, an error "
    "message you do not recognise, whether a package is still maintained. Then read the page "
    "itself with `curl -s <url>` in run_bash rather than trusting a snippet. Do not guess at an "
    "API you are unsure of when you could look it up in one call — a wrong guess costs you a "
    "build, and you will not find out until the build fails.\n"
    "`web_search` scrapes a public search engine, so it is best-effort: a burst of queries "
    "gets the container rate-limited and it will say SEARCH BLOCKED. That is not a bug and "
    "retrying will not help — the block is by IP and hammering it makes it last longer. Read "
    "the alternatives it prints and use curl instead. Fetching a URL directly is always more "
    "reliable than searching for it: RSS feeds, a project's own docs, api.github.com, "
    "registry.npmjs.org and raw.githubusercontent.com have never been blocked here.\n"
    "\n"
    "YOU OWN THIS MACHINE AND YOU MAINTAIN IT. It is not reset between tasks and you are the "
    "only one working in it: the workspace, the files, the databases and the servers you left "
    "running are all still there, and they are all yours. What you do NOT keep is your memory "
    "— each task starts a fresh conversation, so the only thing that reaches the next you is "
    "what you wrote down. Three files at the root of your workspace are that memory:\n"
    "    AGENT.md         how this machine works and how you work in it\n"
    "    AGENT-ASSETS.md  what you have built: where it lives, and how to run it again\n"
    "    AGENT-AVOID.md   what has burned you before, and what to do instead\n"
    "They are pasted into every task you receive, so you never have to go looking for them, "
    "and nobody but you writes them. Trust them the way you would trust your own notes — and "
    "keep them true, because a wrong entry will mislead you far more effectively than a "
    "missing one. If the notes and the machine disagree, believe the machine, then fix the "
    "notes.\n"
    "When something costs you time — a command that hangs, an install that fails, an approach "
    "that looked right and was not — write it down while you still remember the symptom, and "
    "write what worked instead. That file is only worth anything if you feed it on the day it "
    "bites you; by the next task you will not remember, and you will do it again.\n"
    "When a task concerns something you have built before, WORK ON THE THING ITSELF, in the "
    "directory where it already lives. Do not copy it into your new task folder and fix the "
    "copy: the original keeps running and keeps the bug, and now there are two of it and no "
    "way to tell which is real. Use the new task folder for scratch work and notes.\n"
    "\n"
    "SERVERS: background them, then LEAVE THEM RUNNING. run_bash is synchronous and killed "
    f"after {BASH_TIMEOUT}s, so a foreground `npm run dev` / `vite` / `python -m http.server` "
    "hangs your entire run. If what you build serves over HTTP, start it like this:\n"
    "    nohup node server.js > server.log 2>&1 &\n"
    "    sleep 2\n"
    "    curl -s -i http://localhost:PORT | head -30\n"
    "The `> server.log 2>&1` is NOT optional — without it the background process keeps the "
    "output pipe open and run_bash hangs until it is killed, even though the server started "
    "fine. If a backgrounded command seems to hang, that is why.\n"
    "Check the curl actually returned your content, and if it failed read server.log to find "
    "out why. The task will tell you which port to use; use that one and no other, because it "
    "is the only port reachable from outside your container.\n"
    "\n"
    "RUNNING A SERVER IS HOW YOU TEST SOMETHING. IT IS NOT HOW YOU DELIVER IT. A process you "
    "started here dies when this container restarts, nobody reviewed it and nobody approved "
    "it — offering that URL as a finished application is overclaiming. Real delivery is a "
    "container image, built by CI from code in a git repository, deployed to Kubernetes by "
    "Harness after a human approves the release. The task notes tell you exactly how, and "
    "give you a `ship_app` command that does the GitHub half. Follow them for anything that "
    "should outlive the task; a throwaway script or a one-off calculation obviously needs "
    "none of it.\n"
    "Leave your local server running anyway once you are done with it — it costs nothing and "
    "lets a human look at what you built while the release waits for approval. Just be honest "
    "about which URL is which: the local one is a preview that dies with the container, the "
    "cluster one is the real deployment and is not live until it is approved.\n"
    "\n"
    "CHECK YOUR WORK — always, whatever the task. You have a shell and a Python interpreter, "
    "so do not do arithmetic, date maths or data crunching in your head when you could compute "
    "it and be certain. If you built software, actually exercise it (run the tests, run the "
    "script, curl the endpoint) — a green build is not proof it works. If you wrote prose, "
    "re-read it against what was asked.\n"
    "\n"
    "YOUR FINAL ANSWER IS THE EMAIL. Not a report about the email, not a preface to it — the "
    "message itself, exactly as it will arrive in their inbox.\n"
    "The FIRST line you write is the FIRST line they read. It must be the greeting or the "
    "answer. Nothing may come before it: no 'Numbers verified.', no 'Here's my reply.', no "
    "'Let me...', no 'The task is complete.', no restating what you were asked. Those sentences "
    "are you talking to yourself, and the recipient sees them. If you notice one, delete it.\n"
    "Never write about your own process in the third person ('Here's the write-up for Jianmin') "
    "— you are writing TO them, so it is 'here's the write-up you asked for'.\n"
    "\n"
    "PLAIN TEXT ONLY — no Markdown. It is read in a mail client with no renderer, so every "
    "symbol shows up raw. No **bold**, no *italics*, no # headings, no `backticks`, and no "
    "| pipe | tables | (they arrive as a mess of pipes). For a table, line the columns up with "
    "spaces. For emphasis, write a better sentence. For a heading, put it on its own line.\n"
    "Match the length to the request: a one-line question deserves a one-line answer.\n"
    "\n"
    "MARK WHERE THE EMAIL STARTS. Put this line, alone on its own line, immediately before the "
    "message itself:\n"
    "---EMAIL---\n"
    "Everything ABOVE that line is discarded and never reaches anyone — so if you cannot help "
    "writing a note to yourself first, put it up there. Everything BELOW it is sent verbatim, "
    "so the line after the marker is the first line they read. Emit the marker every time, even "
    "when there is nothing above it.\n"
    "\n"
    "When you MADE something (code, a document, a file), also tell them — briefly — what you "
    "made, what you did to check it, and anything you left untested, stubbed or uncertain. Be "
    "honest: if something failed or you could not verify it, say so plainly rather than hiding "
    "it. When you simply answered a question, none of that applies — just answer it well."
)

# ---- Where the email really starts ------------------------------------------
# Three prompt revisions failed to stop "The numbers check out. Here's the write-up..." from
# reaching the recipient, so the boundary moved into the harness. The harness does NOT guess
# where the email starts — the agent marks it (see SYSTEM_PROMPT) and we cut only at that mark.
EMAIL_MARKER = "---EMAIL---"


def strip_preamble(answer):
    """Everything above the agent's own marker is backstage; return what it meant to send.

    FAIL-OPEN by design: no marker, or nothing after it, and the whole answer goes out
    untouched. A leaked preamble is a wart; a truncated reply is a defect. Never trade down.
    """
    idx = (answer or "").rfind(EMAIL_MARKER)
    if idx == -1:
        return answer
    return answer[idx + len(EMAIL_MARKER):].strip() or answer


# ---- Tool schema — the full 6-tool menu -------------------------------------
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read and return the text contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the file"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries in a directory (directories end with '/').",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path; defaults to '.'"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command and return its combined stdout/stderr and exit code.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command to run"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (overwrite) text content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "content": {"type": "string", "description": "The full text to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            # Added because the model kept calling it anyway. It invented `edit_file` in the
            # middle of builds, got "no such tool", and fell back to run_bash with a Python
            # heredoc doing string surgery — a wasted round-trip every time and by far the
            # most fragile thing it did. If the model has a strong prior about a tool, the
            # cheap fix is to provide it rather than to keep refusing.
            "name": "edit_file",
            "description": (
                "Replace an exact string in a file. Prefer this over rewriting a whole file "
                "with write_file when changing part of one. old_string must match the file "
                "byte for byte, including indentation, and must be unique unless replace_all "
                "is true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "old_string": {"type": "string", "description": "Exact text to find"},
                    "new_string": {"type": "string", "description": "Text to put in its place"},
                    "replace_all": {"type": "boolean",
                                    "description": "Replace every occurrence (default false)"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web and return the top results as title, URL and snippet. This "
                "container has real internet access. Use it when you need something your "
                "training will not reliably contain: a library's current API, a version "
                "number, an error message you do not recognise. Follow up by fetching a "
                "promising URL with `curl -s <url>` in run_bash to read the page itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for"},
                    "count": {"type": "integer",
                              "description": "How many results to return (default 8, max 20)"},
                },
                "required": ["query"],
            },
        },
    },
]


# ---- Tool implementations ---------------------------------------------------
def read_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def list_dir(path="."):
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        entries.append(name + ("/" if os.path.isdir(full) else ""))
    return "\n".join(entries) if entries else "(empty directory)"


def run_bash(command):
    # stdin=DEVNULL is load-bearing: nothing the model runs may ever wait on input.
    bash = shutil.which("bash")
    common = dict(capture_output=True, text=True, timeout=BASH_TIMEOUT, stdin=subprocess.DEVNULL)
    try:
        if bash:
            proc = subprocess.run([bash, "-c", command], **common)
        else:
            proc = subprocess.run(command, shell=True, **common)
    except subprocess.TimeoutExpired:
        return (
            f"(timed out after {BASH_TIMEOUT}s — command killed. If this was a server, "
            "background it instead: `nohup cmd > log 2>&1 &`)"
        )
    out = (proc.stdout or "") + (proc.stderr or "")
    return f"(exit {proc.returncode})\n{out}".strip()


def write_file(path, content):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {len(content)} chars to {path}"


def edit_file(path, old_string, new_string, replace_all=False):
    """Exact string replacement. Errors are returned as text, not raised — the loop feeds them
    back to the model, which then fixes its own call. That is why each one says what to do
    next rather than just what went wrong."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        return (f"ERROR: {path} does not exist. Use write_file to create it.")
    except OSError as e:
        return f"ERROR: cannot read {path}: {e}"

    if old_string == new_string:
        return "ERROR: old_string and new_string are identical — nothing to do."
    count = content.count(old_string)
    if count == 0:
        # The overwhelmingly common cause is whitespace, so say so instead of leaving the
        # model to guess and retry the same string with a different quoting style.
        return (f"ERROR: old_string was not found in {path}. It must match exactly, including "
                f"indentation and line breaks. Read the file first and copy the text verbatim.")
    if count > 1 and not replace_all:
        return (f"ERROR: old_string appears {count} times in {path}. Include more surrounding "
                f"context to make it unique, or pass replace_all=true to change all {count}.")

    updated = content.replace(old_string, new_string) if replace_all \
        else content.replace(old_string, new_string, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    line = content[:content.index(old_string)].count("\n") + 1
    where = f"{count} occurrences" if replace_all and count > 1 else f"line {line}"
    return f"Edited {path} at {where} ({len(content)} -> {len(updated)} chars)"


# DuckDuckGo's no-JavaScript endpoint. Chosen because it needs no API key and no account:
# a search tool that depends on a secret nobody has provisioned is a tool that silently does
# not work. The HTML is scraped, so this is best-effort by nature — it reports what it got
# rather than pretending, and run_bash + curl remains available when a page must be read.
DDG_URL = "https://html.duckduckgo.com/html/"
_TAG_RE = re.compile(r"<[^>]+>")
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?P<rest>.*?)(?=<a[^>]+class="[^"]*result__a|\Z)', re.S)
_SNIPPET_RE = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S)


def _clean(html_fragment):
    return unescape(_TAG_RE.sub("", html_fragment or "")).strip()


def _unwrap(url):
    """DDG wraps results as /l/?uddg=<encoded>. Hand back the real URL — the model may want to
    curl it, and a redirector is useless for that."""
    if "uddg=" in url:
        try:
            return urllib.parse.unquote(urllib.parse.parse_qs(
                urllib.parse.urlparse(url).query)["uddg"][0])
        except Exception:
            return url
    return url[2:] if url.startswith("//") else url


# Scraping a search engine is best-effort and WILL be refused sometimes. DuckDuckGo answers a
# rate-limited client with HTTP 202 and a ~14KB challenge page rather than an error status, so
# a naive reader sees "success, no results" and reports the wrong thing — which is exactly what
# this tool did on its first real outing: it told the agent the page had "changed shape" while
# the truth was a burst of queries had tripped the limiter.
BLOCK_MARKERS = ("unusual traffic", "anomaly", "captcha", "challenge-form", "/challenge")
SEARCH_MIN_INTERVAL = 4.0        # seconds between searches; a burst is what trips the limiter
_last_search_at = [0.0]

# What to do instead, when search is unavailable. Concrete and checked: every one of these was
# reachable from this container when the search engines were refusing.
SEARCH_FALLBACK_ADVICE = (
    "Search is unavailable right now, but the network is fine and you have other routes:\n"
    "  - News:  curl -s 'https://news.google.com/rss/search?q=YOUR+TOPIC&hl=en-US&gl=US&ceid=US:en'\n"
    "           Plain XML, ~100 items, no key. But the item <link> does NOT reach the article:\n"
    "           it is a Google redirect that serves a JavaScript shell, and `curl -sL` lands on\n"
    "           news.google.com with no story in it. Use the item's <source url=\"…\"> to learn\n"
    "           the PUBLISHER, then fetch that publisher's own feed (usually /feed or /rss) and\n"
    "           match on the headline to get the real article URL.\n"
    "  - A project's own docs, if you can guess the URL: `curl -s https://…` usually works.\n"
    "  - GitHub:     https://api.github.com/search/repositories?q=…  and raw.githubusercontent.com\n"
    "  - npm:        https://registry.npmjs.org/<package>   (JSON: versions, repo, homepage)\n"
    "  - Wikipedia:  https://en.wikipedia.org/w/api.php?action=opensearch&format=json&search=…\n"
    "Use run_bash with curl for these. Do NOT retry web_search in a loop — the block is by IP "
    "and retrying makes it last longer."
)


def _search_blocked(status, html):
    """A refusal dressed as a success. Detected on the response, not on the absence of results,
    so 'genuinely nothing matched' stays distinguishable from 'we were turned away'."""
    if status == 202:
        return True
    low = html.lower()
    return any(m in low for m in BLOCK_MARKERS) and "result__a" not in html


def web_search(query, count=8):
    count = max(1, min(int(count or 8), 20))
    wait = SEARCH_MIN_INTERVAL - (time.monotonic() - _last_search_at[0])
    if wait > 0:
        time.sleep(wait)
    _last_search_at[0] = time.monotonic()

    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(DDG_URL, data=data, headers={
        # Without a browser UA this endpoint returns an empty result set rather than an error.
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            html = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        if e.code in (429, 403):
            return f"SEARCH BLOCKED (HTTP {e.code} — rate limited).\n\n{SEARCH_FALLBACK_ADVICE}"
        return (f"ERROR: search returned HTTP {e.code}.\n\n{SEARCH_FALLBACK_ADVICE}")
    except (urllib.error.URLError, OSError) as e:
        return f"ERROR: could not reach the search engine ({e}). The network may be down."

    if _search_blocked(status, html):
        return (f"SEARCH BLOCKED — the engine served a bot challenge instead of results "
                f"(HTTP {status}). This is rate limiting, not a bad query, and it clears on "
                f"its own after a while.\n\n{SEARCH_FALLBACK_ADVICE}")

    out = []
    for m in _RESULT_RE.finditer(html):
        title, url = _clean(m.group("title")), _unwrap(m.group("url"))
        if not title or not url.startswith("http"):
            continue
        snip = _SNIPPET_RE.search(m.group("rest"))
        out.append(f"{len(out) + 1}. {title}\n   {url}"
                   + (f"\n   {_clean(snip.group(1))[:300]}" if snip else ""))
        if len(out) >= count:
            break
    if not out:
        return (f"No results for {query!r}. The query may be too specific — try fewer words, "
                f"or drop the quotes if you used an exact phrase.\n\n{SEARCH_FALLBACK_ADVICE}")
    return f"Top {len(out)} results for {query!r}:\n\n" + "\n\n".join(out)


DISPATCH = {
    "read_file": read_file,
    "list_dir": list_dir,
    "run_bash": run_bash,
    "write_file": write_file,
    "edit_file": edit_file,
    "web_search": web_search,
}


# ---- The one LLM call — direct to DeepSeek ----------------------------------
class LLMError(RuntimeError):
    """Raised when DeepSeek can't be reached. The caller decides what to do (the
    inbox loop emails the failure back rather than killing the worker)."""


# Statuses worth trying again. Everything else (400 bad request, 401 bad key) will fail
# identically no matter how many times it is sent, and retrying only delays the real message.
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
LLM_ATTEMPTS = int(os.environ.get("LLM_ATTEMPTS", "4"))


def call_llm(messages):
    """One completion, retried through transient failures.

    WHY THE RETRY EXISTS. The first version caught HTTPError and URLError only — the errors
    raised while *sending*. A dropped connection while *reading* the response body raises
    http.client.IncompleteRead, which is neither, so it escaped this function entirely, past
    the caller's `except LLMError`, and killed the whole run. It cost a completed build: the
    agent had already fixed a bug, committed, pushed and passed CI when the socket dropped,
    and the failure mail claimed nothing had happened.

    A chat completion is safe to repeat — it has no side effects on their end and ours are
    all in the tool loop, which has not run yet at this point. So the cheap fix is to try
    again rather than discard an expensive multi-step run over a blip.
    """
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise LLMError("DEEPSEEK_API_KEY is not set in the environment.")
    payload = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS_SCHEMA}).encode("utf-8")
    last = "no attempt made"

    for attempt in range(1, LLM_ATTEMPTS + 1):
        # Rebuilt per attempt: a Request that has already been sent cannot be reused.
        req = urllib.request.Request(
            DEEPSEEK_URL,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = resp.read()
            return json.loads(body)["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code not in RETRY_STATUSES:
                raise LLMError(f"DeepSeek HTTP {e.code}: {detail}") from None
            last = f"HTTP {e.code}: {detail[:200]}"
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            # IncompleteRead and RemoteDisconnected live under HTTPException; connection
            # resets and timeouts under OSError. This is the family that used to escape.
            last = f"{type(e).__name__}: {e}"
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            # A truncated or unexpected body. Same cause as above most of the time.
            last = f"malformed response ({type(e).__name__}: {e})"

        if attempt < LLM_ATTEMPTS:
            delay = min(2 ** attempt, 30) + random.uniform(0, 1)
            print(f"[llm] {last} — attempt {attempt}/{LLM_ATTEMPTS} failed, "
                  f"retrying in {delay:.1f}s", flush=True)
            time.sleep(delay)

    raise LLMError(f"DeepSeek failed {LLM_ATTEMPTS} times in a row. Last error: {last}")


# ---- The heart: the agent loop ----------------------------------------------
def agent_loop(task, workspace=None, on_event=None, system_prompt=None, messages=None, tag="agent"):
    """Run the task to completion. Returns a dict the caller can turn into a report:

        {"answer": str, "steps": int, "transcript": [ {tool, args, result}, ... ],
         "stopped": "final" | "max_steps" | "error", "messages": [...]}

    Every tool auto-approves — the container is the blast radius.

    `system_prompt` swaps the agent's role, which is how the same loop serves as both the
    worker and its reviewer. `messages` resumes an earlier run: pass back the `messages` from
    a previous result and `task` becomes a follow-up turn in that same conversation, so the
    agent keeps everything it already knows (used to hand it review feedback). `tag` just
    labels the log lines so two agents in one container are tellable apart.
    """
    if workspace:
        os.makedirs(workspace, exist_ok=True)
        os.chdir(workspace)

    def emit(line):
        print(f"[{tag}] {line}", flush=True)
        if on_event:
            on_event(line)

    if messages is None:
        messages = [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
    else:
        messages = list(messages) + [{"role": "user", "content": task}]
    transcript = []

    for step in range(1, MAX_STEPS + 1):
        emit(f"{'-' * 20} step {step} {'-' * 20}")
        try:
            msg = call_llm(messages)
        except LLMError as e:
            emit(f"LLM ERROR: {e}")
            return {"answer": f"Run aborted at step {step}: {e}", "steps": step,
                    "transcript": transcript, "stopped": "error", "messages": messages}
        except Exception as e:
            # Nothing should reach here now that call_llm catches its own transients, but an
            # unexpected error must still not throw away the record of what was done. When
            # this escaped to the worker, the failure mail said "aborted in 0 steps, 0 tool
            # calls" for a run that had already shipped a fix — the report contradicted the
            # commit history. Whatever happens, the step count and transcript are real.
            emit(f"UNEXPECTED ERROR at step {step}: {type(e).__name__}: {e}")
            return {"answer": f"Run aborted at step {step} by an unexpected "
                              f"{type(e).__name__}: {e}\n\nWork completed before this point "
                              f"is still in the workspace.",
                    "steps": step, "transcript": transcript, "stopped": "error",
                    "messages": messages}
        messages.append(msg)

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            answer = msg.get("content") or "(empty)"
            emit("FINAL ANSWER:\n" + answer)
            return {"answer": answer, "steps": step, "transcript": transcript,
                    "stopped": "final", "messages": messages}

        for call in tool_calls:
            name = call["function"]["name"]
            raw_args = call["function"]["arguments"]      # a JSON *string*
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as e:
                args, result = {}, f"ERROR: could not parse arguments as JSON: {e}"
            else:
                try:
                    result = DISPATCH[name](**args)
                except KeyError:
                    result = f"ERROR: no such tool '{name}'."
                except Exception as e:                    # errors-as-text: the model self-heals
                    result = f"ERROR running {name}: {e}"

            if len(result) > MAX_TOOL_CHARS:
                result = result[:MAX_TOOL_CHARS] + f"\n...[truncated {len(result) - MAX_TOOL_CHARS} chars]"

            printable = json.dumps(args, ensure_ascii=False)
            emit(f"  -> {name}({printable[:300]}{'...' if len(printable) > 300 else ''})")
            emit(f"  <- {result[:200]}{'...' if len(result) > 200 else ''}")
            # Stamp who ran it. The worker's report merges its own calls with the reviewer's,
            # and an unlabelled merged list misattributes work to whoever is named at the top.
            transcript.append({"tool": name, "args": args, "result": result, "by": tag})

            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    emit(f"Stopped: hit MAX_STEPS ({MAX_STEPS}) without a final answer.")
    return {"answer": f"Stopped after MAX_STEPS ({MAX_STEPS}) without finishing the task.",
            "steps": MAX_STEPS, "transcript": transcript, "stopped": "max_steps",
            "messages": messages}


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or input("Task: ")
    print(f"TASK: {task}")
    agent_loop(task, workspace=os.environ.get("WORKSPACE"))
