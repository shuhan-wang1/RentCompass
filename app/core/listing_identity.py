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


# A rent word right before the amount, in that amount's own clause. A fare can be "priced at"
# too, so a price counts only as the listing's own.
_RENT_LEAD = re.compile(
    r'(?:\brent(?:al)?(?:\s+(?:price|figure|amount|cost))?(?:\s+(?:is|of|at|for|comes\s+to|stands\s+at))?'
    r'|\b(?:recorded|asking|listed|listing|advertised)\s+(?:price|rent)(?:\s+(?:is|of|at))?'
    r'|\blisted\s+at|\bit\s+is\s+priced\s+at'
    r'|\b(?:this|that|the)\s+(?:listing|flat|apartment|studio|property|home)\s+(?:is|costs)'
    r'|(?:月租|房租|租金)(?:为|是|约)?)\s*[:：=(（]?\s*$', re.I)
# The rent word names the user's limit, or a rent over some other period.
_OTHER_RENT = re.compile(
    r"\b(?:max(?:imum)?|budget|limit|ceiling|weeks?'?|weekly|annual|yearly|years?'?s?|total)\b"
    r'|预算|上限|押金|每周|每年|年租', re.I)
# After "rent is £N": a different period, or a difference from something else.
_OTHER_AFTER_RENT = re.compile(
    r'\s*(?:pw\b|/\s*w(?:ee)?k|per\s+week|a\s+week|weekly|/\s*y(?:ea)?r|per\s+(?:year|annum)|a\s+year|'
    r'yearly|annually|over\b|under\b|more\b|less\b|above\b|below\b|higher\b|lower\b|cheaper\b|short\b|'
    r'extra\b|每周|每年|/周|/年)', re.I)
# The words a reply puts between a listing's name and its rent: "(£N/month)", ": 1 bed flat,
# £N/month", " is the cheapest at £N/month", " flat (the first one, £N a month)". A word that is
# not here means the amount is about something the sentence went on to name, so the list stays
# free of nouns for what a home costs to run or to travel from.
_NAME_TO_RENT_WORDS = frozenset((
    'is at for a an the of and also both are was costs cost priced listed advertised asking '
    'available cheapest cheaper dearest lowest highest priciest option only one live first '
    'second third fourth fifth two three four five flat flats apartment studio house '
    'maisonette penthouse bungalow cottage duplex loft mews home property listing card whole '
    'ensuite share shared room double single terraced terrace end semi detached bed beds '
    'bedroom bedrooms minute minutes min mins mile miles away walk routed estimated london '
    'greater uk').split())
_NAME_TO_RENT_ZH = re.compile(r'[的是为一两二三四五卧居室房套]|公寓|单间|整套|月租|租金|房租|\d+')
_MARKUP = re.compile(r'https?://[^\s)]*|[*_`\[\]]')
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)?")
_POSTCODE = re.compile(r'\b[A-Z]{1,2}\d[A-Z\d]?(?:\s*\d[A-Z]{2})?\b')
# "E8 (Hackney) at", "E3 in Bow, at": the area, which the address need not spell out.
_AREA_NOTE = re.compile(r"\((?:[A-Z][\w'-]*\s?){1,3}\)|\bin\s(?:[A-Z][\w'-]*\s?){1,3}")


def _rent_is_labelled(head: str) -> bool:
    """``head`` ends in a rent word that means this listing's monthly rent."""
    lead = _RENT_LEAD.search(head)
    if not lead:
        return False
    clause = max(head.rfind(mark, 0, lead.start()) for mark in ',;.:!?，；。：！？') + 1
    return not _OTHER_RENT.search(head[clause:lead.start()])


def _follows_name(head: str, row: dict) -> bool:
    """``head`` ends in ``row``'s name, its own address and a description of the home."""
    key = row['address'].split(',')[0].strip().lower()
    at = head.lower().rfind(key)
    if at < 0 or '£' in head[at:]:
        return False
    own = {word.lower() for word in _WORD.findall(row['address'])}
    gap = _AREA_NOTE.sub(' ', _POSTCODE.sub(' ', _MARKUP.sub(' ', head[at + len(key):])))
    return all(word.lower() in own or word.lower() in _NAME_TO_RENT_WORDS
               or not _NAME_TO_RENT_ZH.sub('', word) for word in _WORD.findall(gap))


def _bind_sentence(line: str, row: dict) -> str:
    """Correct the rent and listing id stated in ``line``, which names ``row``."""
    price = re.search(r'\d[\d,]*(?:\.\d+)?', str(row.get('price') or ''))
    if price and not re.search(r'\bpw\b|/week|per week', str(row.get('price') or ''), re.I):
        amount = '£' + price.group()

        # Only a rent assertion is corrected: an amount labelled as rent, or one attached to
        # the listing's name. Any other monthly figure in a sentence naming a listing is the
        # model's own, so a fare, a Travelcard, council tax or broadband keeps its value
        # (r06 643c2132: a £119.90 Travelcard became the listing's £1800).
        def rent(match):
            head, tail = line[:match.start()], line[match.end():]
            if _RANGE_BEFORE.search(head) or _RANGE_AFTER.match(tail):
                return match.group()
            after = tail.lstrip()
            if re.match(r'(?:bills?|utilities|electricity|gas|water|parking|transport|deposit|budget|total)\b|水电|账单|停车|网费|服务费|押金|合计|预算|开支', after, re.I):
                return match.group()
            if _rent_is_labelled(head):
                return amount + match.group(1)
            # An unlabelled amount followed by a word may describe a separate expense.
            if after and after[0] not in ',，;；|.。)]）':
                return match.group()
            return amount + match.group(1) if _follows_name(head, row) else match.group()
        line = re.sub(r'£\s*\d[\d,]*(?:\.\d+)?(\s*(?:/month|pcm|per month|a month|/月|每月))', rent, line, count=1, flags=re.I)

        def stated_rent(match):
            if _OTHER_AFTER_RENT.match(line[match.end():]) or not _rent_is_labelled(line[:match.start(2)]):
                return match.group()
            return match.group(1) + amount
        line = re.sub(r'((?:\brent(?: is| of)?|(?:月租|房租)(?:为|是)?)\s*[:：]?\s*)(£\s*\d[\d,]*(?:\.\d+)?)',
                      stated_rent, line, flags=re.I)
    identifier = re.search(r'/details/(\d+)', row['url'])
    if identifier:
        line = re.sub(r'((?:\bID|房源编号|房源ID)\s*[:：#]?\s*)\d{5,}',
                      lambda m: m.group(1) + identifier.group(1), line, flags=re.I)
    return line
