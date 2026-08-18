# -*- coding: utf-8 -*-
"""
법률 AI 어시스턴트용 클라우드 프록시 서버 (Render.com 배포용, Turso 코퍼스 연동판)
--------------------------------------------------------------------------------

"""
 
import http.server
import socketserver
import urllib.request
import urllib.parse
import os
import sys
import json
 
try:
    import libsql_client
except ImportError:
    libsql_client = None  # Build Command에 pip install libsql-client가 빠지면 여기서 걸림
 
PORT = int(os.environ.get('PORT', 8000))
ALLOWED_HOSTS = ("www.law.go.kr", "law.go.kr")  # 법제처 API 외의 임의 URL 프록시 방지
 
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
GOOGLE_CX = os.environ.get('GOOGLE_CX', '')
 
TURSO_URL = os.environ.get('TURSO_DATABASE_URL', '').strip()
TURSO_TOKEN = os.environ.get('TURSO_AUTH_TOKEN', '').strip()
 
 
def get_turso_client():
    if not libsql_client:
        raise RuntimeError("libsql_client 미설치. Render Build Command를 'pip install libsql-client'로 설정하세요.")
    if not TURSO_URL or not TURSO_TOKEN:
        raise RuntimeError("TURSO_DATABASE_URL / TURSO_AUTH_TOKEN 환경변수가 설정되지 않았습니다.")
    # libsql_client의 동기(sync) HTTP 클라이언트는 https:// 스킴을 기대하므로 libsql:// 를 변환
    url = TURSO_URL.replace('libsql://', 'https://', 1)
    return libsql_client.create_client_sync(url=url, auth_token=TURSO_TOKEN)
 
 
_schema_ready = False
 
 
def ensure_schema():
    # 서버가 켜져 있는 동안 한 번만 스키마를 만든다 (매 요청마다 재실행하지 않음)
    global _schema_ready
    if _schema_ready:
        return
    client = get_turso_client()
    try:
        statements = [
            '''CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY,
                law_name TEXT NOT NULL,
                article_no TEXT NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(law_name, article_no)
            )''',
            '''CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
                law_name, article_no UNINDEXED, content, content='articles', content_rowid='id'
            )''',
            '''CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
                INSERT INTO articles_fts(rowid, law_name, article_no, content)
                VALUES (new.id, new.law_name, new.article_no, new.content);
            END''',
            '''CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
                INSERT INTO articles_fts(articles_fts, rowid, law_name, article_no, content)
                VALUES('delete', old.id, old.law_name, old.article_no, old.content);
                INSERT INTO articles_fts(rowid, law_name, article_no, content)
                VALUES (new.id, new.law_name, new.article_no, new.content);
            END''',
            '''CREATE TABLE IF NOT EXISTS precedents (
                id INTEGER PRIMARY KEY,
                case_id TEXT UNIQUE NOT NULL,
                case_no TEXT,
                case_name TEXT,
                court TEXT,
                date TEXT,
                summary TEXT
            )''',
            '''CREATE VIRTUAL TABLE IF NOT EXISTS precedents_fts USING fts5(
                case_no UNINDEXED, case_name, summary, content='precedents', content_rowid='id'
            )''',
            '''CREATE TRIGGER IF NOT EXISTS precedents_ai AFTER INSERT ON precedents BEGIN
                INSERT INTO precedents_fts(rowid, case_no, case_name, summary)
                VALUES (new.id, new.case_no, new.case_name, new.summary);
            END''',
            '''CREATE TRIGGER IF NOT EXISTS precedents_au AFTER UPDATE ON precedents BEGIN
                INSERT INTO precedents_fts(precedents_fts, rowid, case_no, case_name, summary)
                VALUES('delete', old.id, old.case_no, old.case_name, old.summary);
                INSERT INTO precedents_fts(rowid, case_no, case_name, summary)
                VALUES (new.id, new.case_no, new.case_name, new.summary);
            END''',
        ]
        for stmt in statements:
            client.execute(stmt)
        _schema_ready = True
    finally:
        client.close()
 
 
