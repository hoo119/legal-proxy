# -*- coding: utf-8 -*-
"""
법률 AI 어시스턴트용 클라우드 프록시 서버 (Render.com 배포용, Supabase/PostgreSQL 코퍼스 연동판)
--------------------------------------------------------------------------------
"""

import http.server
import socketserver
import urllib.request
import urllib.parse
import os
import sys
import json
import time
import threading

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
except ImportError:
    psycopg2 = None  # Build Command에 pip install psycopg2-binary가 빠지면 여기서 걸림

PORT = int(os.environ.get('PORT', 8000))
ALLOWED_HOSTS = ("www.law.go.kr", "law.go.kr")  # 법제처 API 외의 임의 URL 프록시 방지

GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
GOOGLE_CX = os.environ.get('GOOGLE_CX', '')

SUPABASE_DB_URL = os.environ.get('SUPABASE_DB_URL', '').strip()

# ---------------- Postgres 연결 풀 ----------------
# 매 요청마다 새 TCP 연결을 맺지 않도록 스레드 안전한 커넥션 풀을 하나 만들어 재사용한다.
_pg_pool = None
_pg_pool_lock = threading.Lock()


def get_pg_pool():
    global _pg_pool
    if not psycopg2:
        raise RuntimeError("psycopg2 미설치. Render Build Command를 'pip install psycopg2-binary'로 설정하세요.")
    if not SUPABASE_DB_URL:
        raise RuntimeError("SUPABASE_DB_URL 환경변수가 설정되지 않았습니다.")
    if _pg_pool is None:
        with _pg_pool_lock:
            if _pg_pool is None:
                _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=SUPABASE_DB_URL)
    return _pg_pool


class PgConn:
    """with문으로 커넥션을 풀에서 빌렸다가 자동 반납하기 위한 헬퍼."""
    def __enter__(self):
        self.pool = get_pg_pool()
        self.conn = self.pool.getconn()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            try:
                self.conn.rollback()
            except Exception:
                pass
        self.pool.putconn(self.conn)


_schema_ready = False
_schema_lock = threading.Lock()

# corpus_count_precedents는 COUNT/SUM 집계라서 인덱스가 있어도 매번 테이블 전체를
# 훑어야 한다. 클라이언트가 몇 초~몇십 초 간격으로 반복 호출해도 실제 DB 쿼리는
# COUNT_CACHE_TTL_SECONDS에 한 번만 나가도록 짧게 캐싱해서 불필요한 부하를 줄인다.
_count_cache = {'data': None, 'ts': 0}
COUNT_CACHE_TTL_SECONDS = 20


