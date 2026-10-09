"""Money-trail tracing with fake chains containing a planted insider cluster."""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import common, trace_sol, trace_rh
from common import State, OutOfBudget

ok = True
def check(n, c, i=''):
    global ok
    ok &= bool(c); print(f"[{'PASS' if c else 'FAIL'}] {n} {i}")

# ---------------- Solana: HUB(exchange) -> F1 -> {creator C, early buyers B1,B2}; B3 funded by HUB; B1 dumps, sends SOL to COLLECTOR
MINT = 'MINTpump'
def tx(sig, payer, d_tok=0.0, d_sol=0.0, transfers=(), ts=1000):
    return {'slot': ts, 'blockTime': ts, 'sig': sig,
            'transaction': {'message': {'accountKeys': [{'pubkey': payer}],
                                        'instructions': [{'program': 'system', 'parsed': {'type': 'transfer', 'info': {'source': s, 'destination': d, 'lamports': int(l * 1e9)}}} for s, d, l in transfers]}},
            'meta': {'err': None, 'preBalances': [int(10e9)], 'postBalances': [int(10e9 + d_sol * 1e9)],
                     'preTokenBalances': [{'owner': payer, 'mint': MINT, 'uiTokenAmount': {'uiAmount': 100.0}}],
                     'postTokenBalances': [{'owner': payer, 'mint': MINT, 'uiTokenAmount': {'uiAmount': 100.0 + d_tok}}]}}
token_txs = [tx('t1', 'C', 50, -1), tx('t2', 'B1', 30, -0.6), tx('t3', 'B2', 20, -0.4), tx('t4', 'B3', 10, -0.3), tx('t5', 'R1', 5, -0.2),
             tx('t6', 'B1', -30, 9.0), tx('t7', 'B2', -20, 4.0), tx('t8', 'C', -50, 12.0)]
history = {  # wallet -> list of (sig, tx) newest first
    'C': [('c_out', tx('c_out', 'C', transfers=[('C', 'COLLECTOR', 11.5)], ts=3000)), ('c_fund', tx('c_fund', 'F1', transfers=[('F1', 'C', 2)], ts=500))],
    'B1': [('b1_out', tx('b1_out', 'B1', transfers=[('B1', 'COLLECTOR', 8.9)], ts=3001)), ('b1_fund', tx('b1_fund', 'F1', transfers=[('F1', 'B1', 1)], ts=501))],
    'B2': [('b2_fund', tx('b2_fund', 'F1', transfers=[('F1', 'B2', 1)], ts=502))],
    'B3': [('b3_fund', tx('b3_fund', 'HUB', transfers=[('HUB', 'B3', 1)], ts=503))],
    'R1': [('r1_fund', tx('r1_fund', 'HUB', transfers=[('HUB', 'R1', 1)], ts=504))],
    'F1': [('f1_fund', tx('f1_fund', 'HUB', transfers=[('HUB', 'F1', 10)], ts=100))],
}
alltx = {s: t for v in history.values() for s, t in v}
calls = {'sigs': {}, 'tx': 0}
def rpc(m, p, cost=1):
    if m == 'getSignaturesForAddress':
        w, opt = p
        calls['sigs'][w] = calls['sigs'].get(w, 0) + 1
        if w == 'HUB':
            return {'result': [{'signature': f'h{i}', 'err': None} for i in range(1000)]}
        return {'result': [{'signature': s, 'err': None} for s, _ in history.get(w, [])][:opt['limit']]}
    if m == 'getTransaction':
        calls['tx'] += 1
        return {'result': alltx.get(p[0])}
    raise AssertionError(m)

tmp = tempfile.mkdtemp()
st = State(os.path.join(tmp, 's.sqlite'))
cfg = dict(early_buyers=4, top_sellers=2, max_hops=3, hub_pages=2, forward_txs=5)
tr = trace_sol.Tracer(rpc, cfg, st)
raw = tr.trace_token(MINT, 'C', token_txs)
roles = raw[0]['roles']
check('SOL roles: creator, 4 early buyers, top-2 sellers', roles.get('C') == ['creator', 'early', 'seller'] and roles.get('B1') == ['early', 'seller']
      and 'B3' in roles and 'R1' not in roles, roles)
