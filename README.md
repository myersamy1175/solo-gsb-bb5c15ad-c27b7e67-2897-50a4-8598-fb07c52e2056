# 工业二进制报文解析 API

基于 FastAPI 的工业二进制报文解析服务。用户注册**带版本的报文定义**（位宽、端序、条件分支、计数驱动的重复字段、校验字段），提交指定版本的十六进制报文后，服务按位解析、验证校验和，并返回字段值、位偏移及实际分支路径；反向地，也可以提交字段值**编码**出十六进制报文（校验和自动计算写入）。此外支持保存**解析回归样例**：把报文与基线字段快照存入 SQLite，版本升级后对新版本重跑并逐字段比对差异。SQLite 持久化，启动时自动建表。

## 快速启动

```bash
docker compose up --build
```

单条命令即可启动，服务监听 **8000** 端口：

- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 配置

通过环境变量配置（见 `docker-compose.yml`）：

| 变量      | 默认值             | 说明                 |
| --------- | ------------------ | -------------------- |
| `DB_PATH` | `/data/parser.db`  | SQLite 数据库文件路径（容器内），已挂载 named volume `parser-data` 持久化 |

端口映射在 `docker-compose.yml` 中为 `8000:8000`，如需修改主机端口，改左侧即可（如 `"9000:8000"`）。

## 报文定义格式

```json
{
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "description": "温湿度传感器报文",
  "fields": [
    {"name": "header",      "type": "uint", "bit_length": 8},
    {"name": "msg_type",    "type": "uint", "bit_length": 4},
    {"name": "flags",       "type": "uint", "bit_length": 4},
    {"name": "temperature", "type": "int",  "bit_length": 16, "endianness": "little",
     "scale": 0.1, "condition": {"field": "msg_type", "op": "eq", "value": 1}},
    {"name": "humidity",    "type": "uint", "bit_length": 8,
     "condition": {"field": "msg_type", "op": "eq", "value": 2}},
    {"name": "crc",         "type": "uint", "bit_length": 16}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt"}
}
```

**字段属性**

| 属性         | 说明                                                                 |
| ------------ | -------------------------------------------------------------------- |
| `name`       | 字段名，唯一定位                                                     |
| `type`       | `uint` / `int`（二进制补码）/ `bool`（1 位）/ `hex` / `ascii`        |
| `bit_length` | 位宽，支持非字节对齐（如 4 位）；`hex`/`ascii`/小端字段需为 8 的倍数 |
| `endianness` | `big`（默认）/ `little`，仅对多字节 `uint`/`int` 有效                |
| `scale`/`offset` | 可选，仅数值字段：`value = raw * scale + offset`                 |
| `condition`  | 可选，条件不满足时跳过该字段（条件比较的是**原始值 raw**，非缩放值） |
| `repeat`     | 可选，形如 `{"field": "count", "max": 8}`：字段按前置计数字段的原始值重复，详见下文 |

**条件操作符**：`eq` `ne` `gt` `ge` `lt` `le` `in`（`in` 的 `value` 为整数数组）。条件只能引用**之前定义**的标量数值/布尔字段。

**重复字段（repeat）**

- `repeat.field` 必须引用**之前定义的标量 `uint` 字段**（计数字段）；解析/编码时取该字段的**原始值 raw**作为重复次数。
- `repeat.max` 取值范围 **1～64**；运行时计数超过 `max` 返回 422。
- 重复字段可以是任意类型（含 `ascii`/`hex`/`bool`、支持小端与 `scale`/`offset`），可以同时带 `condition`：**条件未命中时不展开**任何重复项；命中后重复段长度（`count × bit_length`）正确推动后续字段与校验字段的偏移。
- 计数字段本身被更早的条件跳过时，重复无法确定，返回 422。
- 重复字段是数组，**不能**再被后续条件/计数引用；校验字段本身不能声明为重复字段。
- 校验和算法的计算范围天然覆盖展开后的全部字节（校验字段位于重复段之后时偏移随之顺延）。

**校验和算法**：`sum8`、`xor8`、`crc16-ccitt`（8 位字段用前两者，16 位字段用后两者）。计算范围为校验字段之前的全部字节，校验字段需字节对齐。

