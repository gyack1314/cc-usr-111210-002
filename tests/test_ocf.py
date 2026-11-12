"""OCF 块索引与投影采集的完整检查。

覆盖：null/deflate 两种 codec、全部契约类型、负计数块、嵌套 union 分支身份、
sync 标记碰撞、中途截断、两级核对、恢复书签与内存/记录预算。
合成 OCF 的标准写入器内嵌在本测试中，与交付读取器分离。
"""
import json
import struct
import unittest
import zlib

from source_data.container import (
    Bookmark,
    FileIdentity,
    Limits,
    OcfError,
    TruncatedOcf,
    collect,
    decode_all,
    parse_header,
)
from source_data.ocf import Projection, parse_path

SYNC = bytes.fromhex("00112233445566778899aabbccddeeff")
MAGIC = b"Obj\x01"
LIMITS = Limits(1 << 20, 1 << 20, 32)


def enc_long(v):
    zz = ((v << 1) ^ (v >> 63)) & ((1 << 64) - 1)
    out = bytearray()
    while True:
        if zz & ~0x7F:
            out.append((zz & 0x7F) | 0x80)
            zz >>= 7
        else:
            out.append(zz)
            return bytes(out)


def enc_bytes(b):
    return enc_long(len(b)) + b


def enc_string(s):
    return enc_bytes(s.encode("utf-8"))


def collect_names(schema, names=None, ns=None):
    if names is None:
        names = {}
    if isinstance(schema, str):
        return names
    if isinstance(schema, list):
        for branch in schema:
            collect_names(branch, names, ns)
        return names
    t = schema["type"]
    if t in ("record", "enum", "fixed"):
        full = schema["name"] if "." in schema["name"] or ns is None \
            else ns + "." + schema["name"]
        names[full] = schema
        names[schema["name"]] = schema
        new_ns = schema.get("namespace", ns)
        if t == "record":
            for f in schema["fields"]:
                collect_names(f["type"], names, new_ns)
    elif t == "array":
        collect_names(schema["items"], names, ns)
    elif t == "map":
        collect_names(schema["values"], names, ns)
    return names


def encode_value(schema, value, names):
    if isinstance(schema, str):
        if schema in names:
            return encode_value(names[schema], value, names)
        t = schema
    elif isinstance(schema, list):
        idx, inner = value
        return enc_long(idx) + encode_value(schema[idx], inner, names)
    else:
        t = schema["type"]
    if t == "null":
        return b""
    if t == "boolean":
        return b"\x01" if value else b"\x00"
    if t in ("int", "long"):
        return enc_long(value)
    if t == "float":
        return struct.pack("<f", value)
    if t == "double":
        return struct.pack("<d", value)
    if t == "bytes":
        return enc_bytes(value)
    if t == "string":
        return enc_string(value)
    if t == "fixed":
        assert len(value) == schema["size"]
        return value
    if t == "enum":
        return enc_long(schema["symbols"].index(value))
    if t == "array":
        out = b""
        if value:
            body = b"".join(encode_value(schema["items"], x, names)
                            for x in value)
            out += enc_long(-len(value)) + enc_long(len(body)) + body
        return out + enc_long(0)
    if t == "map":
        out = b""
        if value:
            body = b""
            for k, val in value.items():
                body += enc_string(k) + encode_value(
                    schema["values"], val, names)
            out += enc_long(-len(value)) + enc_long(len(body)) + body
        return out + enc_long(0)
    if t == "record":
        return b"".join(
            encode_value(f["type"], value[f["name"]], names)
            for f in schema["fields"])
    raise ValueError(t)


