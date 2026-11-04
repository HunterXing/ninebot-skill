# ninebot-skill

九号电动车（Ninebot）状态查询 —— 改造自九号官方 ClawHub skill，适配 Hermes / OpenClaw / Claude Code 等通用 Agent。

查电量、位置、充电状态、开关机、预估续航。**只读**，不含任何控车指令。

> [!CAUTION]
> **⚠️ 使用前必读**
>
> **① 不要用主账号，只用子账号。** 九号 Passport 登录拿到的是**账号全量身份**——
> 能改密码、绑手机、进商城订单。主账号一旦泄露，损失不限于车况数据。
> 请在九号出行 APP 里建「用车人 / 家庭账号」子账号，由车主**逐项授权**
> （看位置、看骑行记录），**不要给远程控车权限**，再用子账号登录本工具。
>
> **② 仅供学习研究，不保证可用。** 本项目为非官方实现，与九号公司（Segway-Ninebot）
> 无隶属关系，不提供任何形式的担保。账号风险、数据准确性、接口变更导致的中断，
> 均由使用者自行承担。**禁止任何商业用途；请勿用于控车 / 解锁等实际控制场景。**
>
> 继续使用即表示你理解并接受以上两条。

```
九号 Fz1 60 每日状态
电量 78%
预估续航 42.6 km
状态 已关机 · 未充电
位置 某市某区某路某号
```

## 为什么是改造版

九号 2026-03 发布了官方 `ninebot-device-skill`（ClawHub，1.4k star），但它是 **OpenClaw 生态**的 skill 包——Hermes 装不了；而且只支持一种认证：需要报名 OpenClaw **内测**拿 `NINEBOT_DEVICESERVICE_KEY`。

本仓库在官方基础上做了三件事：

1. **双认证后端** —— 保留官方 Device Service（内测 Key），同时实现**账号通道**：用你自己的九号账号登录，**不需要内测名额**。
2. **跨 Agent 兼容** —— 纯 `SKILL.md` + `scripts/`，零框架依赖；frontmatter 只用 Agent Skills 标准的六个字段，Hermes / OpenClaw / Claude Code 都能吃。
3. **修掉上游的三个安全问题**（ClawHub 自家 scanner 报告过）：

   | 上游问题 | 本仓库做法 |
   |---|---|
   | `base_url` 可配 → 篡改的 `config.json` 可把 API Key 引去别的域名 | **不再自动去 CWD 找 `config.json`**；只有显式 `--config <path>` 才读 |
   | `--api-key` 进 shell history；`config.json` 明文存 Key 且不校验权限 | **取消 `--api-key` 参数**，Key 只从环境变量读；会话文件强制 600 / 目录 700 |
   | 多设备时转储**完整**上游设备对象（含图片等元数据） | 只投影 `sn` + `name` |

另外还修了上游的稳定性问题：连接层失败自动重试（业务层错误不重试）、脚本内自动 `unset` 代理变量。

## ⚠️ 凭据作用域（用真账号前必读）

九号 Passport 登录拿到的是**账号全量身份**，不是「只读车况」凭据——能改密码、绑手机、进商城订单。

**不要用主账号。** 按最小权限，只有前两种可以接受：

| 方案 | 权限范围 | 前提 |
|---|---|---|
| **① 官方 Device Service Key** | 仅车辆只读接口（最干净） | OpenClaw 内测名额 |
| **② 九号「用车人 / 家庭账号」子账号**（推荐） | 车主逐项授权：看位置 / 看骑行记录 | 先在 APP 里接受邀请 + 蓝牙配对 |
| ~~③ 主账号~~ | **全量身份，泄露等于交出账号** | **不要用** |

子账号仍是一个完整 Passport 账号（能登 APP、进商城），只是**权限由车主收窄**；且必须在 APP 里绑定，纯 API 建不出来。

登录前先把子账号的权限收到只读那一档：**位置、骑行记录可以开；远程开关锁、鸣笛找车不要开。**
本仓库的脚本不含任何控车指令，但它持有的是子账号的完整 token ——
**权限边界由账号决定，不由代码决定**。

会话文件落在 `~/.hermes/secrets/ninebot_session.json`（目录 700 / 文件 600），与本机其他凭据同一套规矩。

## 安装

### 作为 Agent Skill

```bash
git clone https://github.com/HunterXing/ninebot-skill.git
cp -r ninebot-skill ~/.hermes/skills/          # Hermes
cp -r ninebot-skill ~/.openclaw/skills/         # OpenClaw
cp -r ninebot-skill ~/.claude/skills/           # Claude Code
```

### 作为 MCP server

