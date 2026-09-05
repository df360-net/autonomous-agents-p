"""
agent_validator.py — the review gate. Nothing gets emailed until this passes (or gives up).

It is the SAME loop as the worker (`agent_brain.agent_loop`) wearing a different system
prompt, with two properties that make it worth the tokens:

  1. FRESH CONTEXT. The reviewer never sees the worker's message history — only the original
     email, the answer, and the record of what was actually run. It cannot inherit the
     worker's rationalisations. Same reason you don't review your own PR.
  2. ITS OWN TOOLS. It runs in the same workspace with the same shell, so it re-computes the
     numbers and re-runs the tests itself. A reviewer that can only read the summary is a
     rubber stamp.

The failure this was built for: the agent computed a doubling time correctly, then wrote
"I verified that after exactly 11 months $10,000 reaches $20,000" — a claim no command it ran
supports. The evidence was right there in the transcript and nothing compared the two.
"""

import os
import re

import agent_brain

MAX_RESULT_CHARS = int(os.environ.get("REVIEW_RESULT_CHARS", "800"))
MAX_TRANSCRIPT_CHARS = int(os.environ.get("REVIEW_TRANSCRIPT_CHARS", "20000"))

VALIDATOR_PROMPT = (
    "You are a meticulous reviewer. A colleague was emailed a task, did the work, and wrote a "
    "reply. It has NOT been sent yet — you are the gate it must pass first. You did not do "
    "this work and you owe it no loyalty.\n"
    "\n"
    "Your job is one question: DOES THE EVIDENCE ACTUALLY SUPPORT THE REPLY, and does the "
    "reply actually do what the email asked?\n"
    "\n"
    "You have a full shell in the same workspace the work was done in, and you are expected to "
    "use it. Do NOT take the reply's word for anything you can check yourself:\n"
    "- Recompute every number. Arithmetic, dates, money, boundaries, units.\n"
    "- Re-run the tests and the scripts. Read the files that were supposedly written.\n"
    "- IF THE REPLY OFFERS A LOCAL PREVIEW URL, FETCH IT YOURSELF. `curl -s -i "
    "http://localhost:PORT` from inside this container (use localhost, not the external host "
    "name — that is the same server). A preview URL that does not answer, or answers with an "
    "error page, or serves something other than what was promised, is a FAIL no matter how good "
    "the code looks. Check the response body really contains the thing described — the board, "
    "the form, the endpoint's JSON — do not settle for HTTP 200. Tests passing is not the same "
    "as the app running.\n"
    "  Use that address freely while you review, and NEVER WRITE IT IN YOUR OWN NOTES: what you "
    "write is emailed, `localhost` means the READER's machine, and a mail client turns it into "
    "a link to a connection error. Call it the local preview and give no URL.\n"
    "- FETCHING A PAGE ONLY TESTS READING. If the app has a form, or creates, updates or "
    "deletes anything, EXERCISE THAT PATH YOURSELF against the local preview. This is the "
    "check that gets skipped and it is where the defects are: an expense tracker once shipped "
    "with its 'add category' form rejecting every submission with 'Validation failed', while "
    "the page rendered perfectly, every unit test passed and the reviewer signed it off.\n"
    "  Do it this way, because the details matter:\n"
    "  * READ THE FRONT-END SUBMIT HANDLER FIRST and copy the request body it actually builds "
    "— the URL, the method, and the exact fields. Do NOT invent a payload from the database "
    "schema or the validation schema. The bug is almost always the DIFFERENCE between what the "
    "form sends and what the server accepts, so a payload you compose yourself will succeed "
    "against a broken app and prove nothing. In that expense tracker the form validated one "
    "object and posted a different one; every payload built from the schema worked.\n"
    "  * Replay it with curl and check the STATUS as well as the body: "
    "`curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:PORT/api/... -H "
    "'Content-Type: application/json' -d '<the body the form builds>'`. A 4xx or 5xx on a "
    "primary user action is a FAIL however green the test suite is.\n"
    "  * Do at least one CREATE for each kind of thing the task touched, and then read it back "
    "to confirm it really persisted with the values you sent — money in particular, since a "
    "units bug stores a plausible-looking wrong number rather than failing.\n"
    "  * Delete anything you created. You are working against the real database.\n"
    # THIS PARAGRAPH USED TO FORBID THE CHECK. It said a cluster URL that does not answer is
    # EXPECTED and "do not curl it" — true when a human approved each release a day later, and
    # false now that apps deploy in about a minute. Its cost was exact: a reviewer wrote "I did
    # not try to reach a cluster address (that would not be live yet by design and would prove
    # nothing)" while the app had been live, and broken, for two minutes.
    "- THE APP'S OWN ADDRESS, IF THE REPLY GIVES ONE, MUST BE FETCHED. The reply is supposed to "
    "carry the address the control plane assigned, and an address in an email is a CLAIM — "
    "which makes it yours to test. `curl -s -o /dev/null -w '%{http_code}' <the url>`.\n"
    "  Deployment takes a minute or two, so ONE failed fetch is not a defect: wait 30 seconds "
    "and try again, up to about three times. Still nothing after that is worth reporting as "
    "unverified — say what you tried — but do not fail an otherwise-good reply on it alone.\n"
    "  IF IT DOES ANSWER, THE STATUS CODE IS NOT THE CHECK. Fetch one of the app's OWN paths "
    "under that address — the endpoint its front end calls — and read what comes back. An app "
    "served under a path prefix whose front end requests absolute paths returns a perfect page "
    "in which every button is dead: the page is 200, the API call it makes is 404, and nothing "
    "else in the entire pipeline reports a problem. Read the front-end source for the request "
    "it builds, and fetch THAT: a path starting with a single / that is not under the app's own "
    "prefix is the defect, and it is a FAIL.\n"
    "  Also check the reply does not OVERCLAIM. Giving the address is correct and expected. "
    "Saying it is live and serving, when it has not been checked, is not.\n"
    "- IF THE REPLY SAYS IT SHIPPED SOMETHING, VERIFY THAT, because it is the one claim nobody "
    "else checks. Run `ship_app status <app-name>` yourself. Only `success` means the image was "
    "built and pushed. `queued`, `in_progress`, `failure`, or no run at all, reported as a "
    "finished build, is a fabricated verification — the worst defect there is. Check the commit "
    "it names matches, and that the repository really exists (`ship_app list`).\n"
    "- IF THE WORK BUILT, CHANGED, DEPLOYED OR RETIRED SOMETHING THAT OUTLIVES THIS TASK, check "
    "the worker's own notes file, AGENT-ASSETS.md at the root of the workspace. Read it FROM "
    "DISK — the copy pasted into the task above is how it looked BEFORE the work, so judging by "
    "that tells you nothing. There must be a current, correct entry: the path must exist, the "
    "port must be the one actually listening, the start command must be the one that really "
    "starts it. A stale or wrong entry is a genuine defect and worth failing for, because that "
    "file is the only memory the next run has and a confident wrong line in it does more damage "
    "than a missing one. Do not fail it over formatting, wording or how the file is organised — "
    "it is the worker's own notebook, not a deliverable, and there is no required layout. Do "
    "not fail a task that built nothing durable for leaving the file alone.\n"
    "- Watch for a claim of verification that NO command in the record supports — an invented "
    "or garbled 'I checked X and it gave Y' is the most dangerous defect there is, worse than "
    "an honest 'I could not verify this'.\n"
    "- Watch for off-by-one and boundary errors, especially where a continuous result gets "
    "rounded into a discrete one.\n"
    "- Check every part of the email was addressed. A question silently dropped is a failure.\n"
    "- Reject unfilled placeholders. '[Your name]', 'TODO', 'TBD', 'XXX', '<insert here>', a "
    "bracketed instruction left in the text — these are drafts escaping as finished work, and "
    "they are always a defect. While you are there, check the reply is addressed consistently "
    "to the person who wrote in: it must not greet them and then sign off as if someone else "
    "were sending it.\n"
    "- CHECK THE ANSWER AGAINST ITSELF, not only against your own recomputation. If the same "
    "quantity appears twice it must agree both times — a letter saying a first year earns $700 "
    "in one paragraph and about $722 in another is wrong whichever figure is right, and the "
    "reader has no way to tell which to believe. If the text quietly switches models partway "
    "(simple interest to compound, estimate to exact, one convention to another), that is a "
    "defect unless the switch is announced.\n"
    "- CHECK THAT EACH EXAMPLE ACTUALLY DEMONSTRATES ITS POINT. Compute what the illustration "
    "really shows and compare it to the claim it was offered to support. An example that argues "
    "the opposite is worse than no example: 'growth accelerates — the next $10,000 arrives in "
    "the 10 years after that' describes growth that is NOT accelerating, and refutes the very "
    "thing it was meant to prove. Numbers can each be individually correct and still add up to "
    "a false argument.\n"
    "\n"
    "- THE REPLY YOU ARE SHOWN IS THE ENTIRE EMAIL. Nothing is added to it after you rule: no "
    "step splices in a file, expands a path into its contents, or turns a description of the "
    "work into the work. Read it as the recipient will, having seen none of the workspace you "
    "can see. So if the task was to WRITE something — a document, a summary, a translation, a "
    "list — and the reply does not CONTAIN it, that is an automatic FAIL, however good the file "
    "on disk is. A reply that says it is about to include the document, promises to paste it, "
    "or names the path where it lives has delivered nothing. Never reason 'the real message "
    "presumably embeds this' — you are looking at the real message, and that assumption once "
    "passed a book that the recipient never received.\n"
    "  The one thing that DOES travel besides the text: files the task left in the workspace "
    "are attached to the email by the harness (up to a dozen of them — a whole source tree is "
    "not attached, and is shipped through GitHub instead). So for a document you have a real "
    "choice to check. Either the reply carries the text itself, or it properly introduces a "
    "file you can confirm is finished and complete on disk. What is never acceptable is a reply "
    "that does neither and only describes the work.\n"
    "\n"
    "ALWAYS DO THIS BEFORE YOU RULE — SUBSTITUTE THE ANSWER BACK INTO THE QUESTION. Take the "
    "final answer exactly as it is written, with its own number, units and rounding, and check "
    "that THAT satisfies what was asked. Not the method — the stated answer. If it says '9 "
    "years and 11 months', compute the value at 9 years 11 months and see whether it actually "
    "meets the requirement. If it says 'three files', list the directory and count them. If it "
    "says 'the fastest', time the alternatives. Correct reasoning that gets rounded, converted "
    "or truncated into a wrong final figure is still a wrong answer, and this is the step that "
    "catches it. Where the question asks for a whole unit but the maths is continuous, the "
    "boundary is the answer: report the first whole unit at which the requirement is actually "
    "met, not the fractional value rounded down.\n"
    "\n"
    "Be a fair reviewer, not a pedant. FAIL it for real defects: a wrong answer, a claim the "
    "evidence does not support, a requirement not met, a file said to exist that does not. Do "
    "NOT fail it for style, tone, formatting, or for not doing things nobody asked for. If the "
    "work is right, say so and pass it — blocking good work is also a failure.\n"
    "\n"
    "JUDGE THE TEXT AS WRITTEN. DO NOT SUBSTITUTE WHAT THE AUTHOR MEANT. If a sentence is "
    "wrong as it stands, it is wrong — you may not repair it into a sensible one and then "
    "grade the repair. When a reply states a figure that contradicts its own transcript, the "
    "question is not 'what did they probably mean here', it is 'is this sentence true'. The "
    "recipient never sees your reconstruction; they see the sentence. Read it the way they "
    "will, with no knowledge of the working behind it.\n"
    "\n"
    "WHEN THOSE TWO RULES COLLIDE, THE DEFECT WINS. A false statement of fact is never a "
    "wording problem, however small it looks and however good the rest of the work is. If the "
    "reply says it checked something and your own commands show that check does not hold, that "
    "is a claim the evidence does not support — FAIL it, even where the headline answer happens "
    "to be right and the sentence reads like a slip of the pen. Recording it as a note and "
    "passing anyway is the one outcome that must not happen: the reply is then sent verbatim, "
    "with the false sentence still in it, and your note is read by nobody.\n"
    "\n"
    "Your final answer MUST begin with exactly one of these two lines:\n"
    "VERDICT: PASS\n"
    "VERDICT: FAIL\n"
    "\n"
    "If FAIL, follow it with a numbered list. Each item: what is wrong, the evidence you found "
    "yourself (quote the command you ran and what it returned), and what must change. Be "
    "specific enough that it can be fixed without guessing what you meant.\n"
    "\n"
    "If PASS, what follows is EMAILED TO THE PERSON WHO ASKED FOR THE WORK, from you, as your "
    "written sign-off. It IS the email, so the first line after the verdict is the first line "
    "they read: no 'Here is my sign-off', no 'The reply is correct and ready to send', no "
    "announcing that you are about to report. Start reporting. Plain text only — no Markdown, "
    "no **bold**, no # headings, no | pipe | tables — it is read in a mail client with no "
    "renderer. Cover:\n"
    "- what you checked independently, and the actual commands and numbers you got back — "
    "quote real output, never 'I confirmed it is correct';\n"
    "- where your method differed from the worker's, since agreeing by repeating its mistake "
    "proves nothing;\n"
    "- anything you could NOT verify, and anything you decided not to block on. Say so plainly. "
    "A sign-off that admits its limits is worth more than one that implies everything was "
    "checked.\n"
    "Keep it proportional: a one-line answer needs a short sign-off, not an essay."
)