def build_ocf(schema, records, codec="null", sync=SYNC, blocks=None):
    names = collect_names(schema)
    schema_bytes = json.dumps(schema, separators=(",", ":")).encode()
    meta = {b"avro.schema": schema_bytes,
            b"avro.codec": codec.encode()}
    header = bytearray(MAGIC) + enc_long(2)
    for k, v in meta.items():
        header += enc_bytes(k) + enc_bytes(v)
    header += sync
    groups = blocks if blocks is not None else [records]
    body = bytearray()
    for group in groups:
        payload = b"".join(encode_value(schema, r, names) for r in group)
        if codec == "deflate":
            co = zlib.compressobj(6, zlib.DEFLATED, -15)
            block = co.compress(payload) + co.flush()
        else:
            block = payload
        body += enc_long(-len(group)) + enc_long(len(block)) + block + sync
    return bytes(header) + bytes(body)


RICH_SCHEMA = {
    "type": "record", "name": "R", "namespace": "x", "fields": [
        {"name": "id", "type": "long"},
        {"name": "tag",
         "type": {"type": "enum", "name": "Color",
                  "symbols": ["RED", "GREEN"]}},
        {"name": "fp", "type": "double"},
        {"name": "fl", "type": "float"},
        {"name": "flag", "type": "boolean"},
        {"name": "u", "type": ["null", "string", "long"]},
        {"name": "arr", "type": {"type": "array", "items": "int"}},
        {"name": "mp", "type": {"type": "map", "values": "bytes"}},
        {"name": "fx", "type": {"type": "fixed", "name": "MD5", "size": 4}},
        {"name": "raw", "type": "bytes"},
    ],
}


def rich_records():
    return [
        {"id": 1, "tag": "RED", "fp": 1.5, "fl": 2.5, "flag": True,
         "u": (0, None), "arr": [10, -2, 30], "mp": {"k": b"\x00\x01"},
         "fx": b"abcd", "raw": b"\xde\xad"},
        {"id": -7, "tag": "GREEN", "fp": -2.25, "fl": -1.0, "flag": False,
         "u": (1, "hi"), "arr": [], "mp": {}, "fx": b"\xff\x00\x10\x20",
         "raw": b""},
        {"id": 9223372036854775807, "tag": "RED", "fp": 3.0, "fl": 0.5,
         "flag": True, "u": (2, 42), "arr": [7], "mp": {"a": b"z"},
         "fx": b"qwer", "raw": b"q"},
    ]


