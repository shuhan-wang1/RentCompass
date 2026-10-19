from browser import Browser,FOLDER
import sys,json,pathlib
p=pathlib.Path(sys.argv[1]);b=Browser()
try:
 r=b.send(p.read_text(),p.stem);q=(r.get('result')or{}).get('payload')or{}
 print(json.dumps({'round':p.stem,'seconds':r['seconds'],'network_request_count':r['network_request_count'],'response_status':r['response_status'],'response_ids':r['response_ids'],'payload':{k:v for k,v in q.items() if k in ['conversation_id','turn_id','message','response_type','options']},'criteria':{k:v for k,v in q.get('view_state',{}).get('criteria',q.get('search_criteria',{})).items() if k not in ['constraint_sources','plans']},'recommendations':[{'address':x.get('address'),'area':x.get('area'),'price':x.get('price'),'url':x.get('url'),'qualification':x.get('qualification')} for x in q.get('recommendations',[])][:3],'cards':r['after']['card_count'],'focus':r['after']['focus'],'favorite_count':r['after']['favorite_count']},ensure_ascii=False))
finally:b.close()
