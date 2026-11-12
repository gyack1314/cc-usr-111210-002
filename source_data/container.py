"""OCF 容器层：完整头解析、块索引递进、codec 处理与恢复书签。

块位置只从合法头和前块声明长度递进；不扫描 sync 猜边界。
每块核对记录数、压缩字节数和尾 sync；跳过块只做结构核对，不声称已验证记录内容。
"""
import hashlib
import json
import zlib

from .avro_codec import Budget, _read_full, project_block
from .ocf import (
    MAGIC,
    SYNC_SIZE,
    ByteReader,
    OcfError,
    Projection,
    build_schema,
)

CODEC_NULL = b"null"
CODEC_DEFLATE = b"deflate"


class TruncatedOcf(OcfError):
    """文件在完整块边界之前截断。

    completed_blocks 为已通过结构（及可能的解码）核对并提交的块索引，
    失败块本身不计入、不推进书签。
    """

    def __init__(self, message, completed_blocks=None, next_record_base=0):
        super().__init__(message)
        self.completed_blocks = completed_blocks or []
        self.next_record_base = next_record_base


class FileIdentity:
    """冻结的原文件身份：大小与 SHA-256，改变即不能接续书签。"""

    __slots__ = ("size", "sha256")

    def __init__(self, raw):
        self.size = len(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()

    def matches(self, other):
        return self.size == other.size and self.sha256 == other.sha256

    def as_dict(self):
        return {"bytes": self.size, "sha256": self.sha256}


class Header:
    __slots__ = ("schema_json", "schema", "names", "codec", "metadata",
                 "sync", "logical_notes", "header_end")

    def __init__(self, schema_json, schema, names, codec, metadata, sync,
                 logical_notes, header_end):
        self.schema_json = schema_json
        self.schema = schema
        self.names = names
        self.codec = codec
        self.metadata = metadata
        self.sync = sync
        self.logical_notes = logical_notes
        self.header_end = header_end


class BlockIndex:
    """单块结构核对结果。verified 区分结构核对与记录解码核对两层。"""

    __slots__ = ("block_no", "offset", "record_count", "negative_count",
                 "compressed_size", "payload_offset", "sync_ok",
                 "structure_verified", "records_verified", "record_base",
                 "uncompressed_size", "noncanonical_count",
                 "declared_size_overshoot")

    def __init__(self, block_no, offset, record_count, negative_count,
                 compressed_size, payload_offset, sync_ok, record_base,
                 uncompressed_size=None):
        self.block_no = block_no
        self.offset = offset
        self.record_count = record_count
        self.negative_count = negative_count
        self.compressed_size = compressed_size
        self.payload_offset = payload_offset
        self.sync_ok = sync_ok
        self.record_base = record_base
        self.uncompressed_size = uncompressed_size
        self.noncanonical_count = False
        self.declared_size_overshoot = 0
        # 结构层：声明长度递进 + 尾 sync 核对
        self.structure_verified = sync_ok
        # 解码层：实际按 schema 解码出的记录数核对
        self.records_verified = False

    def as_dict(self):
        d = {
            "block_no": self.block_no,
            "offset": self.offset,
            "record_base": self.record_base,
            "record_count": self.record_count,
            "negative_count": self.negative_count,
            "compressed_size": self.compressed_size,
            "payload_offset": self.payload_offset,
            "sync_ok": self.sync_ok,
            "noncanonical_count": self.noncanonical_count,
            "declared_size_overshoot": self.declared_size_overshoot,
            "structure_verified": self.structure_verified,
            "records_verified": self.records_verified,
        }
        if self.uncompressed_size is not None:
            d["uncompressed_size"] = self.uncompressed_size
        return d


class Limits:
    __slots__ = ("max_block_bytes", "max_uncompressed_bytes", "max_depth",
                 "max_records_per_block", "max_varint_bytes")

    def __init__(self, max_block_bytes, max_uncompressed_bytes, max_depth,
                 max_records_per_block=None, max_varint_bytes=10):
        self.max_block_bytes = max_block_bytes
        self.max_uncompressed_bytes = max_uncompressed_bytes
        self.max_depth = max_depth
        self.max_records_per_block = max_records_per_block
        self.max_varint_bytes = max_varint_bytes


def parse_header(raw):
    """完整解析 Obj 头：版本字节、metadata map（遵守 Avro 块计数规则）与 16 字节 sync。

    魔数 Obj\\x01 的末字节即版本字节 1；之后直接是 metadata map。
    """
    if len(raw) < len(MAGIC) + 1:
        raise OcfError("文件过小，连容器魔数都不完整")
    if raw[:4] != MAGIC:
        raise OcfError("不是 Avro 对象容器（魔数应为 Obj\\x01）")
    version = raw[3]
    if version != 1:
        raise OcfError("不支持的容器版本字节: %r" % version)
    reader = ByteReader(raw, 4)
    metadata = {}
    # OCF 头的 metadata 是 map：先读总条目数（支持负计数块及其字节长度），
    # 读完即止，map 本身没有额外 0 终止符；之后直接是 16 字节 sync。
    total = reader.read_zigzag_long()
    if total < 0:
        block_size = reader.read_count()
        block_end = reader.pos + block_size
        entries = -total
        for _ in range(entries):
            key = reader.read_string()
            val = reader.take(reader.read_count())
            if key in metadata:
                raise OcfError("metadata 键重复: " + key)
            metadata[key] = val
        if reader.pos != block_end:
            raise OcfError("metadata 负计数块字节长度不符")
    else:
        for _ in range(total):
            key = reader.read_string()
            val = reader.take(reader.read_count())
            if key in metadata:
                raise OcfError("metadata 键重复: " + key)
            metadata[key] = val
    sync = reader.take(SYNC_SIZE)
    if "avro.schema" not in metadata:
        raise OcfError("metadata 缺少 avro.schema")
    try:
        schema_json = json.loads(metadata["avro.schema"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OcfError("avro.schema 不是合法 UTF-8 JSON") from None
    logical_notes = []
    names = {}
    schema = build_schema(schema_json, names=names, logical_notes=logical_notes)
    codec = metadata.get("avro.codec", CODEC_NULL)
    if codec not in (CODEC_NULL, CODEC_DEFLATE):
        raise OcfError("不支持的 codec: %r" % codec)
    return Header(schema_json, schema, names, codec, metadata, sync,
                  logical_notes, reader.pos)


def inflate_block(payload, limits):
    """deflate 是无 zlib 头的原始流；解压上限与记录上限独立。

    始终按“剩余解压预算”限定单次输出，触顶即硬停并报错，保留未完成块；
    内存峰值因此受 max_uncompressed_bytes 与单块声明大小约束，而非整文件。
    """
    if len(payload) > limits.max_block_bytes:
        raise OcfError("压缩块超过声明字节预算")
    decompressor = zlib_decompressobj()
    chunks = []
    total = 0
    view = memoryview(payload)
    pos = 0
    step = 64 * 1024
    while pos < len(payload):
        piece = view[pos:pos + step]
        pos += len(piece)
        room = limits.max_uncompressed_bytes - total
        if room <= 0:
            raise OcfError("解压字节数达到上限，保留未完成块")
        out = decompressor.decompress(piece, room)
        chunks.append(out)
        total += len(out)
        if total >= limits.max_uncompressed_bytes and \
                (decompressor.unconsumed_tail or pos < len(payload)):
            raise OcfError("解压字节数超过上限，保留未完成块")
    tail = decompressor.flush(limits.max_uncompressed_bytes - total)
    total += len(tail)
    if total > limits.max_uncompressed_bytes:
        raise OcfError("解压字节数超过上限，保留未完成块")
    chunks.append(tail)
    if decompressor.unused_data:
        raise OcfError("deflate 流结束后存在多余字节")
    if decompressor.unconsumed_tail:
        raise OcfError("deflate 流在解压上限处仍有未消费输入")
    return b"".join(chunks)


def zlib_decompressobj():
    return zlib.decompressobj(-15)  # 无 zlib/gzip 头的原始 deflate


def _read_block_count(reader):
    """块记录数候选。

    标准口径是规范 zigzag varint；本批导出的写入器混用了非规范写法：
    1) 双字节 varint（首字节高位为 0 仍续一个字节）；
    2) 把记录数写成 ASCII 数字（如 b'1' = 0x31），后跟一个标记字节。
    所有候选只负责“可能的记录数 + 占用字节数”，由调用方用声明边界处的
    尾 sync 精确裁决，不猜测、不扫描。
    """
    start = reader.mark()
    first = reader.read_zigzag_long()
    candidates = [(first, reader.pos - start, False)]
    if reader.pos - start == 1 and reader.remaining() >= 1:
        raw_first = reader.buf[start]
        nxt = reader.buf[reader.pos]
        # 候选 1：ASCII 数字计数 + 1 个标记字节（本批导出的实际口径）
        if 0x30 <= raw_first <= 0x39:
            digits = 0
            p = start
            while p < reader.end and 0x30 <= reader.buf[p] <= 0x39:
                digits = digits * 10 + (reader.buf[p] - 0x30)
                p += 1
            # 数字序列后必须还有标记字节，且数字不能以 0 开头伪造
            if p < reader.end and p > start and reader.buf[start] != 0x30:
                candidates.append((-digits, p - start + 1, True))
        # 候选 2：非规范双字节 varint（首字节高位为 0 仍续一个字节）
        if not (raw_first & 0x80) and not (nxt & 0x80):
            unsigned = (raw_first & 0x7F) | (nxt << 7)
            value = (unsigned >> 1) ^ -(unsigned & 1)
            candidates.append((value, 2, True))
    return candidates


def iter_block_headers(raw, header, limits, committed=None):
    """只按声明长度递进生成块索引，绝不搜索 sync。

    每个块头在规范与已知非规范 varint 候选间，用声明边界处的尾 sync 裁决；
    失败即停止，不推进书签。committed 用于在截断异常里回传已提交块数。
    """
    reader = ByteReader(raw, header.header_end,
                        max_varint=limits.max_varint_bytes)
    block_no = 0
    record_base = 0
    while reader.pos < reader.end:
        # 尾部游离字节（不完整块头）：无法构成完整块声明
        if reader.remaining() < 2:
            raise TruncatedOcf(
                "文件在完整块声明之前截断，剩余 %d 字节" % reader.remaining(),
                completed_blocks=list(committed or ()),
                next_record_base=record_base)
        offset = reader.pos
        chosen = None
        budget_breach = None
        cap_breach = None
        for count, count_len, noncanonical in _read_block_count(reader):
            if count == 0:
                continue
            negative = count < 0
            nrec = -count if negative else count
            if nrec <= 0:
                continue
            size_probe = reader.mark()
            try:
                declared_size = reader.read_count()
            except OcfError:
                continue
            if declared_size < 0:
                reader.reset(size_probe)
                continue
            if declared_size > limits.max_block_bytes:
                # 声明本身越过预算：硬错误，不是截断
                budget_breach = declared_size
                reader.reset(size_probe)
                continue
            payload_offset = reader.pos
            # 候选字节长度：规范声明值；本批导出的写入器还把尾 sync 的首字节
            # 误计入 payload（声明值比真实载荷大 1）。两种口径都只在声明边界
            # 与头部 sync 精确匹配时才成立，不做任何扫描。
            size_options = [declared_size]
            if declared_size >= 1:
                size_options.append(declared_size - 1)
            for size in size_options:
                probe = reader.mark()
                try:
                    if reader.remaining() < size + SYNC_SIZE:
                        reader.reset(probe)
                        continue
                    payload = reader.take(size)
                    tail = reader.take(SYNC_SIZE)
                    if tail != header.sync:
                        reader.reset(probe)
                        continue
                except OcfError:
                    reader.reset(probe)
                    continue
                if limits.max_records_per_block is not None and \
                        nrec > limits.max_records_per_block:
                    # 边界合法但记录数超上限：记下，继续试其它候选
                    cap_breach = nrec
                    reader.reset(probe)
                    continue
                size_overshoot = declared_size - size
                chosen = (nrec, negative, size, payload_offset, payload,
                          noncanonical, size_overshoot)
                break
            if chosen is not None:
                break
        if chosen is None:
            if cap_breach is not None:
                raise OcfError("块记录数 %d 超过上限，保留未完成块" % cap_breach)
            if budget_breach is not None:
                raise OcfError(
                    "声明压缩块 %d 字节超过预算 %d，保留未完成块"
                    % (budget_breach, limits.max_block_bytes))
            raise TruncatedOcf(
                "offset=%d 处块声明无法通过结构与 sync 核对（块未完成）" % offset,
                completed_blocks=list(committed or ()),
                next_record_base=record_base)
        nrec, negative, size, payload_offset, payload, noncanonical, \
            size_overshoot = chosen
        idx = BlockIndex(block_no, offset, nrec, negative, size,
                         payload_offset, True, record_base,
                         uncompressed_size=size if header.codec == CODEC_NULL
                         else None)
        idx.noncanonical_count = noncanonical
        idx.declared_size_overshoot = size_overshoot
        yield idx, payload
        record_base += nrec
        block_no += 1


def _payload_for_codec(payload, header, limits):
    if header.codec == CODEC_NULL:
        if len(payload) > limits.max_block_bytes:
            raise OcfError("块字节数超过预算")
        # null codec 的“解压后”就是载荷本身，仍受独立的解压上限约束
        if len(payload) > limits.max_uncompressed_bytes:
            raise OcfError("块解压字节数超过上限，保留未完成块")
        return payload
    return inflate_block(payload, limits)


def collect(raw, projection_paths, record_ranges, limits, bookmark=None,
            identity=None):
    """块索引 + 指定字段投影采集的主入口。

    record_ranges：零基半开区间列表，全文件记录序号跨块连续。
    返回 dict：身份、schema/codec 绑定、块索引、投影行、核对层级与书签。
    """
    if identity is None:
        identity = FileIdentity(raw)
    if bookmark is not None:
        if not bookmark.identity.matches(identity):
            raise OcfError("原文件身份改变，不能接续书签")
    header = parse_header(raw)
    projection = Projection(projection_paths)
    wanted = _normalize_ranges(record_ranges)

    blocks = []
    rows = []
    next_base = 0
    last_committed_no = None
    truncated = None
    try:
        block_iter = iter_block_headers(raw, header, limits, blocks)
        for idx, compressed in block_iter:
            lo, hi = idx.record_base, idx.record_base + idx.record_count
            overlap = _overlap(lo, hi, wanted)
            if overlap is None:
                # 无关块：只核对结构（声明长度 + 尾 sync 已在迭代中完成）
                idx.structure_verified = True
                blocks.append(idx.as_dict())
                next_base = hi
                last_committed_no = idx.block_no
                continue
            payload = _payload_for_codec(compressed, header, limits)
            if header.codec == CODEC_DEFLATE:
                idx.uncompressed_size = len(payload)
                if len(payload) > limits.max_uncompressed_bytes:
                    raise OcfError("解压块超过字节预算")
            block_rows, decoded = project_block(
                payload, header.schema, header.names, projection,
                limits.max_depth, limits.max_records_per_block)
            if decoded != idx.record_count:
                raise OcfError(
                    "块 %d 声明记录数 %d 与实际解码 %d 不符"
                    % (idx.block_no, idx.record_count, decoded))
            idx.structure_verified = True
            idx.records_verified = True
            for local_no, row in enumerate(block_rows):
                global_no = idx.record_base + local_no
                if _in_ranges(global_no, wanted):
                    rows.append({
                        "record_no": global_no,
                        "block_no": idx.block_no,
                        "block_offset": idx.offset,
                        "projection": row,
                    })
            blocks.append(idx.as_dict())
            next_base = hi
            last_committed_no = idx.block_no
    except TruncatedOcf as exc:
        # 失败块不推进书签：书签只落在最后一个完整提交块之后
        truncated = str(exc)
    new_bookmark = Bookmark(identity, last_committed_no, next_base)
    return {
        "identity": identity.as_dict(),
        "codec": header.codec.decode("ascii"),
        "schema": header.schema_json,
        "logical_type_notes": list(header.logical_notes),
        "blocks": blocks,
        "rows": rows,
        "truncated": truncated,
        "complete": truncated is None,
        "bookmark": new_bookmark.as_dict(),
    }


def decode_all(raw, limits):
    """全量解码（不投影），用于与投影结果一致性对照。"""
    identity = FileIdentity(raw)
    header = parse_header(raw)
    blocks = []
    records = []
    truncated = None
    try:
        for idx, compressed in iter_block_headers(raw, header, limits, blocks):
            payload = _payload_for_codec(compressed, header, limits)
            if header.codec == CODEC_DEFLATE:
                idx.uncompressed_size = len(payload)
            reader = ByteReader(payload)
            budget = Budget(limits.max_depth)
            got = 0
            while reader.remaining():
                if limits.max_records_per_block is not None and \
                        got >= limits.max_records_per_block:
                    raise OcfError("块记录数超过上限")
                records.append({
                    "record_no": idx.record_base + got,
                    "block_no": idx.block_no,
                    "value": _read_full(reader, header.schema, header.names, budget),
                })
                got += 1
            if got != idx.record_count:
                raise OcfError("块 %d 记录数核对失败" % idx.block_no)
            idx.structure_verified = True
            idx.records_verified = True
            blocks.append(idx.as_dict())
    except TruncatedOcf as exc:
        truncated = str(exc)
    return {"identity": identity.as_dict(), "codec": header.codec.decode("ascii"),
            "schema": header.schema_json, "blocks": blocks, "records": records,
            "truncated": truncated, "complete": truncated is None}


class Bookmark:
    """恢复书签：文件身份 + 完整块号 + 输出提交位置；失败块不推进。"""

    __slots__ = ("identity", "last_block_no", "records_committed")

    def __init__(self, identity, last_block_no, records_committed):
        self.identity = identity
        self.last_block_no = last_block_no
        self.records_committed = records_committed

    def as_dict(self):
        return {
            "identity": self.identity.as_dict(),
            "last_committed_block": self.last_block_no,
            "records_committed": self.records_committed,
        }


def _normalize_ranges(ranges):
    out = []
    for pair in ranges:
        a, b = pair
        if not (isinstance(a, int) and isinstance(b, int)):
            raise OcfError("记录区间必须是整数")
        if a < 0 or b < a:
            raise OcfError("记录区间逆序或为负")
        out.append((a, b))
    out.sort()
    return tuple(out)


def _overlap(lo, hi, ranges):
    for a, b in ranges:
        if b <= lo or a >= hi:
            continue
        return (max(a, lo), min(b, hi))
    return None


def _in_ranges(n, ranges):
    return any(a <= n < b for a, b in ranges)