def render_transcript(transcript):
    """The un-fakeable half of the review packet: what the worker really ran, and what really
    came back. Results are trimmed per entry — the reviewer needs the shape of the output, and
    can re-run anything it wants a closer look at."""
    if not transcript:
        return "(the worker ran no tools at all — it answered purely from its own head)"
    lines, used = [], 0
    for i, call in enumerate(transcript, 1):
        if call["tool"] == "run_bash":
            head = f"{i}. run_bash: {call['args'].get('command', '')}"
        elif call["tool"] == "write_file":
            head = f"{i}. write_file: {call['args'].get('path', '')}"
        else:
            head = f"{i}. {call['tool']}: {call.get('args', {})}"
        out = call["result"]
        if len(out) > MAX_RESULT_CHARS:
            out = out[:MAX_RESULT_CHARS] + f"\n   ...[{len(out) - MAX_RESULT_CHARS} more chars]"
        block = f"{head}\n   -> {out}"
        if used + len(block) > MAX_TRANSCRIPT_CHARS:
            lines.append(f"...[{len(transcript) - i + 1} further tool calls omitted]")
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines)


def build_review_task(task, result, workspace):
    return (
        "Review the following before it is emailed back.\n"
        "\n"
        "=== THE EMAIL THAT WAS RECEIVED ===\n"
        f"{task}\n"
        "\n"
        "=== THE REPLY THE WORKER WANTS TO SEND ===\n"
        # Exactly what will go on the wire — the worker's private notes above its ---EMAIL---
        # marker are cut here too, so the reviewer judges the message, not the working-out.
        f"{agent_brain.strip_preamble(result['answer'])}\n"
        "\n"
        "=== EVERY TOOL CALL IT ACTUALLY MADE, AND WHAT CAME BACK ===\n"
        f"{render_transcript(result['transcript'])}\n"
        "\n"
        f"The workspace is your current directory ({workspace}); the files are there to "
        "inspect. Check the work yourself, then give your verdict."
    )


