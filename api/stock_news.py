"""Indian stock news for the Sector Scanner: graded headlines + NSE exchange filings.

Sources (public, no login):
  RSS   Economic Times (Markets, Stocks), LiveMint Markets, Business Standard Markets,
        NDTV Profit, Hindu BusinessLine, Moneycontrol (market reports, buzzing stocks)
  NSE   /api/corporate-announcements (every company filing, with the stock symbol) - the
        exchange record, so it is matched by SYMBOL, not by text

Grade = tier + direction (stock news is often negative, unlike the crypto catalyst grader):
  A strong   : results beat / miss, order win, USFDA / regulator approval, buyback, rating or broker
               upgrade / downgrade, regulator action (SEBI order, penalty, raid, probe), fraud / default
  B moderate : acquisition / merger, partnership / MoU, capacity / commissioning, fund raise, dividend /
               bonus / split, stake buy / sale, block deal, management exit, plant shutdown, litigation
  C weak     : reportedly / sources / likely / plans / in talks, exchange price / news verification queries
  direction  : 'pos' (good for the stock), 'neg' (bad), 'mixed', or None (neutral / unclear)
Headline matching is keyword based (company name, short name, NSE symbol) and WILL miss / mis-assign
some items - the UI always shows the headline.
"""

import csv
import io
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36'}
RSS_FEEDS = {
    'ET Markets': 'https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms',
    'ET Stocks': 'https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms',
    'LiveMint': 'https://www.livemint.com/rss/markets',
    'Business Standard': 'https://www.business-standard.com/rss/markets-106.rss',
    'NDTV Profit': 'https://feeds.feedburner.com/ndtvprofit-latest',
    'BusinessLine': 'https://www.thehindubusinessline.com/markets/stock-markets/feeder/default.rss',
    'Moneycontrol': 'https://www.moneycontrol.com/rss/marketreports.xml',
    'Moneycontrol Buzzing': 'https://www.moneycontrol.com/rss/buzzingstocks.xml',
}
NIFTY500_CSV = 'https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv'
EQUITY_CSV = 'https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv'          # all mainboard companies
SME_CSV = 'https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv'  # NSE Emerge SME companies
MAX_AGE_DAYS = 3

# Short names used in headlines that the company name / symbol would not catch
ALIASES = {
    'RELIANCE': ['RIL', 'Reliance'], 'SBIN': ['SBI', 'State Bank'], 'LT': ['L&T', 'Larsen'],
    'M&M': ['M&M', 'Mahindra & Mahindra'], 'HDFCBANK': ['HDFC Bank'], 'ICICIBANK': ['ICICI Bank'],
    'INFY': ['Infosys'], 'BHARTIARTL': ['Airtel', 'Bharti Airtel'], 'HINDUNILVR': ['HUL', 'Hindustan Unilever'],
    'BAJFINANCE': ['Bajaj Finance'], 'BAJAJFINSV': ['Bajaj Finserv'], 'MARUTI': ['Maruti'],
    'SUNPHARMA': ['Sun Pharma'], 'KOTAKBANK': ['Kotak Mahindra Bank', 'Kotak Bank'], 'AXISBANK': ['Axis Bank'],
    'CONCOR': ['Container Corp', 'Concor'], 'DRREDDY': ["Dr Reddy", "Dr. Reddy"], 'DIVISLAB': ["Divi's"],
    'HCLTECH': ['HCLTech', 'HCL Tech'], 'TECHM': ['Tech Mahindra'], 'ULTRACEMCO': ['UltraTech'],
    'NESTLEIND': ['Nestle India'], 'HEROMOTOCO': ['Hero MotoCorp'], 'EICHERMOT': ['Eicher', 'Royal Enfield'],
    'POWERGRID': ['Power Grid'], 'COALINDIA': ['Coal India'], 'ADANIENT': ['Adani Enterprises'],
    'ADANIPORTS': ['Adani Ports'], 'BEL': ['Bharat Electronics'], 'HAL': ['Hindustan Aeronautics'],
    'NATIONALUM': ['NALCO', 'National Aluminium'], 'IDEA': ['Vodafone Idea', 'Vi'], 'ZOMATO': ['Zomato', 'Eternal'],
    'ETERNAL': ['Eternal', 'Zomato'], 'PAYTM': ['Paytm'], 'NYKAA': ['Nykaa'], 'POLICYBZR': ['PolicyBazaar'],
    'TATASTEEL': ['Tata Steel'], 'TMPV': ['Tata Motors'], 'TMCV': ['Tata Motors'], 'BANKBARODA': ['Bank of Baroda', 'BoB'],
    'PNB': ['PNB', 'Punjab National Bank'], 'IRFC': ['IRFC', 'Indian Railway Finance'], 'RVNL': ['RVNL', 'Rail Vikas'],
    'IREDA': ['IREDA'], 'HUDCO': ['HUDCO'], 'BHEL': ['BHEL'], 'NHPC': ['NHPC'], 'SJVN': ['SJVN'], 'IRCON': ['Ircon'], 'IRCTC': ['IRCTC'], 'LICI': ['LIC'], 'ASIANPAINT': ['Asian Paints'],
}
# Symbols that are also everyday words / index names in headlines -> never matched by symbol alone
SYMBOL_STOPWORDS = {'BSE', 'NSE', 'IT', 'ALL', 'NAM', 'CDSL', 'MCX', 'OIL', 'IDEA', 'GOLD', 'POWER', 'BANK', 'INDIA',
                    'TRENT', 'HAPPY', 'SAIL', 'CAMS', 'ACE', 'PVR', 'VIP', 'ICE', 'DATA', 'ONE', 'LIC'}
