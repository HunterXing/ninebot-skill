---
name: ninebot-skill
description: Use when 查询九号电动车（Ninebot）的车辆状态——电量、位置、充电状态、开关机、续航、骑行记录。
license: PolyForm-Noncommercial-1.0.0
compatibility: Requires Python 3.8+, network access to ninebot.com. Two auth backends: official Device Service key (NINEBOT_DEVICESERVICE_KEY) or your own Ninebot account.
metadata:
  hermes:
    tags: [ninebot, 九号, 电动车, 状态查询, 电量, 定位]
    version: 1.1.0
    author: HunterXing
    upstream: "改造自九号官方 ClawHub skill (ninebot-app/ninebot-device-skill v1.0.6)"
  openclaw:
    requires:
      env: []
---

# 九号电动车状态查询

Query a Ninebot (九号) vehicle's live state: battery, location, charging status,
power state, estimated range and ride history.

Works on Hermes, OpenClaw, Claude Code and any other agent that can run a
Python script — the skill is plain `SKILL.md` + `scripts/`, with no plugin or
framework dependency.

## ⚠️ 凭据作用域与使用边界（用真账号前必读）

**不要用主账号，只用子账号。** 九号 Passport 登录拿到的是**你的账号全量身份**，
不是「只读车况」的凭据。这个 token 可以改密码、绑手机、进商城订单、以你身份操作。
**它不被限制在「读车辆状态」范围内。**

按最小权限排序（只有前两种可接受）：

| 方案 | 权限范围 | 前提 |
|---|---|---|
| **① 官方 Device Service Key** | 仅车辆只读接口 | 需 OpenClaw 内测名额 |
| **② 九号「用车人 / 家庭账号」子账号**（推荐） | 车主逐项授权（看位置 / 看骑行记录）；**控车权限不要给** | 需先在九号出行 APP 里接受邀请 + 蓝牙配对 |
| ~~③ 主账号~~ | **全量身份，泄露等于交出账号** | **不要用** |

**使用边界**：本项目仅供学习研究，与九号公司（Segway-Ninebot）无隶属关系，
不提供任何形式的担保；只读、不含控车指令；账号风险与数据可用性由使用者自行承担。
完整条款见仓库 `README.md` 的「免责声明」。

⚠️ **子账号不是 API 专用的只读凭据**——它仍是一个完整 Passport 账号（能登 APP、进商城），
只是**权限由车主收窄**。用它登录本 skill 时，走的是同一套 Passport 接口。

另外注意：子账号必须在**九号出行 APP** 里完成绑定（邀请 → 接受 → 蓝牙配对），
纯 API 建不出来。

## ⚡ 快速回答：一条命令出成稿（首选）

问「车况 / 电量 / 位置 / 昨天骑了多远」时**不要现拉接口再组织语言**——跑一条命令，
stdout 就是成稿，**原样转给用户，不做二次加工**。这是本 skill 的默认路径。

```bash
bash ~/.hermes/scripts/ninebot_status.sh            # 车况（命中快照 ≈60ms / 过期刷新 ≈2s）
bash ~/.hermes/scripts/ninebot_status.sh battery    # 电池：电压、温度、续航
bash ~/.hermes/scripts/ninebot_status.sh location   # 位置、门牌、锁车
bash ~/.hermes/scripts/ninebot_status.sh ride       # 今日/本周/本月/逐日/最近一次
bash ~/.hermes/scripts/ninebot_status.sh weather    # 实时 + 今天 + 明天
bash ~/.hermes/scripts/ninebot_status.sh params     # 全参数 key=value（非固定问题取数）
bash ~/.hermes/scripts/ninebot_status.sh full       # 完整日报版
bash ~/.hermes/scripts/ninebot_status.sh status 0   # 强制刷新后再答（0 = 不接受任何陈旧）
bash ~/.hermes/scripts/ninebot_status_card.sh       # 状态图：中间真车图，四周状态
```

### 🖼 状态图（中间是真车，四周是状态）

