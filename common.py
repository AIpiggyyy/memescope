"""Shared plumbing: config, HTTP with rate limiting / retries / credit budget, raw storage, run state.

Collectors only FETCH and STORE raw data. All parsing and analysis happens later (in Claude's sandbox),
so a wrong guess about a data format never loses data: we just re-parse the raw files.
"""
import gzip, json, os, random, socket, sqlite3, sys, threading, time, tomllib, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))

# 8 Oct fix: every call on Alex's PC took a constant ~42 s even for tiny replies (= two ~21 s Windows connect
# timeouts before falling back, i.e. a dead IPv6 route, or a slow DNS lookup). Prefer IPv4 and cache lookups.
_orig_getaddrinfo, _dns_cache = socket.getaddrinfo, {}


def _getaddrinfo_v4_cached(host, port, family=0, type=0, proto=0, flags=0):
    key = (host, port, family, type, proto, flags)
    hit = _dns_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    res = v4 or res
    _dns_cache[key] = (time.time(), res)
    return res


socket.getaddrinfo = _getaddrinfo_v4_cached


def load_config():
    with open(os.path.join(ROOT, 'config.toml'), 'rb') as f:
        return tomllib.load(f)


def now_utc():
    return datetime.now(timezone.utc)


def log(msg, logfile=None):
    line = f"{now_utc().strftime('%Y-%m-%d %H:%M:%S')}Z {msg}"
    print(line, flush=True)
    if logfile:
        with open(logfile, 'a', encoding='utf-8') as f:
            f.write(line + '\n')


def need_key(name):
    v = os.environ.get(name, '').strip()
    if not v:
        sys.exit(f'\nMissing {name}. Open a Command Prompt, run:  setx {name} "your-key"  then open a NEW window and retry.\n'
                 f'(Never paste keys into a chat.)')
    return v


class Budget:
    """Daily credit/request budget persisted to disk so restarts don't reset it."""

    def __init__(self, path, daily_limit):
        self.path, self.limit = path, daily_limit
        self.state = {}
        self.lock = threading.Lock()
        if os.path.exists(path):
            try:
                self.state = json.load(open(path))
            except Exception:
                self.state = {}

    def day(self):
        return now_utc().strftime('%Y-%m-%d')

    def used(self):
        return self.state.get(self.day(), 0)

    def left(self):
        return self.limit - self.used()

    def spend(self, n):
        with self.lock:
            d = self.day()
            self.state[d] = self.state.get(d, 0) + n
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)


class OutOfBudget(Exception):
    pass


class Http:
    def __init__(self, rps, budget, name, logfile=None, cost_fn=None):
        self.cost_fn = cost_fn            # 9 Oct: per-URL credit cost (Blockscout charges 20-50 credits per call, not 1)
        self.min_gap = 1.0 / rps
        self.last = 0.0
        self.budget, self.name, self.logfile = budget, name, logfile
        self.calls = 0
        self.t_sum, self.t_n, self.t_max = 0.0, 0, 0.0   # latency stats (8 Oct: diagnosing slow passes)
        self.lock = threading.Lock()      # several worker threads may share one Http; keep the rps limit global

    def _wait(self):
        with self.lock:
            gap = time.time() - self.last
            if gap < self.min_gap:
                time.sleep(self.min_gap - gap)
            self.last = time.time()

    def request(self, url, body=None, cost=1, headers=None, tries=6):
        if self.cost_fn:
            cost = self.cost_fn(url)
        if self.budget.left() < cost:
            raise OutOfBudget(f'{self.name}: daily budget used ({self.budget.used()}/{self.budget.limit})')
        data = json.dumps(body).encode() if body is not None else None
        h = {'Content-Type': 'application/json', 'User-Agent': 'memescope-research/1.0'}
        h.update(headers or {})
        for attempt in range(tries):
            self._wait()
            t0 = time.time()
            try:
                req = urllib.request.Request(url, data=data, headers=h, method='POST' if data else 'GET')
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                self._timed(time.time() - t0, len(raw), body, url)
                self.calls += 1
                self.budget.spend(cost)
                return json.loads(raw)
            except urllib.error.HTTPError as e:
                txt = e.read()[:300].decode('utf-8', 'replace')
                if e.code == 402:   # provider says credits are gone: stop this budget for the day instead of crashing every pass
                    self.budget.spend(max(0, self.budget.left()))
                    raise OutOfBudget(f'{self.name}: provider out of credits (HTTP 402)')
                if e.code in (429, 500, 502, 503, 504):
                    wait = min(60, 2 ** attempt + random.random())
                    log(f'{self.name}: HTTP {e.code}, retry in {wait:.0f}s ({txt[:120]})', self.logfile)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f'{self.name}: HTTP {e.code}: {txt}')
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                wait = min(60, 2 ** attempt + random.random())
                log(f'{self.name}: network error {e}, retry in {wait:.0f}s', self.logfile)
                time.sleep(wait)
        raise RuntimeError(f'{self.name}: gave up after {tries} tries: {url.split("?")[0]}')

    def _timed(self, dt, nbytes, body, url):
        what = body.get('method') if isinstance(body, dict) else url.split('?')[0].split('/v2/')[-1][:60]
        with self.lock:
            self.t_sum += dt; self.t_n += 1; self.t_max = max(self.t_max, dt)
            report = self.t_n % 100 == 0
            avg, mx = self.t_sum / self.t_n, self.t_max
        if dt > 8:
            log(f'{self.name}: SLOW call {dt:.1f}s {what} ({nbytes // 1024} KB)', self.logfile)
        if report:
            log(f'{self.name}: {self.t_n} calls, avg {avg:.2f}s, max {mx:.1f}s', self.logfile)