```bash
hermes config set mcp_servers.ninebot.command python3
hermes config set mcp_servers.ninebot.args '["/abs/path/to/ninebot-skill/scripts/ninebot_mcp.py"]'
hermes config set mcp_servers.ninebot.enabled true
hermes config set mcp_servers.ninebot.lazy true
```

MCP 工具：`list_devices` / `vehicle_status` / `vehicle_battery` / `backend_info`

## 配置认证

### 后端 A：账号登录（推荐，不需内测名额）

```bash
python3 scripts/ninebot_login.py --user 13800000000
```

密码用 `getpass` 读取，不回显、不落盘。会话存 `~/.hermes/secrets/ninebot_session.json`（目录 700 / 文件 600，可用 `NINEBOT_SESSION_DIR` 改）。

> ⚠️ **短信登录在纯 API 下基本走不通**——九号对 `/v4/code/phone` 要求图形验证（`90202`）。
> 走不通就改用密码登录，或在九号出行 APP 里手动登录一次。

### 后端 B：官方 Device Service Key

```bash
export NINEBOT_DEVICESERVICE_KEY=your_key
python3 scripts/ninebot_query.py --backend official --device-name "小九"
```

Key 从九号出行 APP「圈子」的 OpenClaw 内测活动获取。

**不配任何凭据时会自动按 A → B 顺序探测**，也可以用 `--backend` 强制。

## 用法

```bash
python3 scripts/ninebot_query.py --device-name "小九"
python3 scripts/ninebot_query.py --device-sn <SN>
python3 scripts/ninebot_query.py --raw          # 原始上游字段
```

输出稳定 JSON：

```json
{
  "device_name": "小九",
  "battery": "78",
  "powerStatus": "OFF",
  "chargingState": "not_charge",
  "location": null,
  "lat": 39.90923,
  "lon": 117.16,
  "lock": 1,
  "estimateMileage": 42.6
}
```

> 账号后端的位置是**经纬度**（`loc.lat` / `loc.lon`），没有文字地址——官方 Device
> Service 才有 `locationInfo.locationDesc`。

账号下有多台车时，不指定设备会返回候选列表并以退出码 3 结束。

## 排障速查

| 现象 | 原因 |
|---|---|
| `90051 sign is null` | 请求缺 Passport 签名头——用本 skill 的脚本，别手写 curl |
| `00002 account not exists` | 手机号不是九号账号（**签名已通过**，是业务层拒绝） |
| `90014 username or password incorrect` | 凭据被拒——**九号默认是短信验证码登录，「登录密码」需在 APP「我的→设置→账号绑定和管理→登录密码」单独设置过**；从没设过密码则填什么都报这个错 |
| `90202 send code need verify` | 九号风控要图形验证——短信通道走不通，改密码登录或先在 APP 登录一次 |
| `connection reset by peer` | 本机代理掐了连接——脚本已自动 `unset` 代理变量 |
| `340000 … (4102)` | 加密包没带身份——请求信封或 `Uid` 头缺失 |
| `340000 … (4103)` | 身份形式不对——车辆接口要用 **business uid**（短 uid），不是 passport uuid |
| HTTP 404 `oms-device-api-route-ai` | 打到已下线的 `/app-api/inner/device/ai/*` 旧路由 |
| `651001` 智能服务费到期 | 云端数据查询被停（66 元/年，新车首年免费）；欠费过久云盒 eSIM 销号要换硬件 |

> **错误码直接告诉你卡在哪一层**：`90051` = 签名层（工具的 bug）｜`00002` = 账号层（号码不对）｜`90014` = 凭据层（密码错或没设过）。

失败时错误码会自动落 `~/.hermes/logs/ninebot_login.log`（只记错误元数据，不记手机号/密码/token）。

## 账号登录：密码是明文提交的（重要）

`POST /v6/user/login` 的 `password` 字段**直接放明文**，不加密。这不是疏忽，
是九号官方 CLI 的实际行为——我们用它的 `--passport-base` 覆盖参数指向本地
假网关，把它真实发出的请求抓下来逐字节比对确认过：

```json
{"areaCode":"86","device":"ANDROID","password":"<明文>","username":"13800000000"}
```

参考实现 `ninebot-go` 里虽然带着 `PassportEncryptRSA` 和内置 RSA 公钥，
但那个函数**从未被调用**；`LoginWithDevice` 就是把原始密码塞进 body。

**为什么必须这样**：九号服务端直接比对提交的字符串。我们若把密码 RSA 加密
再发，服务端拿密文去比密码，结果必然是
`resultCode=90014 username or password incorrect`——**哪怕密码完全正确**。
这正是本项目一度卡住的原因。