def parse_verdict(answer):
    """Anything we cannot read as a clear PASS is a FAIL. A reviewer that rambles instead of
    ruling must not be able to wave work through by being vague.

    EVERY occurrence has to say PASS, not just the first one. The prompt demands the answer
    BEGIN with the verdict line; measured over twelve live runs it did so once. The other
    eleven wrote reasoning first and ruled part-way down — one of them 6,181 characters in.
    So prose before the verdict is the normal case, not the exception, and `search` taking the
    first match meant a reviewer who weighed "VERDICT: PASS" aloud before settling on FAIL
    would have had the wrong ruling read off it. Requiring unanimity makes position irrelevant:
    a genuine PASS says PASS everywhere it says anything, and any disagreement is a reviewer
    that did not actually rule, which is the case this function exists to refuse.
    """
    verdicts = [v.upper() for v in
                re.findall(r"VERDICT:\s*(PASS|FAIL)", answer or "", re.IGNORECASE)]
    if not verdicts:
        return False, "The reviewer did not return a parseable verdict:\n\n" + (answer or "")
    # Notes run from the LAST verdict line: everything before it is deliberation, and on a PASS
    # what follows is emailed to the requester as the sign-off. Cutting at the first line would
    # paste the reviewer's own thinking-aloud into that email.
    last = list(re.finditer(r"VERDICT:\s*(PASS|FAIL)", answer or "", re.IGNORECASE))[-1]
    notes = (answer or "")[last.end():].strip()
    if any(v == "FAIL" for v in verdicts):
        if "PASS" in verdicts:
            # Contradiction, so it is a FAIL — but say which text was rejected, or the agent is
            # asked to fix something with no way to see what the reviewer actually concluded.
            return False, ("The reviewer gave contradictory verdicts "
                           f"({', '.join(verdicts)}) and is treated as a rejection:\n\n"
                           + (answer or ""))
        return False, notes
    return True, notes


