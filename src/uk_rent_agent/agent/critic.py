"""Grounding critic for the response generator.

The critic is the last line of defence against *fabricated listing figures* (a
model quoting a rent, deposit, or total that never appeared in the data). Its
guiding rule is asymmetric:

    * It must catch invented prices.
    * It must NEVER destroy a legitimate answer.

The previous implementation violated the second half badly: it compared only the
``£``/``GBP``-prefixed numbers in the reply against a JSON dump of ``tool_raw_data``
and, on any mismatch, hard-replaced the entire user-facing answer with a canned
fallback string. Pure formatting ("2678 pcm" vs "£2,678"), any legitimate
arithmetic (rent × 12, weekly↔monthly, deposit = N weeks), and anything the model
was shown through the *context* rather than the raw tool payload all produced
false positives — and the "fix" deleted the answer (and its recommendations).

This module replaces that with three principled pieces:

1. NUMERIC NORMALIZATION (``_money_mentions`` / ``unsupported_reply_prices``).
   Prices are parsed out of both sides regardless of currency formatting — the
   ``£``/``GBP`` may be a prefix or a suffix, thousands separators are dropped, and
   ``pcm``/``pw``/``per month``/``per week`` annotations are read to recover the
   billing period. A reply price is *supported* when it (a) matches an evidence
   number within ~1 %, or (b) is a standard derivation of an evidence price:
   weekly↔monthly conversion, an annual / N-month total (× 1‑36), or a deposit of
   N weeks' rent (× 1‑6). Only prices carrying a currency/period marker are gated
   in the reply, so plain integers ("12 months", "3 beds") are never flagged.

2. EVIDENCE SURFACE. The critic node (in ``langgraph_agent``) gathers *everything
   the generator was shown* — ``tool_raw_data``, the observation, the assembled
   context (previous results, comparison data, current property) and the user's
   own budget — and passes it here as ``evidence``. Quoting any of those is
   therefore grounded, which fixes the "unsupported because it came from context"
   class of false positives.

3. ENFORCEMENT (``enforce_grounding``). User-facing text is never hard-replaced.
   A not-grounded verdict triggers exactly one regeneration pass with an explicit
   corrective instruction (supplied by the caller via the ``regenerate``
   callback). If the regenerated answer still fails, it is delivered anyway with a
   single appended caveat sentence — never the bare fallback, and the caller never
   drops recommendations. Every verdict is surfaced through the optional
   ``on_verdict`` hook so misfires stay measurable.

4. STATION NAMES (``station_name_claims`` / ``ungrounded_station_names``). Prices
   were the only *derived* thing gated; an invented NAME was gated by nothing. The
   same WC1H property was reported as nearest to "Covent Garden" (a string that
   exists nowhere in this repo — no table, no listing field, no scraper, no prompt,
   no dataset; TfL puts Russell Square 214 m away) in one turn and "Russell Square"
   in another, and nothing objected, because this module validated money figures
   only. A name the answer asserts is a *station* is now checked against the same
   evidence surface the prices are checked against, and a station named in no
   artifact and no reference result is reported as ``ungrounded_stations:<names>``
   — the identical mechanism as ``unsupported_prices``, so the eval collector, the
   critic log line and the regeneration pass all see it with no change elsewhere.
   Deliberately scoped to station CLAIMS rather than place names in general: see
   the section comment on :func:`station_name_claims`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from uk_rent_agent.agent.contracts import CriticVerdict


# ── numeric parsing ────────────────────────────────────────────────────────
# A bare number with optional thousands separators / decimals.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Currency immediately *before* the number ("£2,678", "GBP 450").
_CURRENCY_BEFORE = re.compile(r"(?:£|GBP)\s*\Z", re.IGNORECASE)
# Period markers immediately *after* the number, optionally after a "GBP" suffix.
_GBP = r"(?:GBP\s*)?"
_MONTHLY_AFTER = re.compile(
    r"\A\s*" + _GBP + r"(?:pcm|pm\b|/\s*(?:month|mo)\b|per\s+(?:calendar\s+)?month\b"
    r"|a\s+month\b|monthly\b|/month\b)",
    re.IGNORECASE,
)
_WEEKLY_AFTER = re.compile(
    r"\A\s*" + _GBP + r"(?:pw\b|/\s*(?:week|wk|w)\b|per\s+week\b|a\s+week\b|weekly\b|/week\b)",
    re.IGNORECASE,
)
# A "GBP" suffix on its own still marks the number as money (period unknown).
_CURRENCY_AFTER = re.compile(r"\A\s*GBP\b", re.IGNORECASE)

# ~1 % relative tolerance, with a small absolute floor to absorb rounding.
_REL_TOL = 0.01
_ABS_TOL = 1.0

# Integer-multiple range covering annual/N-month totals and deposit multiples.
_MAX_MONTHS = 36
_MAX_DEPOSIT_WEEKS = 6
# Weeks-per-year / months-per-year: the standard UK pcm ↔ pw conversion.
_WEEKS_PER_YEAR = 52
_MONTHS_PER_YEAR = 12

# Delivered (appended, never substituted) when a regenerated answer still fails.
CAVEAT = "Please double-check the exact prices against the source listing."
# Same contract for a station name the evidence never supplied: the answer is still
# delivered, but the reader is told which part of it is not established.
STATION_CAVEAT = (
    "Please double-check the station name against the source listing — it is not "
    "confirmed by the data I retrieved."
)

# Legacy hard-replacement strings. Retained only so callers/tests can assert the
# new pipeline never emits them; the enforcement path no longer uses them.
LEGACY_RETRIEVAL_MISS_FALLBACK = (
    "I couldn't verify this against current listing data. "
    "Please check a live property portal before deciding."
)
LEGACY_INCONSISTENCY_FALLBACK = (
    "I found a possible inconsistency in the available listing data, so I won't "
    "quote unverified details. Please check the source listing."
)


def _serialize(evidence: Any) -> str:
    """Flatten any evidence structure to a single searchable string."""
    if evidence is None:
        return ""
    if isinstance(evidence, str):
        return evidence
    return json.dumps(evidence, ensure_ascii=False, default=str)


def _to_float(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", "").rstrip("."))
    except ValueError:
        return None


def _all_numbers(text: str) -> set[float]:
    """Every numeric token in ``text`` (currency-agnostic); the direct-match pool."""
    out: set[float] = set()
    for match in _NUMBER.finditer(text):
        value = _to_float(match.group())
        if value is not None:
            out.add(value)
    return out


def _money_mentions(text: str) -> list[tuple[float, str]]:
    """Numbers that read as *money* plus their billing period.

    Returns ``(value, unit)`` where ``unit`` is ``"monthly"``, ``"weekly"`` or
    ``"unknown"``. A number qualifies when it carries a currency symbol (prefix or
    ``GBP`` suffix) or a rent-period annotation — plain integers are ignored so we
    never gate "12 months" or "3 bedrooms".
    """
    mentions: list[tuple[float, str]] = []
    if not text:
        return mentions
    for match in _NUMBER.finditer(text):
        value = _to_float(match.group())
        if value is None:
            continue
        before = text[max(0, match.start() - 8):match.start()]
        after = text[match.end():match.end() + 24]

        has_currency_before = bool(_CURRENCY_BEFORE.search(before))
        monthly = bool(_MONTHLY_AFTER.match(after))
        weekly = bool(_WEEKLY_AFTER.match(after))
        has_currency_after = bool(_CURRENCY_AFTER.match(after))

        if not (has_currency_before or monthly or weekly or has_currency_after):
            continue
        unit = "monthly" if monthly else "weekly" if weekly else "unknown"
        mentions.append((value, unit))
    return mentions


def _derivations(value: float, unit: str) -> set[float]:
    """Standard rent-derived figures from a single evidence price.

    * integer multiples (annual / N-month totals, and weekly-rent deposit
      multiples, both covered by ``value × 1‑36``),
    * weekly → monthly conversion,
    * monthly → weekly conversion and deposits of N weeks derived from it.
    """
    out: set[float] = {value}
    for n in range(2, _MAX_MONTHS + 1):
        out.add(value * n)
    if unit in ("weekly", "unknown"):
        out.add(value * _WEEKS_PER_YEAR / _MONTHS_PER_YEAR)
    if unit in ("monthly", "unknown"):
        weekly = value * _MONTHS_PER_YEAR / _WEEKS_PER_YEAR
        out.add(weekly)
        for n in range(1, _MAX_DEPOSIT_WEEKS + 1):
            out.add(weekly * n)
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(_ABS_TOL, _REL_TOL * max(abs(a), abs(b)))


def _close_to_any(value: float, pool: set[float]) -> bool:
    return any(_close(value, candidate) for candidate in pool)


def unsupported_reply_prices(response: str, evidence: Any) -> list[float]:
    """Reply prices that are neither present in nor derivable from the evidence."""
    evidence_text = _serialize(evidence)
    evidence_numbers = _all_numbers(evidence_text)
    evidence_rents = _money_mentions(evidence_text)

    # Derive from annotated rents (with their real unit) *and* every bare evidence
    # number (unit unknown) — the latter catches numeric JSON price fields.
    supported: set[float] = set()
    for value, unit in evidence_rents:
        supported |= _derivations(value, unit)
    for number in evidence_numbers:
        supported |= _derivations(number, "unknown")

    unsupported: list[float] = []
    for value, _unit in _money_mentions(response or ""):
        if not _close_to_any(value, supported):
            unsupported.append(value)
    return sorted(set(unsupported))


# ── station-name grounding ─────────────────────────────────────────────────
# THE LEGITIMATE NAME UNIVERSE, enumerated. Every source of a real station/place name
# that exists at answer time reaches this module through the SAME ``evidence`` surface
# the prices are checked against (``langgraph_agent._collect_grounding_evidence``):
#
#   1. ``core.place_reference.nearest_stations`` — TfL StopPoint ``commonName``, the
#      authoritative nearest-station index, with the measured distance attached.
#   2. ``core.place_reference.nearest_station_for_address`` — ``nearest_station`` /
#      ``other_stations_nearby`` / ``note``, surfaced in ``search_nearby_pois`` results.
#   3. ``core.tools.get_transport_info._resolve_station`` — ``resolved_station`` /
#      ``stations_used`` on a fare or journey lookup.
#   4. ``calculate_commute`` / ``maps_service.calculate_travel_details`` —
#      ``route_summary`` and the per-leg names ("Walk to Angel -> Northern line to
#      Euston"), which is where the station names in a real commute answer come from.
#   5. ``search_nearby_pois`` POI rows (an OSM ``tube_station`` name).
#   6. Listing evidence — address / description / area in ``search_properties`` and
#      ``get_property_details`` raw_data, plus the assembled context (focused listing,
#      previously-shown properties, recommended index) via ``build_context_info``.
#   7. Geocoder ``resolved_name`` (``maps_service._free_geocode``), via
#      ``place_reference.reference_point``.
#   8. ``maps_service.LANDMARK_TO_ADDRESS`` — the ONLY static table in this repo that
#      names stations (7 entries: Kings Cross, Euston, London Bridge). It enters the
#      evidence only when a resolved address is echoed back into a tool payload.
#
# There is deliberately NO vendored gazetteer to check against: grepping the repo for
# station-name constants finds those 7 landmark aliases and nothing else. The closed
# reference table is TfL's StopPoint index, consulted per turn — so the closed set a
# name must belong to is *this turn's evidence*, exactly as for a price.
#
# SCOPE: station claims only, NOT place names in general. A general place-name check
# has no closed set to check against (borough names, "central London", "the West End",
# generic area words and the user's own words echoed back are all legitimate prose), so
# it cannot be given a measured false-positive rate. A *station* claim is lexically
# marked in the answer itself — "X station", "the nearest station is X" — which makes
# both the extraction and the reference set decidable. Measured against the 196 retained
# (answer, evidence) pairs of the 2026-07-25 round: 7 station claims extracted, 0 flagged.
_MD_EMPHASIS = re.compile(r"[*_`~]+")
_WHITESPACE = re.compile(r"\s+")
# Words/tokens, with punctuation kept as its own token so a full stop, a comma or a
# markdown table pipe terminates a name run instead of being absorbed into it.
_NAME_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-]*|[^\sA-Za-z0-9]")

# Mode qualifiers that sit between the name and the word "station" and are not part of
# the name: "Russell Square Underground Station", "Chessington North National Rail
# stations". Walked past on the way back to the name.
_STATION_MODE_WORDS = frozenset({
    "underground", "tube", "rail", "railway", "overground", "dlr", "metro",
    "metrolink", "train", "tram", "mainline", "subway", "national", "light",
})
# Tokens that can never be part of a station name, so they end the run. This is what
# keeps "the station", "a train station", "the nearest station" and "around the station
# and high street" out of the claim set entirely.
_NOT_A_NAME_TOKEN = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "its", "it", "his", "her",
    "their", "our", "your", "my", "each", "both", "either", "neither", "any", "no",
    "some", "one", "another", "other", "nearest", "closest", "nearby", "local",
    "main", "own", "same", "whole", "every", "which", "what", "there", "from", "to",
    "at", "near", "by", "on", "in", "into", "onto", "via", "and", "or", "but",
    "with", "without", "for", "than", "then", "is", "was", "are", "be", "reach",
    "walk", "walking", "toward", "towards", "around", "between", "not", "none",
    "unknown", "unclear", "also", "however", "still", "about", "approximately",
    "roughly", "only", "just", "likely", "probably", "actually",
})
# Internal joiners: legitimate inside a multi-word name ("Highbury & Islington",
# "Isle of Dogs") but only when flanked by a capitalised token on the far side.
_NAME_JOINERS = frozenset({"and", "&", "of", "the", "upon", "on", "le", "de"})
# Copulas/labels that introduce the name in the "nearest station is X" / "Nearest
# station: X" shape — the exact phrasing of the Covent Garden incident.
_STATION_COPULA = frozenset({"is", "are", "was", "were", ":", "-", "=", "would",
                             "will", "be", "seems", "appears", "s"})
# Capitalised things that legitimately precede "station" without naming one. Without
# these, "a London Underground (Tube) station", "central London stations" and "the
# station is Zone 2" would all read as invented names.
_NOT_STATION_NAMES = frozenset({
    "london", "london underground", "underground", "tube", "national rail", "rail",
    "railway", "overground", "dlr", "metro", "train", "tram", "tfl",
    "transport for london", "central london", "greater london", "inner london",
    "outer london", "uk", "england", "britain", "manchester metrolink", "zone",
    "na", "n a", "tbc",
})
_STATION_WORDS = frozenset({"station", "stations"})


def _normalize_name(name: str) -> str:
    """Comparison form of a place name: case-, punctuation- and apostrophe-insensitive.

    "King's Cross" and "Kings Cross" must be the same name, and "Highbury & Islington"
    must match "Highbury and Islington" as TfL/OSM may spell either.
    """
    text = (name or "").lower().replace("’", "'").replace("'", "")
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return _WHITESPACE.sub(" ", text).strip()


def _is_capitalised(token: str) -> bool:
    return bool(token) and token[0].isupper()


def _base_word(token: str) -> str:
    """Lowercased token without a hyphenated tail ("Overground-only" -> "overground")."""
    return token.lower().split("-")[0]


def _clean_run(tokens: list[str]) -> str:
    name = " ".join(tokens).strip()
    name = re.sub(r"^(?:and|&|of|the)\s+", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+(?:and|&|of|the)$", "", name, flags=re.IGNORECASE).strip()
    return name


def station_name_claims(text: str) -> list[str]:
    """Names ``text`` asserts are tube/rail stations, in order of appearance.

    Two shapes, both anchored on the literal word "station"/"stations" so ordinary
    prose about places is never a claim:

    * suffix — ``"Highbury & Islington Station"``, ``"Old Street and Liverpool Street
      stations"``, ``"reach Hackney Central station"``: the capitalised run ending at
      the station word, minus any mode qualifier ("Underground", "National Rail").
    * label — ``"the nearest station is Covent Garden"``, ``"Nearest station: Covent
      Garden"``: the capitalised run introduced by a copula after the station word.

    A ``"<X> line station"`` phrasing names a LINE and is skipped, which is what keeps
    the line/station homonyms (Victoria, Piccadilly, Waterloo) out of the claim set
    without blacklisting the real stations of those names.
    """
    if not text:
        return []
    # Markdown emphasis can sit *inside* a name ("Chessington North **National Rail**
    # stations"), so flatten it before tokenising; arrows and dashes become boundaries.
    flat = _MD_EMPHASIS.sub(" ", str(text))
    for sep in ("->", "→", "—", "--"):
        flat = flat.replace(sep, " | ")

    claims: list[str] = []
    for line in flat.splitlines():
        tokens = _NAME_TOKEN.findall(line)
        for index, token in enumerate(tokens):
            if token.lower() not in _STATION_WORDS:
                continue

            # ── suffix shape: walk backwards to the start of the capitalised run.
            back = index - 1
            if back >= 0 and tokens[back].lower() == "line":
                continue  # "a Northern line station" — a line, not a named station
            while back >= 0 and _base_word(tokens[back]) in _STATION_MODE_WORDS:
                back -= 1
            run: list[str] = []
            while back >= 0:
                current = tokens[back]
                lowered = current.lower()
                if len(current) == 1 and not current.isalnum() and current != "&":
                    break  # punctuation: sentence/cell boundary
                if lowered in _NAME_JOINERS:
                    if run and back - 1 >= 0 and _is_capitalised(tokens[back - 1]):
                        run.append(current)
                        back -= 1
                        continue
                    break
                if lowered in _NOT_A_NAME_TOKEN or not _is_capitalised(current):
                    break
                run.append(current)
                back -= 1
            name = _clean_run(list(reversed(run)))
            if name and _normalize_name(name) not in _NOT_STATION_NAMES:
                claims.append(name)

            # ── label shape: a copula after the station word introduces the name.
            forward = index + 1
            saw_copula = False
            while forward < len(tokens) and tokens[forward].lower() in _STATION_COPULA:
                saw_copula = True
                forward += 1
            if not saw_copula:
                continue
            run = []
            while forward < len(tokens):
                current = tokens[forward]
                lowered = current.lower()
                if len(current) == 1 and not current.isalnum() and current != "&":
                    break
                if lowered in _NAME_JOINERS:
                    if run and forward + 1 < len(tokens) and _is_capitalised(tokens[forward + 1]):
                        run.append(current)
                        forward += 1
                        continue
                    break
                if lowered in _NOT_A_NAME_TOKEN or not _is_capitalised(current):
                    break
                run.append(current)
                forward += 1
            # "...is Russell Square Underground Station" — the trailing mode words and
            # the station word itself are a suffix, not part of the name.
            while run and _base_word(run[-1]) in (_STATION_MODE_WORDS | _STATION_WORDS):
                run.pop()
            name = _clean_run(run)
            if name and _normalize_name(name) not in _NOT_STATION_NAMES:
                claims.append(name)

    unique: list[str] = []
    seen: set[str] = set()
    for name in claims:
        key = _normalize_name(name)
        if key and key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def _split_named_list(run: str) -> list[str]:
    """"Old Street and Liverpool Street" -> two names; "Highbury & Islington" stays one.

    A plural station word with a spelled-out "and" is a list; "&" is a name character
    in TfL's own ``commonName`` values, so it is never a split point.
    """
    return [part.strip() for part in re.split(r"\s+and\s+", run, flags=re.IGNORECASE)
            if part.strip()]


def ungrounded_station_names(response: str, evidence: Any) -> list[str]:
    """Station names the reply asserts that appear nowhere in the evidence surface.

    The mirror of :func:`unsupported_reply_prices` for names: a station is *supported*
    when its normalized name occurs anywhere in the serialized evidence — a TfL
    StopPoint result, a commute route leg, a POI row, a listing address/description or
    the assembled context. Nothing else grounds it, so a name the model supplied from
    memory ("Covent Garden") has no support and is returned.
    """
    evidence_text = _normalize_name(_serialize(evidence))
    ungrounded: list[str] = []
    for run in station_name_claims(response or ""):
        if _normalize_name(run) in evidence_text:
            continue
        for candidate in _split_named_list(run):
            key = _normalize_name(candidate)
            if not key or key in _NOT_STATION_NAMES or key in evidence_text:
                continue
            # "Highbury & Islington" spelled "Highbury and Islington" in the evidence
            # is the same station; accept when every part is present.
            parts = [p for p in key.split(" and ") if p.strip()]
            if len(parts) > 1 and all(part in evidence_text for part in parts):
                continue
            ungrounded.append(candidate)

    unique: list[str] = []
    seen: set[str] = set()
    for name in ungrounded:
        key = _normalize_name(name)
        if key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def evaluate_grounding(
    response: str,
    evidence: Any,
    *,
    retrieval_expected: bool = True,
    tool_errored: bool = False,
) -> CriticVerdict:
    """Deterministic grounding rubric shared by online guardrails and evals.

    Prices *and asserted station names* are gated, and only when retrieval was
    expected. When it was not (direct answers / chat), gating is skipped entirely so
    conversational replies that echo the user's own numbers or the area they just
    named are never penalised.
    """
    answer = (response or "").strip()
    answered = bool(answer)
    issues: list[str] = []
    if not answered:
        issues.append("empty_answer")

    if not retrieval_expected:
        return CriticVerdict(
            grounded=True,
            answered=answered,
            retrieval_hit=True,
            issues=issues,
            needs_replan=False,
        )

    unsupported = unsupported_reply_prices(answer, evidence)
    # An asserted station name is graded by the same rule as a price: present in the
    # evidence, or not asserted at all. This is the check that was missing when the
    # model named "Covent Garden" — see piece 4 of the module docstring.
    ungrounded_stations = ungrounded_station_names(answer, evidence)
    asserts_facts = bool(_money_mentions(answer))
    # A retrieval_miss only matters when the tool actually errored *and* the reply
    # asserts specific figures. A legitimately-empty result is left alone — the
    # generator already narrates "no results" honestly.
    retrieval_miss = tool_errored and asserts_facts

    if unsupported:
        issues.append("unsupported_prices:" + ",".join(f"{v:g}" for v in unsupported))
    if ungrounded_stations:
        issues.append("ungrounded_stations:" + ",".join(ungrounded_stations))
    if retrieval_miss:
        issues.append("retrieval_miss")

    grounded = (answered and not unsupported and not ungrounded_stations
                and not retrieval_miss)
    return CriticVerdict(
        grounded=grounded,
        answered=answered,
        retrieval_hit=not tool_errored,
        issues=issues,
        needs_replan=not grounded,
    )


def _format_price(value: float) -> str:
    if value == int(value):
        return f"£{int(value):,}"
    return f"£{value:,.2f}"


def build_correction_instruction(
    unsupported: list[float],
    ungrounded_stations: Optional[list[str]] = None,
) -> str:
    """Corrective instruction appended to the generation prompt on regeneration.

    ``ungrounded_stations`` names the invented stations explicitly, for the same reason
    the prices are named: a generic "do not fabricate" line is what was already in the
    prompt when "Covent Garden" shipped.
    """
    stations = list(ungrounded_stations or [])
    if unsupported:
        figures = ", ".join(_format_price(v) for v in unsupported)
        cited = f"cited price figure(s) that are NOT present in the data above: {figures}."
    elif stations:
        cited = "named a station that does not appear in the data above."
    else:
        cited = "cited price figures that are not supported by the data above."
    station_rule = ""
    if stations:
        named = ", ".join(f"'{name}'" for name in stations)
        station_rule = (
            f"It also named station(s) that appear NOWHERE in the data above: {named}. "
            "Name a station ONLY if that exact name is present in the data above (a "
            "nearest-station result, a route leg, or a listing's own text). If no "
            "station is given, say the nearest station is not established rather than "
            "naming one, and do not substitute a different station you believe is "
            "nearby. "
        )
    return (
        "=== IMPORTANT CORRECTION ===\n"
        f"Your previous draft {cited} "
        f"{station_rule}"
        "Rewrite the answer so that EVERY monetary figure you mention is either copied "
        "verbatim from the data above or is an explicitly-labelled calculation of those "
        "figures (a weekly-to-monthly conversion, an annual/N-month total, or a deposit "
        "of N weeks' rent). Do NOT invent, guess, round, or approximate any price; if a "
        "figure is not in the data, omit it or say it is unavailable. Keep the rest of "
        "your answer, its structure, and its language unchanged.\n"
        "Corrected response:"
    )


def append_caveat(text: str, caveat: str = CAVEAT) -> str:
    """Append the double-check caveat once, without discarding the answer."""
    body = (text or "").rstrip()
    if caveat in body:
        return body
    return f"{body}\n\n{caveat}" if body else caveat


def _caveat_for(verdict: CriticVerdict) -> str:
    """The caveat that matches what actually failed.

    A station-only failure delivered with "double-check the exact prices" would point
    the reader at the one thing that was fine, so the name case gets its own sentence.
    """
    issues = list(getattr(verdict, "issues", None) or [])
    names = any(i.startswith("ungrounded_stations:") for i in issues)
    prices = any(i.startswith("unsupported_prices:") for i in issues)
    if names and not prices:
        return STATION_CAVEAT
    return CAVEAT


@dataclass
class GroundingOutcome:
    """Result of the enforcement pass handed back to the critic node."""

    response: str
    verdict: CriticVerdict
    attempts: int
    regenerated: bool


async def enforce_grounding(
    response: str,
    evidence: Any,
    *,
    regenerate: Callable[[str], Awaitable[str]],
    retrieval_expected: bool = True,
    tool_errored: bool = False,
    on_verdict: Optional[Callable[..., None]] = None,
) -> GroundingOutcome:
    """Grade ``response`` and, if it fails, run one corrective regeneration pass.

    ``regenerate(correction_instruction)`` must return a fresh answer string (it
    closes over the original generation prompt in the caller). The user-facing text
    is never hard-replaced with a canned fallback: a persistently-failing answer is
    delivered with a single appended caveat instead.
    """

    def _emit(verdict: CriticVerdict, stage: str) -> None:
        if on_verdict is not None:
            on_verdict(verdict, stage=stage)

    verdict = evaluate_grounding(
        response, evidence, retrieval_expected=retrieval_expected, tool_errored=tool_errored
    )
    _emit(verdict, "initial")
    if verdict.grounded:
        return GroundingOutcome(response=response, verdict=verdict, attempts=1, regenerated=False)

    correction = build_correction_instruction(
        unsupported_reply_prices(response, evidence),
        ungrounded_station_names(response, evidence),
    )
    try:
        new_text = await regenerate(correction)
    except Exception:  # regeneration must never crash the turn
        new_text = ""

    if not new_text or not new_text.strip():
        # No usable regeneration — keep the original answer with a caveat.
        return GroundingOutcome(
            response=append_caveat(response, _caveat_for(verdict)),
            verdict=verdict, attempts=2, regenerated=True
        )

    verdict2 = evaluate_grounding(
        new_text, evidence, retrieval_expected=retrieval_expected, tool_errored=tool_errored
    )
    _emit(verdict2, "regenerated")
    if verdict2.grounded:
        return GroundingOutcome(response=new_text, verdict=verdict2, attempts=2, regenerated=True)
    return GroundingOutcome(
        response=append_caveat(new_text, _caveat_for(verdict2)),
        verdict=verdict2, attempts=2, regenerated=True
    )


def has_specific_price_claims(text: str) -> bool:
    """True when ``text`` asserts *specific* monetary figures.

    Reuses the same currency/period-marked number machinery the grounding rubric
    uses (:func:`_money_mentions`), so a plain "12 months" / "3 bedrooms" never
    counts — only figures carrying a ``£``/``GBP`` marker or a rent-period
    annotation. This is the numeric precondition for the deterministic
    no-evidence 兜底 in the critic node.
    """
    return bool(_money_mentions(text or ""))


# ── evidence usability (H3) ──────────────────────────────────────────────────
# Deterministic markers that a retrieval tool returned NOTHING usable. These are the
# real strings the web-search stack emits when its backend (SearXNG) is unreachable or
# a query yields nothing: ``get_search_snippets`` / ``SearXNGSearch.format_for_llm``
# return "No search results found for this query."; the ``web_search`` tool's error
# path uses "No search results"; the legacy DuckDuckGo path used "Could not retrieve
# search information."; ``search_rent_prices`` returns "No rent price information
# found."; the crime/cost-of-living helpers return "No area provided ...". Matched
# case-insensitively as a whole-line signal so a real multi-result blob (whose only
# per-entry default is "No content available") is never mistaken for empty.
_UNUSABLE_EVIDENCE_MARKERS = (
    "no search results found",
    "no search results",
    "could not retrieve search information",
    "could not retrieve",
    "no rent price information found",
    "no results found",
    "no area provided",
    "no information found",
)


def _line_is_structural(line: str) -> bool:
    """A section header / separator emitted by ``web_search`` when it stitches
    sub-query results (``### Web Search: <q>``, ``---``), carrying no evidence itself."""
    s = line.strip()
    return not s or s.startswith("###") or s.startswith("---")


def _string_has_real_content(text: str) -> bool:
    """True when ``text`` contains at least one line of genuine retrieved content —
    i.e. a non-blank, non-structural line that is not a known 'nothing found' marker.

    Whitespace-only, a bare placeholder, or a stitched blob whose every content line is
    a placeholder all read as *no* real content (unusable)."""
    if not text or not str(text).strip():
        return False
    for line in str(text).splitlines():
        if _line_is_structural(line):
            continue
        low = line.strip().lower()
        if any(marker in low for marker in _UNUSABLE_EVIDENCE_MARKERS):
            continue
        return True  # a real content line survived
    return False


# Content-bearing fields on the tool-result / artifact dict shapes this project emits.
_EVIDENCE_CONTENT_KEYS = (
    "results", "content", "snippets", "answer", "text", "message",
    "data", "detailed_data", "recommendations", "properties",
)


def evidence_usable(artifact_or_evidence) -> bool:
    """Deterministic 'is this actually usable retrieval evidence?' predicate (H3).

    Returns False for anything that carries no real retrieved content: an explicit
    ``success is False`` tool result/artifact, ``None``/empty containers, whitespace,
    a known placeholder string ("No search results found ...", "Could not retrieve ...",
    a zero-entry result set), or a stitched web-search blob whose every content line is
    such a placeholder. Returns True only when some genuine content survives.

    Accepts either an fc ``tool_artifacts`` entry ({tool, success, raw_data, ...}), a
    raw tool-result dict ({success, results, ...}), or a bare evidence value (string /
    list / dict). The critic's no-reliable-data 兜底 keys off *this* function so a
    numeric answer built on empty/failed retrieval is caught even when the tool
    mislabels itself as ``success=True``."""
    ev = artifact_or_evidence
    if ev is None:
        return False
    if isinstance(ev, bool):
        return ev
    if isinstance(ev, (int, float)):
        return True  # a concrete numeric datum is content
    if isinstance(ev, str):
        return _string_has_real_content(ev)
    if isinstance(ev, (list, tuple, set)):
        return any(evidence_usable(item) for item in ev)
    if isinstance(ev, dict):
        # An explicit failure flag makes the whole payload unusable regardless of shape.
        if ev.get("success") is False:
            return False
        # fc artifact wrapper: the real payload lives in raw_data.
        if "raw_data" in ev:
            return evidence_usable(ev.get("raw_data"))
        # Tool-result dict: judge by its content-bearing fields when present.
        present = [k for k in _EVIDENCE_CONTENT_KEYS if k in ev]
        if present:
            return any(evidence_usable(ev.get(k)) for k in present)
        # A dict with no known content keys: usable iff any value reads as real content.
        return any(evidence_usable(v) for v in ev.values())
    # Unknown scalar type: treat mere presence as content.
    return bool(ev)


def no_reliable_data_message(reply_language: str) -> str:
    """Deterministic replacement delivered when a numeric answer has *zero* usable
    retrieval evidence (the H3 兜底 — a hard replace, not a caveat).

    Short, offers a retry, emoji-free, and localized off the turn's reply language.
    """
    if (reply_language or "").lower().startswith("zh"):
        return (
            "抱歉，我暂时无法获取可靠数据来回答这个问题里的具体数字，"
            "为避免给出可能不准确的信息，我不便提供估算。请稍后再试，或换个方式提问。"
        )
    return (
        "Sorry, I don't have reliable data to give specific figures for this right now. "
        "Please try again shortly, or rephrase your question."
    )


def safe_fallback(verdict: CriticVerdict) -> str:
    """Deprecated. Retained for backward compatibility only.

    The enforcement pipeline (:func:`enforce_grounding`) no longer hard-replaces
    user-facing text, so this is unused by the live graph. Kept importable so any
    external caller does not break.
    """
    if "retrieval_miss" in verdict.issues:
        return LEGACY_RETRIEVAL_MISS_FALLBACK
    return LEGACY_INCONSISTENCY_FALLBACK


# ── false retrieval provenance (2026-07-22 ruling) ───────────────────────────
# PORTED, DELIBERATELY AND ALONE, from the terminated `hardening/correctness-only`
# branch (that branch's `src/uk_rent_agent/agent/critic.py:543-562`) so the evaluator
# constraint `no_false_retrieval_provenance` can be re-enabled. The general rule is
# "do not cherry-pick product code from a NO-GO branch"; this is the one exception the
# handoff names, and it is scoped to exactly this predicate plus its two literal cue
# tables. NOTHING else came across:
#   * `false_retrieval_provenance(response, evidence)` — the verdict composer — did NOT
#     come, because the grader composes `claims_no_retrieval` with `evidence_usable`
#     itself and nothing on mainline needs the composed form.
#   * `evaluate_grounding(..., check_provenance=...)`, `build_correction_instruction`'s
#     `provenance_denied` arm, `minimal_honest_answer` / `WEB_EVIDENCE_INSUFFICIENT_*`
#     and the fail-closed hard-replacement pipeline did NOT come. The fail-closed
#     fallback is the CONFIRMED A14 quality regression ("must not be carried elsewhere
#     in its present form"); this predicate neither calls it nor is called by it.
#
# `claims_no_retrieval` is a pure text predicate: its only inputs are the two cue
# tables below and `re`. It is read ONLY by the evaluator grader
# (`evaluation/metrics/graders.py::_c_no_false_retrieval_provenance`); no product code
# path reads it, so porting it changes no runtime behaviour.
#
# Detection is deterministic cues only — matched against the ruled phrases
# ("没有搜索" / "无法搜索" / "没有任何搜索数据") and their close variants; English via
# word-boundary regex so ordinary prose never matches. The regex is deliberately
# NARROW on the "retrieve" verb: the critic's own honest fallbacks say a FIGURE could
# not be retrieved, which is not a claim that no search happened. See
# `tests/test_false_retrieval_provenance.py::test_the_critics_own_honest_fallbacks_do_not_self_trip`.
_PROVENANCE_DENIAL_ZH = (
    "没有搜索", "没有进行搜索", "未进行搜索", "未搜索", "无法搜索", "未能搜索",
    "没有任何搜索数据", "没有检索", "无法检索", "未能检索", "未进行检索",
)
_PROVENANCE_DENIAL_EN = re.compile(
    r"(?:\b(?:did\s+not|didn'?t|could\s+not|couldn'?t|cannot|can'?t|unable\s+to|"
    r"have\s+not|haven'?t|was\s+not\s+able\s+to)\s+(?:search\b|run\s+a\s+search\b|"
    r"perform\s+a\s+search\b|do\s+a\s+(?:web\s+)?search\b|retrieve\s+(?:any\s+)?"
    r"(?:search\s+results|web\s+results)\b)"
    r"|\bno\s+search\s+(?:was|has\s+been)\s+(?:performed|done|run)\b"
    r"|\bwithout\s+(?:having\s+)?search(?:ed|ing)\b)",
    re.IGNORECASE)


def claims_no_retrieval(text: str) -> bool:
    """True when ``text`` asserts that no search/retrieval happened or was possible."""
    body = text or ""
    if any(cue in body for cue in _PROVENANCE_DENIAL_ZH):
        return True
    return bool(_PROVENANCE_DENIAL_EN.search(body))
