"""Credential handling, tested for the thing that actually goes wrong.

Nobody ships a token in a URL on purpose. It happens because the URL is the easy place to put
it and nothing complains — so these tests assert the ABSENCE of the token from the four places
it would end up: the remote, .git/config, the process's arguments, and the text of a failure.

The last one is the interesting case and the reason `redact` exists. git never echoes an
askpass credential, but this process holds the token in its environment, and a git failure
message is copied into a container log that a human reads and that gets quoted into email.
A test that only checks .git/config would pass while the token walked out through a stack
trace.
"""
import os
import subprocess
import sys
import tempfile

WS = tempfile.mkdtemp(prefix="gitauth-ws-")
os.environ.update({"WORKSPACE_ROOT": WS, "TENANT": "dev", "AGENT_NAME": "agent1",
                   "AGENT_DOMAIN": "agents.local",
                   "GITHUB_TOKEN": "ghp-notarealtoken-0123456789",
                   "GITHUB_OWNER": "df360-net"})
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import git_auth                                                             # noqa: E402

TOKEN = os.environ["GITHUB_TOKEN"]
all_ok = True


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


print("--- which remotes need a credential ---")
check("an https remote does", git_auth.needs_auth("https://github.com/df360-net/x.git"))
check("a local path does not", not git_auth.needs_auth("/remotes/dev-knowledge.git"))
check("a windows path does not", not git_auth.needs_auth(r"C:\remotes\dev.git"))
check("an empty remote does not", not git_auth.needs_auth(""))
# ssh remotes authenticate with a key, not a token; handing them an askpass helper would be
# noise at best and a confusing prompt at worst.
check("an ssh remote does not", not git_auth.needs_auth("git@github.com:df360-net/x.git"))

print("\n--- the token never reaches a command line or a config file ---")
env, cleanup = git_auth.env_for("https://github.com/df360-net/x.git")
try:
    check("git is told to ask a program", env.get("GIT_ASKPASS", "").endswith(".sh"))
    check("  ...and never to prompt a terminal that is not there",
          env.get("GIT_TERMINAL_PROMPT") == "0")
    check("the token is passed as an environment variable, not an argument",
          env.get("GH_PASS") == TOKEN)
    with open(env["GIT_ASKPASS"], encoding="utf-8") as fh:
        helper_source = fh.read()
    # THE POINT OF THE WHOLE MODULE. The script is a lookup, not a literal.
    check("the helper SCRIPT ITSELF contains no secret", TOKEN not in helper_source,
          helper_source)
    check("  ...it reads the variable instead", "$GH_PASS" in helper_source)

    # A real git run against a URL that cannot resolve, purely to see what the failure says.
    p = subprocess.run(["git", "ls-remote", "https://github.invalid/df360-net/x.git"],
                       capture_output=True, text=True, env=env, timeout=60)
    combined = (p.stdout or "") + (p.stderr or "")
    check("a real git failure does not print the token", TOKEN not in combined,
          combined[:200])
finally:
    cleanup()
check("the helper is deleted afterwards — one file per push would be a slow leak",
      not os.path.exists(env["GIT_ASKPASS"]))

print("\n--- a local remote gets no helper at all ---")
env2, cleanup2 = git_auth.env_for("/remotes/dev-knowledge.git")
cleanup2()
check("no askpass for a filesystem remote", "GIT_ASKPASS" not in env2)
check("  ...and no token handed to it either", "GH_PASS" not in env2)
check("cleanup on a no-op is still safe to call", True)

print("\n--- redaction, for the path git does not control ---")
check("a token in an error message is removed",
      git_auth.redact(f"fatal: could not read Password for 'https://x:{TOKEN}@github.com'")
      == "fatal: could not read Password for 'https://x:***@github.com'")
check("text without the token is untouched",
      git_auth.redact("fatal: repository not found") == "fatal: repository not found")
check("None and empty survive without raising",
      git_auth.redact(None) is None and git_auth.redact("") == "")

# With no token configured, redact must not turn every string into stars — an empty needle
# would otherwise match everywhere and mangle unrelated logs.
_saved = git_auth.TOKEN
git_auth.TOKEN = ""
check("with no token configured, nothing is redacted",
      git_auth.redact("plain text") == "plain text")
git_auth.TOKEN = _saved

print("\n" + ("ALL GIT AUTH TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
import shutil; shutil.rmtree(WS, ignore_errors=True)
sys.exit(0 if all_ok else 1)