车况是「一眼看」的东西——电量多少、停在哪、锁没锁，手机上一张图比一段话快。
`ninebot_card_image.py` 把快照渲染成一张图：中间是九号官方车型图（按本车配色），
四周六张状态卡（电量 / 续航 / 里程 / 状态 / 位置 / 天气），车图下方是近 7 日骑行柱状图。

```bash
bash ~/.hermes/scripts/ninebot_status_card.sh             # stdout 一行 MEDIA:<路径>
bash ~/.hermes/scripts/ninebot_status_card.sh --refresh   # 先强制刷新快照
```

**车型图从哪来**：`my-vehicle` 接口自带本车配色的官方图（`img_url` / `v6_light_img_url`）。
它**随快照一起落盘**（`snapshot.json` 的 `device.imgUrl`），渲染时离线取——
不二次联网，签名 URL 也不会过期后失效。**URL 绝不能写死**（带 `nbchecksignv1` 签名，会轮换）。
图片缓存在 `~/.hermes/cache/ninebot/model.png`，按 URL 比对决定是否重取。

**版式契约**（改的时候别破坏）：
- **横向板（1600×1000）**：官方图是 1.5:1 横图，方形板里中间列会变成细长条，
  车图只能占面板四分之一（实测宽度 78% / 高度 25%）。
- **九宫格显式定位**：车图 `grid-row:1 / span 3` 占中列全高，其余六张卡
  用 `grid-column/grid-row` 显式摆位。靠自动排布会留下空角（实测右下两格全空）。
- **卡片要撑满**：卡片裹在 wrapper 里，wrapper `flex:1`、卡片 `flex:1 1 auto`，
  否则卡片保持内容高度、所有空白堆在卡下（实测像半空的模板）。
- **一个数字一个口径**：续航卡三个数（实景 / 标准工况 / 按骑行习惯）必须各自带口径标签；
  电池温度标「电池」以区别于环境温度。
- **里程两个口径别混**：`vehicle-info` 的 `total_mileage` 是**整车原生里程**（仪表读数），
  标「里程表」；「累计骑行」是本账号可见骑行记录按月累加，**只覆盖账号可见区间**。
  有原生里程时两个都显示（数值不同时），没有时只显示「累计骑行」。
  ⚠️ 共享账号照样读得到原生里程 —— `my-vehicle` 里那个同名字段是 `null`，
  但 `vehicle-info` 照常返回，别拿前者的空值断言「拿不到 ODO」。
- **缺值印 `—`**，不印 0（上游「不支持」和「真的是 0」长得一样）。

**图片落点必须在媒体白名单目录**（`~/.hermes/cache/images/`），否则投递层会丢掉图片
却仍报成功（见 `validate_media_delivery_path`）。

**为什么快**：`ninebot_snapshot.py` 把所有可读数据抓一次落成
`~/.hermes/state/ninebot/snapshot.json`（含 `fetched_at`/`epoch`），
`ninebot_card.py` 只做模板填充、**不发网络请求**。600 秒内直接复用（实测 52ms），
过期才回源（实测 1.9s）。慢的从来不是接口，是「每次重新取数 + 让模型重新组织文字」。

**非固定问题直接查快照，别再打接口**：

```bash
python3 - <<'PY'
import json, os
s = json.load(open(os.path.expanduser('~/.hermes/state/ninebot/snapshot.json')))
print(s['status']['voltage'], s['battery']['cycles'], s['mileage']['week'])
PY
# 或直接 grep 扁平参数：
bash ~/.hermes/scripts/ninebot_status.sh params | grep -E 'voltage|cycles'
```

问什么 → 取哪个字段：

| 问题 | 字段 |
|---|---|
| 电量 / 续航 | `status.battery` / `status.preciseEstimateMileage` |
| 电压 / 电池温度 | `status.voltage` / `status.temperature` |
| 位置 / 锁车 | `status.location` / `status.lock` |
| 开关机 / 充电 | `status.powerStatus` / `status.chargingState` |
| 今天·本周·本月骑了多远 | `mileage.day` / `mileage.week` / `mileage.month` |
| 最近一次骑行 | `travel.list`，按 `end_time` 倒序 —— **不是** index 0 |
| 天气 | `weather.now` / `weather.days[0]` |
| 智能服务到期 | `status.smartServiceExpired`（1 = 已过期） |