## API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/v1/definitions` | 注册报文定义（重复 `device_type+version` 返回 409） |
| GET  | `/api/v1/definitions` | 列出定义，参数：`device_type`、`include_inactive` |
| GET  | `/api/v1/definitions/{device_type}/latest` | 查询该设备类型最新**启用**版本 |
| GET  | `/api/v1/definitions/{device_type}/{version}` | 查询指定版本 |
| POST | `/api/v1/definitions/{device_type}/{version}/deactivate` | 停用定义 |
| POST | `/api/v1/definitions/{device_type}/compatibility` | 两个已注册版本的报文布局兼容性分析（>256 个等价类组合或任一版本含 `repeat` 定义返回 422） |
| POST | `/api/v1/parse` | 解析报文（`version` 缺省时使用最新启用版本） |
| POST | `/api/v1/detect` | 不传版本，用全部已注册版本（含停用）逐一解析以自动识别版本 |
| POST | `/api/v1/encode` | 反向编码：由字段值生成报文（版本选择规则同 parse） |
| POST | `/api/v1/fixtures` | 保存带名称的解析回归样例（名称在**设备类型内**唯一，重复返回 409） |
| GET  | `/api/v1/fixtures` | 列出回归样例，参数：`device_type` |
| GET  | `/api/v1/fixtures/{name}` | 按名称查询样例（跨设备类型重名时用 `?device_type=` 消歧，否则返回 409） |
| DELETE | `/api/v1/fixtures/{name}` | 删除样例（消歧规则同上） |
| POST | `/api/v1/fixtures/{name}/run` | 运行回归样例：用现有解析器重新解析并与基线快照逐字段比较 |

> 停用后的定义不参与列表（默认）和 latest 查询，但仍可按显式版本解析/编码历史报文。

## curl 示例

### 1. 注册定义

```bash
curl -X POST http://localhost:8000/api/v1/definitions \
  -H 'Content-Type: application/json' \
  -d '{
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "description": "温湿度传感器报文",
    "fields": [
      {"name": "header",      "type": "uint", "bit_length": 8},
      {"name": "msg_type",    "type": "uint", "bit_length": 4},
      {"name": "flags",       "type": "uint", "bit_length": 4},
      {"name": "temperature", "type": "int",  "bit_length": 16, "endianness": "little",
       "scale": 0.1, "condition": {"field": "msg_type", "op": "eq", "value": 1}},
      {"name": "humidity",    "type": "uint", "bit_length": 8,
       "condition": {"field": "msg_type", "op": "eq", "value": 2}},
      {"name": "crc",         "type": "uint", "bit_length": 16}
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"}
  }'
```

### 2. 解析报文（温度分支）

报文 `aa10eb008936`：`header=0xAA`，`msg_type=1`，温度原始值 `0x00EB=235`（小端）× 0.1 = 23.5 ℃，CRC-16/CCITT = `0x8936`。

```bash
curl -X POST http://localhost:8000/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor", "version": "1.0.0", "hex": "aa10eb008936"}'
```

响应（节选）：

```json
{
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "fields": [
    {"name": "header",      "bit_offset": 0,  "bit_length": 8,  "value": 170},
    {"name": "msg_type",    "bit_offset": 8,  "bit_length": 4,  "value": 1},
    {"name": "flags",       "bit_offset": 12, "bit_length": 4,  "value": 0},
    {"name": "temperature", "bit_offset": 16, "bit_length": 16, "value": 23.5, "raw_value": 235},
    {"name": "crc",         "bit_offset": 32, "bit_length": 16, "value": 35126}
  ],
  "branch_path": [
    {"field": "temperature", "condition": "msg_type == 1", "actual_value": 1, "matched": true,  "action": "parsed"},
    {"field": "humidity",    "condition": "msg_type == 2", "actual_value": 1, "matched": false, "action": "skipped"}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt", "valid": true, "expected_hex": "0x8936", "calculated_hex": "0x8936"},
  "message_bits": 48, "consumed_bits": 48, "trailing_bits": 0, "warnings": []
}
```

