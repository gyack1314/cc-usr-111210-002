"""按 writer schema 解码、跳过与投影。

一次遍历同时完成两件事：未选字段严格按 schema 跳过，选中字段采集投影值。
跳过与采集共用同一套字节推进逻辑，杜绝把未选字段字节误认成下一字段。
"""
from .ocf import (
    ByteReader,
    OcfError,
    PathToken,
    build_schema,
    resolve,
)

MISSING = object()  # 路径与实际类型不匹配时的占位，与显式 null 区分


class Budget:
    """记录数、深度与字节预算，超限保留未完成而不是伪造空块。"""

    __slots__ = ("max_depth", "depth")

    def __init__(self, max_depth):
        self.max_depth = max_depth
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        if self.depth > self.max_depth:
            raise OcfError("嵌套深度超过预算")
        return self

    def __exit__(self, *exc):
        self.depth -= 1


def _is_union(schema):
    node = schema.node
    return isinstance(node, list) or (
        isinstance(node, dict) and isinstance(node.get("type"), list))


def _union_branches(schema):
    node = schema.node
    return node if isinstance(node, list) else node["type"]


def skip_value(reader, schema, names, budget):
    """只推进字节，不构造值；所有类型都必须能正确跳过。"""
    schema = resolve(schema, names)
    t = schema.type
    with budget:
        if t == "null":
            return
        if t == "boolean":
            reader.take(1)
        elif t in ("int", "long"):
            reader.read_zigzag_long(32 if t == "int" else 64)
        elif t == "float":
            reader.take(4)
        elif t == "double":
            reader.take(8)
        elif t == "bytes" or t == "string":
            n = reader.read_count()
            reader.take(n)
            if t == "string":
                # 跳过也要核对 UTF-8，避免坏编码被静默吞掉
                reader.buf[reader.pos - n:reader.pos].decode("utf-8")
        elif t == "fixed":
            reader.take(schema.node["size"])
        elif t == "enum":
            idx = reader.read_zigzag_long()
            if idx < 0 or idx >= len(schema.node["symbols"]):
                raise OcfError("enum 索引越界")
        elif t == "array":
            _scan_blocks(reader, schema.node["items"], names, budget, mapped=False)
        elif t == "map":
            _scan_blocks(reader, schema.node["values"], names, budget, mapped=True)
        elif t == "record":
            for field in schema.node["fields"]:
                skip_value(reader, build_schema(field["type"], names=names),
                           names, budget)
        elif _is_union(schema):
            branches = _union_branches(schema)
            idx = reader.read_zigzag_long()
            if idx < 0 or idx >= len(branches):
                raise OcfError("union 分支索引越界")
            skip_value(reader, build_schema(branches[idx], names=names),
                       names, budget)
        else:
            raise OcfError("未知 schema 类型: " + repr(t))


def _scan_blocks(reader, child_node, names, budget, mapped):
    """array/map 的 0 终止、负计数附带字节块规则；字节长度必须精确覆盖本块。

    负计数块的声明字节长度只覆盖本块条目，不含后续终止符。若块恰好结束在
    当前字节窗口末尾（常见于 record 最后一个字段），不再强读终止符。
    """
    while True:
        if reader.pos >= reader.end:
            return
        count = reader.read_zigzag_long()
        if count == 0:
            return
        if count < 0:
            block_size = reader.read_count()
            block_end = reader.pos + block_size
            for _ in range(-count):
                if mapped:
                    _read_key(reader)
                skip_value(reader, build_schema(child_node, names=names),
                           names, budget)
            if reader.pos != block_end:
                raise OcfError("负计数块声明字节长度与实际不一致")
        else:
            for _ in range(count):
                if mapped:
                    _read_key(reader)
                skip_value(reader, build_schema(child_node, names=names),
                           names, budget)


def _read_key(reader):
    n = reader.read_count()
    raw = reader.take(n)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise OcfError("map 键不是合法 UTF-8") from None