请求本身走 TLS，且 `Sign` 头覆盖整个 body，明文不会离开加密信道。

**不要"加固"这段代码**：`tests/test_official_parity.py` 会锁死这个行为。

## 业务通道身份两段式

车辆接口不认 Passport token，它认 **business uid** —— 一个账号级的 9 位标识
（如 `123456789`，此处为示意值）：

```
Passport access_token ──business-login──> business uid（账号级）
                                              │
                          车辆接口用这个 uid ▼
```

- 传 19 位 passport uuid → `401905 用户不存在`
- 传 business uid → `code=1 成功`

business uid 是**账号级**的：token 刷新后不变。会话文件里的 `biz_uid` 字段
存着它，登录时若 business-login 拿不到新值，会沿用已保存的。

⚠️ 九号网关的 business-login 端点对请求形状很挑。本仓库未能复现出官方
CLI 二进制所用的那版（内层体比我们的多约 1300 字节），因此 business uid
目前靠复用会话里的值；账号侧 uid 若发生变更，需要重新获取。

## 位置反查成中文地址

九号的车辆接口只给经纬度，不给地址文本。`scripts/ninebot_geo.py` 用第三方
地理 API 补上中文地址，多源回退：

| 优先级 | 源 | 需要凭据 | 精度 | 联网要求 |
|---|---|---|---|---|
| 1 | 高德逆地理编码 | `AMAP_KEY` + `AMAP_JSCODE` | 街道级 | 国内直连 |
| 2 | Nominatim (OSM) | 无 | 路名 / 社区级 | **必须走代理** |


**整个 skill 只需要这一个高德 Key**：逆地理编码和天气都走它，不需要再注册
第二个天气服务。天气按 adcode 查询——adcode 由位置反查顺手带出，不额外请求。

天气字段覆盖：实时（天气/温度/湿度/风向风力）+ 未来 4 天预报。
**不含**日出日落、紫外线、降水量（高德不提供）；下雨与否由预报文本判断，
播报只展示今天。

⚠️ **创建 Key 时平台类型必须选「Web 服务」，不是「Web端 (JS API)」。**
逆地理编码和天气都是服务端 REST 接口；`Web端 (JS API)` 的 Key 只给浏览器 SDK 用，
直接调 REST 会回 `INVALID_USER_SCODE` / `10001`。这个类型在控制台创建后**不能改**，
选错只能重新申请一个。

高德控制台创建 Key 时会同时给出 **Key** 和 **安全密钥 jscode**。是否必需取决于
该 Key 是否开启了安全密钥校验：实测有些 Key 只给 Key 即可，有些会回 `10003`
（缺 jscode）。配了 jscode 就一直带上，两种情况都能过：

```
AMAP_KEY=...            # 32 位
AMAP_JSCODE=...         # 安全密钥
NINEBOT_COORD_SYS=...   # wgs84（默认）或 gcj02
```

放在 `~/.hermes/secrets/map.env`（权限 600），代码会直接读这个文件，
cron 的最小环境也能读到。

结果按坐标缓存 6 小时（`~/.hermes/cache/ninebot_geo.json`），车没挪窝时
不重复请求。

**坐标系**：车机上报通常是 WGS-84；GCJ-02 与 WGS-84 在国内差约 500 米，
足以让「路名」不同，但不影响「区 / 社区」这一级。用 `NINEBOT_COORD_SYS=gcj02`
可切换。`NINEBOT_GEO_VERIFY=1` 会做双坐标系交叉验证并给出
`precision: street / street-approx / area-only`（诊断用，会多发两次请求）。

代理策略与九号 API **相反**：九号必须直连（走代理会被风控 reset），
Nominatim 直连不通必须走代理。`ninebot_geo.py` 按源各自决定，不共用代理设置。

仓库里不写死任何坐标 —— 坐标永远来自九号接口。

## 与官方 CLI 的逐字节一致性

`tests/test_official_parity.py` 把我们的请求和官方二进制的真实请求逐字节对比
（Sign / 全部 header 名与值 / body），全部相同才算通过。

跑法：

```bash
python3 tests/test_official_parity.py   # 需要本机装了 ninecli
```

CI 上**不装** `ninecli`（信任链里不放第三方二进制，且 CI 外网下载不可靠），
该测试在 CI 上自动 skip，在维护者机器上跑。

我们与官方客户端的差异点（都曾导致登录失败）：

