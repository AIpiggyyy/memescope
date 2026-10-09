"""Offline test with fake Helius/Blockscout responses: discovery, sampling, history paging, fallback, raw files."""
import json, os, shutil, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
os.environ['HELIUS_API_KEY'] = 'test'; os.environ['BLOCKSCOUT_API_KEY'] = 'test'
import common
from common import read_jsonl_gz

NOW = int(time.time()); H = 3600
ok = True
def check(n, c, i=''):
    global ok
    ok &= bool(c); print(f"[{'PASS' if c else 'FAIL'}] {n} {i}")

# ---------------- fake Solana: 40 creates over the last 10 hours, each mint has 250 trades
creates = [{'signature': f'c{i}', 'blockTime': NOW - 600 - i * 900, 'err': None} for i in range(40)]
def sol_tx(sig, mint):
    return {'slot': 1, 'blockTime': NOW, 'transaction': {'message': {'accountKeys': [{'pubkey': 'CREATOR' + sig}]}},
            'meta': {'postTokenBalances': [{'mint': mint}], 'logMessages': ['Program log: Instruction: Create']}}
GTFA = {'on': False}
def fake_sol(body):
    m, p = body['method'], body['params']
    if m == 'getSignaturesForAddress':
        addr, opt = p
        if addr == 'TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM':
            start = 0 if 'before' not in opt else [c['signature'] for c in creates].index(opt['before']) + 1
            return {'result': creates[start:start + 7]}                     # small pages to test paging
        sigs = [{'signature': f'{addr}-t{i}', 'err': None} for i in range(250)][::-1]
        start = 0 if 'before' not in opt else [s['signature'] for s in sigs].index(opt['before']) + 1
        return {'result': sigs[start:start + opt['limit']]}
    if m == 'getTransaction':
        s = p[0]
        return {'result': sol_tx(s, f'mint{s}pump') if s.startswith('c') else {'sig': s}}
    if m == 'getTransactionsForAddress':
        if not GTFA['on']:
            return {'error': {'code': -32601, 'message': 'method not available on free plan'}}
        tok = int(p[1].get('paginationToken') or 0)
        data = [{'sig': f'{p[0]}-g{i}'} for i in range(tok, min(tok + 100, 250))]
        return {'result': {'data': data, 'paginationToken': str(tok + 100) if tok + 100 < 250 else None}}
    raise AssertionError(m)

