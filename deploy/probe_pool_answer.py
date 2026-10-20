#!/usr/bin/env python3
"""Prove a pool can ANSWER, not merely that it can identify itself.

Why this exists
---------------
`/ready` verifies dependencies and `X-Agent-*` identity headers, and
`set_canary_weight.sh::verify_local` verifies those headers before exposing a
cohort.  Neither proves the pool can produce a real answer.  On 2026-07-25 a
stale ``DEEPSEEK_MODEL`` in ``app/.env`` broke BOTH pools for a day while
`/ready` stayed green: the readiness LLM check only asserts that a credential
string is non-empty (``connectivity: "not_probed"`` in
``src/uk_rent_agent/web/asgi.py::_check_llm_configuration``).  A greeting cannot
detect it either — ``app/core/agent_loop.py::guard_node`` answers greetings,
rent conversions and statutory-money questions deterministically, with zero LLM
calls.

So this probe drives ONE real turn through ``POST /api/alex`` and asserts:

  1. HTTP 200 with ``X-Agent-Outcome: ok``;
  2. the identity headers match the arch/specialists the caller expects;
  3. the answer text is not produced by any of the canned/fallback renderers
     (the markers below are copied verbatim from the code that emits them);
  4. the answer carries the grounding substring the chosen query can only
     contain when its tool actually returned (default: ``data.police.uk``);
  5. optionally, that the canary record for this exact ``request_id`` reports
     ``llm_usage_status != "not_instrumented"``;
  6. optionally, that a turn whose answer ANNOUNCES a tool call actually ran
     one (``tool_batches > 0``).

(5) and (6) are opt-in via ``--canary-log`` because the turn endpoint does NOT
return the canary record: ``app/app.py`` emits it to the ``CANARY_LOG_PATH``
JSONL sink only, so it is readable only where that file is mounted.

(6) exists because of a class this probe used to score as a PASS.  On 2026-09-03
a live fc follow-up turn ended in a 105-character *"Let me search for actual
one-bedroom listings…"* stub with ``llm_calls=1`` and ``tool_batches=0``: the
announced tool call was never made.  It carries NO canned/fallback marker, so
every marker-based check — this probe and the monitor alike — read it as a real
answer.  Announcing work and not doing it is a failed turn, and after a weight
change it is exactly the turn nobody sees.

Stdlib only (Python >= 3.10), so it runs inside the deploy path with no
dependencies.  Exit codes:

    0  PASS          — the pool answered a real, grounded turn
    1  FAIL          — it did not, and the caller must not expose a cohort
    2  INCONCLUSIVE  — the pool asked a clarifying question instead of
                       answering, so nothing was proven either way (see
                       ``response_type == "clarification"`` below)

    python3 deploy/probe_pool_answer.py --url http://127.0.0.1:5002 \
        --expect-arch manager_v1 --expect-specialists 1
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import uuid


# The default query is benchmark case D1 from evaluation/benchmark/cases.jsonl.
# It is a smoke case, needs no conversation history, routes to exactly one tool
# (`check_safety`), and its answer must cite `data.police.uk` — a string no
# canned renderer produces.  A property search would also work but pays for a
# live scrape on every weight change.
DEFAULT_QUERY = "Is Peckham (SE15 5DP) safe to live in?"
DEFAULT_GROUNDING = "data.police.uk"

# Verbatim openers/bodies of every renderer that answers WITHOUT a healthy model
# turn.  Sources, in order:
#   app/core/agent_loop.py::_artifact_grounded_fallback_answer
#   src/uk_rent_agent/agent/critic.py::no_reliable_data_message
#   app/core/dsml_guard.py::_FALLBACK
#   app/core/agent_loop.py::guard_node          (greeting fast path)
#   app/app.py                                  (crash / empty-response defaults)
FALLBACK_MARKERS: tuple[str, ...] = (
    "Sorry — this turn ran long, so here is a brief answer",
    "Sorry — I couldn't retrieve reliable specific figures right now",
    "This answer was cut short by the time budget",
    "I do not yet have listing results ready to show.",
    "Sorry, I don't have reliable data to give specific figures",
    "Sorry — I couldn't put that answer together properly",
    "Hi! I'm Alex, your UK student-housing assistant.",
    "Sorry, something went wrong while handling your request.",
    "Sorry, this turn could not be saved reliably.",
    "I'm here to help! What would you like to know?",
    "抱歉，我未能获取到可靠的具体数字",
    "抱歉，本轮处理耗时较长",
    "抱歉，我暂时无法获取可靠数据",
    "抱歉，这条回复没能正常生成。",
    "抱歉，处理您的请求时出错了。",
    "抱歉，无法可靠保存本轮结果",
    "你好！我是 Alex，帮你在英国找学生房。",
)

# Openers a turn uses to ANNOUNCE that it is about to call a tool.  Regexes,
# matched case-insensitively against the answer text with `.search()`, and only
# ever consulted together with the joined canary record: the string alone is not
# a defect — the string plus ``tool_batches == 0`` is.  Each pattern requires a
# lookup VERB, not just the auxiliary, so "let me know" and "I'll be honest" do
# not match; a false positive here would fail a weight change on a healthy pool.
#
# TWIN: copied VERBATIM from ``app/core/agent_loop.py::ANNOUNCED_TOOL_PATTERNS``,
# where the loop uses the same list to force one more bounded iteration before it
# ships such a turn.  The two must stay identical;
# ``tests/test_f5e_announced_tool_patterns.py`` asserts it.  This file stays
# stdlib-only and imports nothing from the app, so the list is duplicated rather
# than shared — the probe runs inside the deploy path, under sudo, from arbitrary
# directories, where the application package is not importable.
ANNOUNCED_TOOL_PATTERNS: tuple[str, ...] = (
    # EN
    r"\blet me\s+(?:just\s+|first\s+|quickly\s+|go\s+(?:ahead\s+)?and\s+)?"
    r"(?:search|look\s+up|look\s+for|check|find|fetch|pull|run|query|verify|confirm|"
    r"retrieve|get)\b",
    r"\bi(?:'|’)?ll\s+(?:just\s+|first\s+|quickly\s+|go\s+(?:ahead\s+)?and\s+|now\s+)?"
    r"(?:search|look\s+up|look\s+for|check|find|fetch|pull|run|query|verify|confirm|"
    r"retrieve|get)\b",
    r"\bi\s+will\s+(?:now\s+)?(?:search|look\s+up|look\s+for|check|find|fetch|pull|run|"
    r"query|verify|confirm|retrieve)\b",
    r"\bi(?:'|’)?m\s+going\s+to\s+(?:search|look\s+up|look\s+for|check|find|fetch|pull|"
    r"run|query|verify)\b",
    r"\blet me check the context\b",
    r"\b(?:one\s+moment|give\s+me\s+a\s+(?:moment|second|sec))\s+while\s+i\b",
    r"\b(?:searching|checking|looking)\s+(?:for\s+\w+|now|that\s+up)\b",
    r"\bhold\s+on\s+while\s+i\b",
    # ZH
    r"(?:让我|我)(?:来|先|现在|这就|马上)?(?:搜索|搜一下|搜下|查询|查一下|查下|查找|"
    r"检索|核实|确认|看一下|看看)",
    r"我(?:将|会|要)(?:为(?:您|你)|给(?:您|你))?(?:搜索|查询|查找|检索|核实)",
    r"(?:稍等|请稍候|请稍等)[，,]?\s*我(?:来|先)?(?:搜索|查询|查找|检索)",
    r"正在(?:为(?:您|你))?(?:搜索|查询|查找|检索)",
    r"我需要(?:先)?(?:搜索|查询|查找|检索|核实)",
)

_ANNOUNCED_TOOL_RE = tuple(re.compile(p, re.IGNORECASE) for p in ANNOUNCED_TOOL_PATTERNS)

# The EXPLICIT marker `agent_loop` appends when its bounded retry ALSO produced no
# tool call.  It rides in the answer text, so it needs no canary record: an answer
# carrying it has already admitted the lookup did not happen.  Verbatim from
# ``app/core/agent_loop.py::ANNOUNCED_TOOL_STUB_MARKER_EN`` / ``_ZH``.
ANNOUNCED_TOOL_STUB_MARKER_EN = (
    "I was not able to run that lookup this round, so nothing above is a fresh result.")
ANNOUNCED_TOOL_STUB_MARKER_ZH = "本轮未能完成上述查询，以上内容并非新的检索结果。"
ANNOUNCED_TOOL_STUB_MARKERS: tuple[str, ...] = (
    ANNOUNCED_TOOL_STUB_MARKER_EN,
    ANNOUNCED_TOOL_STUB_MARKER_ZH,
)

# app/core/turn_observations.py::USAGE_NOT_INSTRUMENTED — no observer saw the
# turn, so every rate computed from it has an unknown denominator.
USAGE_NOT_INSTRUMENTED = "not_instrumented"

# app/core/canary_telemetry.py::TOOL_LEDGER_COMPLETE — `tool_batches` is a real
# observed count.  Anything else (today: "unavailable", the crash path) means the
# ledger died with the turn, so a 0 there proves nothing and must not convict.
TOOL_LEDGER_COMPLETE = "complete"


def announced_tool(message: str) -> str:
    """The text that announced a lookup, or "" when the answer announced none."""
    for pattern in _ANNOUNCED_TOOL_RE:
        match = pattern.search(message or "")
        if match:
            return match.group(0)
    return ""


# The control pool's arch string.  The legacy pool predated the
# `X-Agent-Specialists` header (`app` is the standing rollback escape hatch and
# was not recreated just to gain it — deploy/switch_pool.sh), so it sent no such
# header and `header()` returned "".  `set_canary_weight.sh::verify_local` has
# carried an exemption for exactly that since the header was introduced:
#
#     [[ "$label" == legacy && "$specialists" == none && "$want_specialists" == 0 ]]
#
# The rule was widened on 2026-09-01 (R3-H1): an absent header counts as 0 for
# ANY pool whose EXPECTED identity is specialists=0, not only for `legacy`.
# Requiring it for an expected 0 made the emergency rollback and the deploy drain
# refuse against the very images they exist to protect.
#
# CORRECTED 2026-09-06: the 1df0d4f release of 2026-09-03 rebuilt both pools, and
# :5001 and :5002 now DO send `X-Agent-Specialists: 0`.  The exemption stays —
# it is the compatibility path for a pool that has not been rebuilt — but it no
# longer describes this host.  A candidate expected to run specialists=1 must
# still state it: that is the header the manager_v1 rollout is gated on, and no
# exemption applies there.
LEGACY_ARCH = "legacy"
_ABSENT_SPECIALISTS_OK = frozenset({"0", "none"})


EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2


def verdict(passed: bool, message: str) -> int:
    print(f"{'PASS' if passed else 'FAIL'} answer-probe: {message}")
    return EXIT_PASS if passed else EXIT_FAIL


def inconclusive(message: str) -> int:
    print(f"INCONCLUSIVE answer-probe: {message}")
    return EXIT_INCONCLUSIVE


def specialists_match(observed: str, expected: str, arch: str) -> bool:
    """Python twin of the shell `specialists_ok` helper.

    `arch` is kept in the signature (and reported by callers) because the pool's
    architecture is what makes an absent header explicable, but it no longer
    narrows the exemption: any pool EXPECTED to run specialists=0 may omit the
    header.  See the block comment above.
    """
    if not expected:
        return True
    if observed == expected:
        return True
    return not observed and expected in _ABSENT_SPECIALISTS_OK


def post_turn(base_url: str, query: str, timeout: float, request_id: str):
    """POST one turn; return (status, headers, body-or-raw-text)."""
    url = base_url.rstrip("/") + "/api/alex"
    payload = json.dumps({"message": query, "ui_language": "en"}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            # A fresh id per run: app/app.py replays a persisted response for a
            # repeated X-Request-Id, which would prove nothing about this pool now.
            "X-Request-Id": request_id,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, dict(response.headers), raw
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, dict(exc.headers or {}), raw


def canary_record(path: str, request_id: str) -> dict | None:
    """Last canary.turn record for `request_id`, or None when absent."""
    found = None
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                start = line.find("{")
                if start < 0:
                    continue
                try:
                    record = json.loads(line[start:])
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("request_id") == request_id:
                    found = record
    except OSError:
        return None
    return found


def header(headers: dict, name: str) -> str:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    return lowered.get(name.lower(), "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive one real turn against a pool and verify a real answer came back.",
    )
    parser.add_argument("--url", required=True,
                        help="pool base URL, e.g. http://127.0.0.1:5002")
    parser.add_argument("--expect-arch", default="",
                        help="required X-Agent-Arch (empty = do not check)")
    parser.add_argument("--expect-specialists", default="",
                        help="required X-Agent-Specialists (empty = do not check)")
    parser.add_argument("--query", default=DEFAULT_QUERY,
                        help="turn text to send (default: benchmark case D1)")
    parser.add_argument("--expect-substring", default=DEFAULT_GROUNDING,
                        help="substring the grounded answer must contain "
                             "(empty = only the fallback-marker check applies)")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="HTTP timeout in seconds (default: 120)")
    parser.add_argument("--canary-log", default="",
                        help="optional canary JSONL sink; when readable, the record for "
                             "this request_id must not be llm_usage_status="
                             f"{USAGE_NOT_INSTRUMENTED}, and a turn whose answer "
                             "announces a tool call must report tool_batches > 0")
    args = parser.parse_args(argv)

    request_id = f"answer-probe-{uuid.uuid4()}"
    try:
        status, headers, raw = post_turn(args.url, args.query, args.timeout, request_id)
    except Exception as exc:  # noqa: BLE001 — any transport failure is a failed probe
        return verdict(False, f"{args.url} did not answer: {type(exc).__name__}: {exc}")

    if status != 200:
        return verdict(False, f"{args.url} answered HTTP {status} (body: {raw[:160]!r})")

    outcome = header(headers, "X-Agent-Outcome")
    if outcome and outcome != "ok":
        return verdict(False, f"{args.url} reported X-Agent-Outcome={outcome!r}")

    arch = header(headers, "X-Agent-Arch")
    specialists = header(headers, "X-Agent-Specialists")
    if args.expect_arch and arch != args.expect_arch:
        return verdict(False, f"answered as arch {arch!r}, expected {args.expect_arch!r}")
    if not specialists_match(specialists, args.expect_specialists, arch):
        return verdict(
            False,
            f"answered with specialists={specialists!r}, expected "
            f"{args.expect_specialists!r}",
        )

    try:
        body = json.loads(raw)
    except ValueError:
        return verdict(False, f"answer body is not JSON: {raw[:160]!r}")
    if not isinstance(body, dict):
        return verdict(False, "answer body is not a JSON object")

    response_type = str(body.get("response_type", ""))
    message = str(body.get("message", "") or "")
    if response_type == "error":
        return verdict(False, f"response_type=error: {message[:160]!r}")
    if not message.strip():
        return verdict(False, "the pool returned an empty answer")

    for marker in FALLBACK_MARKERS:
        if marker in message:
            return verdict(
                False,
                f"answer came from a canned/fallback renderer (marker {marker[:48]!r}); "
                "the pool is reachable but not answering",
            )
    # The runtime's own admission that an announced lookup did not run
    # (`agent_loop._mark_announced_stub`).  Unlike the heuristic further down this
    # needs no canary record: the answer has already said it.
    for marker in ANNOUNCED_TOOL_STUB_MARKERS:
        if marker in message:
            return verdict(
                False,
                f"the answer carries the announced-but-unexecuted marker "
                f"({marker[:48]!r}): the pool told the user it could not run the "
                "lookup it had announced",
            )
    # `app/app.py` returns response_type "clarification" when the graph asked a
    # follow-up question instead of answering (the missing-area / soft-criteria
    # gates, `response_type in {"question", "clarification"}`).  Such a turn CAN
    # be produced without a model call (the criteria gate is deterministic), so it
    # is not proof the pool works — but it is equally not proof that it is broken,
    # and it can never carry the tool grounding the substring check requires.
    # Reporting it as FAIL would kill a weight change over a legitimate reply, so
    # it is reported separately and the caller retries once.
    if response_type == "clarification":
        return inconclusive(
            f"{args.url} asked a clarifying question instead of answering "
            f"(response_type='clarification', chars={len(message)}); a clarification "
            "carries no tool grounding, so this run proves nothing either way",
        )
    if args.expect_substring and args.expect_substring not in message:
        return verdict(
            False,
            f"answer does not contain the grounding marker {args.expect_substring!r} — "
            f"the query's tool did not produce evidence (response_type={response_type!r})",
        )

    usage = ""
    tool_batches_seen = None
    if args.canary_log:
        record = canary_record(args.canary_log, request_id)
        if record is None:
            return verdict(
                False,
                f"no canary record for request_id={request_id} in {args.canary_log}; "
                "the turn ran but is not instrumented",
            )
        _batches_seen = record.get("tool_batches")
        if isinstance(_batches_seen, int) and not isinstance(_batches_seen, bool):
            tool_batches_seen = _batches_seen
        usage = str(record.get("llm_usage_status", USAGE_NOT_INSTRUMENTED))
        if usage == USAGE_NOT_INSTRUMENTED:
            return verdict(
                False,
                f"canary record reports llm_usage_status={usage!r}; the turn's model "
                "usage was never observed",
            )

        # Only convicts on BOTH halves — an announcement in the
        # text AND an observed, trustworthy zero in the ledger.
        announced = announced_tool(message)
        batches = record.get("tool_batches")
        ledger = str(record.get("tool_ledger_status", ""))
        if (
            announced
            and ledger == TOOL_LEDGER_COMPLETE
            and isinstance(batches, int)
            and not isinstance(batches, bool)
            and batches == 0
        ):
            return verdict(
                False,
                f"the answer announces a tool call ({announced!r}) but the canary "
                f"record reports tool_batches=0 with tool_ledger_status="
                f"{ledger!r}: the pool said it would look something up and never "
                "did. This class carries no fallback marker, so nothing else "
                "catches it",
            )

    return verdict(
        True,
        f"{args.url} answered a real turn (arch={arch or '<unset>'} "
        f"specialists={specialists or '<unset>'} response_type={response_type!r} "
        f"chars={len(message)}"
        + (f" llm_usage_status={usage!r}" if usage else "")
        + (f" tool_batches={tool_batches_seen}" if tool_batches_seen is not None else "")
        + ")",
    )


if __name__ == "__main__":
    sys.exit(main())
