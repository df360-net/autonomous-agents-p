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

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error

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


# ---- Tool schema — the full 4-tool menu -------------------------------------
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


DISPATCH = {
    "read_file": read_file,
    "list_dir": list_dir,
    "run_bash": run_bash,
    "write_file": write_file,
}


# ---- The one LLM call — direct to DeepSeek ----------------------------------
class LLMError(RuntimeError):
    """Raised when DeepSeek can't be reached. The caller decides what to do (the
    inbox loop emails the failure back rather than killing the worker)."""


def call_llm(messages):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise LLMError("DEEPSEEK_API_KEY is not set in the environment.")
    payload = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS_SCHEMA}).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise LLMError(f"DeepSeek HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"Cannot reach DeepSeek: {e.reason}") from None
    return json.loads(body)["choices"][0]["message"]


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
            return {"answer": f"Run aborted: {e}", "steps": step, "transcript": transcript,
                    "stopped": "error", "messages": messages}
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
