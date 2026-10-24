[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

超时修复说明：实际 pre_tool_call 备份同步运行于可终止的独占 Python 子进程，父 hook 仅有界 IPC/清理，
始终返回 None。默认 8 秒、上限 12 秒，清理及独立诊断写进程另有不到 0.2 秒有界开销。
超时或错误可跳过备份，不保证仍可恢复；固定 stderr 警告尽力显示，callback.health 保留失败状态，
普通错误由 worker 尽力写台账。state FS/台账卡住不会反向拖住父 hook；硬杀时无法保证落盘诊断。
注册时不写 state，设置告警延迟至 worker；插件源码加载与宿主配置回调本身不属于 hook 截止范围。
普通文件采用 no-follow/nonblocking 打开、fstat 校验和限长读取，Git 只读取私有字节副本而非重开工作路径。
私有 index 与哈希副本放在仓库 Git 目录内的 `hermes-autosave-index-*` 临时目录；超时可遗留（仅允许保存的源字节，
无原始命令），不是已发布恢复版本，下次该仓库快照时在仓库锁内清理。
内核不可中断任务及 OS 创建进程原语故障仍是系统边界，不能承诺立即释放该任务/锁。
实现和验证边界详见英文 README 的 Coverage and safety rules。

# git-autosave

Hermes 目录级文件系统检查点（`checkpoints.enabled`）的轻量、本地替代方案。修改性工具调用前，使用私有 index
将受影响仓库的工作状态保存到带 reflog 的 `refs/hermes-autosave/<branch>`。同轮按成功保护的路径去重，
后续明确指定但不在先前快照范围内的目标仍会保存。
不修改 HEAD、分支、真实 index 或工作区，不推送，也不阻止、改写或批准工具调用。

在 Git 之外或被拒绝的顶层仓库中，只复制即将被修改的已有文件。新文件没有修改前版本；不为这些位置的目录目标
制作目录副本。

## 为什么选择它，而不是 checkpoints 或 git-hook？

内置 checkpoints 使用独立文件系统快照存储保护目录，包括非 Git 项目。本插件复用各项目本地 Git 对象库，
并提供单文件副本兜底，不提供非 Git 目录的整体恢复；范围较小有助于减少扫描成本。

目录中的 `git-hook` 插件会 fetch/pull，并向真实分支 commit/push。本插件完全不做这些操作：影子 ref 是本地恢复
历史，不是项目提交或同步。两者用途不同；autosave 不是备份服务，也不保证不会丢失数据。

## 安装

需要 Hermes >= 0.22.0、Python 3.11+ 和本地 Git，支持 Linux 与 macOS。0.3.x 不支持 Windows：终端命令路径
解析按 POSIX shell 处理，会误读 Windows 路径。旧版 Hermes 可能在修改前快照仍运行时错误阻止并发工具调用；
v0.22.0 修复了此守卫竞态（[NousResearch/hermes-agent#105223](https://github.com/NousResearch/hermes-agent/pull/105223)）。

```sh
hermes plugins install xiaoyaner0201/hermes-git-autosave
hermes plugins enable git-autosave
```

重启正在运行的 CLI/gateway 会话以加载 hook。隔离插件宿主故意不注册本地 CLI 子命令；观察型 hook 不依赖 CLI 注册。

若要替代内置 checkpoints，在当前 profile 的 `config.yaml` 中设置：

```yaml
checkpoints:
  enabled: false
```

只有当较窄的 Git/单文件保护范围满足恢复需求时才关闭内置 checkpoints。不迁移或删除原检查点数据；可同时启用
两者，但会重复快照工作。

## 设置

全部可选，位于当前 profile 配置的 `plugins.entries.git-autosave.settings`。注册时通过 `ctx.get_config` 读取；
核心函数也可脱离插件上下文运行。数字设置要求有限正数，拒绝布尔值和字符串；数量限制要求整数。无效值使用默认值，
每个服务实例只在 `ledger.jsonl` 中记录一次。

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| `storage_dir` | 未设置 | 覆盖插件数据根目录；展开 `~`，相对路径以启动目录为基准。 |
| `keep_days` | `3` | prune、status 和台账查询的保留窗口；必须显式运行 prune。 |
| `max_file_mb` | `10` | 新计算哈希的文件和单文件副本大小上限，单位 MiB；已有已跟踪 blob 可能仍保留。 |
| `hook_budget_seconds` | `8` | worker 总截止时间（包括启动、IPC），上限 `12` 秒；有界清理/诊断额外不到 0.2 秒，低于宿主 30 秒。 |
| `big_repo_entries` | `20000` | index 条目超过此数量时只更新本次涉及路径。 |
| `max_untracked` | `2000` | 可保存的未跟踪文件超过此数量时只加入明确涉及的未跟踪文件。 |
| `extra_skip_dirs` | `[]` | 额外排除的路径组件名称，包括已跟踪文件。 |
| `deny_toplevels` | `[]` | 额外拒绝的仓库顶层路径，只允许单文件副本。 |

```yaml
plugins:
  entries:
    git-autosave:
      settings:
        keep_days: 3
        hook_budget_seconds: 8
        extra_skip_dirs: [generated-dependencies]
        deny_toplevels: []
```

存储默认使用 `plugins.plugin_storage.plugin_data_dir("git-autosave")`；模块不可用时退回
`<HERMES_HOME>/plugin-data/git-autosave`。布局：

```text
<plugin-data-root>/
  files/<YYYYMMDD>/objects/<hash-prefix>/v2-<path-sha256>.<HHMMSS>-<ns-sequence>-<sha8>
  files/<YYYYMMDD>/objects/<hash-prefix>/<copy-name>.json
  ledger.jsonl
  state/
```

新副本名使用规范绝对路径的 SHA-256，不使用原文件名组件，因此源名称达到文件系统上限时副本与元数据名仍有界。
原始路径、内容标识、模式、观察顺序键保存在仅所有者可访问、原子写入的旁置 JSON 中，不依赖尽力而为的台账。
没有元数据的新副本是不完整备份，不会列出。跨进程只与最新内容及模式观察去重；A → B → A 和模式循环保留全部转换。
建议性锁保护的持久序列为 Git 与副本共同分配递增键，即使时钟停止或回退。旧的按路径布局仍可读取，模式使用旧台账
或仅所有者权限兜底，最旧名称按 mtime 排序。所有布局都拒绝元数据/台账中与查询路径冲突的原始身份。
没有元数据的旧副本请先用 --to 检查，不要直接覆盖原件。插件安装目录中不写运行状态；Git 对象保留在各仓库中。

## 保护范围与安全规则

- write_file、patch 路径及 V4A Update/Add/Delete/Move 标题触发修改前保护。标题空白与 Move 箭头两侧可选空白
  遵循宿主语法。展开 `~` 后的绝对路径直接使用；
  相对路径通过公开的 `resolve_agent_cwd()` 定位（会话 cwd → TERMINAL_CWD → 启动目录）。缺少此函数时使用
  TERMINAL_CWD 或 os.getcwd()。
- 通过符号链接的内容写入（write_file、patch Update/Add、terminal 重定向/tee/原地修改）将解析后的目标
  作为独立路径保护：允许仓库内正常快照，否则单文件复制，仍应用排除规则。patch Delete/Move 与 terminal
  unlink/move 只保护条目；V4A 补丁中经链接的写入按宿主语义建模：Update 标题在任何操作前解析，
  后续每一跳跟随前面 Move/Delete/Add 留下的条目，Add 视为新建普通条目。链接条目仅在有 Git 快照时可恢复；Git 外或拒绝仓库中不保存链接条目本身。
  find <link> 只列链接版本，并尽力从台账提供单独查找目标路径的提示；别名状态不会改变恢复目的地。
- terminal 的 workdir 是初始 cwd，随后应用开头的静态 `cd`。`env -C`/`--chdir` 只改变该命令的 cwd，
  `install -t`/`--target-directory` 包含目标目录。shell 重定向按静态 cd 后的段落 shell cwd 定位，不使用 env/Git
  子命令目录。Git `-C` 和常见修改命令的明确文件参数决定受影响仓库，
  包括 cwd 外的文件。限长分词解析支持换行边界、sudo/env/command/nohup/time/nice/xargs 包装、
  rm/rmdir/unlink/mv、cp/rsync/install/dd/shred/chmod/touch、重定向（`>`、`>|`、`>>`、`&>>`）、原地 sed/perl、
  truncate、Git checkout/restore/reset/clean/stash/rebase/switch/rm/mv/apply/am/merge/pull/cherry-pick/revert、
  checkout-index、read-tree -u、改变内容的 update-index、worktree remove、patch/unzip/tar 解包、find -delete/-exec
  与非追加 tee。`tee -a`/`tee --append` 有意不触发保护，`>>` 会触发。不是完整 shell 解析器；动态路径及脚本、
  解释器、shell 函数等间接修改可能漏报，不作批准决策。超过 65,536 字符（ASCII 为 64 KiB）的命令不分词：
  保守视为破坏性，只保护初始 workdir 仓库；每个服务实例仅记录一次警告，不保存命令内容。
  不解释 heredoc 正文语义。分词失败时（包括正文引号不配对但合法的 heredoc），命令视为不透明：仅尝试保存初始
  cwd 所在仓库，使该仓库的同轮覆盖缓存失效，且不从本次修改前快照重新填充；不猜测前导 `cd`、操作数或重定向。
  原有预算、paths-only 限制和排除规则仍适用。每次降级 hook 调用记录一条固定 `kind=parser`、
  `code=command_parse_fallback` 警告，不包含命令内容或异常文本；这表示覆盖降级，不证明快照已成功。
  若 cwd 不在允许的 Git 仓库内，由于目标未知，不提供单文件保护，也不创建整目录副本。即使命令确实畸形，
  也不拦截原工具调用。
- 依赖目录始终排除，包括已跟踪内容：node_modules、bower_components、jspm_packages、.pnpm-store、.yarn、
  vendor、.venv、venv、site-packages、dist-packages、__pypackages__、.tox、.nox、.eggs、Pods、Carthage、
  .gradle、.m2、.cargo、.bundle。
- 构建/缓存目录排除未跟踪文件与单文件副本，但保留已跟踪文件：__pycache__、.pytest_cache、.mypy_cache、
  .ruff_cache、.hypothesis、.cache、.next、.nuxt、.svelte-kit、.turbo、.parcel-cache、.vite、.angular、.expo、
  dist、build、out、target、coverage、.coverage、htmlcov、.terraform、DerivedData。
- 拒绝的仓库顶层：文件系统/磁盘根目录、用户主目录、HERMES_HOME 及其祖先、HERMES_HOME/profiles/* 直接目录、
  macOS 卷挂载点的直接子目录、Linux /mnt 和 /media 的直接子目录，以及 deny_toplevels。只做单文件副本，
  不做整仓快照。
- 大仓库或过去一小时内最近慢快照耗时至少三秒的仓库使用 paths-only 模式。目录目标只加入已跟踪文件，
  不枚举无关未跟踪条目；未涉及
  文件保留真实 index 中的版本，无关工作区修改可能不保存。不会递归扫描 Git 子模块链接。
- 每仓库非阻塞锁串行化 hook、sweep、prune。POSIX 使用 flock，Windows 使用 msvcrt 字节范围锁，建议性语义
  依赖文件系统；仓库忙则跳过并记录。没有宿主 turn ID 时不猜测轮次边界，仍去重未变化的树。同轮去重是有界、
  进程本地状态，不跨 agent 进程共享。只有同轮、同 HEAD oid 与分支下真正 raw-hash 的同一路径能抑制明确目标。
  仅 stat/index 基线不算覆盖。改变状态的 Git 命令使该仓库缓存失效，其自身修改前快照不重新填充缓存。
  后续忽略、稀疏或未覆盖目标仍会运行；失败、跳过及超大文件尝试不算覆盖。
- Git 子进程及仓库发现共享 worker 剩余预算；hook 中 Git 留在本次独占进程组，由父 hook 有界清理。
  文件系统 I/O 和日志均在 worker 中，不能以协作式 Budget 检查代替整个 hook 的强边界。请使用响应正常
  的本地磁盘。
- 插件 Git 子进程禁用 fsmonitor、hooks、split-index、自动维护；私有 index 从 HEAD 新建，仅以 index-info 写入。
  Python 只读比较 index stat 与 lstat（含 racily-clean；与 Git 默认构建一致不比较 `st_dev`，重启或重新挂载后设备号
  可能变化），不让 Git 刷新或比较工作区/index。明确的现有文件目标
  始终保存原始字节，包括正规化、过滤、assume-unchanged 文件；未涉及文件可保留 index 表示。
  隐式/目录快照保留 skip-worktree 条目的 index/HEAD 基线，即使实际字节不同；稀疏检出缺失文件不会当作删除。
  明确指定且存在的文件目标则保存真实字节。影子符号 ref 会
  导致跳过，ref 写入/删除均用 --no-deref；GIT_NO_LAZY_FETCH=1 和 protocol.allow=never 禁止隐式抓取，缺失对象
  会记录错误并放行工具调用。
- Git 将 mirror 解析为真（含无值键、非零整数、yes、on），或包含 refs/* 推送 refspec 的远端会导致跳过仓库快照。
  无效 mirror 布尔值记录错误并跳过，绝不假定安全。普通 git push、git push --all、
  git push --tags 不发布影子 refs；git push --mirror 或 refs/* refspec 会发布，切勿用于含私有快照的仓库。

## CLI 与恢复方法

每个子命令都支持 `--json`：

```sh
hermes autosave status
hermes autosave find path/to/file
hermes autosave list [repo]
hermes autosave prune --dry-run
hermes autosave prune
hermes autosave sweep --since-minutes 360 --budget-seconds 150
```

status 显示数据根目录、大小、keep_days 内有快照的仓库及最近台账错误。list 列出保留的 reflog 版本和 tip。
find 只列真正读取过修改前原始字节的路径版本，跨分支及 Git/副本后端按观察时间从新到旧排序。Git 提交的
Autosave-Observed trailer 保存观察路径、内容 ID 与模式，提交键保存 at_ns；副本元数据在旁置文件中。
台账丢失不会把 index/HEAD 占位内容变成新版本。paths-only 只观察选定路径（目录展开为已跟踪文件）。
A→B→A 即使跨分支/后端也记录新的 A。仅在没有其他观察介入时才去重连续相同内容；无关观察也保守地创建新事件。
旧 Git 快照没有观察来源、普通 HEAD 不由 find 列出，请手动用 Git 检查；旧副本仍保留元数据/台账兜底。
执行恢复前审阅版本，目标会被替换。版本号从 1 开始；历史变化后请重新运行 find。find <link> 只列链接条目版本，
台账提示可指向目标路径的独立历史；提示和旧别名状态文件不会影响恢复。

```sh
hermes autosave restore path/to/file --version 1
hermes autosave restore path/to/file --version 2 --to recovered/file --json
git -C <repo> reflog show refs/hermes-autosave/<branch>
git -C <repo> diff 'refs/hermes-autosave/<branch>@{1}' -- path/to/file
```

restore 自动创建父目录，通过临时同级文件与原子替换恢复普通文件及保存的模式，重新创建符号链接且不跟随已有
目标链接，不修改真实 index。restore 只写指定路径（或 --to）；目标内容请用 hermes autosave restore <referent>
恢复。Git 链接条目版本可重建链接；Git 外或拒绝仓库不保存链接条目。restored_to 说明被修改的路径。
`restore --to` 按设计会跟随目标路径父目录的符号链接；恢复前请确认并信任这些父目录。
只有最终目标链接会被替换而不是跟随。副本记录原始模式；没有模式元数据的旧副本恢复为仅所有者可访问的文件。
不要使用 git show > file：会跟随链接且丢失模式。git checkout <snapshot> -- <file> 能恢复模式，但也修改真实 index。
reflog 快照不在用户分支的祖先链上；游离 HEAD 使用稳定的 detached-<repository-hash> 影子 ref。

prune 删除 tip 早于 keep_days 的影子 ref，过期旧 reflog，删除旧副本日期目录，修剪台账。日期目录保留精度为
日历日。绝不运行 git gc；不可达对象可能留到用户正常 Git 垃圾回收时。请显式运行或单独调度 prune。
--dry-run 不更改快照、ref 或台账。拒绝符号链接形式的 storage/files/state/日期目录，用 no-follow 目录描述符与
抗链接攻击的 rmtree 删除。台账/锁读写也使用固定的根目录/state 描述符；根目录或 state 身份改变时终止清理。
缺少这些安全原语的平台（含 Windows）报告错误且拒绝清理，不采用不安全删除。
sweep 预算也覆盖台账扫描与逐调用处理；超过 1 MiB 的会话消息跳过并警告。

## 自行调度 sweep

sweep 兜底处理预算耗尽、agent 子进程、尚未重启的会话等 hook 漏保护情况。使用 mode=ro URI 和短 SQLite 超时
只读访问 <HERMES_HOME>/state.db。按 COALESCE(last_activity_at, started_at) 找近期会话，从 messages 获取
write/patch 路径和 terminal workdir/开头 cd；同一回看窗口内的插件台账仓库也包含在内，默认六小时。会话 schema
是私有且随版本变化的，列会被探测，缺失时警告，台账仍可用。不是全盘扫描。sweep 在修改后执行，无法重建丢失的
修改前版本。

成功时无 stdout、退出 0，除非使用 --json。错误或 schema 警告在 stdout 打印短摘要，仍退出 0，让 Hermes
no-agent cron 能投递。--json 始终输出结果对象，含 errors、warnings、仓库结果、副本数量/路径；监控这些字段，
不要依靠退出码。插件不创建计划任务或后台进程。

选择一个调度器，使用相同的当前 profile/HERMES_HOME 和 Hermes 可执行文件的绝对路径：

1. Hermes no-agent cron：自行在 <HERMES_HOME>/scripts/ 中创建 autosave-sweep.sh：

   ```sh
   #!/bin/sh
   exec /absolute/path/to/hermes autosave sweep --since-minutes 360 --budget-seconds 150
   ```

   再自行调度：

   ```sh
   hermes cron create "every 10m" --no-agent --script autosave-sweep.sh --deliver telegram --name autosave-sweep
   ```

   选择已连接消息平台；--deliver local 仅保存输出，普通终端没有实时投递通道。检查当前版本的
   hermes cron create --help。
2. crontab：添加 `*/10 * * * * /absolute/path/to/hermes autosave sweep --since-minutes 360 --budget-seconds 150`。
   必要时显式设置 HERMES_HOME、PATH；cron 环境与 shell 不同。
3. launchd（macOS）：用户 LaunchAgent 的 ProgramArguments 使用 executable、autosave、sweep、--since-minutes、
   360、--budget-seconds、150；StartInterval=600，配置环境及 StandardOutPath/StandardErrorPath，
   自行用 launchctl bootstrap 加载。
4. systemd（Linux）：用户 oneshot service 设置 ExecStart=/absolute/path/to/hermes autosave sweep、显式环境；
   timer 设置 OnUnitActiveSec=10min，自行用 systemctl --user enable --now 启用。

若需要严格保留期限，请另行调度 hermes autosave prune；sweep 不隐式清理。

## Disclosure（披露）

本插件为 sweep 发现只读访问 Hermes 会话数据库；在 agent 涉及的仓库中运行本地 Git 子进程，将影子 refs 和 reflog
写入这些仓库的 .git，并将 agent 即将修改的已有文件复制到插件数据目录。未被 .gitignore 忽略的未跟踪文件可能
进入本地 .git 对象库；启用前审查秘密与敏感文件的忽略规则。副本和对象均为本地、未加密数据。不发起网络调用、
不启动后台进程、不请求凭据，不集成凭据存储或遥测服务。不复制/读取凭据工作文件，也不作为未跟踪文件收集：
HERMES_HOME/.env、HERMES_HOME/auth.json（保守排除任意 auth.json）、.env、.env.*、.netrc、.git-credentials、
.npmrc、.pypirc、*.pem、*.key、id_rsa*、id_ed25519*、id_ecdsa*、.claude/.credentials.json，以及
.ssh、.aws、.docker、.azure、.kube、.config/gcloud、.config/gh 下的所有文件。包括 .aws/credentials、
.aws/config、.docker/config.json、.azure/accessTokens.json、.azure/msal_token_cache.*、.kube/config、
.config/gh/hosts.yml，以及 gcloud 的 application_default_credentials.json、credentials.db、access_tokens.db。
匹配不区分大小写，检查绝对路径祖先；仓库嵌套在凭据目录中也适用。链折叠前检查词法路径和每个符号链接替换
（最多 40 跳），循环或过长内容链安全跳过；最终链接条目仍可独立保存。凭据/依赖命名的中间链接及目录别名
不能绕过内容排除规则。
用户已经提交的凭据条目原样保留 HEAD 版本，不收集工作区或暂存区凭据变化。这是名称排除，不是秘密扫描器，
其他敏感文件仍需忽略规则。

## 限制与开发

恢复保护尽力而为：仓库错误、争用、过大文件、未合并 index、间接 terminal 修改、不可用的宿主本地路径
（远程/容器工具）和耗尽预算可能导致修改未保存。并发修改中的工作区无法保证原子快照。Windows 锁依赖文件系统
支持，隐私由 ACL 决定；恢复命令面向 cmd.exe 而非 PowerShell。同轮去重是进程本地的；探测会话 schema 兼容性，
但不作保证。移除插件不删除持久数据或 refs。

**已知限制：经由符号链接的多操作 V4A 补丁。** 同一补丁先移动或删除链接、再修改移动后的路径时，插件会重放这些
操作来找出实际被写入的文件。重放按路径字面比较，而宿主经由文件系统解析。在大小写不敏感或 Unicode 规范化的
文件系统（macOS、Windows 默认）上，若链接内容以不同大小写或 Unicode 形式（`MIDDLE` 与 `middle`）指向中间
链接，插件可能保护了旧目标而不是宿主实际修改的文件，后者就没有修改前版本。重放也不重做宿主的补丁校验：被
宿主拒绝的补丁仍可能多留一份无关文件的本地副本；V4A 调用中单独的 `path` 参数也可能复制补丁未触及的链接目标。
普通 `write_file` 和单操作 `patch` 经链接的编辑不受影响。若依赖此类补丁的恢复，请先提交，或把 Move 与 Update
拆成两次调用。

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q
```

隔离测试使用临时仓库/HERMES_HOME，无网络。CI 覆盖 Ubuntu、macOS，Python 3.11/3.13。
参阅 CONTRIBUTING.md 和 SECURITY.md。Apache-2.0；Copyright 2026 The hermes-git-autosave Authors，见 LICENSE。
