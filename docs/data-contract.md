# 对象容器读取口径

记录区间为零基半开区间，全文件记录序号在块之间连续。投影语法a.b用于record、[]遍历array、{*}遍历map；对union在选中值上继续路径并保留writer分支索引，null返回明确null。枚举值保留符号和索引，fixed按schema字节数，浮点为小端IEEE，bytes不转文本。record和enum/fixed具名解析遵循namespace与fullname，允许递归schema但按深度预算终止。schema字段default不用于补writer缺失字节。

long为64位，int为32位；长度、计数和union索引都检查非负及配置界，容器零计数终止，负计数的字节大小只覆盖该块条目。metadata的key为string、value为bytes。codec支持null与deflate（无zlib/gzip头的原始流，wbits=-15），不支持的codec报告不支持。每个物理数据块后有头部同一16字节sync，不能在payload中搜索它。

跨块书签由原文件身份、完整块号和输出提交位置组成。原范围指解压前块范围与块内记录号，不虚构解压值有文件连续字节范围。未解码块只标结构核对。

## 输出与核对层级

读取前先冻结原文件字节数与SHA-256，输出与writer schema、codec绑定。块索引给出块offset、payload offset、声明/实际字节数、记录基数（record_base）、记录数与尾sync核对结果。每个块带两个独立标记：

- `structure_verified`：块头声明长度递进、压缩字节数与边界处尾sync核对通过。跳过的无关块只有这一层。
- `records_verified`：块内记录按writer schema实际解码，且解码记录数与块声明计数一致。只有真正解码的块才有这一层。

投影结果带全文件原记录序号、所属块号与块offset；array元素保留原索引，map保留键，union保留`{branch,value}`分支身份。未选字段按schema跳过，跳过与采集共用同一套字节推进，不会把未选字段字节误认成下一字段。

预算相互独立：`max_block_bytes`约束声明压缩块、`max_uncompressed_bytes`约束解压后字节、`max_depth`约束嵌套深度、记录上限约束单块记录数。任一超限都报硬错误并保留未完成块，不返回空块或部分伪造值。deflate按剩余解压预算分块喂入，解压炸弹在预算处硬停，内存主要受单块与声明预算约束而非整文件。

中途截断（含不完整块头或尾sync缺失）时，已完整提交的块照常输出，失败块不推进书签；结果带`truncated`原因与`complete=false`。本批导出存在非规范块写法（ASCII记录数、声明载荷含尾sync首字节的off-by-one），读取器在规范候选之外仅用“声明边界处的已知sync精确匹配”裁决，不做猜测式扫描。
