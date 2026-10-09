"""One-off check that every data source works, saving RAW samples to probe/ so Claude can verify formats.
Costs ~15 Helius credits and ~15 Blockscout calls. Keys are never written to disk."""
import json, os, sys, time, traceback
from common import ROOT, Budget, Http, load_config, log, need_key

OUT = os.path.join(ROOT, 'probe')
os.makedirs(OUT, exist_ok=True)
summary = []


def save(name, obj):
    with open(os.path.join(OUT, name + '.json'), 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1)


def note(msg):
    summary.append(msg)
    log(msg)


def sol_probe(cfg):
    S = cfg['solana']
    key = need_key('HELIUS_API_KEY')
    http = Http(S['rps'], Budget(os.path.join(OUT, 'budget_probe_sol.json'), 1000), 'helius')
    url = S['rpc'].format(key=key)
    rpc = lambda m, p: http.request(url, {'jsonrpc': '2.0', 'id': 1, 'method': m, 'params': p})

    r = rpc('getSlot', []); note(f"SOL getSlot ok: {r.get('result')}")
    sigs = rpc('getSignaturesForAddress', [S['pump_mint_authority'], {'limit': 20}]); save('sol_sigs_mint_authority', sigs)
    res = sigs.get('result') or []
    note(f"SOL creates via mint authority: {len(res)} sigs; newest blockTime {res[0].get('blockTime') if res else None}")
    mint = None
    for s in res[:3]:
        if s.get('err'):
            continue
        tx = rpc('getTransaction', [s['signature'], {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 1}])
        save('sol_create_tx', tx)
        t = tx.get('result') or {}
        logs = (t.get('meta') or {}).get('logMessages') or []
        has_create = any('Instruction: Create' in l for l in logs)
        mints = sorted({b['mint'] for b in (t.get('meta') or {}).get('postTokenBalances') or []})
        note(f"SOL create tx: 'Instruction: Create' in logs={has_create}; mints in balances={mints[:3]}")
        mint = next((m for m in mints if m.endswith('pump')), mints[0] if mints else None)
        break
    if not mint:
        note('SOL: could not find a mint from the create tx')
        return
    note(f'SOL sample mint: {mint}')
    try:
        g = rpc('getTransactionsForAddress', [mint, {'transactionDetails': 'full', 'sortOrder': 'asc', 'limit': 100,
                                                      'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 1}])
        save('sol_gtfa', g)
        if 'error' in g:
            note(f"SOL getTransactionsForAddress NOT available: {str(g['error'])[:200]}")
        else:
            data = (g.get('result') or {}).get('data') or []
            note(f"SOL getTransactionsForAddress OK: {len(data)} txs, keys={list((g.get('result') or {}).keys())}")
    except Exception as e:
        note(f'SOL getTransactionsForAddress failed: {e}')
    ms = rpc('getSignaturesForAddress', [mint, {'limit': 50}]); save('sol_sigs_mint', ms)
    note(f"SOL getSignaturesForAddress(mint): {len(ms.get('result') or [])} sigs")
    tr = [s for s in (ms.get('result') or []) if not s.get('err')]
    if len(tr) > 1:
        tx = rpc('getTransaction', [tr[0]['signature'], {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 1}])
        save('sol_trade_tx', tx)
        logs = ((tx.get('result') or {}).get('meta') or {}).get('logMessages') or []
        note(f"SOL trade tx log hints: {[l for l in logs if 'Instruction:' in l][:4]}")


def rh_probe(cfg):
    R = cfg['robinhood']
    key = need_key('BLOCKSCOUT_API_KEY')
    http = Http(R['rps'], Budget(os.path.join(OUT, 'budget_probe_rh.json'), 1000), 'blockscout')
    api = lambda **p: http.request(R['api'] + '&' + '&'.join(f'{k}={v}' for k, v in p.items()) + f'&apikey={key}')

    b = api(module='block', action='getblocknobytime', timestamp=int(time.time()), closest='before'); save('rh_block', b)
    latest = int((b.get('result') or {}).get('blockNumber', 0) if isinstance(b.get('result'), dict) else b.get('result') or 0)
    b2 = api(module='block', action='getblocknobytime', timestamp=int(time.time()) - 3600, closest='before'); save('rh_block_1h', b2)
    hour_ago = int((b2.get('result') or {}).get('blockNumber', 0) if isinstance(b2.get('result'), dict) else b2.get('result') or 0)
    note(f'RH latest block {latest}, 1h ago {hour_ago} (~{latest - hour_ago} blocks/hour)')
    token = None
    for f in R['factories']:
        lg = api(module='logs', action='getLogs', address=f['address'], fromBlock=hour_ago, toBlock=latest)
        save(f"rh_logs_any_{f['src'].replace(' ', '_')}", lg)
        rows = lg.get('result') or []
        rows = rows if isinstance(rows, list) else []
        topics = {}
        for r in rows:
            t0 = (r.get('topics') or [None])[0]
            topics[t0] = topics.get(t0, 0) + 1
        hit = topics.get(f['topic0'], 0)
        note(f"RH factory {f['src']} {f['address']}: {len(rows)} logs in last hour; topic0 counts {dict(list(topics.items())[:6])}; expected TokenLaunched hits={hit}")
        if hit and not token:
            r = next(r for r in rows if r['topics'][0] == f['topic0'])
            token = '0x' + r['topics'][1][-40:]
            note(f'RH sample token from TokenLaunched topic1: {token}')
    if not token:
        note('RH: no TokenLaunched found; check rh_logs_any_*.json to identify the right event')
        return
    tt = api(module='account', action='tokentx', contractaddress=token, page=1, offset=200, sort='asc'); save('rh_tokentx', tt)
    rows = tt.get('result') or []
    rows = rows if isinstance(rows, list) else []
    note(f'RH tokentx: {len(rows)} transfers; fields={list(rows[0].keys()) if rows else None}')
    zero = '0x0000000000000000000000000000000000000000'
    first = next((r for r in rows if r.get('from', '').lower() == zero), None)
    if first:
        curve = first['to']
        note(f'RH first mint recipient (curve?): {curve}')
        it = api(module='account', action='txlistinternal', address=curve, page=1, offset=200, sort='asc'); save('rh_internal_curve', it)
        irows = it.get('result') or []
        note(f'RH internal txs for curve: {len(irows) if isinstance(irows, list) else irows}')
    rest = lambda path: http.request(f"{R['rest']}{path}?apikey={key}")
    if rows:
        h = rows[-1]['hash']
        tx = rest(f'transactions/{h}'); save('rh_rest_tx', tx)
        it2 = rest(f'transactions/{h}/internal-transactions'); save('rh_rest_internal', it2)
        note(f"RH REST tx ok: keys={list(tx.keys())[:8]}")
        w = rows[-1]['from']
        a = rest(f'addresses/{w}'); save('rh_rest_addr', a)
        c = rest(f'addresses/{w}/counters'); save('rh_rest_counters', c)
        fl = api(module='account', action='txlist', address=w, page=1, offset=5, sort='asc'); save('rh_wallet_first', fl)
        note(f"RH REST address ok: is_contract={a.get('is_contract')} counters={c}; first txs={len(fl.get('result') or [])}")


if __name__ == '__main__':
    cfg = load_config()
    for name, fn in (('Solana', sol_probe), ('Robinhood', rh_probe)):
        try:
            fn(cfg)
        except SystemExit as e:
            note(f'{name} skipped: {e}')
        except Exception as e:
            note(f'{name} probe ERROR: {e}')
            summary.append(traceback.format_exc())
    with open(os.path.join(OUT, 'SUMMARY.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(summary))
    print('\nProbe finished. Tell Claude: "probe done".')
