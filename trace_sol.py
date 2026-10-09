"""Solana money-trail tracing for one token: pick the creator, the earliest buyers and the biggest sellers,
then follow each wallet's money BACK (who funded it, up to N hops) and FORWARD (where its SOL went).
Stores raw responses only. Hubs (exchanges, bridges, busy bots) are detected by activity and not expanded."""
import time

TXOPT = {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 1}
LAMPORTS = 1e9


def _keys(tx):
    ks = ((tx.get('transaction') or {}).get('message') or {}).get('accountKeys') or []
    return [k['pubkey'] if isinstance(k, dict) else k for k in ks]


def wallet_flows(txs, mint):
    """Per fee-payer token and SOL deltas for each tx touching `mint` (enough to pick wallets to trace)."""
    out = []
    for t in txs:
        meta = t.get('meta') or {}
        if meta.get('err'):
            continue
        keys = _keys(t)
        if not keys:
            continue
        payer = keys[0]
        pre = {(b.get('owner'), b.get('mint')): float((b.get('uiTokenAmount') or {}).get('uiAmount') or 0) for b in meta.get('preTokenBalances') or []}
        post = {(b.get('owner'), b.get('mint')): float((b.get('uiTokenAmount') or {}).get('uiAmount') or 0) for b in meta.get('postTokenBalances') or []}
        d_tok = post.get((payer, mint), 0.0) - pre.get((payer, mint), 0.0)
        pb, qb = meta.get('preBalances') or [0], meta.get('postBalances') or [0]
        d_sol = (qb[0] - pb[0]) / LAMPORTS
        out.append({'slot': t.get('slot'), 'ts': t.get('blockTime'), 'wallet': payer, 'd_tok': d_tok, 'd_sol': d_sol})
    return out


def pick_wallets(flows, creator, n_early, n_sellers):
    early, seen = [], set()
    for f in flows:
        if f['d_tok'] > 0 and f['wallet'] not in seen:
            seen.add(f['wallet']); early.append(f['wallet'])
        if len(early) >= n_early:
            break
    proceeds = {}
    for f in flows:
        if f['d_tok'] < 0:
            proceeds[f['wallet']] = proceeds.get(f['wallet'], 0) + f['d_sol']
    sellers = [w for w, _ in sorted(proceeds.items(), key=lambda x: -x[1])[:n_sellers]]
    roles = {}
    for w in ([creator] if creator else []):
        roles.setdefault(w, []).append('creator')
    for w in early:
        roles.setdefault(w, []).append('early')
    for w in sellers:
        roles.setdefault(w, []).append('seller')
    return roles


def sol_inflows(tx, wallet):
    """(source, amount_sol) for SOL that arrived at `wallet` in this tx (system transfers, incl. inner instructions)."""
    res = []
    msg = (tx.get('transaction') or {}).get('message') or {}
    ixs = list(msg.get('instructions') or [])
    for inner in (tx.get('meta') or {}).get('innerInstructions') or []:
        ixs += inner.get('instructions') or []
    for ix in ixs:
        p = ix.get('parsed') if isinstance(ix, dict) else None
        if isinstance(p, dict) and p.get('type') in ('transfer', 'transferWithSeed', 'createAccount') and ix.get('program') == 'system':
            info = p.get('info') or {}
            dest = info.get('destination') or info.get('newAccount')
            if dest == wallet:
                res.append((info.get('source'), (info.get('lamports') or 0) / LAMPORTS))
    return res


class Tracer:
    def __init__(self, rpc, cfg, state):
        self.rpc, self.cfg, self.st = rpc, cfg, state

    def sigs(self, wallet, pages):
        out, before = [], None
        for _ in range(pages):
            p = {'limit': 1000}
            if before:
                p['before'] = before
            res = self.rpc('getSignaturesForAddress', [wallet, p]).get('result') or []
            out += res
            if len(res) < 1000:
                return out, False
            before = res[-1]['signature']
        return out, True                                       # still more history -> busy wallet / hub

    def back(self, wallet, raw):
        cached = self.st.wget(wallet)
        if cached:
            raw.append({'kind': 'wallet_cached', 'wallet': wallet, 'info': cached})
            return cached
        sigs, busy = self.sigs(wallet, self.cfg['hub_pages'])
        info = {'hub': busy, 'funder': None, 'first_ts': None, 'n_sigs': len(sigs)}
        if not busy and sigs:
            oldest = [s for s in sigs if not s.get('err')][-3:][::-1]       # 3 oldest successful txs
            for s in oldest:
                tx = self.rpc('getTransaction', [s['signature'], TXOPT]).get('result')
                if not tx:
                    continue
                raw.append({'kind': 'fund_tx', 'wallet': wallet, 'tx': tx})
                ins = sol_inflows(tx, wallet)
                if ins:
                    info['funder'], info['first_ts'] = max(ins, key=lambda x: x[1])[0], tx.get('blockTime')
                    break
        raw.append({'kind': 'wallet', 'wallet': wallet, 'info': info, 'sigs_head': sigs[:50], 'sigs_tail': sigs[-50:]})
        self.st.wput(wallet, info)
        return info

    def forward(self, wallet, raw):
        res = self.rpc('getSignaturesForAddress', [wallet, {'limit': self.cfg['forward_txs'] * 4}]).get('result') or []
        for s in [s for s in res if not s.get('err')][:self.cfg['forward_txs']]:
            tx = self.rpc('getTransaction', [s['signature'], TXOPT]).get('result')
            if tx:
                raw.append({'kind': 'out_tx', 'wallet': wallet, 'tx': tx})

    def trace_token(self, mint, creator, txs):
        c = self.cfg
        flows = wallet_flows(txs, mint)
        roles = pick_wallets(flows, creator, c['early_buyers'], c['top_sellers'])
        raw = [{'kind': 'roles', 'mint': mint, 'roles': roles, 'traced_at': int(time.time())}]
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
