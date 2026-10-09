"""Robinhood Chain / Pons collector: sample launches per hour from factory TokenLaunched logs, then store raw
token transfers, the curve's internal ETH flows, and (capped) per-tx details for everything else. Raw only."""
from concurrent.futures import ThreadPoolExecutor
import os, random, time
from common import ROOT, Budget, Http, OutOfBudget, State, log, need_key, write_jsonl_gz
import trace_rh

ZERO = '0x0000000000000000000000000000000000000000'


def blockscout_cost(url):
    """Blockscout PRO API credits per call (docs.blockscout.com/devs/plans-and-credits, 9 Oct 2026): default 20,
    token transfers/tokens 30, internal transactions 40, raw-trace/summary 50. Free plan = 100k credits/day."""
    p = url.split('?')[0]
    if 'raw-trace' in p or p.endswith('/summary'):
        return 50
    if 'internal-transactions' in p:
        return 40
    if '/api/v2/' in p and ('token-transfers' in p or '/tokens/' in p):
        return 30
    return 20


def _list(r):
    res = r.get('result')
    return res if isinstance(res, list) else []


class RH:
    def __init__(self, cfg, logfile):
        self.R = cfg['robinhood']
        self.dir = os.path.join(ROOT, cfg['run']['data_dir'])
        self.logfile = logfile
        self.key = need_key('BLOCKSCOUT_API_KEY')
        self.http = Http(self.R['rps'], Budget(os.path.join(self.dir, 'budget_rh.json'), self.R['daily_credits']), 'blockscout', logfile, cost_fn=blockscout_cost)
        self.st = State(os.path.join(self.dir, 'state_rh.sqlite'))
        T = self.R.get('trace', {})
        self.tcfg = T if T.get('enabled') else None
        if self.tcfg:
            self.thttp = Http(self.R['rps'], Budget(os.path.join(self.dir, 'budget_rh_trace.json'), T['daily_credits']), 'blockscout-trace', logfile, cost_fn=blockscout_cost)
            tapi = lambda **p: self.thttp.request(f"{self.R['api']}&" + '&'.join(f'{k}={v}' for k, v in p.items()) + f'&apikey={self.key}')
            self.tracer = trace_rh.Tracer(tapi, self.rest, T, self.st)

    def rest(self, path, http=None):
        try:
            return (http or self.thttp).request(f"{self.R['rest']}{path}?apikey={self.key}")
        except RuntimeError as e:
            if 'HTTP 404' in str(e):
                return None
            raise

    def api(self, **p):
        q = '&'.join(f'{k}={v}' for k, v in p.items())
        return self.http.request(f"{self.R['api']}&{q}&apikey={self.key}")

    def block_at(self, ts):
        r = self.api(module='block', action='getblocknobytime', timestamp=int(ts), closest='before').get('result')
        return int(r.get('blockNumber') if isinstance(r, dict) else r)

    def logs(self, fac, b0, b1, depth=0):
        rows = _list(self.api(module='logs', action='getLogs', address=fac['address'], topic0=fac['topic0'], fromBlock=b0, toBlock=b1))
        if len(rows) >= 1000 and b1 > b0 and depth < 8:          # result cap hit: split the range
            mid = (b0 + b1) // 2
            return self.logs(fac, b0, mid, depth + 1) + self.logs(fac, mid + 1, b1, depth + 1)
        return rows

    # ---------------------------------------------------------------- discovery
    def discover(self):
        R, hour, now = self.R, 3600, int(time.time())
        facs = self.st.get('rh_factories_live', None) or R['factories']
        last_done = self.st.get('rh_last_hour_done', None)
        oldest = (now - R['max_age_hours'] * hour) // hour * hour
        h = max(oldest, (last_done + hour) if last_done else oldest)
        last_complete = (now // hour - 1) * hour
        queued = 0
        while h <= last_complete:
            b0, b1 = self.block_at(h), self.block_at(h + hour - 1)
            found = []
            for fac in facs:
                for r in self.logs(fac, b0, b1):
                    found.append((fac, r))
            pick = random.sample(found, min(R['sample_per_hour'], len(found)))
            for fac, r in pick:
                t = r.get('topics') or []
                if len(t) < 2:
                    continue
                token = '0x' + t[1][-40:]
                deployer = '0x' + t[3][-40:] if len(t) > 3 and t[3] else None     # verified by probe: topic1 token, topic2 curve, topic3 deployer
                ts = int(r.get('timeStamp', '0x0'), 16) if str(r.get('timeStamp', '')).startswith('0x') else int(r.get('timeStamp') or h)
                self.st.queue(token.lower(), ts, deployer, r.get('transactionHash'))
                write_jsonl_gz(os.path.join(self.dir, 'raw_rh_launches', f'{token.lower()}.jsonl.gz'), [{'kind': 'launch', 'factory': fac['address'], 'log': r}])
                queued += 1
            log(f'RH hour {time.strftime("%Y-%m-%d %H:00", time.gmtime(h))}Z: {len(found)} launches, sampled {len(pick)}', self.logfile)
            self.st.put('rh_last_hour_done', h)
            h += hour
        return queued

    # ---------------------------------------------------------------- history
    def paged(self, cap, **p):
        out, page = [], 1
        while len(out) < cap and page * 1000 <= 10000:
            rows = _list(self.api(page=page, offset=1000, sort='asc', **p))
            out += rows
            if len(rows) < 1000:
                return out, False
            page += 1
        return out[:cap], True

    def tx_details(self, hashes):
        """Per-tx lookups, several in flight at once (Blockscout REST is slow per call; the rps cap still holds)."""
        def one(hsh):
            return {'hash': hsh, 'tx': self.rest(f'transactions/{hsh}', self.http),
                    'internal': self.rest(f'transactions/{hsh}/internal-transactions', self.http)}
        details, derr = [], None
        with ThreadPoolExecutor(max_workers=max(1, int(self.R.get('detail_workers', 6)))) as ex:
            futs = [ex.submit(one, h) for h in hashes]
            for f in futs:
                try:
                    details.append(f.result())
                except RuntimeError as e:
                    derr = str(e)[:200]
            if derr:
                log(f'RH tx details: {len(hashes) - len(details)} of {len(hashes)} failed ({derr}); kept the rest', self.logfile)
        return details, derr

    def fetch(self, limit=50):
        R = self.R
        rows = self.st.ready(R['min_age_hours'] * 3600, limit)
        n = 0
        for token, created, deployer, ref in rows:
            t0, c0 = time.time(), self.http.budget.used()
            transfers, trunc = self.paged(R['max_transfers_per_token'], module='account', action='tokentx', contractaddress=token)
            curve = next((t['to'].lower() for t in transfers if t.get('from', '').lower() == ZERO), None)
            internal, trunc_i = ([], False)
            if curve:
                internal, trunc_i = self.paged(R['max_transfers_per_token'], module='account', action='txlistinternal', address=curve)
            covered = {i.get('hash') or i.get('transactionHash') for i in internal} | {ref}
            other = []
            for t in transfers:
                hsh = t.get('hash')
                if hsh and hsh not in covered and hsh not in other:
                    other.append(hsh)
            details, derr = self.tx_details(other[:R['max_graduated_tx_details']])
            day = time.strftime('%Y%m%d', time.gmtime(created))
            write_jsonl_gz(os.path.join(self.dir, 'raw_rh', day, f'{token}.jsonl.gz'),
                           [{'kind': 'meta', 'token': token, 'created': created, 'deployer': deployer, 'launch_tx': ref, 'curve': curve,
                             'truncated': trunc or trunc_i or len(other) > R['max_graduated_tx_details'], 'details_error': derr, 'fetched': int(time.time())}]
                           + [{'kind': 'transfer', 'r': t} for t in transfers]
                           + [{'kind': 'curve_internal', 'r': i} for i in internal]
                           + [{'kind': 'detail', 'r': d} for d in details])
            t1 = time.time()
            note = self.maybe_trace(token, deployer, curve, created, transfers, internal)
            self.st.done(token, len(transfers), trunc, note)
            log(f'RH token {token[:10]}: {len(transfers)} transfers, {len(internal)} curve_internal, {len(details)} details, '
                f'history {t1 - t0:.0f}s / {self.http.budget.used() - c0} calls, {note} {time.time() - t1:.0f}s', self.logfile)
            n += 1
        return n

    def maybe_trace(self, token, deployer, curve, created, transfers, internal):
        if not self.tcfg:
            return ''
        exits = {}
        for i in internal:
            if (i.get('from') or '').lower() == (curve or '') and int(i.get('value') or 0) > 0:
                exits[i['to']] = exits.get(i['to'], 0) + int(i['value']) / 1e18
        best = max(exits.values() or [0])
        why = 'big_exit' if best >= self.tcfg['trace_if_proceeds_over'] else ('random' if random.random() < self.tcfg['trace_fraction'] else None)
        if not why:
            return 'not_traced'
        try:
            raw = self.tracer.trace_token(token, deployer, curve, transfers, internal)
        except OutOfBudget:
            return 'trace_budget_out'
        except RuntimeError as e:
            log(f'RH trace failed for {token}: {str(e)[:200]}', self.logfile)
            return 'trace_error'
        raw[0]['why'], raw[0]['best_exit_eth'] = why, best
        day = time.strftime('%Y%m%d', time.gmtime(created))
        write_jsonl_gz(os.path.join(self.dir, 'raw_rh_trace', day, f'{token}.jsonl.gz'), raw)
        return f'traced:{why}'

    def run_pass(self):
        try:
            q = self.discover()
            f = self.fetch(limit=self.R.get('fetch_per_pass', 40))
            log(f'RH pass: queued {q}, fetched {f}; tokens {self.st.counts()}; calls today {self.http.budget.used()}/{self.http.budget.limit}', self.logfile)
        except OutOfBudget as e:
            log(f'RH: {e} — resumes tomorrow (UTC)', self.logfile)
