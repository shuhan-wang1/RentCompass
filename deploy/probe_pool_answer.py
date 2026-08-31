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
     ``llm_usage_status != "not_instrumented"``.

(5) is opt-in via ``--canary-log`` because the turn endpoint does NOT return the
canary record: ``app/app.py`` emits it to the ``CANARY_LOG_PATH`` JSONL sink
only, so it is readable only where that file is mounted.

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

# app/core/turn_observations.py::USAGE_NOT_INSTRUMENTED — no observer saw the
# turn, so every rate computed from it has an unknown denominator.
USAGE_NOT_INSTRUMENTED = "not_instrumented"

# The control pool's arch string.  The DEPLOYED legacy pool predates the
# `X-Agent-Specialists` header (`app` is the standing rollback escape hatch and
# must not be recreated — deploy/switch_pool.sh), so it sends no such header and
# `header()` returns "".  `set_canary_weight.sh::verify_local` has carried an
# exemption for exactly that since the header was introduced:
#
#     [[ "$label" == legacy && "$specialists" == none && "$want_specialists" == 0 ]]
#
# This probe mirrors it EXACTLY — an absent header counts as 0 only for the
# legacy pool.  A candidate that fails to state its specialist bit is still a
# failure: that is the header the whole manager_v1 rollout is gated on.
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
    """Mirror of `set_canary_weight.sh::verify_local`'s specialist comparison."""
    if not expected:
        return True
    if observed == expected:
        return True
    return (
        not observed
        and expected in _ABSENT_SPECIALISTS_OK
        and arch == LEGACY_ARCH
    )


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
                             f"{USAGE_NOT_INSTRUMENTED}")
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
    if args.canary_log:
        record = canary_record(args.canary_log, request_id)
        if record is None:
            return verdict(
                False,
                f"no canary record for request_id={request_id} in {args.canary_log}; "
                "the turn ran but is not instrumented",
            )
        usage = str(record.get("llm_usage_status", USAGE_NOT_INSTRUMENTED))
        if usage == USAGE_NOT_INSTRUMENTED:
            return verdict(
                False,
                f"canary record reports llm_usage_status={usage!r}; the turn's model "
                "usage was never observed",
            )

    return verdict(
        True,
        f"{args.url} answered a real turn (arch={arch or '<unset>'} "
        f"specialists={specialists or '<unset>'} response_type={response_type!r} "
        f"chars={len(message)}"
        + (f" llm_usage_status={usage!r}" if usage else "")
        + ")",
    )


if __name__ == "__main__":
    sys.exit(main())
