# QQ AI 聊天机器人（NapCat + GLM-5.3）

一个把 **GLM-5.3** 大模型接入 QQ 聊天的机器人程序：

- **群聊**：@机器人 即触发 AI 回复（能看到上下文里 @ 前后的昵称）
- **私聊**：好友私聊机器人即自动回复（可配白名单）
- **记忆**：每个群 / 每个私聊对象独立保存多轮对话历史（默认 20 轮），重启不丢失
- **人格**：机器人的名字和性格（system prompt）在配置文件里自定义

```
QQ 服务器 ⇄ NapCatQQ（扫码登录你的QQ号）
                ⇅ OneBot 11 WebSocket
            bot.py（本程序：触发判断 + 会话记忆 + 人格）
                ⇅ OpenAI 兼容协议
            智谱开放平台 GLM-5.3
```

> ⚠️ **风险提示**：NapCat 属第三方协议实现，存在违反 QQ 用户协议、账号被风控的可能，**强烈建议用小号**登录。

---

## 一、环境要求

- Windows + Python **3.11 或更高**（本机已验证 3.12.4）
- 一个用来当机器人的 **QQ 小号**
- 智谱开放平台账号（<https://open.bigmodel.cn>）

## 二、安装并登录 NapCatQQ

两种安装方式任选：

**方式 A：一键版（推荐，不想单独装 QQ）**
1. 到 NapCatQQ 的 GitHub Releases 下载 `NapCat.Shell.Windows.OneKey.zip` 并解压
2. 运行 `NapCatInstaller.exe` 等待自动化配置
3. 进入生成的 `NapCat.XXXX.Shell` 目录，双击 `napcat.bat` 启动

**方式 B：Shell 版（电脑上已装官方 QQ）**
1. 确保官方 QQ 为最新版
2. 从 GitHub Releases 下载 `NapCat.Shell.zip` 解压
3. 双击 `launcher.bat` 启动（Win10 用 `launcher-win10.bat`）

**扫码登录**：启动后控制台会显示二维码，用当机器人的那个 QQ 号扫码登录。

## 三、配置 NapCat 的 WebSocket

1. 启动 NapCat 后，浏览器打开 WebUI：`http://127.0.0.1:6099`（首次 token 见控制台提示）
2. 进入「网络配置」→ 新建 → 选择 **OneBot v11 → 正向 WebSocket（WebSocket 服务器）**
3. 记下两个值：
   - **监听端口**（默认 `3001`）
   - **token**（可自己设一个，也可以留空）
4. 保存并重启 NapCat 使配置生效

## 四、获取智谱 API Key

1. 打开 <https://open.bigmodel.cn>，注册/登录
2. 右上角头像 → 「API 密钥」→ 创建并复制 API Key
3. （可选）确认账户有可用额度；若你是 GLM Coding Plan 订阅用户，本程序使用的 OpenAI 兼容协议同样适用

## 五、配置并启动机器人

```bash
# 1. 安装依赖（建议在项目目录下的虚拟环境中）
pip install -r requirements.txt

# 2. 复制配置模板并编辑
copy config.example.toml config.toml
#    用记事本打开 config.toml，填写：
#    [glm]    api_key  = 你的智谱API Key
#    [napcat] ws_url   = ws://127.0.0.1:3001   （按上一步的端口改）
#             access_token = 你在 NapCat 里设置的 token（没设就留空）

# 3.（可选但推荐）先自检 API Key
python bot.py --check-glm

# 4. 启动机器人（保持 NapCat 在运行）
python bot.py
```

看到 `已连接 NapCat，机器人账号：xxxx` 即启动成功。

## 六、使用方法

| 场景 | 操作 | 说明 |
|---|---|---|
| 群聊 | `@机器人 你好` | @ 后跟内容；只 @ 不说话会收到提示 |
| 私聊 | 直接发消息 | 好友私聊即触发（可配白名单限制范围） |
| 命令 | `@机器人 /帮助` 或私聊发 `/帮助` | 见下表 |

**可用命令**（群里使用需 @机器人）：

| 命令 | 权限 | 作用 |
|---|---|---|
| `/帮助` | 所有人 | 查看全部命令 |
| `/清空记忆` | 私聊人人可用；群聊仅管理员 | 清空当前会话的对话历史 |
| `/性格` | 仅管理员 | 查看当前人格设定 |
| `/性格 <描述>` | 仅管理员 | 修改人格，如 `/性格 你是一只傲娇的猫娘`，即时生效并持久化（存 `data/persona.json`，重启不丢） |
| `/性格 重置` | 仅管理员 | 恢复 `config.toml` 里的默认人格 |
| `/状态` | 仅管理员 | 查看运行时间、人格、会话数、开关状态 |

**改人格**：编辑 `config.toml` 的 `[persona]`（name 和 system_prompt），重启生效；或在聊天里用 `/性格` 命令即时修改。

## 六点五、桌面一键启动器（推荐日常使用）

两种等价入口：

- **单文件程序**：双击项目目录下的 **`qq-ai-bot.exe`**（约 23MB，无需安装 Python；构建方法见第八节）
- **源码方式**：双击 **`launcher.pyw`**（无黑框）