**陈旧必须可见**：刷新失败时沿用上一份快照，卡片末尾带
`⚠️ 数据来自 X 小时前，刷新失败：…`。看到这行就是数据不是此刻的，回答时要一并说明，
不能当成实时值。

**要加一行输出就改模板，不要在回答里手工拼数据**——手工拼的数值没有回读依据，
容易和快照不一致。模板函数都在 `scripts/ninebot_card.py` 里，加字段只动一处。

## 快速开始

```bash
# 底层单次查询（诊断用；日常问答走上面的快速路径）
python3 scripts/ninebot_query.py --device-name "小九"

# 多台车时不指定会返回候选列表（exit 3）
python3 scripts/ninebot_query.py
```

输出是稳定的 JSON：

```json
{
  "device_name": "小九",
  "battery": 78,
  "powerStatus": "OFF",
  "chargingState": "not_charge",
  "location": "某市某区某路某号",
  "estimateMileage": 42.6,
  "remainChargingTime": ""
}
```

## 两种认证后端

| | official | account |
|---|---|---|
| 凭据 | `NINEBOT_DEVICESERVICE_KEY`（内测 Key） | 自己的九号账号 |
| 获取 | 报名九号 OpenClaw 内测（九号出行 APP「圈子」） | 本机跑一次 `ninebot_login.py` |
| 端点 | `/ai-skill/api/device/info/*` | 九号业务通道（NET 加密）：`ebike.ninebot.com` / `steeldust.ninebot.com` |
| 需要内测名额 | 是 | 否 |

**自动选择**：显式 `--backend` > 环境变量里有 Key > 已有本地会话 > account 兜底。

### official 后端

```bash
export NINEBOT_DEVICESERVICE_KEY=your_key
python3 scripts/ninebot_query.py --backend official --device-name "小九"
```

Key **只从环境变量读**——没有 `--api-key` 参数，也没有 config.json 里的 key 字段。
两者都会把凭据写进 shell history 或 `ps` 输出。

### account 后端

```bash
# 密码登录（推荐；密码不回显、不落盘）
python3 scripts/ninebot_login.py --user 13800000000
```

会话保存在 `~/.hermes/secrets/ninebot_session.json`（目录 700 / 文件 600）。
换目录用环境变量 `NINEBOT_SESSION_DIR`。

⚠️ **登录后还需要一个 business uid**（账号级 9 位标识，如 `123456789`，示意值）：
车辆接口用它标识调用方，传 19 位 passport uuid 会被拒（`401905 用户不存在`）。
会话文件里的 `biz_uid` 字段就是它；取不到时业务通道会拒绝，
见 README「业务通道身份两段式」一节。

⚠️ **短信登录在纯 API 下基本走不通**：九号对 `/v4/code/phone` 要求图形验证
（`resultCode=90202 send code need verify`）。走不通时在**九号出行 APP** 里用短信登录一次，
再回来试。密码登录不受此限制。

## 在 Hermes 里用

### 方式 A — 当脚本用（最简单，无需 MCP）

```bash
bash ~/.hermes/scripts/ninebot_status.sh              # 车况成稿，命中快照 ≈60ms
python3 ~/.hermes/skills/ninebot-skill/scripts/ninebot_query.py --device-name "小九"   # 原始 JSON
```

**方式 B — 配成 MCP server**（工具化给模型直接调）

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  ninebot:
    command: python3
    args: ["/abs/path/to/ninebot-skill/scripts/ninebot_mcp.py"]
    enabled: true
    lazy: true
```

改 `config.yaml` 走 `hermes config set`（该文件对 agent 直写有保护）。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--device-name` | 按名字选车 |
| `--device-sn` | 按 SN 选车（最精确） |
| `--backend official\|account` | 强制后端 |
| `--config <path>` | 显式指定配置 JSON（**不会**自动去 CWD 找） |
| `--raw` | 输出原始上游字段，不做归一化 |

