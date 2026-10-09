"""Robinhood Chain money-trail tracing for one token (same idea as trace_sol): creator + earliest buyers + biggest
sellers; follow funding back (hops) and ETH forward. Bridges/exchanges/busy contracts are hubs (not expanded).
Note: most wallets here are funded through bridges, so the trail often crosses chains at a hub; we keep the
bridge payout's amount and timestamp so it can later be matched to deposits on the source chain."""
import time

ZERO = '0x0000000000000000000000000000000000000000'


def _list(r):
    res = r.get('result')
    return res if isinstance(res, list) else []


def pick_wallets(transfers, internal, curve, deployer, n_early, n_sellers):
    curve = (curve or '').lower()
    early, seen = [], set()
    for t in transfers:
        if t.get('from', '').lower() == curve and t.get('to', '').lower() not in (ZERO, curve):
            w = t['to'].lower()
            if w not in seen:
                seen.add(w); early.append(w)
        if len(early) >= n_early:
            break
    proceeds = {}
    for i in internal:                                   # ETH paid out by the curve
        if i.get('from', '').lower() == curve and int(i.get('value') or 0) > 0:
            proceeds[i['to'].lower()] = proceeds.get(i['to'].lower(), 0) + int(i['value']) / 1e18
    if not proceeds:                                     # fallback: tokens sold back into the curve
        for t in transfers:
            if t.get('to', '').lower() == curve and t.get('from', '').lower() != ZERO:
                proceeds[t['from'].lower()] = proceeds.get(t['from'].lower(), 0) + int(t.get('value') or 0) / 1e18
    sellers = [w for w, _ in sorted(proceeds.items(), key=lambda x: -x[1])[:n_sellers]]
    roles = {}
    for w in ([deployer.lower()] if deployer else []):
        roles.setdefault(w, []).append('creator')
    for w in early:
        roles.setdefault(w, []).append('early')
    for w in sellers:
        roles.setdefault(w, []).append('seller')
    return roles


class Tracer:
    def __init__(self, api, rest, cfg, state):
        self.api, self.rest, self.cfg, self.st = api, rest, cfg, state

    def back(self, w, raw):
        cached = self.st.wget(w)
        if cached:
            raw.append({'kind': 'wallet_cached', 'wallet': w, 'info': cached})
            return cached
        addr = self.rest(f'addresses/{w}')
        cnt = self.rest(f'addresses/{w}/counters')
        n = int((cnt or {}).get('transactions_count') or 0)
        a = addr or {}
        real_contract = bool(a.get('is_contract')) and a.get('proxy_type') != 'eip7702'   # 7702 = normal wallet with delegated code (probe-verified)
        info = {'hub': real_contract or n > self.cfg['hub_tx_count'], 'funder': None, 'first_ts': None, 'n_tx': n,
                'is_contract': a.get('is_contract'), 'proxy_type': a.get('proxy_type'), 'name': a.get('name'),
                'tags': [x.get('display_name') or x.get('label') for x in (a.get('public_tags') or []) if isinstance(x, dict)]}
        if not info['hub']:
            first = _list(self.api(module='account', action='txlist', address=w, page=1, offset=5, sort='asc'))
            first_i = _list(self.api(module='account', action='txlistinternal', address=w, page=1, offset=5, sort='asc'))
            raw.append({'kind': 'fund_rows', 'wallet': w, 'txlist': first, 'internal': first_i})
            ins = [(int(r.get('timeStamp') or 0), r.get('from', '').lower(), int(r.get('value') or 0) / 1e18)
                   for r in first + first_i if r.get('to', '').lower() == w and int(r.get('value') or 0) > 0]
            if ins:
                ts, src, amt = min(ins)
                info.update(funder=src, first_ts=ts, first_amount=amt)
        raw.append({'kind': 'wallet', 'wallet': w, 'info': info, 'addr': addr, 'counters': cnt})
        self.st.wput(w, info)
        return info

    def forward(self, w, raw):
        rows = _list(self.api(module='account', action='txlist', address=w, page=1, offset=self.cfg['forward_txs'], sort='desc'))
        raw.append({'kind': 'out_rows', 'wallet': w, 'txlist': rows})

    def trace_token(self, token, deployer, curve, transfers, internal):
        c = self.cfg
        roles = pick_wallets(transfers, internal, curve, deployer, c['early_buyers'], c['top_sellers'])
        raw = [{'kind': 'roles', 'token': token, 'roles': roles, 'traced_at': int(time.time())}]
        for w in roles:
            node, hops = w, 0
            while node and hops < c['max_hops']:
                info = self.back(node, raw)
                if info['hub']:
                    break
                node, hops = info['funder'], hops + 1
            if 'seller' in roles[w] or 'creator' in roles[w]:
                self.forward(w, raw)
        return raw