_SUFFIX = re.compile(r"\s+(Ltd\.?|Limited|Ltd)$", re.I)

# NSE filing categories that are routine paperwork (not news)
FILING_NOISE = {
    'Analysts/Institutional Investor Meet/Con. Call Updates', 'Copy of Newspaper Publication', 'Investor Presentation',
    'Shareholders meeting', 'Monitoring Agency Report', 'Record Date', 'ESOP/ESOS/ESPS', 'Trading Window',
    'Address Change', 'Statement of deviation(s) or variation(s) under Reg. 32', 'Integrated Filing- Financial',
    'Certificate under SEBI (Depositories and Participants) Regulations, 2018', 'Loss of Share Certificates',
    'Change in Registrar and Share Transfer Agent', 'Closure of Trading Window', 'Duplicate Share Certificate',
}
FILING_CATEGORY = {     # category -> (tier, direction, label) when the text gives nothing stronger
    'Bagging/Receiving of orders/contracts': ('A', 'pos', 'order win'),
    'Awarding of order(s)/contract(s)': ('A', 'pos', 'order win'),
    'Buyback': ('A', 'pos', 'buyback'), 'Acquisition': ('B', 'pos', 'acquisition'),
    'Amalgamation/Merger': ('B', None, 'merger'), 'Agreements': ('B', 'pos', 'agreement'),
    'Capacity addition': ('B', 'pos', 'capacity'), 'Commencement of commercial production/operations': ('B', 'pos', 'capacity'),
    'Dividend': ('B', 'pos', 'dividend'), 'Bonus': ('B', 'pos', 'bonus'), 'Sub-division/Stock Split': ('B', 'pos', 'split'),
    'Resignation of Director/KMP/SMP': ('B', 'neg', 'management exit'), 'Resignation': ('B', 'neg', 'management exit'),
    'Cessation': ('C', None, 'management change'), 'Offer for sale': ('B', 'neg', 'stake sale'),
    'Pendency of Litigation(s)/dispute(s) or the outcome impacting the Company': ('B', 'neg', 'litigation'),
    'Action(s) taken or orders passed': ('B', 'neg', 'regulator action'),
    'Corporate Insolvency Resolution Process': ('A', 'neg', 'insolvency'),
    'Disruption of Operations': ('B', 'neg', 'disruption'), 'Disclosure of material issue': ('B', 'neg', 'material issue'),
    'News Verification': ('C', None, 'exchange query'), 'Rumour Verification - Regulation 30(11)': ('C', None, 'exchange query'),
    'Price movement': ('C', None, 'exchange query'), 'Credit Rating': ('C', None, 'credit rating'),
    'Financial Result Updates': ('B', None, 'results'), 'Outcome of Board Meeting': ('C', None, 'board meeting'),
    'Change in Management': ('C', None, 'management change'), 'Appointment': ('C', None, 'appointment'),
    'Allotment of Securities': ('C', None, 'allotment'), 'Monthly Business Updates': ('C', None, 'business update'),
}

