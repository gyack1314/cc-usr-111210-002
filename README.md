# 商家Avro对象容器的块索引与投影采集

这是商品与服务履约资料的独立Python工具。`data/`保存合成报文、导出文件或解码服务响应，`source_data.io`读取清单并核对原文件身份，`source_data.domain`提供原始资料概况，`source_data.replay`按记录返回响应，`source_data.ocf`提供有界字节读取、writer schema规整与投影路径，`source_data.avro_codec`按schema解码/跳过/投影，`source_data.container`做OCF块索引、codec处理与恢复书签，`source_data.cli`显示JSON摘要。字段与来源格式见[资料约定](docs/data-contract.md)。不接入真实商家、支付账户或在线平台。

Python 3.11及以上，无第三方Python依赖。在项目根目录使用：

```sh
python -m unittest discover -s tests -v
python -m compileall -q source_data tests
python -m source_data.cli inspect data/normal.json
python -m source_data.cli blocks data/normal.json
python -m source_data.cli project data/raw-deflate.json
```

`inspect`仅读取清单、原始字节身份和基本配置，不覆盖资料。`blocks`全量解码并输出每块offset、记录序号与核对层级；`project`按清单`paths`做字段投影，输出块offset、原记录序号、投影值、schema/codec绑定与恢复书签。

## OCF读取口径

- 先冻结原文件大小与SHA-256，结果绑定文件内writer schema与codec；不做reader schema演进，不做logicalType转换（未知logicalType仅保留注释）。
- 块位置只从合法头与前块声明长度递进，**绝不在payload里搜索16字节sync猜边界**；每块核对记录数、压缩字节数和尾sync。字段内即使碰巧出现sync标记也不会错误重开始。
- 整数走有界zigzag varint；字符串按声明字节长度读取并核对UTF-8；负array/map计数附带的字节长度必须精确覆盖本块；union索引按writer分支校验越界，不能被当作null标志，null分支返回显式`null`并保留分支号。
- codec支持`null`和无zlib头的原始`deflate`（wbits=-15），其余codec报告不支持。解压上限与记录上限相互独立，超限保留未完成块而不是伪造空块。
- 跳过无关块时只完成“结构长度及尾sync核对”，明确标注`structure_verified=true、records_verified=false`，不声称跳过块的记录内容已验证；真正解码的块才置`records_verified=true`。
- 恢复书签由文件身份、最后完整块号与已提交记录位置组成，落在完整块提交之后；文件身份改变即拒绝接续，失败块不推进书签。中途截断返回已完成块与`truncated`原因。
- 投影路径`a.b`选record字段、`[]`遍历array（保留元素原索引）、`{*}`遍历map（保留键）；未选字段也严格按writer schema正确跳过，与投影采集共用同一套字节推进。

`invalid-config.json`是配置反例，其他清单有不同的源边界，边界报文可以保持合法清单但源内容不完整。资料中的姓名、编号、订单和站点均为虚构。

Dockerfile固定Linux基础镜像。`docker build -t commerce-source .`构建，`docker run --rm commerce-source`读取正常清单；`docker run --rm commerce-source python -m unittest discover -s tests -v`运行已有检查。运行资料均在本地，不需要账号、安装脚本或生产系统。
