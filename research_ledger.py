"""Durable, append-only storage for research advisories. Its own database file.

Two invariants make the record auditable rather than decorative:

  FIRST OBSERVATION WINS. A (session, role, symbol) row is written once. A later
  cycle that sees the same session again cannot overwrite it, so an advisory can
  never be quietly improved after the fact with data that arrived later. Repeat
  and concurrent cycles are therefore idempotent.

  NO BACKDATING. `newest_session` lets the caller refuse to write an advisory for
  a session older than one already recorded, which is what a replay of history
  into the forward record would look like.

This database is separate from the core paper book and from the hourly lab. It
holds advisories and digests only: no positions, no cash, no orders.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import sqlite3

SCHEMA_VERSION = 'research-ledger-v1'


def clean(value):
    """Make a payload JSON-safe without inventing numbers.

    A non-finite float becomes null, which reads as "not measured" in the UI.
    Storing NaN would either break the strict encoder or, worse, round-trip as a
    number a reader could mistake for a measurement.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


class ResearchLedger:
    def __init__(self, path, version=SCHEMA_VERSION):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.version = version
        with self.connection() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY CHECK(id=1), version TEXT);
            CREATE TABLE IF NOT EXISTS cycles(
                session TEXT PRIMARY KEY, observed_at TEXT NOT NULL, core_session TEXT,
                core_observed_at TEXT, intake_fetched_at TEXT, intake_cached INTEGER,
                intake_sha256 TEXT, used_sha256 TEXT, source TEXT, symbols INTEGER,
                findings INTEGER, abstentions INTEGER, outcome TEXT);
            CREATE TABLE IF NOT EXISTS advisories(
                id INTEGER PRIMARY KEY, session TEXT NOT NULL, role TEXT NOT NULL,
                symbol TEXT NOT NULL, observed_at TEXT NOT NULL, session_age_hours REAL,
                verdict TEXT, evidence TEXT, observations_used INTEGER, proposed_action TEXT,
                abstained INTEGER NOT NULL, abstention_reason TEXT, source TEXT,
                UNIQUE(session, role, symbol));
            CREATE TABLE IF NOT EXISTS digests(
                session TEXT PRIMARY KEY, observed_at TEXT NOT NULL, payload TEXT NOT NULL);
            -- The decision a cycle committed to BEFORE it touched any shadow
            -- book. It is written once and read back verbatim, so a cycle that
            -- dies part-way through can be finished later with the same targets,
            -- the same prices and the same observation time rather than with
            -- whatever the data looks like on the retry.
            CREATE TABLE IF NOT EXISTS decisions(
                session TEXT PRIMARY KEY, observed_at TEXT NOT NULL, core_session TEXT,
                core_observed_at TEXT, intake_sha256 TEXT, payload TEXT NOT NULL);
            -- Append-only record of every cycle that declined to advise, kept
            -- separate from advised sessions so a hold is never mistaken for a
            -- reading and a later retry never erases the earlier refusal.
            CREATE TABLE IF NOT EXISTS holds(
                id INTEGER PRIMARY KEY, session TEXT, observed_at TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS advisories_session ON advisories(session);''')
            row = c.execute('SELECT version FROM meta WHERE id=1').fetchone()
            if row and row['version'] != version:
                raise ValueError('Research ledger version changed: start a new experiment file.')
            c.execute('INSERT OR IGNORE INTO meta VALUES(1,?)', (version,))

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def newest_session(self):
        """Newest session that actually produced advisories."""
        with self.connection() as c:
            return c.execute('SELECT max(session) FROM cycles').fetchone()[0]

    def has_advisories(self, session):
        with self.connection() as c:
            return bool(c.execute('SELECT 1 FROM advisories WHERE session=? LIMIT 1',
                                  (session,)).fetchone())

    def prepare_decision(self, session, payload):
        """Persist the cycle's decision immutably and return what is stored.

        The first writer wins. A retry gets the ORIGINAL decision back, which is
        what makes partial recovery idempotent: the shadow books are then brought
        forward on the targets, prices and observation time that were actually
        decided, never on newly fetched data.

        Returns (stored_payload, created) where created is False on a replay.
        """
        blob = json.dumps(clean(payload), allow_nan=False, default=str, sort_keys=True)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            cursor = c.execute(
                '''INSERT OR IGNORE INTO decisions(session,observed_at,core_session,
                   core_observed_at,intake_sha256,payload) VALUES(?,?,?,?,?,?)''',
                (session, payload['observed_at'], payload.get('core_session'),
                 payload.get('core_observed_at'), payload.get('intake_sha256'), blob))
            created = bool(cursor.rowcount and cursor.rowcount > 0)
            row = c.execute('SELECT payload FROM decisions WHERE session=?', (session,)).fetchone()
        return json.loads(row['payload']), created

    def decision(self, session):
        with self.connection() as c:
            row = c.execute('SELECT payload FROM decisions WHERE session=?', (session,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row['payload'])
        except (TypeError, ValueError):
            return None

    def record_hold(self, session, observed_at, reason):
        """Append a cycle that declined to advise. Never overwrites anything."""
        with self.connection() as c:
            c.execute('INSERT INTO holds(session,observed_at,reason) VALUES(?,?,?)',
                      (session, observed_at, reason))

    def holds(self, limit=20):
        with self.connection() as c:
            return [dict(r) for r in c.execute(
                'SELECT * FROM holds ORDER BY id DESC LIMIT ?', (limit,))]

    def record(self, session, observed_at, findings, digest, *, outcome, core_session=None,
               core_observed_at=None, intake_meta=None, used_sha256=None):
        """Persist one cycle. Returns the number of advisory rows actually added."""
        intake_meta = intake_meta or {}
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            added = 0
            for finding in findings:
                cursor = c.execute(
                    '''INSERT OR IGNORE INTO advisories(session,role,symbol,observed_at,
                       session_age_hours,verdict,evidence,observations_used,proposed_action,
                       abstained,abstention_reason,source)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (session, finding['role'], finding['symbol'], finding['observed_at'],
                     finding['session_age_hours'], finding['verdict'],
                     json.dumps(clean(finding['evidence']), allow_nan=False, default=str),
                     finding['observations_used'], finding['proposed_action'],
                     int(finding['abstained']), finding['abstention_reason'], finding['source']))
                added += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            c.execute('''INSERT OR IGNORE INTO cycles(session,observed_at,core_session,
                         core_observed_at,intake_fetched_at,intake_cached,intake_sha256,
                         used_sha256,source,symbols,findings,abstentions,outcome)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (session, observed_at, core_session, core_observed_at,
                       intake_meta.get('fetched_at'), int(bool(intake_meta.get('cached'))),
                       intake_meta.get('sha256'), used_sha256, intake_meta.get('source'),
                       len({f['symbol'] for f in findings}), len(findings),
                       sum(1 for f in findings if f['abstained']), outcome))
            if digest is not None:
                c.execute('INSERT OR IGNORE INTO digests VALUES(?,?,?)',
                          (session, observed_at,
                           json.dumps(clean(digest), allow_nan=False, default=str)))
            return added

    def latest_session(self):
        with self.connection() as c:
            row = c.execute('SELECT * FROM cycles ORDER BY session DESC LIMIT 1').fetchone()
            return dict(row) if row else None

    def advisories(self, session=None, limit=500):
        with self.connection() as c:
            if session is None:
                row = c.execute('SELECT max(session) FROM advisories').fetchone()
                session = row[0] if row else None
            if session is None:
                return []
            rows = c.execute('''SELECT * FROM advisories WHERE session=?
                                ORDER BY role, symbol LIMIT ?''', (session, limit)).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                try:
                    item['evidence'] = json.loads(item['evidence'])
                except (TypeError, ValueError):
                    item['evidence'] = {}
                item['abstained'] = bool(item['abstained'])
                out.append(item)
            return out

    def digest(self, session=None):
        with self.connection() as c:
            if session is None:
                row = c.execute('SELECT * FROM digests ORDER BY session DESC LIMIT 1').fetchone()
            else:
                row = c.execute('SELECT * FROM digests WHERE session=?', (session,)).fetchone()
            if not row:
                return None
            try:
                return json.loads(row['payload'])
            except (TypeError, ValueError):
                return None

    def cycles(self, limit=50):
        with self.connection() as c:
            return [dict(r) for r in c.execute(
                'SELECT * FROM cycles ORDER BY session DESC LIMIT ?', (limit,))]

    def counts(self):
        with self.connection() as c:
            return {'cycles': c.execute('SELECT count(*) FROM cycles').fetchone()[0],
                    'decisions': c.execute('SELECT count(*) FROM decisions').fetchone()[0],
                    'holds': c.execute('SELECT count(*) FROM holds').fetchone()[0],
                    'advisories': c.execute('SELECT count(*) FROM advisories').fetchone()[0],
                    'abstentions': c.execute('SELECT count(*) FROM advisories WHERE abstained=1').fetchone()[0],
                    'sessions': c.execute('SELECT count(DISTINCT session) FROM advisories').fetchone()[0],
                    'first_observed_at': c.execute('SELECT min(observed_at) FROM cycles').fetchone()[0],
                    'last_observed_at': c.execute('SELECT max(observed_at) FROM cycles').fetchone()[0],
                    'version': self.version}


def now_iso():
    return datetime.now(timezone.utc).isoformat()