_I = re.I
RULES = [  # (tier, direction, label, regex) - all matches are collected
    ('A', 'pos', 'results beat', re.compile(r"\b(profit|PAT|net income|earnings|revenue|sales|EBITDA|income)\b[^.]{0,45}?\b(jumps?|surges?|soars?|rises?|climbs?|grows?|zooms?|doubles?|triples?|beats?|rose|jumped|surged|grew|up \d)", _I)),
    ('A', 'pos', 'results beat', re.compile(r"\b(record|best[- ]ever|highest[- ]ever|strong)\b[^.]{0,30}\b(profit|revenue|results|earnings|quarter|Q[1-4])", _I)),
    ('A', 'neg', 'results miss', re.compile(r"\b(profit|PAT|net income|earnings|revenue|sales|EBITDA|margins?|income)\b[^.]{0,45}?\b(falls?|fell|declines?|declined|drops?|dropped|slumps?|plunges?|dips?|tumbles?|shrinks?|misses?|down \d|contracts?)", _I)),
    ('A', 'neg', 'results miss', re.compile(r"\b(net loss|loss widens|swings? to (a )?loss|weak (Q[1-4]|quarter|results))\b", _I)),
    ('A', 'pos', 'order win', re.compile(r"\b(bags?|bagged|wins?|won|secures?|secured|receives?|received|bagging|lands?|bagged)\b[^.]{0,45}\b(orders?|contracts?|LoA|letter of award)", _I)),
    ('A', 'pos', 'approval', re.compile(r"\b(USFDA|US FDA|FDA|DCGI|CDSCO|EMA)\b[^.]{0,35}\b(approv\w*|nod|clearance|EIR|tentative approval)", _I)),
    ('A', 'pos', 'approval', re.compile(r"\b(approv\w*|nod|go-ahead)\b[^.]{0,25}\b(USFDA|US FDA|FDA|DCGI)", _I)),
    ('A', 'neg', 'regulator', re.compile(r"\b(warning letter|import alert|OAI|Form 483|observations?)\b", _I)),
    ('A', 'neg', 'regulator', re.compile(r"\b(SEBI|ED|CBI|RBI|income[- ]tax|I-T|DGGI|CCI)\b[^.]{0,40}\b(order|penalty|penali[sz]\w*|fine[sd]?|raid\w*|search\w*|probe\w*|ban\w*|bars?|show[- ]cause|notice)", _I)),
    ('A', 'neg', 'fraud/default', re.compile(r"\b(fraud|default\w*|insolvency|CIRP|NCLT admits|forensic audit|accounting lapse)\b", _I)),
    ('A', 'pos', 'buyback', re.compile(r"\bbuy-?backs?\b", _I)),
    ('A', 'pos', 'upgrade', re.compile(r"\b(upgrades?|upgraded|raises? (its )?target|target (price )?(raised|hiked)|initiates? (coverage )?with (a )?buy|top pick|re-?rat\w+ upward)", _I)),
    ('A', 'neg', 'downgrade', re.compile(r"\b(downgrades?|downgraded|cuts? (its )?target|target (price )?(cut|lowered|slashed)|sell rating|underweight|reduce rating)\b", _I)),
    ('B', 'pos', 'acquisition', re.compile(r"\b(acquires?|acquired|acquisition|takeover|to buy .{0,25}stake|buys? .{0,20}stake)\b", _I)),
    ('B', 'pos', 'partnership', re.compile(r"\b(partner\w*|MoU|tie-?up|ties up|joint venture|JV|collaborat\w+|strategic alliance)\b", _I)),
    ('B', 'pos', 'capacity', re.compile(r"\b(new plant|capacity (addition|expansion)|commission\w*|commercial production|capex|expansion|inaugurat\w+)\b", _I)),
    ('B', None, 'fund raise', re.compile(r"\b(QIP|fund ?rais\w*|raises? Rs|raise Rs|preferential (issue|allotment)|rights issue|NCDs?|IPO-bound)\b", _I)),
    ('B', 'pos', 'dividend/bonus', re.compile(r"\b(dividend|bonus (issue|shares)|stock split|sub-?division)\b", _I)),
    ('B', 'neg', 'stake sale', re.compile(r"\b(stake sale|sells? .{0,20}stake|offloads?|trims? stake|OFS|offer for sale|promoters? (sell|pledge)\w*|pledge\w*|block deal)\b", _I)),
    ('B', 'pos', 'stake buy', re.compile(r"\b(raises? stake|ups stake|picks? up .{0,20}stake|increases? stake|bulk deal.{0,20}\bbuys?)\b", _I)),
    ('B', 'neg', 'management exit', re.compile(r"\b(resign\w*|steps? down|quits?|exits? as)\b", _I)),
    ('B', 'neg', 'disruption', re.compile(r"\b(fire at|blaze|shutdown|shuts? down|suspends? (operations|production)|strike at|plant closure|recall\w*)\b", _I)),
    ('B', 'pos', 'launch', re.compile(r"\b(launch\w*|unveils?|rolls? out|forays? into|enters? .{0,15}market)\b", _I)),
]
RUMOR = re.compile(r"\b(reportedly|sources said|sources say|people familiar|likely to|plans? to|planning to|considering|"
                   r"in talks|mulls?|weighs?|exploring|said to be)\b", _I)