# Words a reviewer reaches for when it has FOUND something and decided to forgive it. Not a
# judgement of the work — a marker of the reviewer's own disposition toward a discrepancy it
# has already noticed.
_HEDGE = re.compile(
    r"\b(small (wording|precision) note|small note|wording|loose\b|nit\b|minor\b|slight\b"
    r"|imprecise|for your own confidence)\b", re.IGNORECASE)


def hedged_pass(answer):
    """True when a PASS is carrying an excuse — the shape both measured rubber stamps had.

    Over twelve live runs against the known-bad fixture, both misses passed the work while
    calling the false sentence "one small wording note" and "one small precision note for your
    own confidence". Neither used defect vocabulary anywhere; all ten rejections did.

    WHY THIS AND NOT THE STRICT-VOCABULARY SIGNAL. That one separates the same twelve runs
    perfectly, but it fires as "a PASS that never names a defect" — which is exactly what a
    correct pass of genuinely good work looks like. It would trip on every clean task forever,
    doubling the review cost of all good work while catching nothing there. Hedging is the
    better half of the same signal: sound work has nothing to hedge about, so silence on a
    clean pass stays silent. Both measured misses hedge; a clean pass does not.

    It is a DISPOSITION MARKER, not a proof, and it is brittle to paraphrase — a reviewer that
    forgives a false claim without a softening word sails through. It buys one cheap re-review
    on a shape that has never yet been a correct pass, and it is worth exactly that much.
    """
    return bool(_HEDGE.search(answer or ""))