湿度分支报文（`msg_type=2`，湿度 0x55=85%）：`aa2055ba77`。校验和错误时仍返回 200，但 `checksum.valid` 为 `false`；报文过短或 hex 非法返回 422。

### 3. 编码报文（解析的逆操作）

提交字段值，服务按位宽连续写入（支持非字节对齐、大小端、`int` 补码），自动计算并写入校验字段，返回 `hex`、实际编码字段和分支路径：

```bash
curl -X POST http://localhost:8000/api/v1/encode \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor", "version": "1.0.0",
       "values": {"header": 170, "msg_type": 1, "flags": 0, "temperature": 23.5}}'
```

响应（节选）：

```json
{
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "hex": "aa10eb008936",
  "fields": [
    {"name": "header",      "bit_offset": 0,  "bit_length": 8,  "value": 170,  "raw_value": 170},
    {"name": "msg_type",    "bit_offset": 8,  "bit_length": 4,  "value": 1,    "raw_value": 1},
    {"name": "flags",       "bit_offset": 12, "bit_length": 4,  "value": 0,    "raw_value": 0},
    {"name": "temperature", "bit_offset": 16, "bit_length": 16, "value": 23.5, "raw_value": 235},
    {"name": "crc",         "bit_offset": 32, "bit_length": 16, "value": 35126, "raw_value": 35126}
  ],
  "branch_path": [
    {"field": "temperature", "condition": "msg_type == 1", "actual_value": 1, "matched": true,  "action": "encoded"},
    {"field": "humidity",    "condition": "msg_type == 2", "actual_value": 1, "matched": false, "action": "skipped"}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt", "range": "bytes [0, 4)", "calculated": 35126, "calculated_hex": "0x8936"},
  "message_bits": 48, "padding_bits": 0
}
```

**编码规则**

- `values` 中各值采用**解析响应的类型**：`uint`/`int` 为整数（带 `scale`/`offset` 时可为浮点数），`bool` 为布尔值，`hex` 为 `"0x..."` 字符串，`ascii` 为字符串（长度须等于 `bit_length/8`）。
- 重复字段的值为**数组**，长度必须与计数字段的 raw 值**完全一致**（计数为 0 时传 `[]`，传标量或长度不符均返回 422）；逐元素套用类型/范围/scale 反算规则，错误信息会标出下标（如 `samples[1]`）。
- 带 `scale`/`offset` 的数值按 `raw = (value - offset) / scale` 反算；无法精确还原或 raw 超出位宽范围时返回 422。
- 条件判断统一使用 **raw**（与 parse 一致）：条件未命中的字段不要求输入（提供了也返回 422）；命中字段缺失、出现未知字段均返回 422。
- 校验字段**不接受调用方赋值**，服务在其位置自动计算写入；校验位置未字节对齐时返回 422。

### 4. 重复字段（repeat）：变长数组

定义中 `count` 是普通 uint 字段，`samples` 通过 `repeat` 声明由它的原始值决定重复次数（上限 8）：

```json
{
  "device_type": "array_dev",
  "version": "1.0.0",
  "fields": [
    {"name": "header",  "type": "uint", "bit_length": 8},
    {"name": "count",   "type": "uint", "bit_length": 8},
    {"name": "samples", "type": "uint", "bit_length": 16, "endianness": "little",
     "scale": 0.1, "repeat": {"field": "count", "max": 8}},
    {"name": "crc",     "type": "uint", "bit_length": 16}
  ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt"}
}
```

报文 `aa03e800d00710270236`：`header=0xAA`，`count=3`，后跟 3 个小端 int16 采样值 raw `232 / 2000 / 10000`（即 23.2、200.0、1000.0），最后 2 字节 CRC。

```bash
# 注册
curl -X POST http://localhost:8000/api/v1/definitions \
  -H 'Content-Type: application/json' \
  -d '{ "device_type": "array_dev", "version": "1.0.0",
        "fields": [
          {"name": "header",  "type": "uint", "bit_length": 8},
          {"name": "count",   "type": "uint", "bit_length": 8},
          {"name": "samples", "type": "uint", "bit_length": 16, "endianness": "little",
           "scale": 0.1, "repeat": {"field": "count", "max": 8}},
          {"name": "crc",     "type": "uint", "bit_length": 16}
        ],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"} }'

# 解析（value / raw_value 为数组，count 为实际重复次数）
curl -X POST http://localhost:8000/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "array_dev", "version": "1.0.0", "hex": "aa03e800d00710270236"}'
```

