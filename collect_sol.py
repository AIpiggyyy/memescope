"""Solana / pump.fun collector: sample launches per hour, then (once they are min_age_hours old) store the full raw
transaction history of each sampled token. Raw only; parsing happens later."""
import os, random, time
from common import ROOT, Budget, Http, OutOfBudget, State, log, need_key, write_jsonl_gz, read_jsonl_gz
import trace_sol

TXOPT = {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 1}   # probe/log 8 Oct: v1 transactions exist; 0 drops them


def created_mint(tx):
    """The mint initialised in a pump.fun create tx (initializeMint/initializeMint2), not just any token in the tx."""
    msg = (tx.get('transaction') or {}).get('message') or {}
    ixs = list(msg.get('instructions') or [])
    for inner in (tx.get('meta') or {}).get('innerInstructions') or []:
        ixs += inner.get('instructions') or []
    for ix in ixs:
        p = ix.get('parsed') if isinstance(ix, dict) else None
        if isinstance(p, dict) and p.get('type') in ('initializeMint', 'initializeMint2'):
            return (p.get('info') or {}).get('mint')
    mints = sorted({b['mint'] for b in (tx.get('meta') or {}).get('postTokenBalances') or []})
    return next((m for m in mints if m.endswith('pump')), None)


class Sol:
    def __init__(self, cfg, logfile):
        self.S = cfg['solana']
        self.dir = os.path.join(ROOT, cfg['run']['data_dir'])
        self.logfile = logfile
        key = need_key('HELIUS_API_KEY')
        self.url = self.S['rpc'].format(key=key)
        self.http = Http(self.S['rps'], Budget(os.path.join(self.dir, 'budget_sol.json'), self.S['daily_credits']), 'helius', logfile)
        self.st = State(os.path.join(self.dir, 'state_sol.sqlite'))
        self.migrate_v2()
        self.check_tx_version()
        T = self.S.get('trace', {})
        self.tcfg = T if T.get('enabled') else None
        if self.tcfg:
            self.thttp = Http(self.S['rps'], Budget(os.path.join(self.dir, 'budget_sol_trace.json'), T['daily_credits']), 'helius-trace', logfile)
            trpc = lambda m, p, cost=1: self.thttp.request(self.url, {'jsonrpc': '2.0', 'id': 1, 'method': m, 'params': p}, cost=cost)
            self.tracer = trace_sol.Tracer(trpc, T, self.st)

    def migrate_v2(self):
        """8 Oct fix: (1) some sampled 'mints' were other tokens in the create tx -> re-derive the created mint;
        (2) histories fetched with maxSupportedTransactionVersion=0 silently dropped v1 txs -> refetch everything."""
        if self.st.get('sol_migr_v2'):
            return
        sup, fixed = [], 0
        for token, created, creator, ref in self.st.db.execute('SELECT token, created_ts, creator, create_ref FROM tokens').fetchall():
            p = os.path.join(self.dir, 'raw_sol_creates', f'{token}.jsonl.gz')
            real = None
            if os.path.exists(p):
                try:
                    real = created_mint(read_jsonl_gz(p)[0]['tx'])
                except Exception:
                    real = None
            if real != token:
                sup.append(token)
                self.st.db.execute('DELETE FROM tokens WHERE token=?', (token,))
                if real:
                    self.st.queue(real, created, creator, ref)
                    write_jsonl_gz(os.path.join(self.dir, 'raw_sol_creates', f'{real}.jsonl.gz'), read_jsonl_gz(p))
                    fixed += 1
        self.st.db.execute("UPDATE tokens SET status='queued', note='refetch_v1' WHERE status='done'")
        self.st.db.commit()
        self.st.put('sol_superseded_mints', sup)
        self.st.put('sol_gtfa_ok', True)                 # retry the cheap bulk method with v1 support
        self.st.put('sol_migr_v2', True)
        log(f'SOL migration: {len(sup)} wrong mints superseded ({fixed} re-keyed to the created mint); all fetched tokens re-queued for v1-complete history', self.logfile)

    def check_tx_version(self):
        """If the RPC rejects maxSupportedTransactionVersion=1, fall back to 0 (and say so loudly)."""
        try:
            res = self.rpc('getSignaturesForAddress', [self.S['pump_mint_authority'], {'limit': 1}]).get('result') or []
            if res:
                r = self.rpc('getTransaction', [res[0]['signature'], TXOPT])
                if 'error' in r and 'ersion' in str(r['error']):
                    TXOPT['maxSupportedTransactionVersion'] = 0
                    trace_sol.TXOPT['maxSupportedTransactionVersion'] = 0
                    log(f"SOL WARNING: RPC rejected tx version 1 ({str(r['error'])[:120]}); using 0 - newer txs may be missing", self.logfile)
        except OutOfBudget:
            pass

    def rpc(self, method, params, cost=1):
        r = self.http.request(self.url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, cost=cost)
        return r

    # ---------------------------------------------------------------- discovery
    def discover(self):
        S = self.S
        now = int(time.time())
        hour = 3600
        last_done = self.st.get('sol_last_hour_done', None)       # unix start of the newest fully-sampled hour
        oldest = (now - S['max_age_hours'] * hour) // hour * hour
        start = max(oldest, (last_done + hour) if last_done else oldest)
        last_complete = (now // hour - 1) * hour                   # start of the newest COMPLETE hour
        if start > last_complete:
            return 0
        # page newest -> oldest until we pass `start`
        sigs, before = [], None
        while True:
            p = {'limit': 1000}
            if before:
                p['before'] = before
            res = self.rpc('getSignaturesForAddress', [S['pump_mint_authority'], p]).get('result') or []
            if not res:
                break
            sigs += [s for s in res if not s.get('err') and s.get('blockTime')]
            before = res[-1]['signature']
            if (res[-1].get('blockTime') or 0) < start:
                break
        by_hour = {}
        for s in sigs:
            h = s['blockTime'] // hour * hour
            if start <= h <= last_complete:
                by_hour.setdefault(h, []).append(s)
        queued = 0
        for h in sorted(by_hour):
            pick = random.sample(by_hour[h], min(S['sample_per_hour'], len(by_hour[h])))
            for s in pick:
                tx = self.rpc('getTransaction', [s['signature'], TXOPT]).get('result')
                if not tx:
                    continue
                mint = created_mint(tx)
                keys = ((tx.get('transaction') or {}).get('message') or {}).get('accountKeys') or []
                creator = keys[0]['pubkey'] if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
                if not mint:
                    continue
                self.st.queue(mint, s['blockTime'], creator, s['signature'])
                write_jsonl_gz(os.path.join(self.dir, 'raw_sol_creates', f'{mint}.jsonl.gz'), [{'kind': 'create', 'tx': tx}])
                queued += 1
            log(f'SOL hour {time.strftime("%Y-%m-%d %H:00", time.gmtime(h))}Z: {len(by_hour[h])} launches, sampled {len(pick)}', self.logfile)
            self.st.put('sol_last_hour_done', h)
        return queued

    # ---------------------------------------------------------------- history
    def history_gtfa(self, mint, cap):
        out, token = [], None
        while len(out) < cap:
            opt = {'transactionDetails': 'full', 'sortOrder': 'asc', 'limit': 100, **TXOPT}
            if token:
                opt['paginationToken'] = token
            r = self.rpc('getTransactionsForAddress', [mint, opt], cost=10)
            if 'error' in r:
                raise NotImplementedError(str(r['error'])[:200])
            res = r.get('result') or {}
            data = res.get('data') or []
            out += data
            token = res.get('paginationToken')
            if not token or len(data) < 100:
                return out, False
        return out[:cap], True

    def history_sigs(self, mint, cap):
        sigs, before, pages = [], None, 0
        while pages < self.S.get('max_sig_pages', 30):
            p = {'limit': 1000}
            if before:
                p['before'] = before
            res = self.rpc('getSignaturesForAddress', [mint, p]).get('result') or []
            sigs += res; pages += 1
            if len(res) < 1000:
                break
            before = res[-1]['signature']
        sigs = [s for s in sigs if not s.get('err')][::-1]          # oldest first -> keep the launch window
        trunc = len(sigs) > cap
        out, self.missing = [], 0
        for s in sigs[:cap]:
            tx = self.rpc('getTransaction', [s['signature'], TXOPT]).get('result')
            if tx:
                out.append(tx)
            else:
                self.missing += 1
        return out, trunc

    def fetch(self, limit=50):
        rows = self.st.ready(self.S['min_age_hours'] * 3600, limit)
        n = 0
        for mint, created, creator, ref in rows:
            t0, c0 = time.time(), self.http.budget.used()
            cap = self.S['max_tx_per_token']
            txs, trunc, self.missing = None, False, 0
            if self.S.get('use_get_transactions_for_address', True) and self.st.get('sol_gtfa_ok', True):
                try:
                    txs, trunc = self.history_gtfa(mint, cap)
                except NotImplementedError as e:
                    log(f'SOL getTransactionsForAddress unavailable ({e}); switching to getTransaction (1 credit/tx)', self.logfile)
                    self.st.put('sol_gtfa_ok', False)
            if txs is None:
                txs, trunc = self.history_sigs(mint, cap)
            day = time.strftime('%Y%m%d', time.gmtime(created))
            write_jsonl_gz(os.path.join(self.dir, 'raw_sol', day, f'{mint}.jsonl.gz'),
                           [{'kind': 'meta', 'mint': mint, 'created': created, 'creator': creator, 'create_sig': ref,
                             'truncated': trunc, 'missing': self.missing, 'fetched': int(time.time())}] + [{'kind': 'tx', 'tx': t} for t in txs])
            t1 = time.time()
            note = self.maybe_trace(mint, creator, created, txs)
            self.st.done(mint, len(txs), trunc, note)
            log(f'SOL token {mint[:8]}: {len(txs)} txs, history {t1 - t0:.0f}s / {self.http.budget.used() - c0} credits, {note} {time.time() - t1:.0f}s', self.logfile)
            n += 1
        return n

    def maybe_trace(self, mint, creator, created, txs):
        if not self.tcfg:
            return ''
        flows = trace_sol.wallet_flows(txs, mint)
        best = max([f['d_sol'] for f in flows if f['d_tok'] < 0] or [0])
        why = 'big_exit' if best >= self.tcfg['trace_if_proceeds_over'] else ('random' if random.random() < self.tcfg['trace_fraction'] else None)
        if not why:
            return 'not_traced'
        try:
            raw = self.tracer.trace_token(mint, creator, txs)
        except OutOfBudget:
            return 'trace_budget_out'
        except RuntimeError as e:
            log(f'SOL trace failed for {mint}: {str(e)[:200]}', self.logfile)
            return 'trace_error'
        raw[0]['why'], raw[0]['best_exit_sol'] = why, best
        day = time.strftime('%Y%m%d', time.gmtime(created))
        write_jsonl_gz(os.path.join(self.dir, 'raw_sol_trace', day, f'{mint}.jsonl.gz'), raw)
        return f'traced:{why}'

    def run_pass(self):
        try:
            q = self.discover()
            f = self.fetch(limit=self.S.get('fetch_per_pass', 40))
            log(f'SOL pass: queued {q}, fetched {f}; tokens {self.st.counts()}; credits today {self.http.budget.used()}/{self.http.budget.limit}', self.logfile)
        except OutOfBudget as e:
            log(f'SOL: {e} — resumes tomorrow (UTC)', self.logfile)
