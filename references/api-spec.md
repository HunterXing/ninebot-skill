# 九号 Device Service API 规范

> 本文件记录本 skill 使用的两个后端：官方 Device Service（内测 Key）与账号通道（自有账号）。
> 签名算法与端点均经实测验证。

## 一、官方 Device Service（需内测 Key）

Base URL：`https://cn-cbu-gateway.ninebot.com`

| 用途 | 方法 | 路径 |
|---|---|---|
| 设备列表 | GET | `/ai-skill/api/device/info/get-device-list` |
| 车辆动态 | POST | `/ai-skill/api/device/info/get-device-dynamic-info` |

认证：`Authorization: Bearer <NINEBOT_DEVICESERVICE_KEY>`

请求体（设备动态）：

```json
{"sn": "<设备SN>"}
```

### 字段映射

| 含义 | 路径 | 类型 |
|---|---|---|
| 设备列表 | `data` | array，每项含 `sn` / `deviceName` |
| 电量 | `data.dumpEnergy` | int（百分比） |
| 开关机 | `data.powerStatus` | `0`=OFF · `1`=ON |
| 位置描述 | `data.locationInfo.locationDesc` | string |
| 预估里程 | `data.estimateMileage` | number（km） |
| 充电状态 | `data.chargingState` | `1`=charging |
| 剩余充电时间 | `data.remainChargeTime` | string |

### 响应结构

```json
{"code": 0, "msg": "", "data": { "dumpEnergy": 78, "powerStatus": "0", ... }}
```

`code != 0` → 业务错误。已知：

| code | 含义 |
|---|---|
| `651001` | 智能服务费已到期，**车辆数据查询功能已暂停** |
| `401` | Key 无效 |

## 二、账号通道（自有账号，不需内测名额）

**这一节描述的是业务通道（NET 加密信封），不是官方 Device Service。**
两条通道的字段名不同（官方 camelCase / 账号 snake_case），别混。

### 业务通道（NET 加密信封）

Base URL 按端点分家：`ebike.ninebot.com`（电自）· `steeldust.ninebot.com`（电摩）
· `cn-cbu-gateway.ninebot.com`（骑行记录）· `api-jhcx-v6-bj.ninebot.com`（账号）

| 用途 | 路径 | 序列号参数名 | Business-Type |
|---|---|---|---|
| 车辆列表 | `/vehicle/binding/my-vehicle`（两网关合并） | — | 1 |
| 车辆状态 | `/vehicle/vehicle/desktop-component` | **`sn_str`** | 2 |
| **车辆档案（含原生总里程）** | **`/vehicle/vehicle/vehicle-info`** | **`wnumber`** | 2 |
| **链路状态（GSM/GNSS 信号）** | **`/vehicle/vehicle/vehicle-status`** | **`wnumber`** | 2 |
| 电池 | `/v6/vehicle/battery-info` | `wnumber` | 2 |
| 骑行记录 | `/app-api/travel/v6/travel-list2` | `wnumber` | 2 |

⚠️ **序列号参数名按端点各不一样，没有统一的那个**：
`desktop-component` 用 `sn_str`，`vehicle-info` / `battery-info` / travel 用 `wnumber`。
用错名字的后果分两种，别混：
- 参数名错 → `340000`（内层体形态被拒）
- 路由不存在 → `404 request not found!`

⚠️ **`vehicle-info` 的 `total_mileage` 是整车原生里程**（即 APP 设备信息页显示的总里程），
**共享（家庭）账号照样能读到**。而 `my-vehicle` 记录里那个同名字段在共享账号下恒为
`null` —— 两者不是一回事，别拿 `my-vehicle` 的空值去断言「拿不到 ODO」。

`vehicle-info` 响应的其余字段：`identification_number`（车架号/VIN）、
`lastupdatetime`、`battery_type`、`iov_type`、`vehicle_name`、
`latest_trail`（最近一次骑行，其中 `mileage` 单位是**米**）。

### Passport 认证链