ROUNDUP = re.compile(r"\b(stocks? (in|to) (news|watch|buy|sell)|stocks? that|gainers and losers|top (gainers|losers)|"
                     r"buzzing stocks|stock market (live|today)|market live|sensex today|stocks to buy)\b", _I)

_cache = {}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        logger.warning('stock_news %s failed: %s', key, e)
        if hit:
            return hit[1]
        raise
    _cache[key] = (time.time(), val)
    return val


# --------------------------------------------------------------------------- grading

def grade(text, filing_category=None):
    """-> {'tier': 'A'|'B'|'C'|None, 'direction': 'pos'|'neg'|'mixed'|None, 'category': str|None}."""
    hits = [(t, d, lbl) for t, d, lbl, rx in RULES if rx.search(text)]
    if not hits and filing_category in FILING_CATEGORY:
        t, d, lbl = FILING_CATEGORY[filing_category]
        hits = [(t, d, lbl)]
    elif filing_category in FILING_CATEGORY and FILING_CATEGORY[filing_category][0] < min(h[0] for h in hits):
        hits.append(FILING_CATEGORY[filing_category])
    if not hits:
        return {'tier': None, 'direction': None, 'category': None}
    tier = min(h[0] for h in hits)                       # 'A' < 'B' < 'C'
    top = [h for h in hits if h[0] == tier]
    dirs = {h[1] for h in top if h[1]}
    direction = 'mixed' if dirs == {'pos', 'neg'} else (dirs.pop() if dirs else None)
    if RUMOR.search(text) and tier != 'C' and not filing_category:
        tier = 'C'                                        # unconfirmed: keep the direction, downgrade the tier
    return {'tier': tier, 'direction': direction, 'category': ' / '.join(dict.fromkeys(h[2] for h in top))}


# --------------------------------------------------------------------------- universe + matching

def _csv(url):
    r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text.lstrip('\ufeff'))))


def _clean_name(name):
    return re.sub(r"\s+", " ", _SUFFIX.sub('', (name or '').strip()).rstrip('.')).strip()


def _universe():
    """{symbol: {'name', 'industry', 'tier'}} for every NSE-listed company (cached 1 day).
    tier 'n500' = Nifty 500 (name, short name and symbol matching), 'main' = other mainboard,
    'sme' = NSE Emerge SME (full multi-word name only)."""
    uni = {}
    for row in _csv(NIFTY500_CSV):
        if row.get('Symbol'):
            uni[row['Symbol'].strip()] = {'name': _clean_name(row['Company Name']), 'industry': row.get('Industry'),
                                          'tier': 'n500'}
    for url, tier in ((EQUITY_CSV, 'main'), (SME_CSV, 'sme')):
        try:
            for row in _csv(url):
                row = {k.strip(): (v or '').strip() for k, v in row.items() if k}
                sym = row.get('SYMBOL')
                if sym and sym not in uni:
                    uni[sym] = {'name': _clean_name(row.get('NAME OF COMPANY') or row.get('NAME_OF_COMPANY')),
                                'industry': None, 'tier': tier}
        except Exception as e:
            logger.warning('stock_news universe %s failed: %s', tier, e)
    return uni


_NAME_STOP = {'of', 'and', 'the', '&', 'india', 'indian', 'industries', 'bank', 'finance', 'financial', 'services',
              'limited', 'ltd', 'company', 'corporation', 'international', 'global', 'new', 'national', 'general',
              'technologies', 'enterprises', 'holdings', 'investments', 'capital', 'power', 'energy', 'infra'}


