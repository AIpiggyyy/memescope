"""memescope daily analysis (runs on Alex's PC, stdlib only, no API calls, no AI cost).

Turns the raw files the collector saved into small tables that the daily AI review reads:
  analysis/SUMMARY.md          human-readable snapshot (latest day, rolling totals, pre-registered test status)
  analysis/daily_metrics.csv   one row per (chain, launch day): insider/bundle/coordination/profit metrics
  analysis/tokens_sol.csv      one row per Solana token     analysis/tokens_rh.csv  one row per Robinhood token
  analysis/wallets_sol.csv.gz  one row per Solana wallet (realised SOL P&L, flags, by launch week)
  analysis/test_status.json    pre-registered persistence test (README decision rule), computed only once >=28 days exist
  analysis/cache/<chain>/<token>.json   per-token parse cache (raw files are parsed once; reruns are incremental)

Run:  python analyze.py            (collect.py also calls it once a day after 00:10 UTC)
Definitions (fixed in advance, see claude/research-memecoin-profit-forensics-2026-10.md and memescope README):
  - trade (SOL): a successful tx whose fee payer's token balance in the mint changes; d_sol = payer lamport change + payer WSOL change.
  - creation slot: slot of the token's first recorded tx. "bundled launch": >=2 non-creator wallets buy in the creation slot.
  - sniper: non-creator wallet whose first buy is within 2 slots of creation.
  - coordinated dump: >=3 distinct wallets that were among the first 20 buyers sell within the same 5-second window.
  - wash/volume-bot wallet: >=10 trades in the token, both buys and sells, |net tokens| <=1% of tokens bought, |net SOL| <= 2% of its SOL volume.
  - creator-linked wallet (traced tokens only): funded by the creator, or shares a non-hub funder within 3 hops with the creator.
  - insider (for the persistence test): creator, creation-slot buyer, creator-linked, coordinated-dump member or wash wallet in ANY token.
  - realised P&L: sum of d_sol for wallets whose token position is closed (sold >=99% of tokens bought); open positions are excluded.
"""
import csv, datetime as dt, glob, gzip, io, json, math, os, random, sqlite3, sys, time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
OUT = os.path.join(ROOT, 'analysis')
WSOL = 'So11111111111111111111111111111111111111112'
LAMPORTS = 1e9
VERSION = 3   # bump to force a full re-parse when parsing logic changes