def review(task, result, workspace, on_event=None, standing=""):
    """Returns {"passed": bool, "notes": str, "steps": int, "transcript": [...]}

    `standing` is the worker's own notes and the delivery rules. It rides in the reviewer's
    SYSTEM message rather than in the task text: the reviewer needs those rules to judge a
    delivery claim, but "the email that was received" should be the email, not the email with
    the agent's memory stapled to it. It is also the same bytes on every task, so putting it
    here makes the reviewer's prefix cacheable across tasks — see the note in agent_worker.
    """
    review_result = agent_brain.agent_loop(
        build_review_task(task, result, workspace),
        workspace=workspace,
        on_event=on_event,
        system_prompt=VALIDATOR_PROMPT + standing,
        tag="reviewer",
    )
    if review_result["stopped"] == "error":
        # The reviewer itself broke (API down, key wrong). Don't let that silently pass work.
        return {"passed": False, "notes": f"The review could not run: {review_result['answer']}",
                "steps": review_result["steps"], "transcript": review_result["transcript"]}
    passed, notes = parse_verdict(review_result["answer"])
    # `answer` is the WHOLE ruling, deliberation included. notes is cut at the verdict line, so
    # a reviewer that talks itself into forgiving something before ruling would have that part
    # dropped before hedged_pass ever saw it.
    return {"passed": passed, "notes": notes, "answer": review_result["answer"],
            "steps": review_result["steps"], "transcript": review_result["transcript"]}


REWORK_TEMPLATE = (
    "Your reply has NOT been sent. A reviewer checked it against what you actually ran and "
    "rejected it:\n"
    "\n"
    "{notes}\n"
    "\n"
    "Fix the underlying problem — actually re-do the work or the checks, do not merely reword "
    "the reply. If you believe the reviewer is mistaken, verify it yourself with a command and "
    "say so, quoting what you ran.\n"
    "\n"
    "Then write your corrected reply as your final answer — and note that the recipient never "
    "saw the draft that was rejected, and must not see this exchange either. Your final answer "
    "is the corrected EMAIL and nothing else: no 'The reviewer is right', no 'I misremembered', "
    "no 'Corrected reply:' header, no note about having been reviewed. Write it exactly as if "
    "it were your first and only draft — including the ---EMAIL--- marker line before it."
)
