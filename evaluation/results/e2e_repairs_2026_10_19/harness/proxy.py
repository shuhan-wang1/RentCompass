import select,socket,socketserver
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as upstream:
            try:upstream.connect('/tmp/rentcompass-e2e-repair-20261019/runtime/web.sock')
            except OSError:return
            while True:
                ready,_,_=select.select([self.request,upstream],[],[],180)
                if not ready:return
                for source in ready:
                    data=source.recv(65536)
                    if not data:return
                    (upstream if source is self.request else self.request).sendall(data)
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True;daemon_threads=True
with Server(('127.0.0.1',15008),Handler) as server:
    print('Isolated persona test at http://127.0.0.1:15008',flush=True);server.serve_forever()