# ---------------- fake Robinhood: 30 launches/hour, logs capped at 1000 -> force split once
def fake_rh(url):
    if '/api/v2/transactions/' in url:
        h = url.split('/transactions/')[1].split('?')[0]
        return [{'value': '1'}] if h.endswith('internal-transactions') else {'hash': h, 'value': '1', 'from': {'hash': '0xw'}}
    q = dict(kv.split('=', 1) for kv in url.split('?', 1)[1].split('&'))
    a = q.get('action')
    if a == 'getblocknobytime':
        return {'result': {'blockNumber': str(int(q['timestamp']) // 10)}}
    if a == 'getLogs':
        b0, b1 = int(q['fromBlock']), int(q['toBlock'])
        if q['address'].lower().startswith('0xa5'):
            return {'result': []}
        n = 1000 if b1 - b0 > 300 else 15
        return {'result': [{'topics': [q.get('topic0', '0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607'), '0x' + '0' * 24 + f'{b0:08x}{i:032x}', '0x' + '0' * 24 + 'd' * 40],
                            'timeStamp': hex(b0 * 10), 'transactionHash': f'0xlaunch{b0}{i}'} for i in range(n)]}
    if a == 'tokentx':
        tok = q['contractaddress']; page = int(q['page'])
        rows = [{'from': '0x' + '0' * 40, 'to': '0xcurve', 'hash': 'h0'}] + [{'from': '0xcurve', 'to': f'0xw{i}', 'hash': f'h{i}'} for i in range(1, 1500)]
        return {'result': rows[(page - 1) * 1000: page * 1000]}
    if a == 'txlistinternal' and 'address' in q:
        page = int(q['page'])
        rows = [{'hash': f'h{i}'} for i in range(1, 1400)]
        return {'result': rows[(page - 1) * 1000: page * 1000]}
    if a == 'txlistinternal':
        return {'result': [{'value': '1'}]}
    if a == 'eth_getTransactionByHash':
        return {'result': {'hash': q['txhash'], 'value': '0x1'}}
    raise AssertionError(a)

def fake_request(self, url, body=None, cost=1, headers=None, tries=6):
    if self.budget.left() < cost:
        raise common.OutOfBudget('budget')
    self.budget.spend(cost); self.calls += 1
    return fake_sol(body) if body is not None else fake_rh(url)
common.Http.request = fake_request

tmp = tempfile.mkdtemp(); shutil.copy(os.path.join(ROOT, "config.toml"), tmp); common.ROOT = tmp
import collect_sol, collect_rh
collect_sol.ROOT = collect_rh.ROOT = tmp
cfg = common.load_config()
cfg['solana'].update(sample_per_hour=2, min_age_hours=0, max_age_hours=12, max_tx_per_token=3000)
cfg['solana']['trace']['enabled'] = cfg['robinhood']['trace']['enabled'] = False  # tracing covered by test_trace.py
cfg['robinhood'].update(sample_per_hour=3, min_age_hours=0, max_age_hours=3, max_graduated_tx_details=50)

s = collect_sol.Sol(cfg, None)
q = s.discover()
hours = {c['blockTime'] // H for c in creates if c['blockTime'] // H * H <= (NOW // H - 1) * H and c['blockTime'] >= (NOW - 12 * H) // H * H}
check('SOL samples <=2 per complete hour', 0 < q <= 2 * len(hours), f'queued {q} over {len(hours)} hours')
check('SOL second discover adds nothing new', s.discover() == 0)
f = s.fetch()
check('SOL fetch all queued', f == q, f'{f}')
check('SOL gtfa unavailable -> fallback recorded', s.st.get('sol_gtfa_ok') is False)
files = [os.path.join(dp, x) for dp, _, fs in os.walk(os.path.join(tmp, 'data', 'raw_sol')) for x in fs]
rows = read_jsonl_gz(files[0])
check('SOL raw file has meta + 250 txs oldest-first', rows[0]['kind'] == 'meta' and len(rows) == 251 and rows[1]['tx']['sig'].endswith('t0'), len(rows))
# gtfa path
GTFA['on'] = True; s.st.put('sol_gtfa_ok', True)
txs, tr = s.history_gtfa('mintX', 3000)
check('SOL gtfa paging collects 250', len(txs) == 250 and not tr)
txs, tr = s.history_gtfa('mintX', 120)
check('SOL gtfa cap flags truncation', len(txs) == 120 and tr)

r = collect_rh.RH(cfg, None)
q = r.discover()
check('RH samples 3 per complete hour', q == 3 * 3 or q == 3 * 4, f'queued {q}')
f = r.fetch()
check('RH fetch all queued', f == q)
files = [os.path.join(dp, x) for dp, _, fs in os.walk(os.path.join(tmp, 'data', 'raw_rh')) for x in fs]
rows = read_jsonl_gz(files[0])
kinds = {k: sum(1 for x in rows if x['kind'] == k) for k in ('meta', 'transfer', 'curve_internal', 'detail')}
check('RH raw: 1500 transfers, 1399 internal, uncovered hashes -> details (capped 50)', kinds == {'meta': 1, 'transfer': 1500, 'curve_internal': 1399, 'detail': 50}, kinds)
check('RH curve detected', rows[0]['curve'] == '0xcurve' and rows[0]['truncated'])
# budget stop
s.http.budget.limit = s.http.budget.used()
s.st.put('sol_last_hour_done', None)
try:
    s.run_pass(); check('SOL out-of-budget handled gracefully', True)
except Exception as e:
    check('SOL out-of-budget handled gracefully', False, e)

# ---------------- 8 Oct fixes: created-mint detection, migration of wrong mints, threads
from collect_sol import created_mint, Sol
ctx = {'transaction': {'message': {'instructions': [], 'accountKeys': [{'pubkey': 'CR'}]}},
       'meta': {'postTokenBalances': [{'mint': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}, {'mint': 'OTHERtoken'}, {'mint': 'NEWmintNoSuffix'}],
                'innerInstructions': [{'instructions': [{'program': 'spl-token', 'parsed': {'type': 'initializeMint2', 'info': {'mint': 'NEWmintNoSuffix'}}}]}]}}
check('created_mint uses initializeMint2, not other tokens in the tx', created_mint(ctx) == 'NEWmintNoSuffix')
check('created_mint returns None rather than a random token', created_mint({'meta': {'postTokenBalances': [{'mint': 'OTHERtoken'}]}}) is None)
tmp2 = tempfile.mkdtemp(); shutil.copy(os.path.join(ROOT, 'config.toml'), tmp2)
collect_sol.ROOT = tmp2
from common import State, write_jsonl_gz
st0 = State(os.path.join(tmp2, 'data', 'state_sol.sqlite'))
st0.queue('OTHERtoken', NOW - 9 * H, 'CR', 'sigX'); st0.done('OTHERtoken', 900, True)
st0.queue('GOODpump', NOW - 9 * H, 'CR2', 'sigY'); st0.done('GOODpump', 10, False)
write_jsonl_gz(os.path.join(tmp2, 'data', 'raw_sol_creates', 'OTHERtoken.jsonl.gz'), [{'kind': 'create', 'tx': ctx}])
write_jsonl_gz(os.path.join(tmp2, 'data', 'raw_sol_creates', 'GOODpump.jsonl.gz'), [{'kind': 'create', 'tx': {'meta': {'postTokenBalances': [{'mint': 'GOODpump'}]}}}])
st0.db.close()
cfg2 = common.load_config(); cfg2['solana']['trace']['enabled'] = False
s2 = Sol(cfg2, None)
rows = dict(s2.st.db.execute('SELECT token, status FROM tokens').fetchall())
check('migration re-keys wrong mint and re-queues all fetched tokens', rows == {'NEWmintNoSuffix': 'queued', 'GOODpump': 'queued'}, rows)
check('migration remembers superseded mints and retries bulk method', s2.st.get('sol_superseded_mints') == ['OTHERtoken'] and s2.st.get('sol_gtfa_ok') is True)
import threading as _th
err = []
def worker():
    try:
        s2.st.put('x', 1); s2.st.counts()
    except Exception as e:
        err.append(e)
t = _th.Thread(target=worker); t.start(); t.join()
check('state usable from a chain thread', not err, err)
shutil.rmtree(tmp2, ignore_errors=True)
shutil.rmtree(tmp, ignore_errors=True)
print('\nOFFLINE TEST PASSED' if ok else '\nOFFLINE TEST FAILED'); sys.exit(0 if ok else 1)