功能（两者一致）：

- **🚀 一键启动**：自动部署插件 → NapCat 快速登录 → 等就绪 → 启动机器人 → 打开 6099 管理控制台
- 状态灯实时显示 NapCat / 机器人是否在线（🟢已连接 / 🟡运行中未连上 / ⚪未运行）
- 可单独启动/停止 NapCat 或机器人（停止只结束启动器自己拉起的进程，不影响桌面上的其它 QQ）
- 日志区实时滚动两个进程的输出；「📌 桌面快捷方式」一键创建桌面图标

启动器读取 `config.toml` 的 `[launcher]` 段：`napcat_dir`（NapCat Shell 目录）、`qq_path`（QQ.exe 路径，留空自动探测）、`qq_account`（机器人QQ号，快速登录用；0 = 扫码）。

## 七、管理控制台（WebUI，唯一入口）

机器人运行时，打开 NapCat WebUI（`http://127.0.0.1:6099`），侧边栏「🤖 QQ AI 机器人」即为配置控制台：

- 运行状态：机器人小号在线状态、运行时长、会话/记忆数、群聊/私聊开关
- NapCat 接口：输入 WS 地址 + token，可「测试连接」（显示当前登录小号）或「保存并重连」
- 智谱 API Key：输入新 Key 可先「测试」再「保存」，保存后立即按新 Key 调用
- 管理员账号绑定：输入 QQ 号添加/删除，即时影响聊天命令权限

所有修改**即时生效并持久化**（写入 `data/runtime.json`，优先级高于 `config.toml`）。

> 技术说明：控制台页面与数据接口全部走 6099 同源代理（插件转发给机器人的内部随机端口），**不占用任何额外固定端口**。机器人未运行时页面会显示「机器人程序未运行」。

### NapCat 插件说明

`napcat-plugin/napcat-plugin-qq-ai-bot/` 是 NapCat 插件（WebUI 侧边栏页面 + 同源 API 转发）。**用启动器或 exe 启动时会自动部署**（复制插件 → 白名单补丁 → 启用，全部幂等），无需手工操作。手工部署步骤：

1. 复制插件文件夹到 NapCat 的 `plugins` 目录（Shell 版为 `<NapCat目录>/plugins/`）
2. NapCat v4.18+ 非官方插件需加入 `napcat.mjs` 中的官方白名单 Set（搜 `napcat-plugin-qce`，其后追加 `"napcat-plugin-qq-ai-bot"`）
3. 在 NapCat 的 `config/plugins.json` 写入 `{"napcat-plugin-qq-ai-bot": true}` 并重启 NapCat

> NapCat 升级会还原 `napcat.mjs`，启动器下次启动会自动重新打补丁。

## 八、打包成单文件 exe

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name qq-ai-bot ^
  --add-data "napcat-plugin;napcat-plugin" ^
  --exclude-module pandas --exclude-module numpy launcher.pyw
```

产物 `dist/qq-ai-bot.exe`（约 23MB）与 `config.toml` 放同一目录即可双击使用，**目标机器无需安装 Python**。exe 双击 = 启动器窗口；内部以 `--run-bot` 参数自拉起机器人进程。`--exclude-module` 用于剔除 openai SDK 可选依赖拉进来的 pandas/numpy（否则体积会膨胀到 400MB+）。

## 九、常见问题

- **连接不上 NapCat**：确认 NapCat 已启动、端口一致；若仍失败，把 `ws_url` 改成 `ws://127.0.0.1:3001/onebot/v11/ws` 再试（不同版本路径要求不同）。
- **连接被拒（401/403）**：`access_token` 与 NapCat 网络配置里的 token 不一致。
- **机器人不回复私聊**：检查 `[behavior] private_enabled` 是否为 true、`private_whitelist` 是否把你排除在外。
- **回复很慢**：GLM-5.3 思考始终开启，把 `[glm] reasoning_effort` 保持为 `low` 最快；`high/max` 会显著变慢。
- **日志在哪**：控制台 + `data/bot.log`；对话历史在 `data/sessions.json`。
- **想重置所有记忆**：停机后删除 `data/sessions.json`。

## 十、目录结构

```
qq聊天工具/
├── qq-ai-bot.exe        # 单文件程序（本地构建，不入库）
├── launcher.pyw         # 桌面一键启动器（双击运行）
├── bot.py               # 主程序：WS 客户端、事件分发、命令处理、内部API
├── glm_client.py        # GLM-5.3 调用封装
├── conversation.py      # 会话记忆管理（持久化）
├── napcat-plugin/       # NapCat 插件（控制台页面 + 同源API转发）
│   └── napcat-plugin-qq-ai-bot/
│       ├── index.mjs    # 插件入口
│       └── webui/index.html  # 控制台页面
├── config.example.toml  # 配置模板（复制为 config.toml 使用）
├── config.toml          # 实际配置（含密钥，勿外传，不入库）
├── requirements.txt     # Python 依赖
├── 项目说明.md           # 项目说明（架构/端口/技术栈/需求达成）
├── data/                # 运行时生成：日志、会话历史、人格、端口文件（不入库）
└── backups/             # 修改文件时的自动备份（不入库）
```