| 用途 | 方法 | 路径 | 参数 |
|---|---|---|---|
| 密码登录 | POST | `/v6/user/login` | `device` `areaCode` `username` `password` |
| 发短信码 | POST | `/v4/code/phone` | `rawTemplate=login` `areaCode` `account` |
| 验证码登录 | POST | `/v6/user/phoneCodeLogin` | `device` `areaCode` `phone` `code` `region` |
| 刷新 token | POST | `/v3/user/refresh` | `device` `accessToken` `refreshToken` |
| 校验 token | POST | `/v5/user` | {} |

Base URL：`https://api-passport-bj.ninebot.com`

### 签名算法（Passport）

**请求头**（九号 App 的固定集合）：

```
Content-Type: application/json; charset=UTF-8
Clientid:     vehicle_app_prod
Timestamp:    <unix 毫秒>
Sign:         <sha256 hex>
App_version:  610103831
Os:           ios
Os_language:  zh
Os_version:   27.0
Authorization: <token>          # 仅带 token 的请求
```

**canonical** = 以下键值对**按字典序排序**后用 `&` 连接：

```
app_version, clientKey, os, os_language, os_version, timestamp, url, + 请求参数
```

固定值：

```
clientKey = e177176a-3b3e-1513-e26e-d1123034cb66
url       = 请求路径（不含域名），如 /v6/user/login
timestamp = 与请求头 Timestamp 相同的毫秒时间戳
```

**Sign** = `sha256(canonical)` 的**小写 hex**（普通哈希，非 HMAC）。

请求体是明文 JSON（该通道无 NET 加密信封），**但密码字段需 RSA 加密**：

- 公钥：App 内置的 RSA-1024（`PASSPORT_PUBLIC_KEY_PEM`，见 `ninebot_sign.py`）
- 填充：PKCS#1 v1.5
- 编码：**标准 base64 无换行**（Android `Base64.NO_WRAP`，非 URL-safe）

### 实测校验样本

参考实现 `likz0421/ninebot-go` 的注释记录了四份真实抓包样本（`url=/v5/user`，`params={}`）：

| timestamp | 期望 sign 前缀 |
|---|---|
| `1789786169488` | `5be65140` ✅ 复现 |
| `1789786159887` | `2fba2b15` ✅ 复现 |
| `1789786083735` | `c1fe0d30` ✅ 复现 |
| `1789965521546` | `f559cb8d`（Android 样本，平台参数不同） |

> ⚠️ 参考实现自己的常量是 `os_language=zh-hans-cn`，但三份 iOS 抓包**只有在 `os_language=zh` 时才逐字节复现**。
> 本 skill 用 `zh`。第四份 Android 样本的参数组合未能复现（可能 canonical 还含额外字段）。

### 错误码

| resultCode | 含义 |
|---|---|
| `90051` | `sign is null` —— 缺签名头（协议层，说明请求没被解析） |
| `00002` | `account not exists` —— 账号不存在（业务层，签名已通过） |
| `90202` | `send code need verify` —— 图形验证风控 |
| `651001` | 智能服务费到期（车辆数据查询暂停） |

> **判据**：`90051` = 协议没通；`00002`/`90202` = **协议已通**，是业务层拒绝。
> 这个区分很重要——别把业务错误当网络故障去排查。

## 三、代理与直连

九号域名通常**不在本机 mihomo/clash 的 `no_proxy` 白名单**里，走代理会被对端 reset：

```
read tcp 192.0.2.1:54504->43.250.32.142:443: read: connection reset by peer
```

直连实测：TLS 92ms 稳定命中 `43.250.32.142`。

**本 skill 的脚本内部已 `unset` 全部代理变量**。若自行拼接请求，务必同样处理，
或把 `*.ninebot.com` 加入 `no_proxy`。

## 四、参考来源

- 官方 skill（改造自）：ClawHub `ninebot-app/ninebot-device-skill` v1.0.6
- 签名算法参考实现：<https://github.com/likz0421/ninebot-go> → `internal/crypto/passport.go`、`internal/api/passport_headers.go`
- 账号端点参考：<https://github.com/PrintNow/ninebot-monitor> → `app/Services/NinebotApiService.php`