class OcfCoreTests(unittest.TestCase):
    def test_all_contract_types_round_trip(self):
        for codec in ("null", "deflate"):
            with self.subTest(codec=codec):
                raw = build_ocf(RICH_SCHEMA, rich_records(), codec)
                full = decode_all(raw, LIMITS)
                self.assertTrue(full["complete"])
                self.assertEqual(len(full["records"]), 3)
                r0 = full["records"][0]["value"]
                self.assertEqual(r0["id"], 1)
                self.assertEqual(r0["tag"], {"symbol": "RED", "index": 0})
                self.assertEqual(r0["fp"], 1.5)
                self.assertEqual(r0["fl"], 2.5)
                self.assertIs(r0["flag"], True)
                self.assertEqual(r0["u"], {"branch": 0, "value": None})
                self.assertEqual(r0["arr"], [10, -2, 30])
                self.assertEqual(r0["mp"], {"k": b"\x00\x01"})
                self.assertEqual(r0["fx"], b"abcd")
                self.assertEqual(r0["raw"], b"\xde\xad")
                r2 = full["records"][2]["value"]
                self.assertEqual(r2["u"], {"branch": 2, "value": 42})
                self.assertEqual(r2["id"], 9223372036854775807)

    def test_projection_matches_full_decode_subset(self):
        for codec in ("null", "deflate"):
            with self.subTest(codec=codec):
                raw = build_ocf(RICH_SCHEMA, rich_records(), codec)
                paths = ["id", "u", "arr[]", "mp{*}", "fx", "tag"]
                proj = collect(raw, paths, [(0, 3)], LIMITS)
                full = decode_all(raw, LIMITS)
                self.assertTrue(proj["complete"])
                for row in proj["rows"]:
                    ref = full["records"][row["record_no"]]["value"]
                    p = row["projection"]
                    self.assertEqual(p["id"], ref["id"])
                    self.assertEqual(p["u"], ref["u"])
                    self.assertEqual(
                        [x["value"] for x in p["arr"]], ref["arr"])
                    self.assertEqual(p["mp"], ref["mp"])
                    self.assertEqual(p["fx"], ref["fx"])
                    self.assertEqual(p["tag"], ref["tag"])
                # 未选字段不出现，但字节被正确跳过（消费位置由完整解码印证）
                self.assertNotIn("fp", proj["rows"][0]["projection"])
                self.assertNotIn("raw", proj["rows"][0]["projection"])

    def test_array_indices_and_map_keys_preserved(self):
        schema = {"type": "record", "name": "A", "fields": [
            {"name": "xs", "type": {"type": "array", "items": "int"}},
            {"name": "kv", "type": {"type": "map", "values": "string"}}]}
        rec = [{"xs": [100, 200, 300], "kv": {"b": "b1", "a": "a1"}}]
        raw = build_ocf(schema, rec)
        proj = collect(raw, ["xs[]", "kv{*}"], [(0, 1)], LIMITS)
        p = proj["rows"][0]["projection"]
        self.assertEqual([x["index"] for x in p["xs"]], [0, 1, 2])
        self.assertEqual([x["value"] for x in p["xs"]], [100, 200, 300])
        self.assertEqual(p["kv"], {"b": "b1", "a": "a1"})

    def test_union_branch_identity_and_explicit_null(self):
        schema = {"type": "record", "name": "U", "fields": [
            {"name": "u", "type": ["null", "string"]}]}
        raw = build_ocf(schema, [{"u": (0, None)}, {"u": (1, "n")}])
        full = decode_all(raw, LIMITS)
        self.assertEqual(full["records"][0]["value"]["u"],
                         {"branch": 0, "value": None})
        self.assertEqual(full["records"][1]["value"]["u"],
                         {"branch": 1, "value": "n"})
        # 非法 union 索引不能被当成 null
        bad = (raw[:parse_header(raw).header_end] + enc_long(-1)
               + enc_long(1) + enc_long(5) + SYNC)
        with self.assertRaises(OcfError):
            decode_all(bad, LIMITS)

    def test_negative_block_byte_size_must_match(self):
        schema = {"type": "record", "name": "N", "fields": [
            {"name": "a", "type": {"type": "array", "items": "int"}}]}
        hb = build_ocf(schema, [{"a": [1]}])
        hend = parse_header(hb).header_end
        body = enc_long(1)  # one int
        good = enc_long(-1) + enc_long(len(body)) + body + enc_long(0)
        bad = enc_long(-1) + enc_long(len(body) + 5) + body + enc_long(0)
        wrap = lambda rec: hb[:hend] + enc_long(-1) + enc_long(len(rec)) \
            + rec + SYNC
        self.assertTrue(decode_all(wrap(good), LIMITS)["complete"])
        with self.assertRaises(OcfError):
            decode_all(wrap(bad), LIMITS)

    def test_logical_type_kept_as_note_no_conversion(self):
        schema = {"type": "record", "name": "L", "fields": [
            {"name": "ts",
             "type": {"type": "long", "logicalType": "timestamp-millis"}},
            {"name": "amt", "type": {"type": "bytes",
                                     "logicalType": "decimal",
                                     "precision": 10, "scale": 2}}]}
        raw = build_ocf(schema, [{"ts": 123456789, "amt": b"\x01\x02"}])
        header = parse_header(raw)
        self.assertTrue(any("timestamp-millis" in n
                            for n in header.logical_notes))
        value = decode_all(raw, LIMITS)["records"][0]["value"]
        self.assertEqual(value["ts"], 123456789)
        self.assertEqual(value["amt"], b"\x01\x02")

    def test_recursive_schema_and_depth_budget(self):
        schema = {"type": "record", "name": "Node", "fields": [
            {"name": "v", "type": "int"},
            {"name": "next", "type": ["null", "Node"]}]}
        rec = [{"v": 1, "next": (1, {"v": 2, "next": (0, None)})}]
        raw = build_ocf(schema, rec)
        node = decode_all(raw, LIMITS)["records"][0]["value"]
        self.assertEqual(node["next"]["value"]["v"], 2)
        with self.assertRaises(OcfError):
            decode_all(raw, Limits(1 << 20, 1 << 20, 1))

    def test_utf8_length_checked(self):
        schema = {"type": "record", "name": "S",
                  "fields": [{"name": "s", "type": "string"}]}
        base = build_ocf(schema, [{"s": "ok"}])
        hend = parse_header(base).header_end
        bad_rec = enc_long(3) + b"\xff\xfe\xfd"
        bad = base[:hend] + enc_long(-1) + enc_long(len(bad_rec)) \
            + bad_rec + SYNC
        with self.assertRaises(OcfError):
            decode_all(bad, LIMITS)