# --------------------------------------------------------------------------- #
# 投影采集
# --------------------------------------------------------------------------- #
def project_block(payload, root_schema, names, projection, max_depth, record_limit):
    """对一个已解压块的全部记录做投影；返回 (投影行列表, 已解码记录数)。"""
    reader = ByteReader(payload)
    budget = Budget(max_depth)
    rows = []
    decoded = 0
    while reader.remaining():
        if record_limit is not None and decoded >= record_limit:
            raise OcfError("块内记录数超过上限")
        row = _project_record(reader, root_schema, names, projection.trie, budget)
        rows.append(row)
        decoded += 1
    return rows, decoded


def _project_record(reader, schema, names, trie, budget):
    schema = resolve(schema, names)
    if schema.type != "record":
        raise OcfError("容器顶层 schema 必须是 record")
    with budget:
        out = {}
        fields = schema.node["fields"]
        selected = {tok.name: sub for tok, sub in trie.items()
                    if tok.kind == "field"}
        for field in fields:
            fname = field["name"]
            sub = selected.get(fname)
            child_schema = build_schema(field["type"], names=names)
            if sub is None:
                skip_value(reader, child_schema, names, budget)
                continue
            value = _project_value(reader, child_schema, names, sub, budget)
            if value is not MISSING:
                out[fname] = value
        return out


def _project_value(reader, schema, names, trie, budget):
    schema = resolve(schema, names)
    t = schema.type
    with budget:
        # 路径终点：完整取值
        if None in trie:
            return _read_full(reader, schema, names, budget)
        if t == "record":
            out = {}
            selected = {tok.name: sub for tok, sub in trie.items()
                        if tok.kind == "field"}
            for field in schema.node["fields"]:
                sub = selected.get(field["name"])
                child_schema = build_schema(field["type"], names=names)
                if sub is None:
                    skip_value(reader, child_schema, names, budget)
                else:
                    v = _project_value(reader, child_schema, names, sub, budget)
                    if v is not MISSING:
                        out[field["name"]] = v
            return out
        if t == "array":
            want = trie.get(PathToken("array_each"))
            if want is None:
                skip_value(reader, schema, names, budget)
                return MISSING
            return _project_array(reader, schema, names, want, budget)
        if t == "map":
            want = trie.get(PathToken("map_each"))
            if want is None:
                skip_value(reader, schema, names, budget)
                return MISSING
            return _project_map(reader, schema, names, want, budget)
        if _is_union(schema):
            return _project_union(reader, schema, names, trie, budget)
        # 路径要求继续深入，但当前值是叶子类型：跳过并标记不匹配
        skip_value(reader, schema, names, budget)
        return MISSING


def _project_array(reader, schema, names, sub, budget):
    items_node = schema.node["items"]
    out = []
    index = 0
    while reader.pos < reader.end:
        count = reader.read_zigzag_long()
        if count == 0:
            break
        if count < 0:
            block_size = reader.read_count()
            block_end = reader.pos + block_size
            for _ in range(-count):
                child = build_schema(items_node, names=names)
                out.append({"index": index,
                            "value": _project_value(reader, child, names, sub, budget)})
                index += 1
            if reader.pos != block_end:
                raise OcfError("负计数 array 块声明字节长度与实际不一致")
        else:
            for _ in range(count):
                child = build_schema(items_node, names=names)
                out.append({"index": index,
                            "value": _project_value(reader, child, names, sub, budget)})
                index += 1
    return out


def _project_map(reader, schema, names, sub, budget):
    values_node = schema.node["values"]
    out = {}
    while reader.pos < reader.end:
        count = reader.read_zigzag_long()
        if count == 0:
            break
        if count < 0:
            block_size = reader.read_count()
            block_end = reader.pos + block_size
            for _ in range(-count):
                key = _read_key(reader)
                child = build_schema(values_node, names=names)
                v = _project_value(reader, child, names, sub, budget)
                out[key] = None if v is MISSING else v
            if reader.pos != block_end:
                raise OcfError("负计数 map 块声明字节长度与实际不一致")
        else:
            for _ in range(count):
                key = _read_key(reader)
                child = build_schema(values_node, names=names)
                v = _project_value(reader, child, names, sub, budget)
                out[key] = None if v is MISSING else v
    return out


