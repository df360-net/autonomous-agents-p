"""Identity is derived, and today's derivation reproduces today's values exactly.

The second half is the point of the whole slice: this refactor claims nothing changed, so the
test that matters is that agent1 still reads agent1@agents.local and still signs off as
validator1. If that ever stops being true it must be a deliberate operational rename, not a
side effect of touching this module.
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
# BOTH LAYOUTS: agent/ in the source tree, flat /app in the image. See the Dockerfile.
# This one also spawns a subprocess, so the same pair has to reach it via PYTHONPATH —
# a child process inherits sys.path from the environment, not from us.
AGENT = os.path.join(ROOT, "agent")
PYPATH = os.pathsep.join([AGENT, ROOT])
sys.path[:0] = [AGENT, ROOT]

# Run against a known-empty identity environment. This matters in the real container, where
# AGENT_ADDRESS is still set from when the container was created: inheriting it would make
# every derivation below contradict the environment and the guard would (correctly) refuse.
DERIVED_VARS = ("AGENT_ADDRESS", "VALIDATOR_NAME", "VALIDATOR_ADDRESS")
for _v in ("TENANT", "AGENT_NAME", "AGENT_DOMAIN") + DERIVED_VARS:
    os.environ.pop(_v, None)
import fleet_identity as fi

all_ok = True


def check(label, cond, detail=""):
    global all_ok
    print(("PASS" if cond else "FAIL"), " " + label + (f"   {detail}" if not cond else ""))
    all_ok &= bool(cond)


def run(env_extra, code):
    """Import fleet_identity in a fresh process with exactly this identity environment.

    The derived vars are stripped from the inherited environment first, so a test that wants a
    conflict has to ask for one — otherwise the container's own AGENT_ADDRESS would silently
    supply the conflict and every case would look like it passed for the wrong reason.
    """
    env = {k: v for k, v in os.environ.items() if k not in DERIVED_VARS}
    env.update(PYTHONPATH=PYPATH, **env_extra)
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


# ---- The values the running system already uses ------------------------------
print("\n--- nothing changed for the agent that exists ---")
check("agent name", fi.NAME == "agent1", fi.NAME)
check("mailbox is exactly today's", fi.AGENT_ADDRESS == "agent1@agents.local", fi.AGENT_ADDRESS)
check("reviewer name is exactly today's", fi.VALIDATOR_NAME == "validator1", fi.VALIDATOR_NAME)
check("reviewer mailbox is exactly today's",
      fi.VALIDATOR_ADDRESS == "validator1@agents.local", fi.VALIDATOR_ADDRESS)
check("the id is tenant-qualified", fi.AGENT_ID == "dev/agent1", fi.AGENT_ID)


# ---- Derivations -------------------------------------------------------------
print("\n--- derived, not configured ---")
check("a second agent derives its own mailbox", fi.address("agent2") == "agent2@agents.local")
check("reviewer tracks the worker's name", fi.validator_name("agent2") == "validator2")
check("and a hyphenated one", fi.validator_name("agent-01") == "validator-01",
      fi.validator_name("agent-01"))
check("a name without 'agent' still gets a distinct reviewer",
      fi.validator_name("builder") == "validator-builder", fi.validator_name("builder"))
check("the reviewer can never collide with the worker",
      all(fi.validator_name(n) != n for n in ("agent1", "agent-01", "builder", "x")))

out = run({"TENANT": "acme", "AGENT_NAME": "web-03", "AGENT_DOMAIN": "acme.agents.local"},
          "import fleet_identity as f; print(f.AGENT_ID, f.AGENT_ADDRESS, f.VALIDATOR_ADDRESS)")
check("a different tenant derives cleanly",
      out.stdout.strip() == "acme/web-03 web-03@acme.agents.local validator-web-03@acme.agents.local",
      out.stdout.strip() + out.stderr[:200])


# ---- A contradiction is a startup error, not an override ---------------------
print("\n--- the environment cannot contradict the identity ---")
check("agreeing env is not a conflict",
      fi.check_environment({"AGENT_ADDRESS": "agent1@agents.local",
                            "VALIDATOR_NAME": "validator1"}) == [])
check("an absent env var is not a conflict", fi.check_environment({}) == [])

conflicts = fi.check_environment({"AGENT_ADDRESS": "agent7@agents.local"})
check("a stale mailbox IS a conflict", len(conflicts) == 1 and "AGENT_ADDRESS" in conflicts[0],
      str(conflicts))
check("  ...and the message shows both values",
      "agent7@agents.local" in conflicts[0] and "agent1@agents.local" in conflicts[0],
      str(conflicts))

# The real thing: the process must refuse to start, not warn and carry on.
out = run({"AGENT_NAME": "agent2", "AGENT_ADDRESS": "agent1@agents.local"},
          "import fleet_identity; print('STARTED ANYWAY')")
check("a worker pointed at another agent's inbox refuses to start",
      out.returncode != 0 and "STARTED ANYWAY" not in out.stdout, f"rc={out.returncode}")
check("  ...and says why", "contradicts" in (out.stdout + out.stderr), (out.stdout + out.stderr)[:200])

out = run({"AGENT_NAME": "Agent One!"}, "import fleet_identity; print('STARTED ANYWAY')")
check("an unusable name refuses to start",
      out.returncode != 0 and "STARTED ANYWAY" not in out.stdout, f"rc={out.returncode}")
out = run({"TENANT": "Acme Corp"}, "import fleet_identity; print('STARTED ANYWAY')")
check("an unusable tenant refuses to start",
      out.returncode != 0 and "STARTED ANYWAY" not in out.stdout, f"rc={out.returncode}")


# ---- The consumers agree with the module -------------------------------------
print("\n--- one identity, everywhere ---")
out = run({}, "import agent_budget as b, fleet_identity as f; "
              "print(b.AGENT_ID == f.AGENT_ID, b.TENANT == f.TENANT, b.AGENT_NAME == f.NAME)")
check("the ledger uses the derived identity", out.stdout.strip() == "True True True",
      out.stdout.strip() + out.stderr[:300])

print("\n" + ("ALL IDENTITY TESTS PASSED" if all_ok else "SOME TESTS FAILED"))
sys.exit(0 if all_ok else 1)
