"""检查容器魔数和请求区间，不解释schema或块。"""
from .io import asset,integer

def summarize(v,root):
    raw,digest=asset(root,v['source'])
    if raw[:4]!=b'Obj\x01':raise ValueError('不是Avro对象容器')
    for a,b in v['record_ranges']:
        if integer(a)>integer(b):raise ValueError('记录区间逆序')
    return {'raw_bytes':len(raw),'sha256':digest,'requested_paths':v['paths'],'record_ranges':v['record_ranges']}
