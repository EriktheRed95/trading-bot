import json,re,tempfile,threading,unittest
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from paper_book import PaperBook
from trading_app import Controller,make_server

class StockStub:
 def __init__(self):self.paused=False;self.calls=0
 def status(self):return {'paused':self.paused,'accounts':[]}
 def pause(self,p):self.paused=p
 def request_cycle(self,should_pause):
  if should_pause():return False
  self.calls+=1;return True
 def record(self,key):
  if key!='known':raise KeyError(key)
  return {'observations':[],'trades':[]}

class Routes(unittest.TestCase):
 def test_stock_protection_pause_and_record_routes(self):
  with tempfile.TemporaryDirectory() as temp:
   stocks=StockStub();book=PaperBook(Path(temp)/'core.db');server=make_server(Controller(book,stocks=stocks),0)
   threading.Thread(target=server.serve_forever,daemon=True).start();base=f'http://127.0.0.1:{server.server_port}'
   try:
    page=urlopen(base).read().decode();token=re.search("const token='([^']+)'",page)[1]
    def post(path,data,authorized=True):
     headers={'Content-Type':'application/json','Origin':base}
     if authorized:headers['X-Paper-Token']=token
     return json.load(urlopen(Request(base+path,data=json.dumps(data).encode(),headers=headers)))
    with self.assertRaises(HTTPError) as err:post('/api/stock-cycle',{},False)
    self.assertEqual(err.exception.code,403);self.assertEqual(stocks.calls,0)
    self.assertTrue(post('/api/stock-cycle',{})['started'])
    post('/api/pause',{'paused':True});self.assertTrue(stocks.paused)
    self.assertFalse(post('/api/stock-cycle',{})['started']);self.assertEqual(stocks.calls,1)
    with self.assertRaises(HTTPError) as err:post('/api/stock-pause',{'paused':False})
    self.assertEqual(err.exception.code,409)
    with self.assertRaises(HTTPError) as err:post('/api/stock-pause',{'paused':'false'})
    self.assertEqual(err.exception.code,400)
    self.assertEqual(json.load(urlopen(base+'/api/record?account=stocks:known'))['trades'],[])
    with self.assertRaises(HTTPError) as err:urlopen(base+'/api/record?account=stocks:unknown')
    self.assertEqual(err.exception.code,404)
    post('/api/pause',{'paused':False});self.assertFalse(stocks.paused)
   finally:server.shutdown();server.server_close()
