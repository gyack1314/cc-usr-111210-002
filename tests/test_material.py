"""原始清单、读取边界与既有摘要检查。"""
import json,tempfile,unittest
from pathlib import Path
from source_data.io import load,integer,asset,unique
from source_data.domain import summarize
from source_data.replay import Replay

class MaterialTests(unittest.TestCase):
    def test_normal(self):
        v,digest=load('data/normal.json');value=summarize(v,Path('data'))
        self.assertEqual(len(digest),64)
        self.assertEqual(value,summarize(json.loads(json.dumps(v)),Path('data')))
        self.assertEqual(value['record_ranges'],[[0,2]]);self.assertEqual(len(value['requested_paths']),3)
    def test_invalid_config(self):
        v,_=load('data/invalid-config.json')
        with self.assertRaises((ValueError,KeyError,TypeError)):summarize(v,Path('data'))
    def test_other_material(self):
        for p in Path('data').glob('*.json'):
            if p.name.startswith('invalid-'):continue
            with self.subTest(file=p.name):self.assertIsNotNone(summarize(load(p)[0],p.parent))
    def test_exact_file_identity(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'raw';p.write_bytes(b'abc')
            with self.assertRaises(ValueError):asset(d,{'path':'raw','bytes':3,'sha256':'wrong'})
            with self.assertRaises(ValueError):asset(d,{'path':'../other','bytes':0,'sha256':'none'})
    def test_input_values(self):
        for x in (True,-1,1.5):
            with self.assertRaises(ValueError):integer(x)
        with self.assertRaises(ValueError):unique([{'id':'x'},{'id':'x'}],'id')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'资料.json';p.write_text('[]',encoding='utf8')
            with self.assertRaises(ValueError):load(p)
    def test_replay_boundary(self):
        x=Replay([{'operation':'read','arguments':{'id':1},'response':{'raw':[1]}}])
        with self.assertRaises(ValueError):x.call('read',{'id':2})
        response=x.call('read',{'id':1});response['raw'].append(2)
        self.assertEqual(x.remaining(),0)
        with self.assertRaises(RuntimeError):x.call('read',{'id':1})