响应（节选）：

```json
{
  "fields": [
    {"name": "header",  "bit_offset": 0,  "bit_length": 8,  "value": 170},
    {"name": "count",   "bit_offset": 8,  "bit_length": 8,  "value": 3},
    {"name": "samples", "bit_offset": 16, "bit_length": 16, "count": 3,
     "value": [23.2, 200.0, 1000.0], "raw_value": [232, 2000, 10000]},
    {"name": "crc",     "bit_offset": 64, "bit_length": 16, "value": 566}
  ],
  "consumed_bits": 80, "trailing_bits": 0
}
```

- `bit_length` 仍是**单个元素**的位宽；重复段总位数为 `count × bit_length`，后续字段（含校验字段）的偏移随之顺延。
- 计数为 0 时该字段返回 `"value": []`（数值字段同时有 `"raw_value": []`），不占用报文位。
- 编码时数组长度必须与 `count` 完全一致：

```bash
curl -X POST http://localhost:8000/api/v1/encode \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "array_dev", "version": "1.0.0",
       "values": {"header": 170, "count": 3, "samples": [23.2, 200.0, 1000.0]}}'
# -> {"hex": "aa03e800d00710270236", ...}

# count=0 的报文：aa00 + crc16-ccitt(aa00) = aa00efba
curl -X POST http://localhost:8000/api/v1/encode \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "array_dev", "version": "1.0.0",
       "values": {"header": 170, "count": 0, "samples": []}}'
```

返回 422 的情形：运行时计数超过声明的 `max`、编码数组长度与计数不一致（或不是数组）、重复段超出报文长度、计数字段被更早的条件分支跳过。注册时若计数引用不是前置标量 `uint`、`max` 不在 1～64、重复字段被后续条件引用或校验字段声明为重复，同样返回 422。

### 5. 列表 / 最新版本 / 停用

```bash
# 列出全部启用中的定义（可加 ?device_type=temp_sensor 或 ?include_inactive=true）
curl http://localhost:8000/api/v1/definitions

# 查询设备类型的最新启用版本
curl http://localhost:8000/api/v1/definitions/temp_sensor/latest

# 停用指定版本
curl -X POST http://localhost:8000/api/v1/definitions/temp_sensor/1.0.0/deactivate
```

### 6. 版本兼容性分析

比较同一 `device_type` 的两个**已注册**版本（无需提供报文）。分析器：

1. 收集所有被条件引用的字段（控制字段），按条件中出现的整数常量把每个控制字段的原始值域划分为有限个**等价类**——同一等价类内的取值使该字段上的全部条件真假恒定（如 `eq 1` 分出 `[0] / [1] / [2..2ⁿ-1]`，`gt 5` 在 `6` 处切分）；
2. 枚举各控制字段等价类代表值的笛卡尔积，对每个可达组合**模拟布局**：按条件保留/跳过字段，计算每个字段的位偏移与布局总长度（条件引用的字段被更早分支跳过时该组合不可达，自动跳过）；
3. 对旧版每个可达布局，在新版的**相同条件代表值**下寻找对应布局：字段名称、类型、位宽、位偏移、总长度及校验算法必须全部一致。任一旧布局找不到对应布局即判为 `breaking`，逐分支返回差异；否则判为 `compatible`。

等价类组合总数超过 **256** 时返回 `422`（边界值 256 正常分析）。**任一版本含 `repeat` 定义时也直接返回 422**：重复长度由运行时计数值决定，无法在不提供报文的情况下给出静态布局结论，因此拒绝分析而不是给出可能误导的判定。

```bash
curl -X POST http://localhost:8000/api/v1/definitions/temp_sensor/compatibility \
  -H 'Content-Type: application/json' \
  -d '{"old_version": "1.0.0", "new_version": "2.0.0"}'
```

兼容时响应（节选）：

