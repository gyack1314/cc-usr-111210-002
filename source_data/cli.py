"""显示清单身份、原始来源概况与 OCF 块索引/投影采集结果。"""
import argparse
import json
from pathlib import Path

from .io import load
from .domain import summarize
from .container import collect, decode_all, Limits, OcfError, TruncatedOcf


def _limits(v):
    return Limits(
        max_block_bytes=v["max_block_bytes"],
        max_uncompressed_bytes=v["max_uncompressed_bytes"],
        max_depth=v["max_depth"],
    )


def _resolve_manifest(path):
    v, digest = load(path)
    root = Path(path).parent
    return v, root, digest


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["inspect", "blocks", "project"])
    p.add_argument("file")
    a = p.parse_args()
    v, root, digest = _resolve_manifest(a.file)
    if a.command == "inspect":
        print(json.dumps(
            {"source_sha256": digest,
             "summary": summarize(v, root)},
            ensure_ascii=False, indent=2, allow_nan=False))
        return
    # blocks/project 需要读取并冻结原文件身份
    from .io import asset
    raw, raw_digest = asset(root, v["source"])
    limits = _limits(v)
    if a.command == "blocks":
        result = decode_all(raw, limits)
    else:
        result = collect(raw, v["paths"], v["record_ranges"], limits)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False,
                     default=_json_default))


def _json_default(obj):
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    raise TypeError("不可序列化的类型: %r" % type(obj))


if __name__ == "__main__":
    main()