def write_jsonl_gz(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with gzip.open(tmp, 'wt', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, separators=(',', ':')) + '\n')
    os.replace(tmp, path)


def read_jsonl_gz(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def _locked(fn):
    def w(self, *a, **k):
        with self.lock:
            return fn(self, *a, **k)
    return w


class State:
    """Which tokens we've queued / fetched. One small SQLite file per chain."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)   # one chain thread writes; main thread reads counts
        self.lock = threading.RLock()
        self.db.execute('''CREATE TABLE IF NOT EXISTS tokens(
            token TEXT PRIMARY KEY, created_ts INTEGER, creator TEXT, create_ref TEXT,
            queued_at TEXT, status TEXT, fetched_at TEXT, n_items INTEGER, truncated INTEGER, note TEXT)''')
        self.db.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS wallets(w TEXT PRIMARY KEY, info TEXT)')
        self.db.commit()

    @_locked
    def wget(self, w):
        r = self.db.execute('SELECT info FROM wallets WHERE w=?', (w,)).fetchone()
        return json.loads(r[0]) if r else None

    @_locked
    def wput(self, w, info):
        self.db.execute('INSERT OR REPLACE INTO wallets VALUES(?,?)', (w, json.dumps(info)))
        self.db.commit()

    @_locked
    def get(self, k, default=None):
        r = self.db.execute('SELECT v FROM kv WHERE k=?', (k,)).fetchone()
        return json.loads(r[0]) if r else default

    @_locked
    def put(self, k, v):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (k, json.dumps(v)))
        self.db.commit()

    @_locked
    def queue(self, token, created_ts, creator, ref):
        self.db.execute('INSERT OR IGNORE INTO tokens(token,created_ts,creator,create_ref,queued_at,status) VALUES(?,?,?,?,?,?)',
                        (token, created_ts, creator, ref, now_utc().isoformat(), 'queued'))
        self.db.commit()

    @_locked
    def ready(self, min_age_sec, limit):
        cut = int(time.time()) - min_age_sec
        return self.db.execute("SELECT token, created_ts, creator, create_ref FROM tokens WHERE status='queued' AND created_ts<=? "
                               "ORDER BY created_ts LIMIT ?", (cut, limit)).fetchall()

    @_locked
    def done(self, token, n, truncated, note='', status='done'):
        self.db.execute('UPDATE tokens SET status=?, fetched_at=?, n_items=?, truncated=?, note=? WHERE token=?',
                        (status, now_utc().isoformat(), n, int(truncated), note, token))
        self.db.commit()

    @_locked
    def counts(self):
        return dict(self.db.execute('SELECT status, COUNT(*) FROM tokens GROUP BY status').fetchall())