def ensure_schema():
    # 서버가 켜져 있는 동안 한 번만 스키마를 만든다 (매 요청마다 재실행하지 않음)
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with PgConn() as conn:
            cur = conn.cursor()
            try:
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS articles (
                        id SERIAL PRIMARY KEY,
                        law_name TEXT NOT NULL,
                        article_no TEXT NOT NULL,
                        content TEXT NOT NULL,
                        UNIQUE(law_name, article_no)
                    )
                ''')
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS precedents (
                        id SERIAL PRIMARY KEY,
                        case_id TEXT UNIQUE NOT NULL,
                        case_no TEXT,
                        case_name TEXT,
                        court TEXT,
                        date TEXT,
                        summary TEXT
                    )
                ''')
                # 1단계(목록 수집)가 "어느 범위를 몇 페이지까지 했는지"를 브라우저
                # localStorage가 아니라 서버에 저장해둔다. 이러면 다른 PC에서 같은
                # collector.html을 열어도 항상 정확한 지점부터 이어서 시작할 수 있다.
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS collection_progress (
                        scope_key TEXT PRIMARY KEY,
                        next_page INTEGER NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                ''')
                # 부분 인덱스: summary가 비어있는(=상세조회 대상인) 행만 인덱싱.
                # pending 조회가 LIMIT과 함께 이 인덱스를 타면, 전체 테이블을
                # 스캔하지 않고 필요한 건수만 읽는다.
                cur.execute('''
                    CREATE INDEX IF NOT EXISTS idx_precedents_pending
                    ON precedents (id) WHERE summary IS NULL OR summary = ''
                ''')
                cur.execute('''
                    CREATE INDEX IF NOT EXISTS idx_precedents_court
                    ON precedents (court)
                ''')
                # 전문 검색용 GIN 인덱스 ('simple' 설정: 형태소 분석 없이 토큰 매칭만 함,
                # 이전 SQLite FTS5 unicode61 토크나이저와 비슷한 수준의 단순 매칭)
                cur.execute('''
                    CREATE INDEX IF NOT EXISTS idx_articles_fts
                    ON articles USING GIN (to_tsvector('simple', law_name || ' ' || content))
                ''')
                cur.execute('''
                    CREATE INDEX IF NOT EXISTS idx_precedents_fts
                    ON precedents USING GIN (
                        to_tsvector('simple', coalesce(case_name, '') || ' ' || coalesce(summary, ''))
                    )
                ''')
                conn.commit()
                _schema_ready = True
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()


def to_tsquery_expr(query_text):
    """공백으로 구분된 검색어들을 OR로 묶은 tsquery 문자열을 만든다."""
    terms = [t.strip() for t in query_text.split() if t.strip()]
    if not terms:
        return None
    # 특수문자가 tsquery 문법과 충돌하지 않도록 단순 치환
    safe_terms = [t.replace("'", "").replace(':', '').replace('&', '').replace('|', '').replace('!', '') for t in terms]
    safe_terms = [t for t in safe_terms if t]
    if not safe_terms:
        return None
    return ' | '.join(safe_terms)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self._cors()
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        try:
            self._handle_get()
        except Exception as e:
            try:
                self.send_response(500)
                self._cors()
                self.end_headers()
                self.wfile.write(('내부 오류: ' + str(e)).encode('utf-8'))
            except Exception:
                pass

    def _handle_get(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == '/':
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write('법률 AI 어시스턴트 프록시 서버가 정상 동작 중입니다.'.encode('utf-8'))
            return

        if parsed.path == '/web_search':
            self._handle_web_search(parsed)
            return

        if parsed.path == '/corpus_search_articles':
            self._handle_corpus_search_articles(parsed)
            return

        if parsed.path == '/corpus_search_precedents':
            self._handle_corpus_search_precedents(parsed)
            return

        if parsed.path == '/corpus_pending_precedents':
            self._handle_corpus_pending_precedents(parsed)
            return

        if parsed.path == '/corpus_count_precedents':
            self._handle_corpus_count_precedents(parsed)
            return

        if parsed.path == '/corpus_count_articles':
            self._handle_corpus_count_articles(parsed)
            return

        if parsed.path == '/corpus_get_progress':
            self._handle_corpus_get_progress(parsed)
            return

        if parsed.path != '/proxy':
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get('url', [None])[0]

        if not target:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('url 파라미터가 없습니다.'.encode('utf-8'))
            return

        target_host = urllib.parse.urlparse(target).hostname or ''
        if target_host not in ALLOWED_HOSTS:
            self.send_response(403)
            self._cors()
            self.end_headers()
            self.wfile.write('허용되지 않은 대상 호스트입니다.'.encode('utf-8'))
            return

        try:
            req = urllib.request.Request(
                target,
                headers={'User-Agent': 'Mozilla/5.0 (legal-ai-assistant cloud proxy)'}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()

            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/xml; charset=utf-8')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self._cors()
            self.end_headers()
            self.wfile.write(('법제처 API 호출 실패: ' + str(e)).encode('utf-8'))

    def do_POST(self):
        try:
            self._handle_post()
        except Exception as e:
            try:
                self.send_response(500)
                self._cors()
                self.end_headers()
                self.wfile.write(('내부 오류: ' + str(e)).encode('utf-8'))
            except Exception:
                pass

    def _handle_post(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b'{}'
        try:
            body = json.loads(raw.decode('utf-8'))
        except Exception:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('JSON 파싱 실패'.encode('utf-8'))
            return

        if parsed.path == '/corpus_save_article':
            self._handle_corpus_save_article(body)
            return
        if parsed.path == '/corpus_save_precedent':
            self._handle_corpus_save_precedent(body)
            return
        if parsed.path == '/corpus_save_precedents_batch':
            self._handle_corpus_save_precedents_batch(body)
            return
        if parsed.path == '/corpus_save_articles_batch':
            self._handle_corpus_save_articles_batch(body)
            return
        if parsed.path == '/corpus_set_progress':
            self._handle_corpus_set_progress(body)
            return

        self.send_response(404)
        self._cors()
        self.end_headers()

    # ---------------- 웹 검색 (발견용) ----------------
    def _handle_web_search(self, parsed):
        if not GOOGLE_API_KEY or not GOOGLE_CX:
            self.send_response(500)
            self._cors()
            self.end_headers()
            self.wfile.write('Google 검색 API 키가 서버에 설정되어 있지 않습니다.'.encode('utf-8'))
            return

        qs = urllib.parse.parse_qs(parsed.query)
        query = qs.get('query', [None])[0]
        num = qs.get('num', ['5'])[0]
        if not query:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('query 파라미터가 없습니다.'.encode('utf-8'))
            return
        try:
            num_int = max(1, min(int(num), 10))
        except ValueError:
            num_int = 5

        search_url = ('https://www.googleapis.com/customsearch/v1'
                      + '?key=' + urllib.parse.quote(GOOGLE_API_KEY)
                      + '&cx=' + urllib.parse.quote(GOOGLE_CX)
                      + '&q=' + urllib.parse.quote(query)
                      + '&num=' + str(num_int))
        try:
            req = urllib.request.Request(search_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self._cors()
            self.end_headers()
            self.wfile.write(('Google 검색 API 호출 실패: ' + str(e)).encode('utf-8'))

    # ---------------- 코퍼스 (Supabase/PostgreSQL) ----------------
    def _handle_corpus_search_articles(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        query = qs.get('query', [None])[0]
        law = qs.get('law', [None])[0]
        limit = qs.get('limit', ['5'])[0]
        try:
            limit_num = max(1, min(int(limit), 20))
        except ValueError:
            limit_num = 5

        tsq = to_tsquery_expr(query or '')
        items = []
        if tsq:
            ensure_schema()
            with PgConn() as conn:
                cur = conn.cursor()
                try:
                    sql = (
                        "SELECT law_name, article_no, content, "
                        "ts_rank(to_tsvector('simple', law_name || ' ' || content), to_tsquery('simple', %s)) AS rank "
                        "FROM articles WHERE to_tsvector('simple', law_name || ' ' || content) @@ to_tsquery('simple', %s)"
                    )
                    params = [tsq, tsq]
                    if law:
                        sql += " AND law_name = %s"
                        params.append(law)
                    sql += " ORDER BY rank DESC LIMIT %s"
                    params.append(limit_num)
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                    items = [{'law_name': r[0], 'article_no': r[1], 'content': r[2]} for r in rows]
                finally:
                    cur.close()

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'items': items}, ensure_ascii=False).encode('utf-8'))

    def _handle_corpus_search_precedents(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        query = qs.get('query', [None])[0]
        limit = qs.get('limit', ['8'])[0]
        try:
            limit_num = max(1, min(int(limit), 30))
        except ValueError:
            limit_num = 8

        tsq = to_tsquery_expr(query or '')
        items = []
        if tsq:
            ensure_schema()
            with PgConn() as conn:
                cur = conn.cursor()
                try:
                    cur.execute(
                        "SELECT case_id, case_no, case_name, court, date, summary, "
                        "ts_rank(to_tsvector('simple', coalesce(case_name,'') || ' ' || coalesce(summary,'')), to_tsquery('simple', %s)) AS rank "
                        "FROM precedents "
                        "WHERE to_tsvector('simple', coalesce(case_name,'') || ' ' || coalesce(summary,'')) @@ to_tsquery('simple', %s) "
                        "ORDER BY rank DESC LIMIT %s",
                        [tsq, tsq, limit_num]
                    )
                    rows = cur.fetchall()
                    items = [{'case_id': r[0], 'case_no': r[1], 'case_name': r[2],
                              'court': r[3], 'date': r[4], 'summary': r[5]} for r in rows]
                finally:
                    cur.close()

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'items': items}, ensure_ascii=False).encode('utf-8'))

    def _handle_corpus_pending_precedents(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        limit = qs.get('limit', ['20'])[0]
        try:
            limit_num = max(1, min(int(limit), 100))
        except ValueError:
            limit_num = 20

        ensure_schema()
        with PgConn() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT case_id, case_no FROM precedents WHERE summary IS NULL OR summary = '' LIMIT %s",
                    [limit_num]
                )
                rows = cur.fetchall()
                items = [{'case_id': r[0], 'case_no': r[1]} for r in rows]
            finally:
                cur.close()

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'items': items}, ensure_ascii=False).encode('utf-8'))

    def _handle_corpus_get_progress(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        scope = (qs.get('scope', [None])[0] or '').strip()
        if not scope:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('scope 파라미터가 필요합니다.'.encode('utf-8'))
            return

        ensure_schema()
        with PgConn() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT next_page FROM collection_progress WHERE scope_key = %s", [scope])
                row = cur.fetchone()
            finally:
                cur.close()

        next_page = row[0] if row else 1
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'next_page': next_page}).encode('utf-8'))

    def _handle_corpus_set_progress(self, body):
        scope = (body.get('scope') or '').strip()
        next_page = body.get('next_page')
        if not scope or not isinstance(next_page, int):
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('scope, next_page(정수)가 필요합니다.'.encode('utf-8'))
            return

        ensure_schema()
        with PgConn() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    'INSERT INTO collection_progress (scope_key, next_page, updated_at) VALUES (%s, %s, now()) '
                    'ON CONFLICT (scope_key) DO UPDATE SET next_page = EXCLUDED.next_page, updated_at = now()',
                    [scope, next_page]
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

        self.send_response(200)
        self._cors()
        self.end_headers()
        self.wfile.write(b'ok')

    def _handle_corpus_count_precedents(self, parsed):
        now = time.time()
        if _count_cache['data'] is not None and (now - _count_cache['ts']) < COUNT_CACHE_TTL_SECONDS:
            payload = _count_cache['data']
        else:
            ensure_schema()
            with PgConn() as conn:
                cur = conn.cursor()
                try:
                    cur.execute(
                        "SELECT "
                        "COUNT(*), "
                        "SUM(CASE WHEN summary IS NULL OR summary = '' THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN court = '대법원' THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN court = '대법원' AND (summary IS NULL OR summary = '') THEN 1 ELSE 0 END) "
                        "FROM precedents"
                    )
                    row = cur.fetchone()
                finally:
                    cur.close()

            total = row[0] or 0
            pending = row[1] or 0
            supreme_total = row[2] or 0
            supreme_pending = row[3] or 0
            lower_total = total - supreme_total
            lower_pending = pending - supreme_pending

            payload = {
                'total': total, 'pending': pending, 'done': total - pending,
                'supreme': {'total': supreme_total, 'pending': supreme_pending, 'done': supreme_total - supreme_pending},
                'lower': {'total': lower_total, 'pending': lower_pending, 'done': lower_total - lower_pending}
            }
            _count_cache['data'] = payload
            _count_cache['ts'] = now

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def _handle_corpus_save_article(self, body):
        law = (body.get('law') or '').strip()
        no = (body.get('no') or '').strip()
        content = (body.get('content') or '').strip()
        if not law or not no or not content:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('law, no, content이 모두 필요합니다.'.encode('utf-8'))
            return

        ensure_schema()
        with PgConn() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    'INSERT INTO articles (law_name, article_no, content) VALUES (%s, %s, %s) '
                    'ON CONFLICT (law_name, article_no) DO UPDATE SET content = EXCLUDED.content',
                    [law, no, content]
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

        self.send_response(200)
        self._cors()
        self.end_headers()
        self.wfile.write(b'ok')

    def _handle_corpus_save_articles_batch(self, body):
        items = body.get('items') or []
        if not isinstance(items, list) or not items:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('items 배열이 필요합니다.'.encode('utf-8'))
            return

        ensure_schema()
        rows_to_upsert = []
        for it in items:
            law = (it.get('law') or '').strip()
            no = (it.get('no') or '').strip()
            content = (it.get('content') or '').strip()
            if not law or not no or not content:
                continue
            rows_to_upsert.append((law, no, content))

        saved = 0
        if rows_to_upsert:
            with PgConn() as conn:
                cur = conn.cursor()
                try:
                    psycopg2.extras.execute_values(
                        cur,
                        'INSERT INTO articles (law_name, article_no, content) VALUES %s '
                        'ON CONFLICT (law_name, article_no) DO UPDATE SET content = EXCLUDED.content',
                        rows_to_upsert
                    )
                    conn.commit()
                    saved = len(rows_to_upsert)
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    cur.close()

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'saved': saved}).encode('utf-8'))

    def _handle_corpus_count_articles(self, parsed):
        ensure_schema()
        with PgConn() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT COUNT(*), COUNT(DISTINCT law_name) FROM articles")
                row = cur.fetchone()
            finally:
                cur.close()

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({
            'total_articles': row[0] or 0,
            'total_laws': row[1] or 0
        }).encode('utf-8'))

    def _handle_corpus_save_precedent(self, body):
        case_id = (body.get('case_id') or '').strip()
        if not case_id:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('case_id가 필요합니다.'.encode('utf-8'))
            return

        ensure_schema()
        with PgConn() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    'INSERT INTO precedents (case_id, case_no, case_name, court, date, summary) '
                    'VALUES (%s, %s, %s, %s, %s, %s) '
                    'ON CONFLICT (case_id) DO UPDATE SET '
                    'case_no = EXCLUDED.case_no, case_name = EXCLUDED.case_name, '
                    'court = EXCLUDED.court, date = EXCLUDED.date, summary = EXCLUDED.summary',
                    [case_id, body.get('case_no', ''), body.get('case_name', ''),
                     body.get('court', ''), body.get('date', ''), body.get('summary', '')]
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

        self.send_response(200)
        self._cors()
        self.end_headers()
        self.wfile.write(b'ok')

    def _handle_corpus_save_precedents_batch(self, body):
        items = body.get('items') or []
        if not isinstance(items, list) or not items:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('items 배열이 필요합니다.'.encode('utf-8'))
            return

        ensure_schema()
        rows_to_upsert = []
        for it in items:
            case_id = (it.get('case_id') or '').strip()
            if not case_id:
                continue
            rows_to_upsert.append((
                case_id, it.get('case_no', ''), it.get('case_name', ''),
                it.get('court', ''), it.get('date', ''), it.get('summary', '')
            ))

        saved = 0
        if rows_to_upsert:
            with PgConn() as conn:
                cur = conn.cursor()
                try:
                    # execute_values로 여러 건을 한 번의 왕복(배치)으로 처리
                    # — 건별 요청보다 훨씬 빠르고 커넥션 부하도 적다.
                    psycopg2.extras.execute_values(
                        cur,
                        'INSERT INTO precedents (case_id, case_no, case_name, court, date, summary) VALUES %s '
                        'ON CONFLICT (case_id) DO UPDATE SET '
                        'case_no = EXCLUDED.case_no, case_name = EXCLUDED.case_name, '
                        'court = EXCLUDED.court, date = EXCLUDED.date, summary = EXCLUDED.summary',
                        rows_to_upsert
                    )
                    conn.commit()
                    saved = len(rows_to_upsert)
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    cur.close()

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'saved': saved}).encode('utf-8'))

    def log_message(self, format, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == '__main__':
    with ThreadingServer(("0.0.0.0", PORT), ProxyHandler) as httpd:
        print("서버 실행 중: 포트 %d" % PORT)
        httpd.serve_forever()