fund = {r['wallet']: r['info'] for r in raw if r['kind'] == 'wallet'}
check('SOL funding: C, B1, B2 all funded by F1 (insider cluster visible)', all(fund[w]['funder'] == 'F1' for w in ('C', 'B1', 'B2')), {w: fund[w]['funder'] for w in fund})
check('SOL hop continues F1 -> HUB, HUB marked hub and not expanded', fund['F1']['funder'] == 'HUB' and fund['HUB']['hub'])
check('SOL F1 traced once (cache reused)', calls['sigs']['F1'] == 1, calls['sigs'])
outs = [r for r in raw if r['kind'] == 'out_tx']
check('SOL forward: profits from C and B1 to COLLECTOR captured',
      {r['wallet'] for r in outs if any(i['parsed']['info']['destination'] == 'COLLECTOR' for i in r['tx']['transaction']['message']['instructions'])} == {'C', 'B1'})
check('SOL inflow parser', trace_sol.sol_inflows(alltx['c_fund'], 'C') == [('F1', 2.0)])

# ---------------- Robinhood: same shape
CURVE = '0xcurve'
transfers = [{'from': '0x' + '0' * 40, 'to': CURVE, 'value': str(10**27), 'hash': 'h0'}] + \
            [{'from': CURVE, 'to': w, 'value': str(10**24), 'hash': f'h{i}'} for i, w in enumerate(['0xc', '0xb1', '0xb2', '0xb3', '0xr1'], 1)] + \
            [{'from': w, 'to': CURVE, 'value': str(10**24), 'hash': f's{i}'} for i, w in enumerate(['0xb1', '0xc'])]
internal = [{'from': CURVE, 'to': '0xb1', 'value': str(3 * 10**18)}, {'from': CURVE, 'to': '0xc', 'value': str(5 * 10**18)}]
fund_rh = {'0xc': '0xf1', '0xb1': '0xf1', '0xb2': '0xf1', '0xb3': '0xbridge', '0xf1': '0xbridge'}
def api(**p):
    w = p.get('address')
    if p['action'] == 'txlist' and p.get('sort') == 'asc':
        return {'result': [{'from': fund_rh[w], 'to': w, 'value': str(10**18), 'timeStamp': '100'}] if w in fund_rh else []}
    if p['action'] == 'txlistinternal':
        return {'result': []}
    if p['action'] == 'txlist':
        return {'result': [{'from': w, 'to': '0xcollector', 'value': str(4 * 10**18)}]}
    raise AssertionError(p)
def rest(path):
    w = path.split('/')[1]
    if path.endswith('counters'):
        return {'transactions_count': '500000' if w == '0xbridge' else '3'}
    if w == '0xb2':
        return {'is_contract': True, 'proxy_type': 'eip7702'}       # smart-account wallet must still be traced
    return {'is_contract': w == '0xbridge'}
st2 = State(os.path.join(tmp, 'r.sqlite'))
t2 = trace_rh.Tracer(api, rest, dict(early_buyers=4, top_sellers=2, max_hops=3, hub_tx_count=20000, forward_txs=10), st2)
raw2 = t2.trace_token('0xtok', '0xc', CURVE, transfers, internal)
roles2 = raw2[0]['roles']
check('RH roles from transfers + curve ETH payouts', roles2.get('0xc') == ['creator', 'early', 'seller'] and roles2.get('0xb1') == ['early', 'seller'], roles2)
f2 = {r['wallet']: r['info'] for r in raw2 if r['kind'] == 'wallet'}
check('RH cluster: creator + early buyers funded by same wallet; bridge = hub', all(f2[w]['funder'] == '0xf1' for w in ('0xc', '0xb1', '0xb2')) and f2['0xbridge']['hub'], {w: f2[w]['funder'] for w in f2})
check('RH EIP-7702 wallet is traced, not treated as hub', f2['0xb2']['hub'] is False and f2['0xb2']['funder'] == '0xf1')
check('RH forward rows for sellers/creator', {r['wallet'] for r in raw2 if r['kind'] == 'out_rows'} == {'0xc', '0xb1'})
print('\nTRACE TEST PASSED' if ok else '\nTRACE TEST FAILED'); sys.exit(0 if ok else 1)