def fts_match_expr(query_text):
    terms = [t.strip() for t in query_text.split() if t.strip()]
    if not terms:
        return None
    escaped = ['"' + t.replace('"', '""') + '"' for t in terms]
    return ' OR '.join(escaped)
 
 
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
 
    # ---------------- 코퍼스 (Turso) ----------------
    def _handle_corpus_search_articles(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        query = qs.get('query', [None])[0]
        law = qs.get('law', [None])[0]
        limit = qs.get('limit', ['5'])[0]
        try:
            limit_num = max(1, min(int(limit), 20))
        except ValueError:
            limit_num = 5
 
        match = fts_match_expr(query or '')
        items = []
        if match:
            ensure_schema()
            client = get_turso_client()
            try:
                sql = 'SELECT law_name, article_no, content FROM articles_fts WHERE articles_fts MATCH ?'
                params = [match]
                if law:
                    sql += ' AND law_name = ?'
                    params.append(law)
                sql += ' ORDER BY bm25(articles_fts) LIMIT ?'
                params.append(limit_num)
                rs = client.execute(sql, params)
                items = [{'law_name': r[0], 'article_no': r[1], 'content': r[2]} for r in rs.rows]
            finally:
                client.close()
 
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
 
        match = fts_match_expr(query or '')
        items = []
        if match:
            ensure_schema()
            client = get_turso_client()
            try:
                rs = client.execute(
                    'SELECT p.case_id, p.case_no, p.case_name, p.court, p.date, p.summary '
                    'FROM precedents_fts JOIN precedents p ON p.id = precedents_fts.rowid '
                    'WHERE precedents_fts MATCH ? ORDER BY bm25(precedents_fts) LIMIT ?',
                    [match, limit_num]
                )
                items = [{'case_id': r[0], 'case_no': r[1], 'case_name': r[2],
                          'court': r[3], 'date': r[4], 'summary': r[5]} for r in rs.rows]
            finally:
                client.close()
 
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
        client = get_turso_client()
        try:
            rs = client.execute(
                "SELECT case_id, case_no FROM precedents WHERE summary IS NULL OR summary = '' LIMIT ?",
                [limit_num]
            )
            items = [{'case_id': r[0], 'case_no': r[1]} for r in rs.rows]
        finally:
            client.close()
 
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'items': items}, ensure_ascii=False).encode('utf-8'))
 
    def _handle_corpus_count_precedents(self, parsed):
        ensure_schema()
        client = get_turso_client()
        try:
            # 쿼리 4번을 왕복하던 것을 1번으로 합침. Turso 무료 티어에서 짧은 시간에
            # 여러 번 연속 요청하면 간헐적으로 응답이 깨져 libsql_client 내부에서
            # KeyError: 'result' 가 나는 경우가 있었는데, 왕복 횟수를 줄여서 방지한다.
            row = client.execute(
                "SELECT "
                "COUNT(*), "
                "SUM(CASE WHEN summary IS NULL OR summary = '' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN court = '대법원' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN court = '대법원' AND (summary IS NULL OR summary = '') THEN 1 ELSE 0 END) "
                "FROM precedents"
            ).rows[0]

            total = row[0] or 0
            pending = row[1] or 0
            supreme_total = row[2] or 0
            supreme_pending = row[3] or 0
            lower_total = total - supreme_total
            lower_pending = pending - supreme_pending
        finally:
            client.close()
 
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({
            'total': total, 'pending': pending, 'done': total - pending,
            'supreme': {'total': supreme_total, 'pending': supreme_pending, 'done': supreme_total - supreme_pending},
            'lower': {'total': lower_total, 'pending': lower_pending, 'done': lower_total - lower_pending}
        }).encode('utf-8'))
 
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
        client = get_turso_client()
        try:
            client.execute(
                'INSERT INTO articles (law_name, article_no, content) VALUES (?, ?, ?) '
                'ON CONFLICT(law_name, article_no) DO UPDATE SET content=excluded.content',
                [law, no, content]
            )
        finally:
            client.close()
 
        self.send_response(200)
        self._cors()
        self.end_headers()
        self.wfile.write(b'ok')
 
    def _handle_corpus_save_precedent(self, body):
        case_id = (body.get('case_id') or '').strip()
        if not case_id:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write('case_id가 필요합니다.'.encode('utf-8'))
            return
 
        ensure_schema()
        client = get_turso_client()
        try:
            client.execute(
                'INSERT INTO precedents (case_id, case_no, case_name, court, date, summary) '
                'VALUES (?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(case_id) DO UPDATE SET '
                'case_no=excluded.case_no, case_name=excluded.case_name, '
                'court=excluded.court, date=excluded.date, summary=excluded.summary',
                [case_id, body.get('case_no', ''), body.get('case_name', ''),
                 body.get('court', ''), body.get('date', ''), body.get('summary', '')]
            )
        finally:
            client.close()
 
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
        client = get_turso_client()
        saved = 0
        try:
            statements = []
            for it in items:
                case_id = (it.get('case_id') or '').strip()
                if not case_id:
                    continue
                statements.append((
                    'INSERT INTO precedents (case_id, case_no, case_name, court, date, summary) '
                    'VALUES (?, ?, ?, ?, ?, ?) '
                    'ON CONFLICT(case_id) DO UPDATE SET '
                    'case_no=excluded.case_no, case_name=excluded.case_name, '
                    'court=excluded.court, date=excluded.date, summary=excluded.summary',
                    [case_id, it.get('case_no', ''), it.get('case_name', ''),
                     it.get('court', ''), it.get('date', ''), it.get('summary', '')]
                ))
            if statements:
                # 여러 건을 한 번의 왕복(배치)으로 처리 — 건별 요청보다 훨씬 빠르고 타임아웃도 적음
                client.batch(statements)
                saved = len(statements)
        finally:
            client.close()
 
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
