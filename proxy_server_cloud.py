# -*- coding: utf-8 -*-
"""
법률 AI 어시스턴트용 클라우드 프록시 서버 (Render.com 배포용)
--------------------------------------------------------------
로컬 PC 하나에서만 쓰는 proxy_server.py 와 달리, 이 버전은 Render.com 같은
클라우드 호스팅에 올려서 '공개 URL'을 부여받아 어느 PC/사람이든 접속할 수 있게 합니다.

배포 방법 (Render.com, 무료):
1) https://github.com 에 가입 후, 새 저장소(Repository)를 만들고
   이 파일 하나만 업로드합니다. 파일 이름은 반드시 proxy_server_cloud.py 로 둡니다.
2) https://render.com 에 GitHub 계정으로 가입합니다.
3) Render 대시보드에서 "New +" -> "Web Service" -> 방금 만든 GitHub 저장소 선택
4) 설정값:
     - Runtime: Python 3
     - Build Command: (비워둠)
     - Start Command: python proxy_server_cloud.py
     - Instance Type: Free
5) "Create Web Service" 클릭 -> 몇 분 후 다음과 비슷한 공개 주소가 생깁니다:
     https://legal-ai-proxy-xxxx.onrender.com
6) legal-ai-assistant.html 파일 안에서
     var PROXY_BASE = 'http://localhost:8000';
   부분을 위에서 받은 주소로 교체합니다. 예:
     var PROXY_BASE = 'https://legal-ai-proxy-xxxx.onrender.com';
7) 이제 HTML 파일을 어느 PC에서 열어도(더블클릭해도 됩니다) 이 클라우드 프록시를 통해
   법제처 API를 호출할 수 있습니다.

주의: 무료 요금제는 15분간 요청이 없으면 서버가 잠들고, 다음 요청 시 깨어나는 데
      약 20~50초가 걸릴 수 있습니다. (첫 질문만 느리고 이후엔 빠릅니다)
"""

import http.server
import socketserver
import urllib.request
import urllib.parse
import os
import sys
import sqlite3
import json

PORT = int(os.environ.get('PORT', 8000))  # Render가 자동으로 PORT 환경변수를 지정해줌
ALLOWED_HOSTS = ("www.law.go.kr", "law.go.kr")  # 법제처 API 외의 임의 URL 프록시 방지

# Google Custom Search API 인증정보. Render 대시보드의 Environment 탭에서 환경변수로 설정하세요
# (코드에 직접 적지 않는 이유: 이 파일은 GitHub에 공개로 올라가므로, 키를 그대로 적으면 누구나 볼 수 있습니다)
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
GOOGLE_CX = os.environ.get('GOOGLE_CX', '')  # Programmable Search Engine ID

