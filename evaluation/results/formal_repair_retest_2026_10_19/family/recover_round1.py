from browser import Browser,FOLDER,URL
import json,time
b=Browser()
try:
 r={'time':time.time(),'ui':b.ui(),'conversations':b.context.request.get(URL+'/api/conversations').json(),'model_requests':b.requests,'note':'The initial browser Response.body CDP retrieval failed; the model request was not resent. This is a no-model recovery snapshot.'}
 conversations=r['conversations'].get('conversations',[])
 if conversations:
  cid=conversations[0]['id'];r['conversation_id']=cid;r['stored']=b.context.request.get(URL+'/api/conversations/'+cid+'/messages').json()
 (FOLDER/'round01-recovered.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));b.screenshot('round01-recovered');print(json.dumps({'conversation_id':r.get('conversation_id'),'stored':r.get('stored'),'ui_cards':r['ui']['card_count'],'ui_chat':r['ui']['chat'][:16000],'model_requests':len(b.requests)},ensure_ascii=False))
finally:b.close()
