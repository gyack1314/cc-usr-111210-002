"""Avro 对象容器（OCF）的块索引与字段投影采集。

只使用文件内 writer schema，不做 reader schema 演进，不做 logicalType 转换。
块位置只从合法头与前块声明长度递进，绝不扫描同步标记猜边界。
"""
import struct

MAGIC = b"Obj\x01"
SYNC_SIZE = 16
DEFAULT_MAX_VARINT_BYTES = 10

PRIMITIVES = frozenset(
    {"null", "boolean", "int", "long", "float", "double", "bytes", "string"}
)


class OcfError(ValueError):
    """容器结构、编码或口径不满足约定时抛出。"""


# --------------------------------------------------------------------------- #
# 有界字节读取器
# --------------------------------------------------------------------------- #
class ByteReader:
    """在固定字节窗口内读取，越界即 OcfError，绝不越窗扫描。"""

    __slots__ = ("buf", "pos", "end", "max_varint")

    def __init__(self, buf, start=0, end=None, max_varint=DEFAULT_MAX_VARINT_BYTES):
        self.buf = buf
        self.pos = start
        self.end = len(buf) if end is None else end
        self.max_varint = max_varint

    def remaining(self):
        return self.end - self.pos

    def mark(self):
        return self.pos

    def reset(self, pos):
        self.pos = pos

    def read_byte(self):
        if self.pos >= self.end:
            raise OcfError("字节窗口已耗尽")
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def take(self, n):
        if n < 0:
            raise OcfError("负长度读取")
        if self.pos + n > self.end:
            raise OcfError("声明长度越过字节窗口")
        v = self.buf[self.pos:self.pos + n]
        self.pos += n
        return v

    def read_zigzag_long(self, bits=64):
        """有界 zigzag varint；bits=32 时按 int 域复核。"""
        result = 0
        shift = 0
        used = 0
        while True:
            if used >= self.max_varint:
                raise OcfError("varint 超过有界字节数")
            if self.pos >= self.end:
                raise OcfError("varint 被截断")
            byte = self.buf[self.pos]
            self.pos += 1
            used += 1
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                break
            shift += 7
            if shift >= 64:
                raise OcfError("varint 超过 64 位")
        value = (result >> 1) ^ -(result & 1)
        if bits == 32:
            if not (-(1 << 31) <= value < (1 << 31)):
                raise OcfError("int 超出 32 位域")
        elif bits == 64:
            if not (-(1 << 63) <= value < (1 << 63)):
                raise OcfError("long 超出 64 位域")
        return value

    def read_count(self):
        """非负计数（长度、集合块计数绝对值、union 分支索引等）。"""
        v = self.read_zigzag_long()
        if v < 0:
            raise OcfError("计数必须非负")
        return v

    def read_string(self):
        n = self.read_count()
        raw = self.take(n)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise OcfError("字符串字节长度与 UTF-8 不符") from None

    def read_float(self):
        return struct.unpack("<f", self.take(4))[0]

    def read_double(self):
        return struct.unpack("<d", self.take(8))[0]


# --------------------------------------------------------------------------- #
# Writer schema 规整：具名表、fullname、递归与 logicalType 注释
# --------------------------------------------------------------------------- #
class Schema:
    """包装 writer schema 节点；具名类型登记到 names 表支持递归引用。"""

    __slots__ = ("node", "name", "logical_note", "names")

    def __init__(self, node, names=None, name=None, logical_note=None):
        self.node = node
        self.names = {} if names is None else names
        self.name = name
        self.logical_note = logical_note

    @property
    def type(self):
        node = self.node
        return node["type"] if isinstance(node, dict) else node


def _fullname(name, namespace):
    if "." in name or namespace is None:
        return name
    return namespace + "." + name