## 退出码

| 码 | 含义 | 该怎么办 |
|---|---|---|
| 0 | 成功 | — |
| 2 | 缺凭据 / 没有设备 | 看返回的 `error` 与 `hint` |
| 3 | 账号下有多台车 | 用返回的 `choose_device` 里的 sn/name 重查 |
| 4 | 指定的设备找不到 | 同上 |

## 字段映射（官方端点）

| 含义 | 路径 |
|---|---|
| 设备列表 | `data`（每项 `sn` / `deviceName`） |
| 电量 | `data.dumpEnergy` |
| 开关机 | `data.powerStatus`（`0`=OFF / `1`=ON） |
| 位置 | `data.locationInfo.locationDesc` |
| 预估里程 | `data.estimateMileage` |
| 充电状态 | `data.chargingState`（`1`=charging） |
| 剩余充电时间 | `data.remainChargeTime` |

## 排障

| 现象 | 原因与解法 |
|---|---|
| 高德回 `INVALID_USER_SCODE` / `10001` | Key 的平台类型选错了——必须是**「Web 服务」**，不是「Web端 (JS API)」。控制台创建后不能改，只能重新申请；申请时一并拿安全密钥 jscode |
| `90051 sign is null` | 请求没带 Passport 签名头 —— 本 skill 自带 `ninebot_sign.py`，别手写 curl |
| `ERROR: 快照不可用且刷新失败` | 没有可用旧快照时的硬失败：会话失效（重跑 `ninebot_login.py`）或网络被代理掐断。有旧快照时会降级并标 `⚠️ 数据来自…` |
| `connection reset by peer` | 本机代理（mihomo/clash）掐了连接。九号域名通常不在 `no_proxy` 白名单里 —— 本 skill 已在脚本内 `unset` 代理变量 |
| `90202 send code need verify` | 九号风控要图形验证 —— 走密码登录，或在 APP 里手动登录一次 |
| `00002 account not exists` | 账号不存在 —— 检查手机号 / 区号 |
| `resultCode=651001` 智能服务费到期 | 车端功能正常，但**云端数据查询被停**（九号智能服务 66 元/年，新车首年免费）。欠费过久云盒 eSIM 会销号，要花钱换云盒 |
| 查到设备但字段为空 | 车离线（没电 / 无信号 / 未上报）—— 云盒数据是车端上报的快照 |

## 安全须知

- 凭据只走环境变量或本地 600 会话文件；**不要**把 key/密码写进命令行参数或会进 git 的文件。
- 不要在不可信目录里跑带 `--config` 的命令：配置文件里的 `base_url` 会改变请求目标。
- 多设备列表只输出 `sn` + `name`（不转储上游完整对象），避免多余元数据进对话记录。
- 本 skill 只做**只读**查询，不含任何控车指令。

## 参考

- `scripts/ninebot_snapshot.py` —— 抓一次全量数据写本地快照（`--refresh` / `--max-age` / `--summary`）
- `scripts/ninebot_card.py` —— 从快照渲染模板（`status` / `full` / `battery` / `location` / `ride` / `weather` / `params` / `--json`）
- `scripts/ninebot_report.py` —— `collect_snapshot()` 取数 + `render()` 渲染；`--live` 是诊断模式
- `~/.hermes/scripts/ninebot_status.sh` —— 秒答入口（快照 + 模板）
- `~/.hermes/scripts/ninebot_daily_status.sh` —— 每日 07:45 日报 cron 的 wrapper（刷新快照 → 渲染完整版）
- `references/api-spec.md` —— 端点、字段、签名算法的完整说明
- 上游：九号官方 ClawHub skill `ninebot-app/ninebot-device-skill` v1.0.6（MIT/官方发布）
- 签名算法参考实现：<https://github.com/likz0421/ninebot-go>（`internal/crypto/passport.go`，含四份真实抓包样本）
- 账号端点参考：<https://github.com/PrintNow/ninebot-monitor>（`app/Services/NinebotApiService.php`）