class OcfContainerTests(unittest.TestCase):
    def test_multi_block_offsets_and_record_numbers(self):
        schema = {"type": "record", "name": "M",
                  "fields": [{"name": "id", "type": "long"}]}
        raw = build_ocf(schema, [{"id": 1}, {"id": 2}, {"id": 3}],
                        blocks=[[{"id": 1}], [{"id": 2}, {"id": 3}]])
        full = decode_all(raw, LIMITS)
        self.assertEqual([r["record_no"] for r in full["records"]], [0, 1, 2])
        offsets = [b["offset"] for b in full["blocks"]]
        self.assertEqual(len(offsets), 2)
        self.assertLess(offsets[0], offsets[1])
        # 块位置只来自声明递进：第二个块 offset 可由头与第一块长度算出
        b0 = full["blocks"][0]
        expected = (b0["payload_offset"] + b0["compressed_size"] + 16)
        self.assertEqual(offsets[1], expected)

    def test_skip_block_is_structure_only(self):
        schema = {"type": "record", "name": "M",
                  "fields": [{"name": "id", "type": "long"}]}
        raw = build_ocf(schema, [{"id": 1}, {"id": 2}, {"id": 3}],
                        blocks=[[{"id": 1}], [{"id": 2}], [{"id": 3}]])
        proj = collect(raw, ["id"], [(2, 3)], LIMITS)
        b0, b1, b2 = proj["blocks"]
        self.assertTrue(b0["structure_verified"])
        self.assertFalse(b0["records_verified"])
        self.assertTrue(b1["structure_verified"])
        self.assertFalse(b1["records_verified"])
        self.assertTrue(b2["records_verified"])
        self.assertEqual([r["record_no"] for r in proj["rows"]], [2])

    def test_bookmark_identity_binding(self):
        schema = {"type": "record", "name": "M",
                  "fields": [{"name": "id", "type": "long"}]}
        raw = build_ocf(schema, [{"id": 1}, {"id": 2}],
                        blocks=[[{"id": 1}], [{"id": 2}]])
        ident = FileIdentity(raw)
        ok = collect(raw, ["id"], [(0, 2)], LIMITS,
                     bookmark=Bookmark(ident, None, 0))
        self.assertEqual(len(ok["rows"]), 2)
        changed = bytearray(raw)
        changed[-1] ^= 0xFF
        with self.assertRaises(OcfError):
            collect(bytes(changed), ["id"], [(0, 2)], LIMITS,
                    bookmark=Bookmark(ident, None, 0))

    def test_bookmark_commits_only_complete_blocks(self):
        schema = {"type": "record", "name": "M",
                  "fields": [{"name": "id", "type": "long"}]}
        rec = encode_value(schema, {"id": 9}, collect_names(schema))
        hb = build_ocf(schema, [{"id": 1}])
        hend = parse_header(hb).header_end
        block = enc_long(-1) + enc_long(len(rec)) + rec + SYNC
        two = hb[:hend] + block + block
        # 只切掉第二块的尾部 sync，第一块保持完整可提交
        first_block_end = hend + len(block)
        cut = two[:first_block_end + 3]
        result = decode_all(cut, LIMITS)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["blocks"]), 1)
        self.assertTrue(result["blocks"][0]["records_verified"])

    def test_budgets_are_hard_limits(self):
        schema = {"type": "record", "name": "M",
                  "fields": [{"name": "id", "type": "long"}]}
        raw = build_ocf(schema, [{"id": 1}])
        with self.assertRaises(OcfError):
            collect(raw, ["id"], [(0, 1)], Limits(0, 1 << 20, 32))
        with self.assertRaises(OcfError):
            collect(raw, ["id"], [(0, 1)], Limits(1 << 20, 0, 32))
        with self.assertRaises(OcfError):
            decode_all(raw, Limits(1 << 20, 1 << 20, 32,
                                   max_records_per_block=0))

    def test_deflate_uses_raw_stream_and_inflate_cap(self):
        schema = {"type": "record", "name": "M",
                  "fields": [{"name": "s", "type": "string"}]}
        raw = build_ocf(schema, [{"s": "x" * 100}], codec="deflate")
        self.assertTrue(decode_all(raw, LIMITS)["complete"])
        # 解压上限独立且更小 -> 硬错误
        with self.assertRaises(OcfError):
            decode_all(raw, Limits(1 << 20, 5, 32))

    def test_unsupported_codec_rejected(self):
        schema = {"type": "record", "name": "M",
                  "fields": [{"name": "id", "type": "long"}]}
        raw = build_ocf(schema, [{"id": 1}], codec="snappy")
        with self.assertRaises(OcfError):
            parse_header(raw)


