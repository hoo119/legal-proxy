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

PORT = int(os.environ.get('PORT', 8000))  # Render가 자동으로 PORT 환경변수를 지정해줌
ALLOWED_HOSTS = ("www.law.go.kr", "law.go.kr")  # 법제처 API 외의 임의 URL 프록시 방지


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