```json
{
  "device_type": "temp_sensor",
  "old_version": "1.0.0",
  "new_version": "2.0.0",
  "compatible": true,
  "status": "compatible",
  "equivalence_classes": [
    {"field": "msg_type", "classes": 4, "representatives": [0, 1, 2, 3]}
  ],
  "combination_count": 4,
  "reachable_branches": 4,
  "unreachable_combinations": 0,
  "breaking_branches": 0,
  "branches": [
    {
      "representative": {"msg_type": {"op": "==", "value": 1}},
      "compatible": true,
      "old_layout": {
        "fields": [
          {"name": "header",      "type": "uint", "bit_length": 8,  "bit_offset": 0},
          {"name": "msg_type",    "type": "uint", "bit_length": 4,  "bit_offset": 8},
          {"name": "flags",       "type": "uint", "bit_length": 4,  "bit_offset": 12},
          {"name": "temperature", "type": "int",  "bit_length": 16, "bit_offset": 16},
          {"name": "crc",         "type": "uint", "bit_length": 16, "bit_offset": 32}
        ],
        "total_bits": 48,
        "checksum_algorithm": "crc16-ccitt"
      },
      "new_layout": { "...": "结构相同..." }
    }
  ]
}
```

发生破坏时，顶层 `"status"` 为 `"breaking"`、`"compatible"` 为 `false`，不兼容分支的 `"differences"` 逐项列出差异，`kind` 取值：

| `kind` | 含义 |
| ------ | ---- |
| `field_added` / `field_removed` | 新版多出 / 删除了字段（含因新分支引入的字段） |
| `field_changed` | 同名字段的 `type` / `bit_length` / `bit_offset` 变化 |
| `total_length_changed` | 布局总位数变化 |
| `checksum_algorithm_changed` | 校验算法变化（如 `crc16-ccitt` → `crc16-modbus`） |
| `branch_unreachable` | 旧版可达分支在新版下条件无法满足（控制字段被跳过） |

两版本对同一控制字段的位宽不同时，按两者原始值域的**交集**划分（无交集返回 422）；新版新增控制字段或删除旧控制字段时，仅在受影响的等价类上产生差异，未受影响分支仍可判为兼容。

### 7. 自动识别版本（/detect）

收到报文但不知道版本时，调用 `/api/v1/detect`。请求体**只含** `device_type` 与 `hex`（不接受 `version`，多余字段返回 422）；服务会取出该设备类型下**全部已注册版本（含停用版本）**，按版本顺序逐一解析：

- **候选条件**：定义**完整消费**报文（无剩余尾部位）；若该版本配置了校验和，还要求校验通过。未配置校验和的版本只按完整消费判断。
- **唯一候选**：返回识别出的版本及与 `/parse` 完全一致的解析结果。
- **无候选**：返回 `422`，并在 `detail.failures` 中按版本给出失败原因（报文过短、存在尾部位、校验和不符、校验字段被条件分支跳过等）。
- **多个候选**：返回 `409` 并在 `detail.candidates` 中给出候选版本列表，**不会**静默选择最新版本——调用方需自行消歧（如改用带 `version` 的 `/parse`）。

```bash
# 示例设备注册了 1.0.0（2 字节，sum8）与 2.0.0（4 字节，crc16-ccitt）
curl -X POST http://localhost:8000/api/v1/detect \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "detect_sensor", "hex": "aa2a6a92"}'
```

唯一候选时响应（与 `/parse` 相同）：

```json
{
  "device_type": "detect_sensor",
  "version": "2.0.0",
  "definition_active": true,
  "fields": [ "...同 /parse..." ],
  "checksum": {"field": "crc", "algorithm": "crc16-ccitt", "valid": true, "...": "..."},
  "message_bits": 32, "consumed_bits": 32, "trailing_bits": 0, "warnings": []
}
```

无候选时 `422` 响应示例（4 字节报文对 1.0.0 多出尾部位，对 2.0.0 则校验和不符）：

