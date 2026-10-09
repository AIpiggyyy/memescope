"""Run both collectors in a loop. Leave the window open. Ctrl+C to stop; it resumes where it left off."""
import os, sys, threading, time, traceback
from common import ROOT, load_config, log
import analyze

cfg = load_config()
os.makedirs(os.path.join(ROOT, 'logs'), exist_ok=True)
LOG = os.path.join(ROOT, 'logs', 'collect.log')


def build():
    chains = []
    for name, env, mod, cls in (('Solana', 'HELIUS_API_KEY', 'collect_sol', 'Sol'), ('Robinhood', 'BLOCKSCOUT_API_KEY', 'collect_rh', 'RH')):
        if not os.environ.get(env):
            log(f'{name}: {env} not set, skipping this chain', LOG)
            continue
        chains.append((name, getattr(__import__(mod), cls)(cfg, LOG)))
    if not chains:
        sys.exit('No API keys set. See README.md.')
    return chains


def status(chains):
    lines = [f"memescope status {time.strftime('%Y-%m-%d %H:%M:%S')} (local)"]
    for name, c in chains:
        lines.append(f"{name}: tokens {c.st.counts()} | budget today {c.http.budget.used()}/{c.http.budget.limit}")
    open(os.path.join(ROOT, 'STATUS.txt'), 'w').write('\n'.join(lines) + '\n')


def maybe_analyze(state={'thread': None}):
    """Once per UTC day (after 00:10 UTC, i.e. after the daily budgets reset), rebuild memescope\\analysis\\ in the
    background (incremental; no API calls). Marker: analysis\\last_run_day.txt."""
    t = time.gmtime()
    if t.tm_hour == 0 and t.tm_min < 10:
        return
    day = time.strftime('%Y%m%d', t)
    marker = os.path.join(ROOT, 'analysis', 'last_run_day.txt')
    try:
        last = open(marker).read().strip()
    except OSError:
        last = ''
    if last == day or (state['thread'] and state['thread'].is_alive()):
        return

    def job():
        try:
            analyze.run()
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            open(marker, 'w').write(day)
            log(f'daily analysis done ({day})', LOG)
        except Exception:
            log(f'daily analysis failed:\n{traceback.format_exc()}', LOG)
    state['thread'] = threading.Thread(target=job, daemon=True, name='analyze')
    state['thread'].start()


def chain_loop(name, c, once):
    while True:
        try:
            c.run_pass()
        except Exception:
            log(f'{name} pass crashed (will retry next loop):\n{traceback.format_exc()}', LOG)
        if once:
            return
        time.sleep(cfg['run']['loop_minutes'] * 60)


if __name__ == '__main__':
    once = '--once' in sys.argv
    chains = build()
    log(f"memescope collector started ({', '.join(n for n, _ in chains)}); each chain loops every {cfg['run']['loop_minutes']} min in parallel", LOG)
    threads = [threading.Thread(target=chain_loop, args=(n, c, once), daemon=True, name=n) for n, c in chains]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        status(chains)
        if not once:
            maybe_analyze()
        for t in threads:
            t.join(timeout=60)
    status(chains)