| 项 | 早期实现 | 官方 |
|---|---|---|
| `password` | RSA 密文 | **明文** |
| `device` | `IOS` | `ANDROID` |
| `Os` | `ios` | `Android` |
| `Os_language` | `zh` | `zh-hans-cn` |
| `Os_version` | `27.0` | `13` |
| `App_version` | `610103831` | `610063322` |
| header 名大小写 | `App_Version`（urllib 的 `.title()` 所致） | `App_version` |

⚠️ header 名大小写：`urllib.request` 会对 header 名做 `.title()`，把
`App_version` 改成 `App_Version`。HTTP 规范说 header 名不敏感，但服务端未必——
所以改用 `http.client`，它原样透传。

## 测试

```bash
python3 tests/test_ninebot_skill.py     # 82 个单元测试，不依赖网络
python3 tests/test_card_templates.py     # 16 个模板 / 快照缓存测试，不依赖网络
python3 tests/e2e_fake_api.py            # 官方后端：真 CLI 打假 HTTP 端点
python3 tests/e2e_account_net.py         # 账号后端：真客户端打假 NET 网关
```

覆盖签名（对照 3 份真实抓包样本逐字节复现）、NET 加密（AES 往返 / 密钥派生 /
信封结构 / 响应解密）、三个安全修复、端点配置、后端选择、重试策略。

`e2e_account_net.py` 里的假网关**按真网关的方式解密**：用 RSA 私钥从信封的 `k`
还原 AES 会话密钥、AES 解密 `d`、校验 `h` 的 md5，再用派生的响应密钥加密应答——
所以整个线格式两个方向都被验过。

## 依赖

**零第三方 Python 依赖**——只用 Python 3.8+ 标准库（`hashlib` / `urllib` / `json` /
`argparse` / `subprocess`）。

- Passport 签名与 RSA-1024 PKCS#1 v1.5：手写的最小实现，不依赖 `cryptography`
- NET 加密的 AES-128-CBC：调用**系统 `openssl`**（本机已装）做 AES，RSA 与密钥派生仍是纯 Python

**信任链里没有任何第三方二进制**——`ninecli` 只发预编译 wheel（无 sdist、无源码），
把账号密码交给一个没人能读懂的二进制不可接受，因此整条加密链路用纯 Python 重写。

## 致谢与出处

- 上游官方 skill：ClawHub `ninebot-app/ninebot-device-skill` v1.0.6
- Passport 签名与 NET 加密算法参考（MIT）：<https://github.com/likz0421/ninebot-go>
  （`internal/crypto/passport.go` 含四份真实抓包样本；`internal/crypto/netease.go`、
  `keyderive.go`、`wrapper.go` 为 NET 层移植来源）
- 九号关停旧账号 API 路由的记录：<https://github.com/Wuty-zju/ha_ninebot/issues/1>

## 免责声明

1. **定位是学习与研究。** 本项目用于学习移动端 API 的签名、加密与协议分析，不是九号官方产品。
   代码以 **[PolyForm Noncommercial 1.0.0](LICENSE)** 发布，**仅允许非商业用途，
   不允许任何商业使用**。
2. **与九号公司无隶属关系。** 本项目为第三方独立实现，未获九号公司（Segway-Ninebot）授权、
   认可或背书；相关商标与名称归其权利人所有。
3. **只读，不含控车。** 仓库内所有脚本只调用车辆状态 / 电池 / 骑行记录等查询接口，
   不含开锁、上锁、鸣笛、启动等任何控制指令。
4. **风险自担。** 使用本项目可能违反九号用户协议，可能导致账号被风控、限流或封禁。
   是否使用、如何使用由使用者自行判断并承担全部后果；作者不提供任何明示或暗示的担保，
   也不对任何直接或间接损失负责。
5. **数据可用性不保证。** 车辆数据依赖九号云服务与智能服务订阅状态；
   接口变更、欠费、车机离线都会导致查询失败，本项目不对数据的完整性、准确性、
   及时性作任何承诺。
6. **凭据自负其责。** 请使用子账号、按最小权限授权，并自行保管会话文件；
   因凭据泄露造成的损失，作者不承担责任。

## 许可

代码以 [**PolyForm Noncommercial License 1.0.0**](LICENSE) 发布：

- **允许**：个人学习、研究、实验、爱好项目，以及慈善机构、教育机构、公共研究机构、
  公共安全 / 卫生机构、环保组织、政府机构的非商业使用。
- **不允许**：任何商业用途 —— 包括企业内部使用、SaaS 服务、嵌入商业产品、付费分发。
- 分发时须随附本许可条款或指向它的 URL，以及 `Required Notice:` 行。

简言之：**只能用于非商业目的，任何商业使用一律不允许。**