def build_schema(node, enclosing_ns=None, names=None, logical_notes=None):
    """把 JSON writer schema 包装成可递归解析的 Schema，并登记具名类型。"""
    if names is None:
        names = {}
    if logical_notes is None:
        logical_notes = []
    # 字符串引用：具名类型简写
    if isinstance(node, str):
        if node in PRIMITIVES:
            return Schema(node, names)
        if node not in names:
            raise OcfError("schema 引用了未定义具名类型: " + node)
        return names[node]
    if isinstance(node, list):
        for branch in node:
            build_schema(branch, enclosing_ns, names, logical_notes)
        return Schema(node, names)
    if not isinstance(node, dict):
        raise OcfError("schema 节点必须是字符串、数组或对象")
    t = node.get("type")
    note = None
    if isinstance(t, str) and "logicalType" in node and t in PRIMITIVES:
        # 未知 logicalType 只保留注释，不做任何转换；已知的也不转换
        note = "logicalType=%s 保留底层 %s 编码" % (node["logicalType"], t)
        logical_notes.append(note)
    if t == "record":
        ns = node.get("namespace", enclosing_ns)
        full = _fullname(node["name"], ns)
        if full in names:
            rec = names[full]
            if rec.node is not node:
                # 同一 fullname 的结构等价重复声明视为同一类型；结构不同才报错
                if json.dumps(rec.node, sort_keys=True) != \
                        json.dumps(node, sort_keys=True):
                    raise OcfError("具名 record 重复定义且结构不一致: " + full)
            return rec
        rec = Schema(node, names, full, note)
        names[full] = rec
        # 别名也可解析
        for alias in node.get("aliases", []):
            names.setdefault(_fullname(alias, ns), rec)
        for field in node["fields"]:
            if not isinstance(field, dict) or "name" not in field or "type" not in field:
                raise OcfError("record 字段缺少 name/type")
            build_schema(field["type"], ns, names, logical_notes)
        return rec
    if t == "enum":
        ns = node.get("namespace", enclosing_ns)
        full = _fullname(node["name"], ns)
        if full in names:
            return names[full]
        syms = node.get("symbols")
        if not isinstance(syms, list) or not syms or any(not isinstance(s, str) for s in syms):
            raise OcfError("enum symbols 必须是非空字符串数组")
        en = Schema(node, names, full, note)
        names[full] = en
        for alias in node.get("aliases", []):
            names.setdefault(_fullname(alias, ns), en)
        return en
    if t == "fixed":
        ns = node.get("namespace", enclosing_ns)
        full = _fullname(node["name"], ns)
        if full in names:
            return names[full]
        if not isinstance(node.get("size"), int) or node["size"] < 0:
            raise OcfError("fixed size 必须是非负整数")
        fx = Schema(node, names, full, note)
        names[full] = fx
        for alias in node.get("aliases", []):
            names.setdefault(_fullname(alias, ns), fx)
        return fx
    if t == "array":
        build_schema(node["items"], enclosing_ns, names, logical_notes)
        return Schema(node, names, None, note)
    if t == "map":
        build_schema(node["values"], enclosing_ns, names, logical_notes)
        return Schema(node, names, None, note)
    if isinstance(t, list):
        for i, branch in enumerate(t):
            build_schema(branch, enclosing_ns, names, logical_notes)
        return Schema(node, names, None, note)
    if isinstance(t, str) and t in PRIMITIVES:
        return Schema(node, names, None, note)
    raise OcfError("不支持的 schema 类型: " + repr(t))


def resolve(schema, names):
    """解码期间按名字解析引用。"""
    node = schema.node
    if isinstance(node, str):
        if node in PRIMITIVES:
            return schema
        if node not in names:
            raise OcfError("未登记的具名类型: " + node)
        return names[node]
    return schema


# --------------------------------------------------------------------------- #
# 投影路径
# --------------------------------------------------------------------------- #
class PathToken:
    __slots__ = ("kind", "name")

    def __init__(self, kind, name=None):
        self.kind = kind  # field | array_each | map_each
        self.name = name

    def __eq__(self, other):
        return isinstance(other, PathToken) and \
            self.kind == other.kind and self.name == other.name

    def __hash__(self):
        return hash((self.kind, self.name))

    def __repr__(self):
        if self.kind == "field":
            return "field:" + self.name
        return self.kind


def parse_path(text):
    """a.b 选 record 字段，[] 遍历 array，{*} 遍历 map。"""
    if not isinstance(text, str) or not text:
        raise OcfError("投影路径不能为空")
    tokens = []
    i = 0
    cur = ""
    while i < len(text):
        ch = text[i]
        if ch == ".":
            if cur:
                tokens.append(PathToken("field", cur))
                cur = ""
            i += 1
        elif ch == "[":
            if i + 1 >= len(text) or text[i + 1] != "]":
                raise OcfError("数组路径只能是 []: " + text)
            if cur:
                tokens.append(PathToken("field", cur))
                cur = ""
            tokens.append(PathToken("array_each"))
            i += 2
        elif ch == "{":
            if i + 2 >= len(text) or text[i + 1] != "*" or text[i + 2] != "}":
                raise OcfError("map 路径只能是 {*}: " + text)
            if cur:
                tokens.append(PathToken("field", cur))
                cur = ""
            tokens.append(PathToken("map_each"))
            i += 3
        else:
            cur += ch
            i += 1
    if cur:
        tokens.append(PathToken("field", cur))
    return tuple(tokens)


class Projection:
    """按路径前缀组织的投影选择器，驱动一次遍历同时完成跳过与采集。"""

    def __init__(self, paths):
        self.raw = list(paths)
        self.trie = {}
        for p in self.raw:
            toks = parse_path(p)
            node = self.trie
            for tok in toks:
                node = node.setdefault(tok, {})
            node[None] = p  # 终点标记

    def child(self, tok):
        return self.trie.get(tok)

    def is_terminal(self):
        return None in self.trie

    def terminal_path(self):
        return self.trie.get(None)
