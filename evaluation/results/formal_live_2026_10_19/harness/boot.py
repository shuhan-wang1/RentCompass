import pathlib,os,socket,socketserver,threading,select
B=pathlib.Path('/tmp/rentcompass-live-20261019')
class Forward(socketserver.BaseRequestHandler):
 def handle(self):
  with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as upstream:
   upstream.connect(str(B/'runtime/model.sock'))
   while True:
    ready,_,_=select.select([self.request,upstream],[],[],120)
    if not ready:return
    for source in ready:
     data=source.recv(65536)
     if not data:return
     (upstream if source is self.request else self.request).sendall(data)
class Server(socketserver.ThreadingTCPServer):allow_reuse_address=True;daemon_threads=True
bridge=Server(('127.0.0.1',19093),Forward);threading.Thread(target=bridge.serve_forever,daemon=True).start()
import network_guard,instrumentation
network_guard.install();instrumentation.install()
import uvicorn
uvicorn.run('uk_rent_agent.web.asgi:create_asgi_app',factory=True,host='127.0.0.1',port=15009,log_level='info')
