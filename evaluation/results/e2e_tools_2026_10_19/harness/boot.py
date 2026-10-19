import socket,socketserver,threading,select,os
class Forward(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as upstream:
            upstream.connect('/audit/runtime/model.sock')
            while True:
                ready,_,_=select.select([self.request,upstream],[],[],120)
                if not ready:return
                for source in ready:
                    data=source.recv(65536)
                    if not data:return
                    (upstream if source is self.request else self.request).sendall(data)
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True;daemon_threads=True
bridge=Server(('127.0.0.1',19091),Forward)
threading.Thread(target=bridge.serve_forever,daemon=True).start()

# Keep production filtering/ranking/detail code and LLM, replacing only the
# network listing producer with a fail-closed, explicitly stale snapshot read.
from core.scraping import on_demand
original=on_demand.get_listings
def get_listings(*args,**kwargs):
    kwargs['cache_only']=True;kwargs['force_refresh']=False
    result=original(*args,**kwargs)
    if result.get('rows'):
        result['meta'].update(source='stale-cache',stale=True,message='Historical listing snapshot; live availability is unverified.')
    return result
on_demand.get_listings=get_listings
import transport_fixtures, instrumentation
transport_fixtures.install()
instrumentation.install()
import uvicorn
uvicorn.run('uk_rent_agent.web.asgi:create_asgi_app',factory=True,uds='/audit/runtime/web.sock',log_level='info')