```json
{
  "detail": {
    "message": "no registered version matches the message",
    "device_type": "detect_sensor",
    "failures": [
      {"version": "1.0.0", "active": false, "reason": "definition does not fully consume the message: 16 trailing bit(s)"},
      {"version": "2.0.0", "active": true,  "reason": "checksum mismatch: expected 0x0000, calculated 0x6a92"}
    ]
  }
}
```

多候选时 `409` 响应示例（按版本顺序列出，含停用状态）：

```json
{
  "detail": {
    "message": "message matches multiple registered versions; an explicit version is required",
    "device_type": "detect_dup",
    "candidates": [
      {"version": "1.0.0", "active": true},
      {"version": "2.0.0", "active": false}
    ]
  }
}
```

> 设备类型下没有任何注册版本（含停用）时返回 `404`；hex 非法/为空返回 `422`，清洗规则与 `/parse` 一致（忽略空白、`_`、`-` 与 `0x` 前缀）。

### 8. 解析回归样例（/fixtures）

报文定义升级后，需要确认同一批历史报文在新版本上的解析表现。回归样例把一条报文与它在某个版本上的**字段快照**绑定存储（SQLite），随后可对任意目标版本（或默认的基线版本）重跑，逐字段比较：

1. **字段名 + `raw_value`**：数值字段比较原始整数（`uint`/`int`；重复字段为整数数组；`bool` 归一化为 0/1；`hex`/`ascii` 无 raw 值，只按字段名比较存在性）。
2. **分支路径 `branch_path`**：逐条件字段比较 `matched` 与 `action`（`parsed`/`skipped`）。
3. **校验结果**：比较 `checksum.valid`（目标定义没有校验字段时为 `null`）。

**保存样例**

```bash
curl -X POST http://localhost:8000/api/v1/fixtures \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "temp-branch",
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "hex": "aa10eb008936",
    "expect": {
      "fields": [
        {"name": "header", "raw_value": 170},
        {"name": "temperature", "raw_value": 235}
      ],
      "branch_path": [{"field": "temperature", "matched": true, "action": "parsed"}],
      "checksum_valid": true
    }
  }'
```

- 创建时服务先按 `version` 解析一次 `hex`，把完整快照（全部字段 / 全部分支 / 校验结果）作为**基线**持久化；基线解析失败返回 422，引用的定义不存在返回 404。
- `expect` 各部分均可选，只做**子集断言**：`fields[].raw_value` 省略时仅要求该字段在基线中出现；与实际解析不符返回 422 并在 `detail.problems` 中逐项列出原因。无论是否提供 `expect`，持久化的始终是完整快照。
- `name` 在**同一 `device_type` 内唯一**（允许字母数字及 `_.:-`）；重复保存返回 409。不同设备类型可以重名，查询/删除/运行时用 `device_type` 消歧。

返回示例（节选）：

```json
{
  "name": "temp-branch",
  "device_type": "temp_sensor",
  "version": "1.0.0",
  "hex": "aa10eb008936",
  "snapshot": {
    "fields": [
      {"name": "header", "raw_value": 170},
      {"name": "msg_type", "raw_value": 1},
      {"name": "flags", "raw_value": 0},
      {"name": "temperature", "raw_value": 235},
      {"name": "crc", "raw_value": 35126}
    ],
    "branch_path": [
      {"field": "temperature", "matched": true, "action": "parsed"},
      {"field": "humidity", "matched": false, "action": "skipped"}
    ],
    "checksum_valid": true
  },
  "created_at": "2026-09-21T..."
}
```

**运行样例**

```bash
# 对基线版本自身回归（body 也可省略 device_type 以外的字段）
curl -X POST http://localhost:8000/api/v1/fixtures/temp-branch/run \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor"}'

# 对新版本回归
curl -X POST http://localhost:8000/api/v1/fixtures/temp-branch/run \
  -H 'Content-Type: application/json' \
  -d '{"device_type": "temp_sensor", "target_version": "2.0.0"}'
```

- 请求体必须包含 `device_type`（名称只在设备类型内唯一）；`target_version` 缺省时使用样例的基线版本。目标版本不存在返回 404。
- 目标版本解析**失败**（报文过短等）不返回 5xx，而是 HTTP 200 + `parse_error` 字段，并把每个基线字段/分支/校验项都保留为失败项，便于一次看到全部问题。