class FixtureTests(unittest.TestCase):
    """商家真实导出素材：含 sync 标记碰撞与非规范块编码。"""

    def setUp(self):
        def read(p):
            with open(p, "rb") as f:
                return f.read()
        self.null_raw = read("data/orders-null.avro")
        self.deflate_raw = read("data/orders-deflate.avro")
        self.trunc_raw = read("data/truncated.avro")
        self.paths = ["order", "items[].sku", "attrs{*}"]

    def test_identity_frozen(self):
        ident = FileIdentity(self.null_raw)
        self.assertEqual(ident.size, 513)
        self.assertEqual(
            ident.sha256,
            "c4845a98cfd51ec39099ca8a3dab874508ab6dd32d1b8da8ae9fcbb385ded47f")

    def test_projection_both_codecs_identical_values(self):
        rn = collect(self.null_raw, self.paths, [(0, 2)], LIMITS)
        rd = collect(self.deflate_raw, self.paths, [(0, 2)], LIMITS)
        fn = decode_all(self.null_raw, LIMITS)
        fd = decode_all(self.deflate_raw, LIMITS)
        # 投影与全量解码在两个 codec 下值一致
        for proj, full in ((rn, fn), (rd, fd)):
            for row in proj["rows"]:
                ref = full["records"][row["record_no"]]["value"]
                p = row["projection"]
                self.assertEqual(p["order"], ref["order"])
                self.assertEqual(
                    [x["value"]["sku"] for x in p["items"]],
                    [i["sku"] for i in ref["items"]])
                self.assertEqual(p["attrs"], ref["attrs"])
        self.assertEqual(
            [r["projection"] for r in rn["rows"]],
            [r["projection"] for r in rd["rows"]])
        # sync 碰撞没有导致错误重同步：块边界全部结构核对通过
        self.assertTrue(all(b["sync_ok"] for b in rn["blocks"]))
        self.assertTrue(all(b["sync_ok"] for b in rd["blocks"]))

    def test_nested_union_branch_in_fixture(self):
        full = decode_all(self.null_raw, LIMITS)
        self.assertEqual(full["records"][0]["value"]["comment"],
                         {"branch": 0, "value": None})
        self.assertEqual(
            full["records"][1]["value"]["comment"]["branch"], 1)

    def test_truncated_fixture_keeps_only_complete_block(self):
        result = collect(self.trunc_raw, self.paths, [(0, 2)], LIMITS)
        self.assertFalse(result["complete"])
        self.assertIsNotNone(result["truncated"])
        self.assertEqual(len(result["blocks"]), 1)
        self.assertTrue(result["blocks"][0]["records_verified"])
        self.assertEqual(len(result["rows"]), 1)
        # 书签停在完整块 0 之后，失败块不推进
        self.assertEqual(
            result["bookmark"]["last_committed_block"], 0)

    def test_block_offsets_reported(self):
        result = decode_all(self.null_raw, LIMITS)
        offsets = [b["offset"] for b in result["blocks"]]
        self.assertEqual(offsets, [408, 450])
        for b in result["blocks"]:
            self.assertIn("payload_offset", b)
            self.assertIn("record_base", b)