def _project_union(reader, schema, names, trie, budget):
    branches = _union_branches(schema)
    idx = reader.read_zigzag_long()
    if idx < 0 or idx >= len(branches):
        raise OcfError("union 分支索引越界: %r" % idx)
    chosen = build_schema(branches[idx], names=names)
    # union 分支身份必须保留；null 分支给出明确 null
    if chosen.type == "null":
        return {"branch": idx, "value": None}
    value = _project_value(reader, chosen, names, trie, budget)
    return {"branch": idx, "value": None if value is MISSING else value}


# --------------------------------------------------------------------------- #
# 完整取值（路径终点）
# --------------------------------------------------------------------------- #
def _read_full(reader, schema, names, budget):
    schema = resolve(schema, names)
    t = schema.type
    with budget:
        if t == "null":
            return None
        if t == "boolean":
            b = reader.take(1)
            if b[0] not in (0, 1):
                raise OcfError("boolean 编码非 0/1")
            return bool(b[0])
        if t == "int":
            return reader.read_zigzag_long(32)
        if t == "long":
            return reader.read_zigzag_long(64)
        if t == "float":
            return reader.read_float()
        if t == "double":
            return reader.read_double()
        if t == "bytes":
            return reader.take(reader.read_count())
        if t == "string":
            return reader.read_string()
        if t == "fixed":
            return reader.take(schema.node["size"])
        if t == "enum":
            idx = reader.read_zigzag_long()
            syms = schema.node["symbols"]
            if idx < 0 or idx >= len(syms):
                raise OcfError("enum 索引越界")
            return {"symbol": syms[idx], "index": idx}
        if t == "array":
            out = []
            while reader.pos < reader.end:
                count = reader.read_zigzag_long()
                if count == 0:
                    break
                if count < 0:
                    block_size = reader.read_count()
                    end = reader.pos + block_size
                    for _ in range(-count):
                        out.append(_read_full(reader,
                                              build_schema(schema.node["items"],
                                                           names=names),
                                              names, budget))
                    if reader.pos != end:
                        raise OcfError("负计数 array 块字节长度不符")
                else:
                    for _ in range(count):
                        out.append(_read_full(reader,
                                              build_schema(schema.node["items"],
                                                           names=names),
                                              names, budget))
            return out
        if t == "map":
            out = {}
            while reader.pos < reader.end:
                count = reader.read_zigzag_long()
                if count == 0:
                    break
                if count < 0:
                    block_size = reader.read_count()
                    end = reader.pos + block_size
                    for _ in range(-count):
                        key = _read_key(reader)
                        out[key] = _read_full(reader,
                                              build_schema(schema.node["values"],
                                                           names=names),
                                              names, budget)
                    if reader.pos != end:
                        raise OcfError("负计数 map 块字节长度不符")
                else:
                    for _ in range(count):
                        key = _read_key(reader)
                        out[key] = _read_full(reader,
                                              build_schema(schema.node["values"],
                                                           names=names),
                                              names, budget)
            return out
        if t == "record":
            out = {}
            for field in schema.node["fields"]:
                out[field["name"]] = _read_full(
                    reader, build_schema(field["type"], names=names), names, budget)
            return out
        if _is_union(schema):
            branches = _union_branches(schema)
            idx = reader.read_zigzag_long()
            if idx < 0 or idx >= len(branches):
                raise OcfError("union 分支索引越界")
            chosen = build_schema(branches[idx], names=names)
            if chosen.type == "null":
                return {"branch": idx, "value": None}
            return {"branch": idx, "value": _read_full(reader, chosen, names, budget)}
        raise OcfError("未知 schema 类型: " + repr(t))
