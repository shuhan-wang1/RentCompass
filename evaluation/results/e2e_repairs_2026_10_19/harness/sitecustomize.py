"""Test-only fail-closed network guard, loaded before any application imports."""
import socket,json,time,threading,os
_connect=socket.socket.connect;_connect_ex=socket.socket.connect_ex;_gai=socket.getaddrinfo
_lock=threading.Lock()
def deny(host):
    host=str(host)
    if host in ('127.0.0.1','localhost','::1','0.0.0.0'):return False
    with _lock:
        with open('/audit/private/blocked_network.jsonl','a') as f:f.write(json.dumps({'time':time.time(),'host':host})+'\n')
    return True
def connect(self,address):
    if self.family in (socket.AF_INET,socket.AF_INET6) and deny(address[0]):raise OSError('External network disabled by isolated user-test harness')
    return _connect(self,address)
def connect_ex(self,address):
    if self.family in (socket.AF_INET,socket.AF_INET6) and deny(address[0]):return 101
    return _connect_ex(self,address)
def gai(host,*args,**kwargs):
    if host is not None and deny(host):raise socket.gaierror('External DNS disabled by isolated user-test harness')
    return _gai(host,*args,**kwargs)
socket.socket.connect=connect;socket.socket.connect_ex=connect_ex;socket.getaddrinfo=gai
