"""原始资料身份与配置读取。"""
import hashlib,json,math
from pathlib import Path

def load(path):
    raw=Path(path).read_bytes()
    value=json.loads(raw.decode('utf8'),parse_constant=lambda x:(_ for _ in ()).throw(ValueError('不接受非有限数值')))
    if not isinstance(value,dict):raise ValueError('资料清单应为对象')
    return value,hashlib.sha256(raw).hexdigest()

def integer(x,minimum=0):
    if type(x) is not int or x<minimum:raise ValueError('整数越界')
    return x

def asset(root,ref):
    base=Path(root).resolve();p=(base/ref['path']).resolve()
    if not p.is_relative_to(base):raise ValueError('资料路径越界')
    raw=p.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    if len(raw)!=integer(ref['bytes']) or digest!=ref['sha256']:raise ValueError('原文件身份不符')
    return raw,digest

def unique(rows,key):
    seen=set()
    for row in rows:
        x=row[key]
        if not isinstance(x,str) or not x or x in seen:raise ValueError('身份缺失或重复')
        seen.add(x)
    return seen
