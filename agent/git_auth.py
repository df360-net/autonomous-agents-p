"""git_auth.py — hand a token to git without writing it anywhere it can be read back.

Two callers need this now: `ship_app` pushes application repositories, and `agent_memory`
pushes the agent's notes once those live on GitHub instead of a bind mount. It is one module
rather than two copies because both are credential handling, and the copy that does not get
the next fix is the one that leaks.

WHY NOT PUT THE TOKEN IN THE URL. `https://x-access-token:TOKEN@github.com/...` is the obvious
thing and it is wrong in four places at once: `git remote add` writes it into `.git/config`,
`git remote -v` echoes it, git prints the remote back in most of its error messages, and any
log line that mentions the remote now contains a credential. The agent's stdout is read by a
human and quoted into email reports, and `.git/config` sits in a workspace the model can cat.

So the token goes to git the way git asks for one: an askpass helper. The script itself holds
no secret — it echoes environment variables that exist only in the child process, so the value
never reaches a command line, a config file, or `ps`.
"""

import os
import tempfile

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "df360-net").strip()
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

# What an askpass script prints. Written with `case` on the prompt because git asks twice —
# once for the username, once for the password — through the same program.
_HELPER = ("#!/bin/sh\n"
           "case \"$1\" in *Username*) echo \"$GH_USER\";; *) echo \"$GH_PASS\";; esac\n")


def have_token():
    return bool(TOKEN)


def needs_auth(remote):
    """Whether this remote is one we hold a credential for.

    A filesystem path or a local bare repo needs nothing, and attaching an askpass helper to
    one would be harmless but misleading — the point of asking is that the answer is visible in
    the caller, so `memory: local remote, no credentials` reads as a fact rather than a guess.
    """
    remote = (remote or "").strip()
    return remote.startswith("https://") or remote.startswith("http://")


def env_for(remote, base=None):
    """(env, cleanup) for running git against `remote`.

    `cleanup()` is always safe to call and always worth calling in a finally: the helper is a
    real file with the executable bit set, and leaving one per push in a long-running container
    is a slow leak of files that exist only to hand out a credential.
    """
    env = dict(base or os.environ)
    if not (needs_auth(remote) and TOKEN):
        return env, (lambda: None)

    helper = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    helper.write(_HELPER)
    helper.close()
    os.chmod(helper.name, 0o700)
    env.update({
        "GIT_ASKPASS": helper.name,
        "GH_USER": GITHUB_OWNER,
        "GH_PASS": TOKEN,
        # Without this a missing or rejected credential makes git sit waiting on a terminal
        # that is not there, and the agent hangs until the git timeout rather than failing.
        "GIT_TERMINAL_PROMPT": "0",
    })

    def cleanup():
        try:
            os.unlink(helper.name)
        except OSError:
            pass

    return env, cleanup


def redact(text):
    """Strip the token out of anything on its way to a log or an exception.

    Belt to the braces above. git does not print the token when it comes from askpass, but the
    token is also in this process's environment, and a subprocess that dumps its environment on
    failure would carry it into a RuntimeError, into the container log, and from there into the
    transcript attached to an email. One cheap substitution removes the whole class.
    """
    if not TOKEN or not text:
        return text
    return str(text).replace(TOKEN, "***")
