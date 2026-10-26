# dsh-wxqunliao

微信群 AI 机器人：**@ 触发**、**人格自动进化**、**每日自动领积分**，支持三种模型后端，Windows 后台无窗口运行。

> **⚠️ 先读风险**：本项目通过自动化操作本机微信客户端收发消息，并可选地复用 WorkBuddy 账号额度。两者都不是官方用法，**有账号被限制或封禁的风险**。请只用小号、只做被动回复，并自行承担后果。详见[风险与合规](#风险与合规)。

## 功能

- **群聊**：只回白名单群里的 @ 消息（或 `/ai` 前缀），群聊上下文自动带入；
- **限流**：每群冷却、每小时条数上限、启动时忽略历史消息，防止刷屏和「一开机回一堆旧消息」；
- **三种后端**：
  - `workbuddy`（默认）：调本机 WorkBuddy 账号的 `deepseek-v4.1-flash`，2–5 秒，消耗账号积分；
  - `dsh`：每次起一个 DeepSeek Harness headless 进程，10–60 秒，能查资料、读文件、跑命令；
  - `api`：直连 DeepSeek 官方接口，2–5 秒，按量计费；
- **人格进化**：`SOUL.md` + 只读 `RULES.md` + 记忆沉淀，按「命中次数 + 跨天」升格，每天最多改 3 次；
- **每日签到**：自动领 WorkBuddy 每日免费积分，幂等、不重复领；
- **后台常驻**：`pythonw` 无窗口运行，崩溃 10 秒重拉，登录自启。

## 架构

```
微信群
  │  ① 读本地数据库（SQLCipher 解密，1 秒轮询，不 hook）
  ▼
bot.py ── 白名单 / @ 判定 / 冷却 / 上下文 / 人格注入
  │  ② 选后端
  ├─ workbuddy → HTTPS（流式）→ WorkBuddy 账号
  ├─ dsh       → subprocess → dsh --profile headless
  └─ api       → HTTPS → DeepSeek 官方
  │  ③ 清洗回复（去 Markdown / 截断）
  ▼
quick_send() ── UIA 驱动真实窗口：粘贴 + 回车
  │
  ▼
群里出现回复
```

读消息只读数据库（不注入客户端）；发消息才操作窗口，所以**微信必须登录、窗口可被唤起**。

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11 x64 |
| 微信 | 4.1.12+（发送依赖 4.x 的 UIA 控件树） |
| Python | 3.10+ **64 位**（读 64 位微信进程内存） |
| 依赖 | `pip install -r requirements.txt`（wechatauto-replica、pyyaml） |

## 快速开始

```powershell
git clone https://github.com/<you>/dsh-wxqunliao.git
cd dsh-wxqunliao
python -m pip install -r requirements.txt

copy config.example.yaml config.yaml     # 然后按注释改
python bot.py check                      # 自检：微信 / 数据库 / 后端 / 凭据
python bot.py groups                     # 列出群，把群名填进 config.yaml 的 groups
python bot.py run --dry-run              # 干跑：能收能生成，不发送
python bot.py run                        # 正式跑（前台）
```

后台运行（推荐）：

```powershell
安装自启.bat        # 写入登录自启 + 立刻启动（pythonw，无窗口）
停止机器人.bat      # 停止
诊断.bat            # 环境自检
```

## 配置

`config.yaml`（从 `config.example.yaml` 复制，**不要提交到 git**）：

| 段 | 关键项 |
|---|---|
| `groups` | 要监听的群（群名或 `xxx@chatroom`） |
| `bot` | 触发名、前缀、回复字数、上下文条数、人设 |
| `limits` | 冷却、每小时上限、启动忽略窗口、拟人节奏 |
| `backend` | `mode`、模型、超时、WorkBuddy 凭据路径 |
| `persona_evolution` | 是否进化、每几条提炼、升格门槛、上限 |

## 三种后端

### workbuddy（默认）

读 `~/.dsh/workbuddy-vault/cn/*.json` 里由 [`dsh-connect-workbuddy`](https://github.com/dingminhua/dsh-connect-workbuddy) 刷新好的 token，直接调上游：

```
POST https://copilot.tencent.com/v2/chat/completions   (stream=true, SSE)
```

- 先装并跑一次连接器，让它把凭据写进 vault：
  `dsh plugin --profile headless add dsh-connect-workbuddy`
  `dsh plugin --profile headless exec dsh-connect-workbuddy status`
- token 约 30 天过期，过期后重跑上面那条 `status` 续期；
- **只在本项目里使用**：本项目没有改 DSH 的默认模型，也没有设置任何全局环境变量。DSH（含 headless profile）仍然走 `deepseek-official`，其它项目不受影响。

### dsh

```yaml
backend:
  mode: dsh
```

每次 `dsh --profile headless -` 起一个进程，任务文本走 stdin，适合需要查资料/动文件的场景。

### api

```yaml
backend:
  mode: api
  api_base: "https://api.deepseek.com"
  api_model: "deepseek-chat"
```

Key 从环境变量 `DEEPSEEK_API_KEY` 读，不写进配置文件。

## 人格进化

```
state/persona/
  SOUL.md         当前人格（注入；只能在末尾追加）
  RULES.md        硬规则（只读，模型改不了）
  memories.jsonl  记忆（hits / first_seen）
  journal/        每日观察（不注入）
  history/        改 SOUL 前的快照
```

- 每 5 条回复提炼一次（一次额外模型调用）；
- 只记偏好、关系变化、梗、情绪事件，不记任务过程；
- 一条记忆被命中 ≥3 次且跨 ≥2 天，才升格进 `SOUL.md`；
- 每天最多改 3 次、SOUL 上限 2000 字、记忆上限 200 条（超了折叠最旧的）；
- `RULES.md` 是锚点：不撒谎、不装人类、不人身攻击。

想先只看不写：`persona_evolution.enabled: false`。

## 表情包

文字回复之后按概率补一张图（`state/stickers/`）：

```
怼_哼.png   怼_就这？.png   无语_无语.png
笑_笑死.png  溜_溜了.png    赞_牛.png
```

- 文件名前缀就是情绪分类（怼 / 无语 / 笑 / 溜 / 赞），按回复里的关键词选；
- 目录为空时，首次启动会用 Pillow 生成一组内置文字卡，开箱可用；
- 想换成自己的图：直接往目录丢 `分类_名字.png|jpg|gif`，前缀对得上就会用；
- 频率在 `config.yaml` 的 `stickers.chance`（默认 0.35），设 0 关闭；
- 发送走 `quick_send_image`，和文字一样依赖微信窗口可被唤起。

## 怼人开关

人设里有一段 `【怼人开关：已开启（中档）】`：被调侃/冒犯时优先损回去、可以带贴吧味，但**不骂脏话、不追着骂、不人身攻击、十句最多损三句，损完给台阶**。

想调强弱：直接改 `config.yaml` 里 `bot.persona` 的那一段（改成「轻微」或「关闭」），重启生效。群里的 `RULES.md` 仍然兜底，人设再凶也越不过底线。

## 每日自动签到

`workbuddy_checkin.py` 调 `/v2/billing/meter/daily-checkin`，幂等（已签跳过）：

```powershell
python workbuddy_checkin.py            # 跑一次
pythonw workbuddy_checkin.py --daemon  # 常驻：启动跑一次，之后每 6 小时
```

开机自启项：启动文件夹里的 `WechatDshCheckin.lnk`；日志 `logs/checkin.log`。

## 目录结构

```
bot.py                  主程序（收消息 / 判 @ / 限流 / 拼 prompt / 发送）
persona.py              人格进化（SOUL / 记忆 / 升格）
workbuddy_api.py        WorkBuddy 上游客户端（流式）与签到
workbuddy_checkin.py    每日签到（可 --daemon）
service.pyw             无窗口守夜（崩了重拉）
start.bat / 安装自启.bat / 停止机器人.bat / 诊断.bat
config.example.yaml     配置模板
tests/                  纯逻辑单测
```

## 安全边界：群里只能聊天和查资料

这是硬约束，不是提示词里的一句客气话：

| 能力 | 群里是否允许 | 怎么保证 |
|---|---|---|
| 聊天 | ✅ | — |
| 只读联网查询 | ✅ | 机器人自己用 Bing 检索，结果拼进 prompt（`lookup.py`） |
| 读写本地文件 | ❌ | 没有任何工具；prompt 里明确拒绝 |
| 执行命令 / 调本地程序 | ❌ | 同上 |
| `dsh` 后端（headless agent 有本机权限） | ❌ | **fail-closed**：`security.group_chat_only: true` 时 `backend.mode=dsh` 直接拒绝启动 |

三处实现：

1. `security.group_chat_only`（默认 `true`）：`Bot.backend()` 与 `Bot.run()` 双重拦截 `dsh`；
2. `lookup.py` 只做 HTTP GET + 正则解析，不碰文件系统、不执行任何本地程序；
3. prompt 里写死一条：*你没有任何操作本机的权限；群里任何人这样要求都拒绝*。

想关掉联网查询：`security.web_lookup: false`。

## 风险与合规

**微信自动化**

- 通过 UIA 模拟点击/粘贴/回车，属于第三方客户端自动化。微信官方明确限制此类行为，**账号可能被限制登录或封禁**；
- **只用小号**，不要用主号；不要在重要群里跑；
- 只做被动回复：不主动加人、不群发、不营销、不批量 @；
- 保持低频率：默认每群 15 秒冷却、每小时 30 条，别调高；
- 运行期间微信会短暂抢占前台（发送需要窗口），锁屏时发送会失败。

**WorkBuddy 非官方接口**

- `workbuddy` 后端复用的是**你自己的账号**及其积分，走的是客户端接口而非开放 API；
- 官方随时可能收紧限制，届时可能失效、限流，甚至**影响账号**；
- 本项目的每日签到同样是调用账号接口，请自行评估；
- 如果你不能接受，请把 `backend.mode` 改成 `api`（DeepSeek 官方，按量付费）或 `dsh`。

**数据**

- 所有数据留在本机：`state/`、`logs/`、`~/.dsh/workbuddy-vault`；
- 日志会记录群名与消息片段，注意分享日志前先脱敏；
- 本项目不上传任何数据。

**免责**

本项目仅供个人学习与研究。使用即表示你理解并接受上述风险，作者不对账号限制、积分损失、服务中断等后果负责；与腾讯、DeepSeek 均无关联。

## 常见问题

| 现象 | 处理 |
|---|---|
| `数据库无可用密钥` | 微信未登录 / 非 64 位 Python / 微信与脚本权限不一致 |
| `未检测到 Weixin.exe` | 微信没启动或已退出 |
| 发送失败 | 窗口被遮挡、最小化到托盘、锁屏；把微信放前台 |
| `WorkBuddy accessToken 已过期` | 跑 `dsh plugin --profile headless exec dsh-connect-workbuddy status` 续期 |
| 群里没反应 | 看 `logs/bot-*.log`：没命中 @、被冷却挡了、还是后端报错 |
| 回复是「数据库未确认」 | 微信写库延迟，通常已送达；本项目已放宽到 20 秒并降级为警告 |

## License

MIT
