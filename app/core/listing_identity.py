"""Bind listing hyperlinks to structured entities before any answer is saved."""
from __future__ import annotations

import re

_LINK = re.compile(r'\[([^\]\n]+)\]\((https?://(?:www\.)?onthemarket\.com/[^\s)]+)\)', re.I)
_BARE = re.compile(r'https?://(?:www\.)?onthemarket\.com/details/\d+/?', re.I)


def listing_entities(value, depth=0):
    if depth > 7:
        return []
    if isinstance(value, dict):
        rows = []
        url = value.get('url') or value.get('URL') or value.get('property_url')
        address = value.get('address') or value.get('Address') or value.get('property_address')
        if url and address:
            rows.append({'url': str(url), 'address': str(address),
                         'price': value.get('price') or value.get('Price') or value.get('property_price')})
        for child in value.values():
            if isinstance(child, (dict, list)):
                rows.extend(listing_entities(child, depth + 1))
        return rows
    if isinstance(value, list):
        return [row for child in value[:100] for row in listing_entities(child, depth + 1)]
    return []


def bind_listing_links(text: str, entities: list[dict]) -> str:
    by_url = {}
    for row in entities:
        if row.get('url') and row.get('address'):
            by_url.setdefault(row['url'].rstrip('/'), row)  # current evidence precedes history
    rows = list(by_url.values())

    def entity_for_label(label):
        matches = [row for row in rows if len(row['address'].split(',')[0].strip()) >= 5
                   and row['address'].split(',')[0].strip().lower() in label.lower()]
        return max(matches, key=lambda row: len(row['address'].split(',')[0])) if matches else None

    def replace(match):
        label, url = match.groups()
        row = entity_for_label(label)
        if row:
            return f"[{label}]({row['url']})"
        return match.group(0) if url.rstrip('/') in by_url else label

    result = _LINK.sub(replace, str(text or ''))
    # A fabricated raw ID is not a valid source either. Keep its surrounding answer.
    def bare(match):
        start = result.rfind('\n', 0, match.start()) + 1
        end = result.find('\n', match.end())
        line = result[start:end if end >= 0 else len(result)]
        named = [row for row in rows if len(row['address'].split(',')[0]) >= 5
                 and row['address'].split(',')[0].lower() in line.lower()]
        if len(named) == 1:
            return named[0]['url']
        return match.group(0) if match.group(0).rstrip('/') in by_url else ''
    result = _BARE.sub(bare, result)
    lines = []
    for line in result.split('\n'):
        named = [row for row in rows if len(row['address'].split(',')[0]) >= 5
                 and row['address'].split(',')[0].lower() in line.lower()]
        if len(named) == 1:
            # The rent correction is for a claim ABOUT this listing: from the sentence that
            # names it on. "Prices range from £2400 to £2650 per month. … the St. Augustine's
            # Road flat is …" had its range rewritten to "£2400 to £2400" because the line
            # named one listing in a LATER sentence (r07 7d807c65, B-F9).
            key = named[0]['address'].split(',')[0].lower()
            sentences = _sentences(line)
            first = next(i for i, sentence in enumerate(sentences) if key in sentence.lower()) \
                if any(key in sentence.lower() for sentence in sentences) else len(sentences)
            if first < len(sentences):
                line = ''.join(sentences[:first]) + _bind_sentence(''.join(sentences[first:]), named[0])
        lines.append(line)
    return '\n'.join(lines)


# A sentence ends at . ! ? (or 。！？) before a space or the end, never at "St." / "Dr." or a
# decimal point.
_SENTENCE_END = re.compile(r'[.!?](?=\s|$)|[。！？]')
_ABBREVIATION = re.compile(r'\b(?:St|Dr|Mr|Mrs|Ms|Prof|Sr|Jr|vs|No)\.$', re.I)
# The amount is one end of a range ("from £2,400 to £2,650", "£2,400–£2,650"), not the rent.
_RANGE_BEFORE = re.compile(
    r'(?:\bfrom|\bbetween)\s*£\s*\d[\d,]*(?:\.\d+)?\s*(?:/month|pcm|per month|a month)?\s*'
    r'(?:to|and|-|–|—)\s*$|£\s*\d[\d,]*(?:\.\d+)?\s*(?:-|–|—|to)\s*$|(?:从|介于)\s*£', re.I)
_RANGE_AFTER = re.compile(r'^\s*(?:-|–|—|to|and)\s*£\s*\d', re.I)


def _sentences(line: str) -> list[str]:
    parts, start = [], 0
    for match in _SENTENCE_END.finditer(line):
        if _ABBREVIATION.search(line[:match.end()]):
            continue
        parts.append(line[start:match.end()])
        start = match.end()
    parts.append(line[start:])
    return [part for part in parts if part]


def _bind_sentence(line: str, row: dict) -> str:
    """Correct the rent and listing id stated in ``line``, which names ``row``."""
    price = re.search(r'\d[\d,]*(?:\.\d+)?', str(row.get('price') or ''))
    if price and not re.search(r'\bpw\b|/week|per week', str(row.get('price') or ''), re.I):
        amount = '£' + price.group()
        # Correct only a rent assertion, preserving budgets, deposits and
        # independent arithmetic in the same answer.
        def rent(match):
            before = line[max(0, match.start()-24):match.start()]
            after = line[match.end():].lstrip()
            if _RANGE_BEFORE.search(line[:match.start()]) or _RANGE_AFTER.match(line[match.end():]):
                return match.group()
            if re.match(r'(?:bills?|utilities|electricity|gas|water|parking|transport|deposit|budget|total)\b|水电|账单|停车|网费|服务费|押金|合计|预算|开支', after, re.I):
                return match.group()
            if re.search(r'budget|ceiling|deposit|bills?|utilities|total|difference|预算|押金|差额|账单|开支|合计|水电|通勤费', before, re.I):
                return match.group()
            # An unlabelled amount followed by a word may describe a
            # separate expense. Only explicit rent can override that.
            if after and after[0] not in ',，;；|.。)]' and not re.search(r'\brent\b|月租|房租', before, re.I):
                return match.group()
            return amount + match.group(1)
        line = re.sub(r'£\s*\d[\d,]*(?:\.\d+)?(\s*(?:/month|pcm|per month|a month|/月|每月))', rent, line, count=1, flags=re.I)
        line = re.sub(r'((?:\brent(?: is| of)?|(?:月租|房租)(?:为|是)?)\s*[:：]?\s*)£\s*\d[\d,]*(?:\.\d+)?',
                      lambda m: m.group(1) + amount, line, flags=re.I)
    identifier = re.search(r'/details/(\d+)', row['url'])
    if identifier:
        line = re.sub(r'((?:\bID|房源编号|房源ID)\s*[:：#]?\s*)\d{5,}',
                      lambda m: m.group(1) + identifier.group(1), line, flags=re.I)
    return line