def _matcher(extra=None):
    """One combined case-insensitive name regex + one case-sensitive symbol regex for all companies."""
    uni = {k: dict(v) for k, v in _cached('universe', 86400, _universe).items()}
    for sym, name in (extra or {}).items():
        if sym not in uni:
            uni[sym] = {'name': _clean_name((name or sym).title()), 'industry': None, 'tier': 'n500'}
    first_two = {}
    for sym, u in uni.items():
        w = u['name'].split()
        if len(w) >= 3:
            first_two.setdefault(' '.join(w[:2]).lower(), set()).add(sym)
    name_to_sym = {}
    def add(alias, sym):
        a = alias.strip().lower()
        if a and a not in name_to_sym:
            name_to_sym[a] = sym
    for sym, u in sorted(uni.items(), key=lambda kv: kv[1]['tier'] != 'n500'):   # Nifty 500 wins name clashes
        for a in ALIASES.get(sym, []):
            add(a, sym)
        name, words = u['name'], u['name'].split()
        if u['tier'] == 'n500':
            if len(name) >= 5:
                add(name, sym)
            if len(words) >= 3:
                two = ' '.join(words[:2])
                if (len(first_two.get(two.lower(), ())) == 1 and words[1].lower() not in _NAME_STOP
                        and words[0].lower() not in _NAME_STOP and len(two) >= 8):
                    add(two, sym)
        elif len(words) >= 2 and len(name) >= 10 and not all(x.lower() in _NAME_STOP for x in words[1:]):
            add(name, sym)                                   # smaller companies: full multi-word name only
    names = sorted(name_to_sym, key=len, reverse=True)
    rx_name = re.compile(r"(?<![A-Za-z0-9])(%s)(?![A-Za-z0-9])" % '|'.join(re.escape(n) for n in names), re.I)
    syms = sorted((s for s, u in uni.items() if u['tier'] == 'n500' and len(s) >= 3 and s.isupper()
                   and s not in SYMBOL_STOPWORDS), key=len, reverse=True)
    rx_sym = re.compile(r"(?<![A-Za-z0-9&-])(%s)(?![A-Za-z0-9&-])" % '|'.join(re.escape(x) for x in syms))
    return {'rx_name': rx_name, 'rx_sym': rx_sym, 'name_to_sym': name_to_sym, 'uni': uni}


def match_stocks(text, m, max_hits=6):
    out = []
    for hit in m['rx_name'].finditer(text):
        sym = m['name_to_sym'].get(hit.group(1).lower())
        if sym and sym not in out:
            out.append(sym)
    for hit in m['rx_sym'].finditer(text):
        if hit.group(1) not in out:
            out.append(hit.group(1))
    return out[:max_hits + 1]


# --------------------------------------------------------------------------- sources

def _raw_rss():
    from api.catalyst_feeds import fetch_rss
    items, errors = [], []
    for name, url in RSS_FEEDS.items():
        try:
            items += fetch_rss(name, url)
        except Exception as e:
            errors.append(f'{name}: {e}')
    return items, errors


def _headlines(extra_key=()):
    items, errors = _cached('rss', 300, _raw_rss)
    pats = _matcher(dict(extra_key))
    now = time.time()
    seen, out = set(), []
    for it in sorted(items, key=lambda x: -x['ts']):
        key = re.sub(r'\W+', '', it['title'].lower())[:80]
        if key in seen or now - it['ts'] > MAX_AGE_DAYS * 86400 or it['ts'] > now + 600:
            continue
        seen.add(key)
        roundup = bool(ROUNDUP.search(it['title']))
        syms = match_stocks(it['title'], pats) or ([] if roundup else match_stocks(it['title'] + '. ' + it['summary'][:200], pats, 3))
        g = grade(it['title'] + '. ' + it['summary'][:200])
        if roundup or len(syms) > 3:
            g = {'tier': None, 'direction': None, 'category': 'round-up'}
        out.append({'kind': 'news', 'title': it['title'], 'summary': it['summary'][:280], 'link': it['link'],
                    'source': it['source'], 'ts': int(it['ts']), 'symbols': syms[:6], **g})
    return {'items': out, 'errors': errors}