def log(msg):
    os.makedirs(OUT, exist_ok=True)
    line = f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S}Z {msg}"
    print(line)
    with open(os.path.join(OUT, 'analyze.log'), 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def read_jsonl_gz(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        out = []
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    break          # file still being written; take what is complete
        return out


def db_ro(path):
    if not os.path.exists(path):
        return None
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30)


def load_wallet_graph(dbpath):
    """wallet -> (funder, hub) from the collector's trace cache."""
    g = {}
    con = db_ro(dbpath)
    if not con:
        return g
    try:
        for w, info in con.execute('SELECT w, info FROM wallets'):
            try:
                i = json.loads(info)
            except Exception:
                continue
            g[w.lower() if w.startswith('0x') else w] = ((i.get('funder') or '').lower() if (i.get('funder') or '').startswith('0x') else i.get('funder'), bool(i.get('hub')))
    finally:
        con.close()
    return g


def funder_chain(g, w, hops=3):
    out, node = [], w
    for _ in range(hops):
        f = g.get(node)
        if not f or not f[0]:
            break
        fund, _ = f
        hub = g.get(fund, (None, False))[1]
        out.append((fund, hub))
        if hub:
            break
        node = fund
    return out


def linked_to_creator(g, w, creator):
    """True if creator funded w (within 3 hops) or they share a non-hub funder within 3 hops."""
    if not creator or w == creator:
        return False
    cw = funder_chain(g, w)
    if any(f == creator for f, _ in cw):
        return True
    cc = {f for f, hub in funder_chain(g, creator) if not hub}
    if any(f == w for f in cc):
        return True
    return any((not hub) and f in cc for f, hub in cw)


# ----------------------------------------------------------------------------------------------- Solana
def sol_trades(txs, mint):
    """Per (tx, trader) token and SOL deltas. The trader is any non-pool owner whose balance in `mint` changed (not only
    the fee payer: relayed/aggregator trades have a different payer). Pool/curve owners are the owners present in >=20% of
    the token's txs. Returns (trades, n_touch, n_zero_net) where zero-net txs touch the mint but change no balance."""
    rows, freq, n_touch = [], {}, 0
    for t in txs:
        meta = t.get('meta') or {}
        if meta.get('err'):
            continue
        pre = {}
        post = {}
        for b in meta.get('preTokenBalances') or []:
            if b.get('mint') in (mint, WSOL):
                k = (b.get('owner'), b.get('mint')); pre[k] = pre.get(k, 0.0) + float((b.get('uiTokenAmount') or {}).get('uiAmount') or 0)
        for b in meta.get('postTokenBalances') or []:
            if b.get('mint') in (mint, WSOL):
                k = (b.get('owner'), b.get('mint')); post[k] = post.get(k, 0.0) + float((b.get('uiTokenAmount') or {}).get('uiAmount') or 0)
        owners = {o for (o, m) in set(pre) | set(post) if m == mint}
        if not owners:
            continue
        n_touch += 1
        for o in owners:
            freq[o] = freq.get(o, 0) + 1
        rows.append((t, pre, post, owners))
    pool = {o for o, c in freq.items() if c >= max(5, 0.2 * n_touch)}
    out, zero_net = [], 0
    for t, pre, post, owners in rows:
        meta = t['meta']
        ks = ((t.get('transaction') or {}).get('message') or {}).get('accountKeys') or []
        keys = [k['pubkey'] if isinstance(k, dict) else k for k in ks]
        pb, qb = meta.get('preBalances') or [], meta.get('postBalances') or []
        moved = False
        for o in owners:
            d_tok = post.get((o, mint), 0.0) - pre.get((o, mint), 0.0)
            if abs(d_tok) < 1e-9:
                continue
            moved = True
            if o in pool:
                continue
            lam = 0.0
            if o in keys:
                i = keys.index(o)
                if i < len(pb) and i < len(qb):
                    lam = (qb[i] - pb[i]) / LAMPORTS
            d_sol = lam + post.get((o, WSOL), 0.0) - pre.get((o, WSOL), 0.0)
            out.append((t.get('slot') or 0, t.get('blockTime') or 0, (t.get('transactionIndex') or 0), o, d_tok, d_sol))
        if not moved:
            zero_net += 1
    out.sort(key=lambda x: (x[0], x[2]))
    return out, n_touch, zero_net


def parse_token_common(tr, creator, created, max_buy_rank=20):
    """tr: list of (slot_or_block, ts, idx, wallet, d_tok, d_quote). Returns token summary + per-wallet rows."""
    if not tr:
        return None
    s0 = tr[0][0]
    W = {}
    first_buyers = []
    for slot, ts, _, w, dtok, dq in tr:
        r = W.setdefault(w, {'bought': 0.0, 'sold': 0.0, 'q_in': 0.0, 'q_out': 0.0, 'n_buy': 0, 'n_sell': 0, 'first_buy_slot': None, 'first_ts': ts})
        if dtok > 0:
            r['bought'] += dtok; r['n_buy'] += 1; r['q_out'] += max(0.0, -dq)
            if r['first_buy_slot'] is None:
                r['first_buy_slot'] = slot
                if len(first_buyers) < max_buy_rank:
                    first_buyers.append(w)
        else:
            r['sold'] += -dtok; r['n_sell'] += 1; r['q_in'] += max(0.0, dq)
        r['net_q'] = r.get('net_q', 0.0) + dq
    creation_slot_buyers = [w for w, r in W.items() if w != creator and r['first_buy_slot'] == s0]
    snipers = [w for w, r in W.items() if w != creator and r['first_buy_slot'] is not None and r['first_buy_slot'] - s0 <= 2]
    # coordinated dump: >=3 of the first 20 buyers selling in the same 5-second window
    fb = set(first_buyers) - {creator}
    sells = sorted((ts, w) for slot, ts, _, w, dtok, dq in tr if dtok < 0 and w in fb)
    coord, j, coord_members = 0, 0, set()
    for i in range(len(sells)):
        while sells[i][0] - sells[j][0] > 5:
            j += 1
        ws = {w for _, w in sells[j:i + 1]}
        if len(ws) >= 3:
            coord = max(coord, len(ws)); coord_members |= ws
    wallets = []
    for w, r in W.items():
        vol = r['q_in'] + r['q_out']
        closed = r['bought'] > 0 and r['sold'] >= 0.99 * r['bought']
        wash = (r['n_buy'] + r['n_sell'] >= 10 and r['n_buy'] > 0 and r['n_sell'] > 0 and r['bought'] > 0
                and abs(r['bought'] - r['sold']) <= 0.01 * r['bought'] and vol > 0 and abs(r['net_q']) <= 0.02 * vol)
        role = 'creator' if w == creator else ('bundle' if w in creation_slot_buyers else ('sniper' if w in snipers else 'other'))
        wallets.append({'w': w, 'role': role, 'closed': closed, 'net_q': round(r['net_q'], 9), 'q_out': round(r['q_out'], 9),
                        'q_in': round(r['q_in'], 9), 'n_buy': r['n_buy'], 'n_sell': r['n_sell'],
                        'buy_delay': None if r['first_buy_slot'] is None else r['first_buy_slot'] - s0,
                        'wash': wash, 'coord': w in coord_members})
    cr = W.get(creator, {})
    tok = {'created': created, 'n_trades': len(tr), 'n_wallets': len(W), 'buy_volume': round(sum(r['q_out'] for r in W.values()), 6),
           'bundle_n': len(creation_slot_buyers), 'sniper_n': len(snipers), 'coord_max': coord,
           'creator_bought': round(cr.get('bought', 0.0), 3), 'creator_net_q': round(cr.get('net_q', 0.0), 6) if cr else 0.0,
           'creator_sold_all': bool(cr and cr.get('bought', 0) > 0 and cr.get('sold', 0) >= 0.99 * cr.get('bought', 0)),
           'wash_n': sum(1 for x in wallets if x['wash']), 'first_ts': tr[0][1], 'last_ts': tr[-1][1]}
    return tok, wallets


def parse_sol_token(path, superseded):
    rows = read_jsonl_gz(path)
    if not rows or rows[0].get('kind') != 'meta':
        return None
    m = rows[0]
    mint = m['mint']
    if mint in superseded:
        return None
    tr, n_touch, zero_net = sol_trades([r['tx'] for r in rows if r.get('kind') == 'tx'], mint)
    res = parse_token_common(tr, m.get('creator'), m.get('created'))
    if not res:
        return None
    tok, wallets = res
    tok.update({'n_touch_tx': n_touch, 'zero_net_tx': zero_net, 'pct_zero_net_tx': round(100.0 * zero_net / n_touch, 1) if n_touch else None})
    tok.update({'chain': 'sol', 'token': mint, 'creator': m.get('creator'), 'truncated': bool(m.get('truncated')),
                'day': path.replace('\\', '/').split('/')[-2]})
    return tok, wallets


# ----------------------------------------------------------------------------------------------- Robinhood (approximate)
def parse_rh_token(path):
    """Robinhood/Pons: token flows are exact (ERC-20 transfers); ETH flows are approximate (curve payouts often go via a
    router and internal records carry no tx hash), so RH P&L is NOT used for the persistence test."""
    rows = read_jsonl_gz(path)
    if not rows or rows[0].get('kind') != 'meta':
        return None
    m = rows[0]
    curve = (m.get('curve') or '').lower()
    creator = (m.get('deployer') or '').lower()
    zero = '0x0000000000000000000000000000000000000000'
    eth_out = {}            # curve -> wallet ETH payouts (sells), direct only
    for r in rows:
        if r.get('kind') == 'curve_internal':
            i = r['r']
            if (i.get('from') or '').lower() == curve and str(i.get('isError', '0')) == '0':
                eth_out[(i.get('to') or '').lower(), i.get('timeStamp')] = eth_out.get(((i.get('to') or '').lower(), i.get('timeStamp')), 0) + int(i.get('value') or 0) / 1e18
    buy_cost = {}           # tx hash -> ETH value from fetched tx details (graduated / uncovered txs only)
    for r in rows:
        if r.get('kind') == 'detail':
            tx = (r.get('r') or {}).get('tx') or {}
            try:
                buy_cost[(r['r'].get('hash') or '').lower()] = int(tx.get('value') or 0) / 1e18
            except Exception:
                pass
    tr = []
    for r in rows:
        if r.get('kind') != 'transfer':
            continue
        t = r['r']
        f, to = (t.get('from') or '').lower(), (t.get('to') or '').lower()
        v = int(t.get('value') or 0) / 1e18
        blk, ts = int(t.get('blockNumber') or 0), int(t.get('timeStamp') or 0)
        if f == curve and to not in (zero, curve):          # buy from curve
            tr.append((blk, ts, int(t.get('transactionIndex') or 0), to, v, -buy_cost.get((t.get('hash') or '').lower(), 0.0)))
        elif to == curve and f not in (zero, curve):        # sell into curve
            tr.append((blk, ts, int(t.get('transactionIndex') or 0), f, -v, eth_out.get((f, t.get('timeStamp')), 0.0)))
    tr.sort(key=lambda x: (x[0], x[2]))
    res = parse_token_common(tr, creator, m.get('created'))
    if not res:
        return None
    tok, wallets = res
    tok.update({'chain': 'rh', 'token': m['token'], 'creator': creator, 'truncated': bool(m.get('truncated')),
                'day': path.replace('\\', '/').split('/')[-2], 'pnl_quality': 'approx'})
    return tok, wallets


# ----------------------------------------------------------------------------------------------- driver
def traced_tokens(chain):
    out = {}
    for p in glob.glob(os.path.join(DATA, f'raw_{chain}_trace', '*', '*.jsonl.gz')):
        tok = os.path.basename(p)[:-len('.jsonl.gz')]
        out[tok.lower() if tok.startswith('0x') else tok] = p
    return out


def load_or_parse(chain, path, superseded):
    tok = os.path.basename(path)[:-len('.jsonl.gz')]
    cp = os.path.join(OUT, 'cache', chain, tok + '.json')
    mt = os.path.getmtime(path)
    if os.path.exists(cp):
        try:
            c = json.load(open(cp, encoding='utf-8'))
            if c.get('mtime') == mt and c.get('v') == VERSION:
                return c.get('tok'), c.get('wallets')
        except Exception:
            pass
    try:
        res = parse_sol_token(path, superseded) if chain == 'sol' else parse_rh_token(path)
    except Exception as e:  # noqa: BLE001
        log(f'parse error {chain} {tok}: {e}')
        res = None
    tok_s, wallets = res if res else (None, None)
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    tmp = cp + '.tmp'
    json.dump({'v': VERSION, 'mtime': mt, 'tok': tok_s, 'wallets': wallets}, open(tmp, 'w', encoding='utf-8'))
    os.replace(tmp, cp)
    return tok_s, wallets


def median(a):
    a = sorted(a)
    return None if not a else (a[len(a) // 2] if len(a) % 2 else 0.5 * (a[len(a) // 2 - 1] + a[len(a) // 2]))


def pct(n, d):
    return None if (not d or n is None) else round(100.0 * n / d, 1)


def run():
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    sup = set()
    con = db_ro(os.path.join(DATA, 'state_sol.sqlite'))
    if con:
        try:
            r = con.execute("SELECT v FROM kv WHERE k='sol_superseded_mints'").fetchone()
            sup = set(json.loads(r[0])) if r else set()
        finally:
            con.close()
    graphs = {'sol': load_wallet_graph(os.path.join(DATA, 'state_sol.sqlite')), 'rh': load_wallet_graph(os.path.join(DATA, 'state_rh.sqlite'))}
    tokens, wallet_rows = {'sol': [], 'rh': []}, {'sol': [], 'rh': []}
    n_new = 0
    for chain in ('sol', 'rh'):
        traced = traced_tokens(chain)
        for p in sorted(glob.glob(os.path.join(DATA, f'raw_{chain}', '*', '*.jsonl.gz'))):
            tok, wallets = load_or_parse(chain, p, sup)
            if not tok:
                continue
            key = tok['token'].lower() if chain == 'rh' else tok['token']
            tok['traced'] = key in traced
            g = graphs[chain]
            creator = tok.get('creator')
            linked = 0
            early = [w for w in wallets if w['buy_delay'] is not None and w['role'] != 'creator']
            early.sort(key=lambda w: w['buy_delay'])
            for w in wallets:
                w['linked'] = bool(tok['traced'] and linked_to_creator(g, w['w'], creator))
            if tok['traced']:
                linked = sum(1 for w in early[:10] if w['linked'])
            tok['early10_linked'] = linked if tok['traced'] else None
            tokens[chain].append(tok)
            for w in wallets:
                w['token'] = tok['token']; w['day'] = tok['day']
            wallet_rows[chain].extend(wallets)
        n_new += 0
    # ---------------- token tables
    cols = ['chain', 'day', 'token', 'creator', 'created', 'n_touch_tx', 'zero_net_tx', 'n_trades', 'n_wallets', 'buy_volume', 'bundle_n', 'sniper_n', 'coord_max',
            'wash_n', 'creator_bought', 'creator_net_q', 'creator_sold_all', 'traced', 'early10_linked', 'truncated']
    for chain in ('sol', 'rh'):
        with open(os.path.join(OUT, f'tokens_{chain}.csv'), 'w', newline='', encoding='utf-8') as f:
            wr = csv.writer(f); wr.writerow(cols)
            for t in tokens[chain]:
                wr.writerow([t.get(c) for c in cols])
    # ---------------- daily metrics (by chain x launch day)
    daily = []
    for chain in ('sol', 'rh'):
        days = sorted({t['day'] for t in tokens[chain]})
        for d in days:
            T = [t for t in tokens[chain] if t['day'] == d]
            W = [w for w in wallet_rows[chain] if w['day'] == d and w['closed']]
            tot_pos = sum(w['net_q'] for w in W if w['net_q'] > 0)
            def prof(pred):
                if chain != 'sol':
                    return None            # RH ETH flows are incomplete (buys paid via router/tokens) -> no profit shares
                return sum(w['net_q'] for w in W if w['net_q'] > 0 and pred(w))
            insiders = lambda w: w['role'] in ('creator', 'bundle') or w['linked'] or w['coord'] or w['wash']
            tr = [t for t in T if t['traced']]
            daily.append({
                'chain': chain, 'day': d, 'tokens': len(T), 'median_wallets': median([t['n_wallets'] for t in T]),
                'median_trades': median([t['n_trades'] for t in T]),
                'pct_bundled_launch': pct(sum(1 for t in T if t['bundle_n'] >= 2), len(T)),
                'pct_creator_sold_all': pct(sum(1 for t in T if t['creator_sold_all']), len(T)),
                'pct_creator_profitable': pct(sum(1 for t in T if (t['creator_net_q'] or 0) > 0), len(T)),
                'median_creator_net': median([t['creator_net_q'] for t in T]),
                'pct_coordinated_dump': pct(sum(1 for t in T if t['coord_max'] >= 3), len(T)),
                'pct_with_wash_wallets': pct(sum(1 for t in T if t['wash_n'] > 0), len(T)),
                'zero_net_tx_share': pct(sum(t.get('zero_net_tx') or 0 for t in T), sum(t.get('n_touch_tx') or 0 for t in T)) if chain == 'sol' else None,
                'traced_tokens': len(tr),
                'pct_traced_with_linked_early_buyers': pct(sum(1 for t in tr if (t['early10_linked'] or 0) > 0), len(tr)),
                'realised_profit_total': round(tot_pos, 4) if chain == 'sol' else None,
                'profit_share_creator': pct(prof(lambda w: w['role'] == 'creator'), tot_pos),
                'profit_share_bundle': pct(prof(lambda w: w['role'] == 'bundle'), tot_pos),
                'profit_share_snipers': pct(prof(lambda w: w['role'] == 'sniper'), tot_pos),
                'profit_share_insider_any': pct(prof(insiders), tot_pos),
                'profit_share_independent': pct(prof(lambda w: not insiders(w) and w['role'] != 'sniper'), tot_pos),
                'pnl_quality': 'approx' if chain == 'rh' else 'exact',
            })
    if daily:
        with open(os.path.join(OUT, 'daily_metrics.csv'), 'w', newline='', encoding='utf-8') as f:
            wr = csv.DictWriter(f, fieldnames=list(daily[0].keys())); wr.writeheader(); wr.writerows(daily)
    # ---------------- Solana wallet table + pre-registered persistence test
    days_sol = sorted({t['day'] for t in tokens['sol']})
    d0 = dt.datetime.strptime(days_sol[0], '%Y%m%d') if days_sol else None
    insider_w = set()
    for w in wallet_rows['sol']:
        if w['role'] in ('creator', 'bundle') or w['linked'] or w['coord'] or w['wash']:
            insider_w.add(w['w'])
    agg = {}
    for w in wallet_rows['sol']:
        a = agg.setdefault(w['w'], {'n_tokens': 0, 'n_closed': 0, 'pnl_closed': 0.0, 'wins': 0, 'pnl_w12': 0.0, 'n_w12': 0, 'pnl_w34': 0.0, 'n_w34': 0,
                                    'first_day': w['day'], 'roles': set()})
        a['n_tokens'] += 1; a['roles'].add(w['role']); a['first_day'] = min(a['first_day'], w['day'])
        if w['closed']:
            a['n_closed'] += 1; a['pnl_closed'] += w['net_q']; a['wins'] += w['net_q'] > 0
            k = (dt.datetime.strptime(w['day'], '%Y%m%d') - d0).days
            if k < 14:
                a['pnl_w12'] += w['net_q']; a['n_w12'] += 1
            elif k < 28:
                a['pnl_w34'] += w['net_q']; a['n_w34'] += 1
    with gzip.open(os.path.join(OUT, 'wallets_sol.csv.gz'), 'wt', newline='', encoding='utf-8') as f:
        wr = csv.writer(f)
        wr.writerow(['wallet', 'insider', 'n_tokens', 'n_closed', 'wins', 'pnl_closed_sol', 'pnl_weeks12', 'n_weeks12', 'pnl_weeks34', 'n_weeks34', 'first_day', 'roles'])
        for k, a in agg.items():
            wr.writerow([k, int(k in insider_w), a['n_tokens'], a['n_closed'], a['wins'], round(a['pnl_closed'], 6), round(a['pnl_w12'], 6), a['n_w12'],
                         round(a['pnl_w34'], 6), a['n_w34'], a['first_day'], '|'.join(sorted(a['roles']))])
    span = (dt.datetime.strptime(days_sol[-1], '%Y%m%d') - d0).days + 1 if days_sol else 0
    test = {'rule': 'README decision rule (pre-registered 7 Oct 2026); Solana only', 'days_of_data': span, 'first_day': days_sol[0] if days_sol else None,
            'ready': span >= 29, 'status': 'waiting for 28+ full days of launches' if span < 29 else None}
    if span >= 29:
        elig = {k: a for k, a in agg.items() if k not in insider_w and a['n_w12'] >= 3}
        ranked = sorted(elig.items(), key=lambda kv: -kv[1]['pnl_w12'])
        top = ranked[:max(1, len(ranked) // 100)]
        later = [a['pnl_w34'] / a['n_w34'] for k, a in agg.items() if k not in insider_w and a['n_w34'] >= 1]
        top_later = [a['pnl_w34'] / a['n_w34'] for k, a in top if a['n_w34'] >= 1]
        med = median(later)
        boot, rnd = [], random.Random(7)
        if top_later:
            for _ in range(2000):
                s = [top_later[rnd.randrange(len(top_later))] for _ in top_later]
                boot.append(sum(s) / len(s) - med)
            boot.sort()
            lo = boot[int(0.05 * len(boot))]
        else:
            lo = None
        test.update({'status': 'computed (steps 1-3); step 4 copy-trade simulation is done by the AI review',
                     'eligible_wallets': len(elig), 'top_n': len(top), 'top_active_in_weeks34': len(top_later),
                     'top_mean_pnl_per_token_w34': None if not top_later else round(sum(top_later) / len(top_later), 6),
                     'population_median_pnl_per_token_w34': med, 'bootstrap_5pct_lower_bound_of_diff': lo,
                     'step3_pass': bool(lo is not None and lo > 0)})
    json.dump(test, open(os.path.join(OUT, 'test_status.json'), 'w'), indent=1)
    # ---------------- SUMMARY.md
    L = [f"# memescope analysis — {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z", '',
         f"Tokens analysed: Solana {len(tokens['sol'])}, Robinhood {len(tokens['rh'])} (RH profit numbers are approximate).",
         f"Persistence test: {test['status']} (days of Solana data: {span}).", '']
    for chain in ('sol', 'rh'):
        rows = [r for r in daily if r['chain'] == chain][-4:]
        if not rows:
            continue
        L.append(f"## {'Solana (pump.fun)' if chain == 'sol' else 'Robinhood (Pons)'} — last {len(rows)} launch days")
        keys = ['day', 'tokens', 'median_wallets', 'zero_net_tx_share', 'pct_bundled_launch', 'pct_creator_profitable', 'median_creator_net', 'pct_coordinated_dump',
                'pct_with_wash_wallets', 'traced_tokens', 'pct_traced_with_linked_early_buyers', 'realised_profit_total', 'profit_share_creator',
                'profit_share_bundle', 'profit_share_snipers', 'profit_share_insider_any', 'profit_share_independent']
        L.append('| ' + ' | '.join(keys) + ' |'); L.append('|' + '---|' * len(keys))
        for r in rows:
            L.append('| ' + ' | '.join(str(r.get(k)) for k in keys) + ' |')
        L.append('')
    sol_closed = [a for a in agg.values() if a['n_closed'] >= 3]
    if sol_closed:
        indep = [a for k, a in agg.items() if a['n_closed'] >= 3 and k not in insider_w]
        L += ['## Solana wallets (>=3 closed positions)',
              f"- wallets: {len(sol_closed)}; independent (no insider flag): {len(indep)}",
              f"- share of independent wallets net profitable: {pct(sum(1 for a in indep if a['pnl_closed'] > 0), len(indep))}%",
              f"- median independent wallet P&L: {median([a['pnl_closed'] for a in indep])} SOL", '']
    L.append(f"_Generated by analyze.py v{VERSION} in {time.time() - t0:.0f}s. Definitions are in the file header._")
    open(os.path.join(OUT, 'SUMMARY.md'), 'w', encoding='utf-8').write('\n'.join(L) + '\n')
    log(f"done: sol {len(tokens['sol'])} tokens, rh {len(tokens['rh'])} tokens, {len(daily)} day rows, {time.time() - t0:.0f}s")


if __name__ == '__main__':
    try:
        run()
    except Exception as e:  # noqa: BLE001
        import traceback
        log('FAILED: ' + traceback.format_exc())
        sys.exit(1)