class CombinedScenarioTests(unittest.TestCase):
    """负计数字节块 + 嵌套 union + 标记碰撞 + 中途截断的集中对照。"""

    def test_projection_equals_full_decode_under_all_stresses(self):
        schema = {
            "type": "record", "name": "Order", "namespace": "shop",
            "fields": [
                {"name": "order", "type": "string"},
                {"name": "comment", "type": ["null", "string"]},
                {"name": "items", "type": {
                    "type": "array",
                    "items": {"type": "record", "name": "Item", "fields": [
                        {"name": "sku", "type": "string"},
                        {"name": "count", "type": "long"}]}}},
                {"name": "attrs", "type": {"type": "map",
                                           "values": ["null", "string"]}},
            ],
        }
        # attrs 的字符串值里嵌入完整 sync 标记，制造标记碰撞
        collide = "z" + SYNC.decode("latin1")
        records = [
            {"order": "o1", "comment": (0, None),
             "items": [{"sku": "clean", "count": 2}],
             "attrs": {"city": (1, "a")}},
            {"order": "o2", "comment": (1, collide),
             "items": [{"sku": "hotel", "count": 1}],
             "attrs": {"note": (1, "中文"), "empty": (0, None)}},
        ]
        for codec in ("null", "deflate"):
            with self.subTest(codec=codec):
                raw = build_ocf(schema, records, codec=codec,
                                blocks=[records[:1], records[1:]])
                full = decode_all(raw, LIMITS)
                self.assertTrue(full["complete"])
                self.assertEqual(len(full["blocks"]), 2)
                proj = collect(raw, ["order", "comment", "items[].sku",
                                     "attrs{*}"], [(0, 2)], LIMITS)
                for row in proj["rows"]:
                    ref = full["records"][row["record_no"]]["value"]
                    p = row["projection"]
                    self.assertEqual(p["order"], ref["order"])
                    self.assertEqual(p["comment"], ref["comment"])
                    self.assertEqual(
                        [x["value"]["sku"] for x in p["items"]],
                        [i["sku"] for i in ref["items"]])
                    self.assertEqual(p["attrs"], ref["attrs"])
                # 碰撞字符串原样保留，union 分支身份不丢
                self.assertEqual(
                    proj["rows"][1]["projection"]["comment"],
                    {"branch": 1, "value": collide})
                self.assertEqual(
                    proj["rows"][1]["projection"]["attrs"]["empty"],
                    {"branch": 0, "value": None})
                # 中途截断：只保留完整首块，投影与全量在该块上仍一致
                cut = raw[:full["blocks"][1]["offset"] + 2]
                part = collect(cut, ["order", "items[].sku", "attrs{*}"],
                               [(0, 2)], LIMITS)
                self.assertFalse(part["complete"])
                self.assertEqual(len(part["blocks"]), 1)
                self.assertEqual(
                    part["rows"][0]["projection"]["order"],
                    full["records"][0]["value"]["order"])


class PathSyntaxTests(unittest.TestCase):
    def test_parse_and_trie(self):
        toks = parse_path("a.b[].c{*}")
        kinds = [(t.kind, t.name) for t in toks]
        self.assertEqual(kinds, [
            ("field", "a"), ("field", "b"), ("array_each", None),
            ("field", "c"), ("map_each", None)])

    def test_bad_paths(self):
        for bad in ("", "a.[", "a{}", "x[0]", "a.{x}"):
            with self.assertRaises(OcfError):
                Projection([bad])


if __name__ == "__main__":
    unittest.main()