响应示例（版本变化，字段差异）：

```json
{
  "name": "temp-branch",
  "device_type": "temp_sensor",
  "baseline_version": "1.0.0",
  "target_version": "2.0.0",
  "version_changed": true,
  "hex": "aa10eb008936",
  "baseline": { "...": "保存时的快照..." },
  "actual": { "...": "目标版本的实际快照..." },
  "parse_error": null,
  "passed": false,
  "comparison": {
    "fields": {
      "added":    [{"name": "status", "raw_value": 0}],
      "removed":  [{"name": "flags", "raw_value": 0}],
      "changed":  [{"name": "temperature", "baseline_raw_value": 235, "actual_raw_value": 2350}],
      "unchanged": [{"name": "header", "raw_value": 170}],
      "passed": false
    },
    "branch_path": {
      "added": [],
      "removed": [],
      "changed": [
        {"field": "humidity",
         "baseline": {"field": "humidity", "matched": false, "action": "skipped"},
         "actual":   {"field": "humidity", "matched": true,  "action": "parsed"}}
      ],
      "unchanged": [],
      "passed": false
    },
    "checksum": {"baseline_valid": true, "actual_valid": true, "passed": true},
    "total_checks": 9,
    "passed_checks": 6,
    "pass_rate": 0.6667,
    "errors": [
      "field 'status' newly appears in target parse (raw_value 0)",
      "field 'flags' missing in target parse (baseline raw_value 0)",
      "field 'temperature' raw_value changed: baseline 235 -> actual 2350",
      "branch on 'humidity' changed: baseline matched false (skipped) -> actual matched true (parsed)"
    ]
  }
}
```

- `comparison.fields` 与 `comparison.branch_path` 各自给出 `added` / `removed` / `changed` / `unchanged` 明细；`errors` 是所有未通过项的逐条说明（含目标解析失败时的 `target parse failed: ...`）。
- `total_checks = 字段项数 + 分支项数 + 1（校验）`，`pass_rate = passed_checks / total_checks`；全部一致且无解析错误时顶层 `passed` 为 `true`、`pass_rate` 为 `1.0`。
- 列表 `GET /api/v1/fixtures?device_type=temp_sensor`；按名查询/删除支持 `?device_type=` 消歧，名称跨设备类型重复且未消歧时返回 409（`detail.candidates` 列出候选）。

## 本地开发（不用 Docker）

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # http://localhost:8000
pytest                               # 运行测试
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与生命周期
  schemas.py    # 请求模型与定义语义校验
  parser.py     # 按位解析器（位读取、条件分支、repeat 数组展开、校验和验证）
  encoder.py    # 按位编码器（位写入、scale/offset 反算、repeat 数组、校验和自动写入）
  analyzer.py   # 版本兼容性分析（条件等价类划分、可达分支枚举、布局比对；遇 repeat 拒绝分析）
  fixtures.py   # 回归样例：基线快照捕获、expect 断言、逐字段 added/removed/changed 比较
  checksum.py   # sum8 / xor8 / crc16-ccitt / crc16-modbus
  database.py   # SQLite 持久化（definitions / fixtures 两表，启动自动建表）
tests/test_api.py
Dockerfile
docker-compose.yml
```

## Docker 镜像与持久化

`Dockerfile` 基于 `python:3.12-slim`，仅安装运行依赖（`requirements.txt`），启动命令为
`uvicorn app.main:app --host 0.0.0.0 --port 8000`。`docker-compose.yml` 将容器内数据库
`/data/parser.db` 挂载到 named volume `parser-data`，因此注册的报文定义与回归样例在容器
重建后仍然保留；首次启动时两表（`definitions`、`fixtures`）自动创建，无需手动迁移。

```bash
# 启动（首次自动构建镜像）
docker compose up --build

# 后台运行 / 查看日志 / 停止
docker compose up -d --build
docker compose logs -f
docker compose down            # 不删除数据卷；加 -v 才会清空 parser-data

# 不用 compose 直接运行
docker build -t msg-parser .
docker run -p 8000:8000 -e DB_PATH=/data/parser.db \
  -v parser-data:/data msg-parser
```