def _nse_ts(s):
    try:
        return int(datetime.strptime(s, '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST).timestamp())
    except (TypeError, ValueError):
        return None


def _filings():
    from api.sector_scanner import _nse_get
    today = datetime.now(IST).date()
    rows = _nse_get('/api/corporate-announcements', {
        'index': 'equities', 'from_date': (today - timedelta(days=MAX_AGE_DAYS - 1)).strftime('%d-%m-%Y'),
        'to_date': today.strftime('%d-%m-%Y')})
    out = []
    for r in rows if isinstance(rows, list) else []:
        cat = (r.get('desc') or '').strip()
        if cat in FILING_NOISE:
            continue
        text = (r.get('attchmntText') or '').strip()
        ts = _nse_ts(r.get('an_dt') or r.get('exchdisstime'))
        if not ts or not r.get('symbol'):
            continue
        g = grade(f"{cat}. {text}", filing_category=cat)
        out.append({'kind': 'filing', 'title': f"{r.get('sm_name') or r['symbol']}: {cat}", 'summary': text[:400],
                    'link': r.get('attchmntFile'), 'source': 'NSE filing', 'ts': ts, 'symbols': [r['symbol']],
                    'filing_category': cat, 'industry': r.get('smIndustry'), **g})
    out.sort(key=lambda x: -x['ts'])
    return out


# --------------------------------------------------------------------------- public API

def news_index(extra=None):
    """All recent items (headlines + filings), newest first. `extra` = {symbol: company name} to match too."""
    key = tuple(sorted((extra or {}).items()))
    heads = _cached(('heads', key), 300, lambda: _headlines(key))
    try:
        fil = _cached('filings', 180, _filings)
        ferr = []
    except Exception as e:
        fil, ferr = [], [f'NSE filings: {e}']
    items = sorted(heads['items'] + fil, key=lambda x: -x['ts'])
    return {'items': items, 'errors': heads['errors'] + ferr}


def summarize(items):
    graded = [x for x in items if x.get('tier')]
    tiers = sorted(x['tier'] for x in graded)
    pos = sum(x['direction'] == 'pos' for x in graded)
    neg = sum(x['direction'] == 'neg' for x in graded)
    return {'news_count': sum(x['kind'] == 'news' for x in items), 'filings_count': sum(x['kind'] == 'filing' for x in items),
            'news_tier': tiers[0] if tiers else None,
            'news_direction': 'pos' if pos > neg else 'neg' if neg > pos else ('mixed' if pos else None),
            'news_pos': pos, 'news_neg': neg}


def by_symbol(symbols, extra=None):
    items = news_index(extra)['items']
    out = {}
    for s in symbols:
        mine = [x for x in items if s in x['symbols']]
        out[s] = dict(summarize(mine), items=sort_items(mine)[:25])
    return out


TIER_WEIGHT = {'A': 3, 'B': 2, 'C': 1}


def impact(x):
    """Higher = more likely to move the stock: tier A 3 / B 2 / C 1, +0.5 when it clearly points up or down;
    items that name no NSE company get 20% of that."""
    w = TIER_WEIGHT.get(x.get('tier'), 0)
    score = w + (0.5 if w and x.get('direction') in ('pos', 'neg', 'mixed') else 0)
    return score if x.get('symbols') else score * 0.2      # market-wide / foreign news ranks below company news


def sort_items(items, sort='impact'):
    if sort == 'latest':
        return sorted(items, key=lambda x: -x['ts'])
    return sorted(items, key=lambda x: (-impact(x), -x['ts']))


def companies(items, limit=60):
    """Companies in the news, ranked by their strongest item, then # of A/B items, then latest."""
    try:
        uni = _cached('universe', 86400, _universe)
    except Exception:
        uni = {}
    agg = {}
    for x in items:
        for sym in x.get('symbols') or []:
            a = agg.setdefault(sym, {'symbol': sym, 'items': []})
            a['items'].append(x)
    out = []
    for sym, a in agg.items():
        its = sort_items(a['items'])
        top = its[0]
        summ = summarize(its)
        u = uni.get(sym) or {}
        industry = u.get('industry') or next((x.get('industry') for x in its if x.get('industry')), None)
        out.append(dict(summ, symbol=sym, name=u.get('name') or top['title'].split(':')[0], industry=industry,
                        listing=u.get('tier'), impact=impact(top), strong=sum(1 for x in its if x.get('tier') in ('A', 'B')),
                        latest_ts=max(x['ts'] for x in its),
                        top={k: top.get(k) for k in ('title', 'link', 'source', 'ts', 'tier', 'direction', 'category', 'kind')}))
    out.sort(key=lambda c: (-c['impact'], -c['strong'], -c['latest_ts']))
    return out[:limit]


def get_board(limit=150, sort='impact'):
    d = news_index()
    try:
        n = len(_cached('universe', 86400, _universe))
    except Exception:
        n = None
    return {'items': sort_items(d['items'], sort)[:limit], 'companies': companies(d['items']), 'sort': sort,
            'errors': d['errors'], 'timestamp': datetime.now(IST).isoformat(), 'universe_size': n,
            'sources': list(RSS_FEEDS) + ['NSE filings']}