# ============================================================================
# 로컬 코퍼스(SQLite + FTS5) — 법제처 API로 검증에 성공한 조문·판례를 여기에 영구 저장해서,
# 사용할수록 우리만의 코퍼스가 쌓이도록 한다. FTS5의 bm25() 랭킹을 써서 crow-tit(Elasticsearch)와
# 같은 계열의 검색 알고리즘을 적용한다.
# 주의: Render 무료 웹서비스의 디스크는 "재배포(새 코드 push)" 시 초기화된다. 서버가 잠들었다
# 깨어나는 것만으로는 지워지지 않지만, 완전히 끊기지 않는 영구 저장을 원하면 Render의 유료
# Persistent Disk나 Turso 같은 외부 SQLite 호스팅으로 옮겨야 한다.
# ============================================================================
DB_PATH = os.environ.get('CORPUS_DB_PATH', 'corpus.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY,
        law_name TEXT NOT NULL,
        article_no TEXT NOT NULL,
        content TEXT NOT NULL,
        UNIQUE(law_name, article_no)
    )''')
    conn.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
        law_name, article_no UNINDEXED, content, content='articles', content_rowid='id'
    )''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts(rowid, law_name, article_no, content)
        VALUES (new.id, new.law_name, new.article_no, new.content);
    END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, law_name, article_no, content)
        VALUES('delete', old.id, old.law_name, old.article_no, old.content);
        INSERT INTO articles_fts(rowid, law_name, article_no, content)
        VALUES (new.id, new.law_name, new.article_no, new.content);
    END''')

    conn.execute('''CREATE TABLE IF NOT EXISTS precedents (
        id INTEGER PRIMARY KEY,
        case_id TEXT UNIQUE NOT NULL,
        case_no TEXT,
        case_name TEXT,
        court TEXT,
        date TEXT,
        summary TEXT
    )''')
    conn.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS precedents_fts USING fts5(
        case_no UNINDEXED, case_name, summary, content='precedents', content_rowid='id'
    )''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS precedents_ai AFTER INSERT ON precedents BEGIN
        INSERT INTO precedents_fts(rowid, case_no, case_name, summary)
        VALUES (new.id, new.case_no, new.case_name, new.summary);
    END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS precedents_au AFTER UPDATE ON precedents BEGIN
        INSERT INTO precedents_fts(precedents_fts, rowid, case_no, case_name, summary)
        VALUES('delete', old.id, old.case_no, old.case_name, old.summary);
        INSERT INTO precedents_fts(rowid, case_no, case_name, summary)
        VALUES (new.id, new.case_no, new.case_name, new.summary);
    END''')
    conn.commit()
    return conn


def fts_match_expr(query_text):
    # 사용자가 입력한 여러 단어를 FTS5 MATCH 문법에 맞게 "단어1" OR "단어2" 형태로 안전하게 변환
    terms = [t.strip() for t in query_text.split() if t.strip()]
    if not terms:
        return None
    escaped = ['"' + t.replace('"', '""') + '"' for t in terms]
    return ' OR '.join(escaped)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self._cors()
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        # 어떤 예외가 나더라도 서버 프로세스 자체는 절대 죽지 않도록 전체를 감쌉니다.
        # (이전 버전은 클라이언트가 응답을 기다리다 먼저 끊으면 write 과정에서 예외가 나서
        #  서버 전체가 죽는 문제가 있었고, 그 뒤 모든 요청이 CORS 헤더 없는 오류로 실패했습니다.)
        try:
            self._handle_get()
        except Exception as e:
            try:
                self.send_response(500)
                self._cors()
                self.end_headers()
                self.wfile.write(('내부 오류: ' + str(e)).encode('utf-8'))
            except Exception:
                pass  # 클라이언트가 이미 연결을 끊은 경우 등 - 조용히 무시하고 다음 요청을 받는다

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
            with urllib.request.urlopen(req, timeout=20) as resp:
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

    def _handle_web_search(self, parsed):
        # Google Custom Search API 프록시. API 키는 서버(이 프로세스)에만 있고 브라우저에는
        # 절대 노출되지 않는다. 이 결과는 "발견"용일 뿐이며, 최종 답변에 실제로 인용되는 조문·판례는
        # 반드시 /proxy를 통해 law.go.kr에서 별도로 검증한 원문만 사용해야 한다.
        if not GOOGLE_API_KEY or not GOOGLE_CX:
            self.send_response(500)
            self._cors()
            self.end_headers()
            self.wfile.write('Google 검색 API 키가 서버에 설정되어 있지 않습니다. Render 대시보드 > Environment에서 GOOGLE_API_KEY, GOOGLE_CX를 설정하세요.'.encode('utf-8'))
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
            num_int = max(1, min(int(num), 10))  # Google Custom Search는 1회 요청당 최대 10건
        except ValueError:
            num_int = 5

        search_url = ('https://www.googleapis.com/customsearch/v1'
                      + '?key=' + urllib.parse.quote(GOOGLE_API_KEY)
                      + '&cx=' + urllib.parse.quote(GOOGLE_CX)
                      + '&q=' + urllib.parse.quote(query)
                      + '&num=' + str(num_int))

        try:
            req = urllib.request.Request(
                search_url,
                headers={'User-Agent': 'Mozilla/5.0 (legal-ai-assistant cloud proxy)'}
            )
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

        self.send_response(404)
        self._cors()
        self.end_headers()

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
        if not match:
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({'items': []}).encode('utf-8'))
            return

        conn = get_db()
        try:
            sql = ('SELECT law_name, article_no, content FROM articles_fts '
                   'WHERE articles_fts MATCH ?')
            params = [match]
            if law:
                sql += ' AND law_name = ?'
                params.append(law)
            sql += ' ORDER BY bm25(articles_fts) LIMIT ?'
            params.append(limit_num)
            rows = conn.execute(sql, params).fetchall()
            items = [{'law_name': r[0], 'article_no': r[1], 'content': r[2]} for r in rows]
        finally:
            conn.close()

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
        if not match:
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({'items': []}).encode('utf-8'))
            return

        conn = get_db()
        try:
            rows = conn.execute(
                'SELECT p.case_id, p.case_no, p.case_name, p.court, p.date, p.summary '
                'FROM precedents_fts JOIN precedents p ON p.id = precedents_fts.rowid '
                'WHERE precedents_fts MATCH ? ORDER BY bm25(precedents_fts) LIMIT ?',
                [match, limit_num]
            ).fetchall()
            items = [{'case_id': r[0], 'case_no': r[1], 'case_name': r[2],
                      'court': r[3], 'date': r[4], 'summary': r[5]} for r in rows]
        finally:
            conn.close()

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'items': items}, ensure_ascii=False).encode('utf-8'))

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

        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO articles (law_name, article_no, content) VALUES (?, ?, ?) '
                'ON CONFLICT(law_name, article_no) DO UPDATE SET content=excluded.content',
                [law, no, content]
            )
            conn.commit()
        finally:
            conn.close()

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

        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO precedents (case_id, case_no, case_name, court, date, summary) '
                'VALUES (?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(case_id) DO UPDATE SET '
                'case_no=excluded.case_no, case_name=excluded.case_name, '
                'court=excluded.court, date=excluded.date, summary=excluded.summary',
                [case_id, body.get('case_no', ''), body.get('case_name', ''),
                 body.get('court', ''), body.get('date', ''), body.get('summary', '')]
            )
            conn.commit()
        finally:
            conn.close()

        self.send_response(200)
        self._cors()
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, format, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # 요청마다 별도 스레드로 처리 -> 한 요청이 문제를 일으켜도 다른 요청/서버 전체에 영향 없음
    daemon_threads = True
    allow_reuse_address = True


if __name__ == '__main__':
    with ThreadingServer(("0.0.0.0", PORT), ProxyHandler) as httpd:
        print("서버 실행 중: 포트 %d" % PORT)
        httpd.serve_forever()
