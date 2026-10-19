# Changelog

本项目所有重要变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### Added

- **停止正在生成的任务（2026-10-16 用户需求）**：模型/参数设置错了时不必干等整条流程跑完——原来只能等到任务自己结束（还带转写重试、总结降级阶梯这类「一直不停地尝试」）才能改设置重来。现在三处都能点「停止生成」：生成中页面的大按钮、新建表单的按钮位（原来的禁用「正在生成…」变成可点的「停止生成」）、生成历史卡片上的小方块按钮；状态落 `CANCELLED`（界面「已停止」，中性色不是失败红），卡片/表单随即恢复成可「重新生成」。取消在阶段边界与重试间隙即时生效：yt-dlp 下载（进度回调中断）、流式总结（逐增量检查）、总结降级阶梯、分批喂图与合并、转写重试退避、分段转写段间、抽帧逐帧、等 GPU 转写信号量——点停止后正在跑的任务秒级退出。停止不是失败：自动化不计入失败冷却与告警，轮次汇总单列「已停止」；`GET /task_status` 走成功信封返回终态，`POST /api/cancel_task` 幂等（已结束的任务如实回「无需停止」并清掉预取消标记，不误伤后续重试），MCP 侧新增 `cancel_task` 工具。并发语义用「每次执行一个 run + 旧 run 作废」收口：停止后立刻重试不会与还在退场的旧执行互相覆盖状态（被作废的 run 禁止一切状态写入），提交与执行线程启动之间毫秒级窗口的停止请求由预取消墓碑兜住（下一次提交自动清除）

- **YouTube 受限视频（年龄验证）全链路支持**：yt-dlp 新增三件套——(1) 登录 Cookie：专用浏览器配置 `~/.bilinote-youtube-profile` 登录一次后，后端直读解密其 cookie 库（默认配置被 App-Bound Encryption v20 保护不可读，专用配置走 DPAPI+AES-GCM 可读），登录态可由每日计划任务自动续期；(2) JS 挑战求解 `yt-dlp-ejs`（随 sidecar 打包）；(3) PoToken 提供器 bgutil（`~/.bilinote-bgutil`）。下载、字幕、元信息三条路径全部接入 cookie，解析支持 `k=v; k=v` 串与 Netscape cookies.txt 文本。前端「下载配置 → YouTube」新增 Cookie 文本框，受限视频不再直接因「Sign in to confirm your age」失败
- **笔记合并后统一重建目录与 AI 总结**：分批喂图时每批各自产出一份「目录」与「本段指出…」口吻的 AI 总结，多层合并后成堆残留（2026-10-14 用户实拍：最终笔记的「AI 总结」是十几段「本段/本批」的拼贴）。现在分批阶段明确不产出这两个整篇级段落，合并完成后统一「后整理」：目录从二级标题确定性重建（带原片时间入口，不调模型），AI 总结用一次纯文本调用重写为「总览 + 关键要点」；总结规模按信息量自适应（2026-10-15 用户反馈固定档太死板）：以章节数量为信息量代理，短笔记 2~3 句 + 3~5 条、中等 3~4 句 + 4~8 条、长笔记 4~6 句 + 6~12 条，区间内按信息密度取上下限、宁少勿滥；重写失败回退保留旧总结，整理失败不拖垮任务
- **存量笔记重整理端点 `POST /api/refinalize_note`**：对已生成的笔记重跑「后整理」（摘掉目录/总结残留 → 重建目录 → 重写 AI 总结），只读结果文件 + 一次纯文本调用，不重新下载/转写/喂图——旧笔记无需重新生成即可升级到新版整理逻辑。格式缺省按正文推断；模型缺省沿用结果文件记录、可显式覆盖
- **外部提交后界面即时可见**：桌面端任务同步从 30s 收紧到 5s（浏览器端保持 30s），并把「内容变更」纳入同步判据——外部通过 API（MCP/自动化）提交或重跑的任务，卡片状态与正文数秒内自动刷新，不必等 Ctrl+R；窗口重新聚焦/切回前台立即同步一次
- **目录紧凑网格渲染**：阅读视图的「## 目录」段脱离正文单独渲染——多列「按列填充」（CSS columns：先在第一列往下列满再进第二列；之前 grid 按行填充，53 条目录读序在列间折返 18 次，2026-10-15 用户指出），条目带统一序号列（笔记自带编号优先、缺省按阅读顺序补），点击条目走问答来源同一套定位机制跳到对应章节，带原片时间入口的条目显示时间徽标（点击开原视频对应时间点）；面板移出 `.markdown-body` 渲染，避免 github-markdown-css 的 ol 编号与自有序号列叠加
- **生成性能与预算设置 UI 化**：「设置 → 通用」新增「生成性能与预算」卡片——同时生成笔记数、抽帧预算、拼图预算、分批总结并发四个旋钮，落盘 `config/task_config.json`（取值优先级：配置文件 > 环境变量 > 默认），改动即存；默认档上调为激进档 4800 帧/1000 张拼图（旧 2400/500，再往前 600/150），环境变量退居部署侧逃生口。「同时生成笔记数」热切换（在途任务不打断）；在线转写引擎并发放开（`TRANSCRIBE_ONLINE_CONCURRENCY`，默认 2），本地 whisper 仍独占 GPU 恒 1
- **降级阶梯改逐级升格优先（用户定版）**：预算兜不住时先升 2×2、再升 3×3，每级都用原采样间隔试（升格重建复用已抽取的帧缓存，不再全量重新抽帧），实在兜不住才在最大可用格子上拉最小间隔——时间密度比单格分辨率更值得保。总结失败阶梯同步从 3 级扩为 5 级：原样 → 升格 2×2 → 升格 3×3 → 间隔×3（3×3 兜底）→ 纯文本。自动化设置页「生成配置」补上采样间隔/拼图尺寸控件（后端字段早已就绪，之前只有开关）
- **模型级「思考强度」（reasoning effort）**：不同模型的档位不同，思考强度不跟供应商捆绑——「设置 → AI 模型设置」每个供应商页的「已启用模型」chip 内嵌档位下拉（默认（不传参）/minimal/low/medium/high/xhigh/max），按模型独立配置、改动即存即生效；笔记生成/AI 问答/自动化三条链路统一从模型行取值。responses 协议翻译成 `reasoning.effort`，网关不认识该参数时自动去参重试（识别口径收窄到「参数名+拒绝字样」，不误伤推理预算类报错）。旧库自动迁移：原供应商上的值一次性复制到其名下全部模型行并清空供应商列（模型已有值不覆盖），供应商表单不再有该下拉。推理模型空回（预算被思考吃光）时可调 low 缓解
- **批量导入并发提交**：稍后再看批量生成原来经 FastAPI BackgroundTasks 按注册顺序同步逐条跑，上一条全部完成才开始下一条——批量导入实际完全串行（2 个 worker 只用得上 1 个）。现改为每条一个后台线程提交（与自动化调度器同款），并发度仍受「同时生成笔记数」约束
- **分批总结并发**：长视频时间对齐分批的各批 LLM 调用原来严格串行，现按「分批总结并发」（默认 2）线程池并发；结果按批次索引归位保证合并顺序，checkpoint 只落连续前缀（断点续跑语义与旧格式完全兼容），任一批失败取消余批、进入降级阶梯
- **拼图/编码并行**：抽帧早已并行但拼图与 base64 编码一直串行，长视频上百组时省几十秒（各写各的文件，线程池 ≤4 保序）

### Fixed

- **「开启截图后滑动文章，内容一直上下跳动 / 图片盖住正文」（2026-10-19~20 用户实拍，截图懒加载版面不稳）**：两个独立缺陷叠加。(1) **卸载即塌**：旧实现只有 `<img>` 自己是内容，摘掉 `src` 后它退化成 19×19 的破图占位（实测 rect 19.3×19.3），一张约 330px 的图瞬间消失——滚动时每有一张图离开装载带，下方正文就被往上拽一次；外层又是 `flex justify-center` 的 shrink-to-fit，`width/height` 属性也撑不住。(2) **占位比例不符**：写死 16:9，而实图是 1920×886（缩略 1600×738），每张图加载后高度缩约 70px，逐张把下方内容拽一次；首帧 `initialInView:true` 还会一次性挂上整篇几百张。修复：`NoteImage` 改为「**固定比例占位盒 + 盒内绝对定位图层**」——外层容器改普通块（宽度=正文列宽，给百分比一个确定基准），占位盒用 `aspect-ratio` 撑高度且始终存在，`<img>` 只动态增删 `src`、任何时刻不参与文档流；比例按笔记记忆——第一张量到真实尺寸后经外部 store 一次性同步给同篇所有尚未测量的占位盒（不再逐张跳），并写入 `localStorage`，下次打开首帧即正确；`initialInView` 改 false（只挂装载带内的图），带内改 `loading="eager"` 保证比例在进可视区前测好。**关键细节**：img 的 `position/width/height/object-fit` 必须用**内联样式**——正文样式 `.markdown-body img { height:auto; max-width:100% }` 的特异性 (0,1,1) 高于 Tailwind 的 `h-full` (0,1,0)：宽窗口下占位盒被 `max-height:500px` 截到 500px、图片却按自身比例算成 640px，绝对定位的图片会溢出占位盒 140px 盖住下面正文（第一版正是栽在这里，只在窄视口验证没有暴露，已回退重做）。实测（浏览器驱动线上同一后端）：列宽 732 / 927 / 1182 / 1415px 四档下，129 张图逐张比对 img 盒子与占位盒边界，偏差 **0.00px**；导出模式强制全量挂载 129/129 完成，偏差仍 0.00px；多个滚动位置来回切换，全部占位盒文档坐标漂移 **0.0px**、`scrollHeight` 恒定；缩放弹层、隐藏/显示截图、长图导出、跨笔记比例隔离均正常

- **「停止一个任务后再点重新生成，新任务又被莫名其妙停止」（2026-10-18 用户实拍，停止误触）**：不是后端取消串了——日志实证两次 `已请求停止任务` 都紧跟用户点「重新生成」之后到达（`d918a16a` 12:55:11→12:55:14、12:55:26→12:55:34 两连），而全前端只有三处会发 cancel，用户以为只点了一次「重新生成」。真凶是**按钮原地变形**：表单主按钮在生成中就从「重新生成」原地变成红色「停止生成」（Live AX 实测两态同像素矩形），加载面板也立刻顶替刚才「重新生成」的位置，历史卡片则在开始时在旁边凭空长出一个停止图标——手还停在原地的第二下点击（双击/觉得没反应再点一次）正好命中刚变出的停止按钮，把刚起来的新 run 停掉。三层修复：(1) 三处停止入口统一「防误触窗口」（新增 `hooks/useStopArmed`：启动后 2 秒内不可用——表单按钮显示「启动中…」并禁用、加载面板暂不渲染停止按钮、卡片图标窗口内不显示；store 层 `cancelTask` 对窗口内请求一并拒绝）；(2) 表单主按钮不再原地变形：生成中主位是禁用的「生成中…」，停止挪到右侧独立槽位（原位置怎么点都不会触发操作）；历史卡片停止槽位常驻（不可用时 `invisible` 占位），按钮组不再伸缩；(3) 「重新生成」改乐观反馈（先本地切 PENDING，失败回滚并提示）——请求在后端繁忙时要几秒才回，界面毫无变化正是连点的诱因。后端并发语义（superseded / 预取消墓碑）复核无误，未改动

- **「最小化后过一段时间再打开还是白屏，要按 Ctrl+R」（2026-10-18 用户实拍，渲染进程 OOM 未除根）**：`logs/frontend_crash.log` 实证 10-18 11:47–11:52 渲染进程又连崩 4 次（`reason=OUT_OF_MEMORY`、退出码 `0xe0000008`，与 10-17 同因），第 4 次撞上「10 分钟最多重载 3 次」的预算上限后**永久放弃自动恢复**——白屏一直挂到用户手按 Ctrl+R。10-17 的懒加载 + 1600px 缩略只解决了「装载太贵」，没解决「装了不放」：浏览器对仍在 DOM 里的 `<img>` 一直保留解码位图（1600px 缩略约 5.8MB/张），790 张的重笔记滚一遍累计 4.5GB，照样把渲染进程（实测基线已 640MB）顶爆。四层修复：(1) 新增 `components/NoteImage` 替换正文截图渲染：离开可视区（含 ±2500px 装载带，以阅读区滚动容器为 IO root）即**摘掉 `src` 属性**释放解码内存、滚回来再挂上（后端磁盘缓存 + 浏览器 HTTP 缓存，重载毫秒级）——常驻解码量压到装载带内十几张（占位盒定版方案见 2026-10-20 条目）；(2) 长图导出开「导出模式」（`utils/screenshotLoadPolicy`）：capture 前强制全部截图挂真 src 并等待加载完成（上限 20s），截完恢复；浏览器实测导出瞬间 41/41 张全部挂载、导出完成后回到 5 张，导出画质与完整性不变；(3) Rust 侧重载预算改「10 分钟 3 次 + 用尽后每 60 秒退避一次」——不再永久放弃，最坏情况白屏 1 分钟内自愈；(4) 自动重载带 `?bn_crash=1` 标记，前端启动读到（`utils/crashBoot`）就不自动打开上次的笔记（重笔记正是反复爆内存的元凶），停在历史列表页并提示一句，避免「重载→又落在同一篇→再崩」死循环；手动 Ctrl+R 不带标记、行为不变

- **「AI 总结里文字像被叠印/串行」（2026-10-17 用户实拍）**：正文数据是好的——是 markdown 把**范围号**当成了删除线。`remark-gfm` 默认 `singleTilde: true`，单个 `~` 也是删除线定界符：模型写的「传统车企开发一款车需 3~5 年并完成碰撞、制动…中国车企一年出 3~5 款」里两个 `~` 之间的 30 个字被整段解析成删除线（mdast 删除线节点实测正好命中该段），且两个 `~` 被解析器吃掉不显示、只留下一条横线穿过文字，读起来就像文字叠印/拖出一道笔画（全库解析扫描：29 篇正文含 `~` 的笔记中 2 篇实际命中，另一篇为 `699b357f`）。修复：`remarkPlugins` 给 gfm 传 `{ singleTilde: false }`——此后只有成对 `~~` 才是删除线、`3~5` 原样渲染（解析级回归：全库删除线节点 2 → 0，显式 `~~` 语法不受影响）。桌面端与手机 Viewer 同用此组件，一处修复两端生效

- **「页面时不时莫名全白」（2026-10-17 用户实拍，WebView2 渲染进程 OOM）**：crashpad 转储四连（10:06 / 11:33 / 11:35 / 11:36）全部是渲染进程被杀（`ProcessType=renderer`、`SubCode=0xe0000008` 即 OOM）——笔记正文的原片截图是 4K JPEG（解码后单张约 31MB），重度笔记一篇引用可达数百张（实测最多 790 张），且此前没有任何懒加载，打开笔记即被整篇解码吃掉 GB 级内存；渲染进程被杀后 WebView2 不会自动恢复，页面就永久白屏。四层修复：(1) 正文截图一律 `loading="lazy"` + `decoding="async"`，只解码视口附近的图（实测 790 张只解码 33 张）；(2) 新增后端 `/api/img_thumb` 缩略端点（缩到 1600px、磁盘缓存 `static/thumb_cache`、命中约 7ms、原图本就小于目标宽时直接发原文件），屏幕显示用缩略（解码内存降约 5 倍、体积降约 4 倍）、点击放大仍取原图（`zoomImg`），缩略失败自动回退原图；长图导出画质不受影响（1600px 正好覆盖 2 倍倍率导出的像素需求）；(3) Rust 侧挂 WebView2 `ProcessFailed` 事件：渲染进程退出（含 OOM）自动 Reload 把白屏变成闪一下，独立日志 `logs/frontend_crash.log` 记录 kind/reason/exit_code，10 分钟内最多自动重载 3 次防崩溃循环；(4) 前端补全局 `ErrorBoundary`（根边界 + 路由内层 + 笔记预览区三层）：JS 渲染崩溃不再整页白屏，就地给出「重新加载 / 复制错误详情」，预览区单独崩溃时历史列表仍可用、可直接换一篇；`window` 级 error/unhandledrejection 一并留痕（`window.__bilinoteErrors` 环形缓冲）。另将长图导出画布面积上限从 2.68 亿像素降到 1.34 亿：2.68 亿 × 4B 的 ARGB 画布是单次 1GB 级分配，叠加数百张截图内联时很容易把渲染进程推到 OOM 边缘

- **老笔记封面「刷不出来 / 整片空白」（2026-10-17 用户反馈）**：三个叠加根因一起修。(1) 封面代理 `/image_proxy` 无缓存、不重试、不跟随重定向，上游 CDN 单张实测 3~4s——历史列表上百张封面要几分钟才逐个刷出来，且 `http://i.ytimg.com/...` 这类 http 地址在 CDN 侧直接挂死必然失败。现在：磁盘缓存（`static/cover_cache`，键=URL sha1，命中约 10ms，1500 文件上限按 mtime 清理）、http 地址追加 https 变体兜底、20s 超时 + 跟随重定向、网络级失败重试一次、明确 4xx 不重试、失败地址 10 分钟负缓存（不再每次浏览都去上游撞），最终失败返回 404 + `no-store`（不再 500，前端走占位图、错误也不会被浏览器缓存住）。(2) 列表封面组件 `LazyImage`：改前 inView 后整块换成 opacity-0 的真图，封面加载的 3~4s 里就是一片空白；且只在 `onLoad` 里置 loaded，占位分支切过来时 `src` 未变、事件不再触发会永远不可见；失败锁死无重试。现在占位图常驻底层（永远不空白，加载中也能看见），图片已完成（缓存命中/重挂载）立即显示，失败自动带参数重试一次再回退占位图。(3) 结果文件 `audio_meta` 整包替换会清掉卡片上刚从 `/tasks/recent` 拿到的好封面（老结果文件常缺封面/标题）——懒加载正文 / 轮询 / 增量同步改为空字段不覆盖本地的 `mergeAudioMeta` 合并
- **「重新生成」按钮永远转圈（2026-10-17 用户实拍）**：上一版「停止生成」把非生成态的按钮图标误写成 `Loader2 animate-spin`——按钮处于「重新生成/生成笔记」时图标一直空转，看起来像任务还在跑。恢复为仅生成中显示图标（方形停止符）

- **目录点击大量报「未找到对应章节」（2026-10-16 用户实拍）**：模型自写目录把时间写成标题尾缀「标题（00:03）」，而正文标题渲染成「标题 原片（00:03）」（原片二字插在中间、匹配器又不剥全角括号），双向 substring 全部失配。全库 251 篇目录笔记、3453 条条目逐一模拟跳转：365 条必失败、其中 285 条（78%）出自这一形态。三层修复：(1) `parseTocItem` 把尾缀「（mm:ss）」识别为 time 并剥出标题（目录面板顺带显示右侧时间徽标，`*Content-`/`[原片@](url)` 两形态回归不受影响）；(2) 跳转匹配器对两侧同口径剥离「原片（00:03）」pill 与裸时间尾缀，兜住任何漏网形态；(3) 严格匹配失败后新增模糊兜底：先按「A / B」拆复合标题逐段匹配，再退最长公共子串（≥8 规范化字符、唯一最佳领先亚军≥2）。仿真 365→32（剩 32 条为模型标题与正文完全改写，无法可靠定位，保持提示不乱跳）。浏览器 6 项实测 + 桌面端实拍点击全过（精确落位 + 高亮 2s）
- **「突然出现一堆终端窗口」（2026-10-16 用户实拍）**：后端以 `DETACHED_PROCESS` 方式拉起时，Windows 会给后端**自己**分配一个可见控制台窗口（Win11 默认终端下由 WindowsTerminal 承载）；其后每个 ffmpeg 子进程（抽帧是并行批量的，一次任务几十次调用）又各开一个可见控制台窗口，成批弹出。两层修复：(1) 代码层新增 `app/utils/proc.py`，所有从后端 spawn 的子进程（ffmpeg / ffprobe / `ffmpeg -version` 探测 / netstat / tasklist / 封面与 mp3 转换）显式带 `CREATE_NO_WINDOW`（新增源码级回归测试 `test_no_window_subprocess` 锁定「每处 spawn 都带」）；(2) 部署层 `bn_deploy_restart.py` 不再用 `DETACHED_PROCESS`，改为继承 launcher 的隐藏控制台（子进程继承后永不弹窗，覆盖 yt-dlp 内部调 node 等非本仓库 spawn），并兜底隐藏残留的 BiliNoteBackend 控制台窗口。另：ffmpeg 探测失败不再一律写「未安装」，带出真实异常类型与文本——窗口风暴期进程创建瞬时失败曾被误判为「未安装」，连带一条任务以「视频处理失败」收场（2026-10-16 19:38 实拍）
- **桌面端强杀后「生成失败: OSError: [Errno 22] Invalid argument」全灭（2026-10-16）**：BiliNoteBackend 由桌面端以管道方式拉起，app 被强杀/替换部署时 sidecar 成孤儿继续服务（收编机制不重启它），但 stdout/stderr 管道读端已随 app 消失——Windows 上此后任何一次写入都抛 `OSError 22`，yt-dlp 的 `to_screen`/`write_string` 连带 B 站字幕获取与音视频下载整链全崩（10-15 23:42 换 app.exe 重启后，00:19 起 B 站任务 100% 失败）。三层修复：(1) `backend/safe_stdio.py` 在 main.py 入口、早于一切 app 导入把 stdout/stderr 换成容错代理——写失败不再抛异常、只记一条 warning，后续输出降级进 `logs/stdio_fallback.log`（实测坏流状态下真实 B 站视频完整下载成功）；(2) yt-dlp 全部调用点挂 `logger`（`YDL_RETRY_OPTS` 与 video_meta 元信息路径），输出走 app.log、不再写标准流；(3) 新增 `test_safe_stdio` / `test_ydl_logger` / `test_mcp_loopback_direct` 回归
- **MCP server 全工具 502（2026-10-16）**：`list_notes` 等对 `http://127.0.0.1:8483` 的请求经 `httpx` 读了 Windows 注册表系统代理——trust_env 不尊重 ProxyOverride 例外列表，Clash 系统代理开着时回环请求被代理层回 502 空响应（与后端 a6b2523「回环不走代理」同根因）。修复：`BILINOTE_BASE_URL` 为回环地址（127.0.0.1/localhost/::1）时 `trust_env=False` 恒直连，远端 Worker 语义不变
- **截图/跳转标记替换后的残留星号（2026-10-15）**：合并后整理上线后全库复查发现 82/242 篇笔记带残留星号——旧清理规则要求「整行以图片开头」，列表项「- 文本![](img)*」形态与「两图之间」「图片被 \\*…\\* 包住」三类全部漏网，渲染层会开启跨行斜体。规则放宽为「图片后跟星号到行尾 / 先解包星号包裹的图片 / 摘两图之间的孤星」，只碰图片与跳转链接相邻的星号，`**加粗**`、正文斜体不受影响；已对全库 242 篇存量笔记就地清洗（原文件备份于 `~/bilinote-note-backup-20261015`）
- **「生成性能与预算」改动不保存（2026-10-13 用户实锤）**：失焦提交拿输入值跟「已被 onChange 同步过的本地态」比较，`v !== taskCfg[key]` 恒为 false，POST 永远发不出去——离开页面再回来数值全部回弹。改为与服务器基线（savedRef）比较，提交序号防止慢响应回写覆盖新编辑，非法值真回滚到基线；顺带修复「已启用模型」chip 在添加新模型后不刷新
- **开着视频理解却出无截图笔记（2026-10-13 用户实锤）**：命中音频缓存但缓存里没有可用本地视频（旧缓存来自未开视频理解的运行）时，旧逻辑静默降级纯文本 → 无帧、无截图，且 `*Screenshot-[mm:ss]` 标记原样残留在正文。现在改为继续走视频帧流程重新下载视频补帧（音频文件还在则复用、不重下）；截图插入环节无论整段跳过还是个别失败，标记一律兜底清扫
- **排队卡片「未命名笔记」（2026-10-13 用户实拍）**：手动单条提交写 PENDING 时不含标题/封面，几十条排队卡全是未命名。现在提交后后台轻量预取元信息合并进状态文件（YouTube 元信息路径补上用户配置代理——此前不设代理国内必失败），排队几秒内即显示标题封面；提交参数同时落盘进状态文件，进程重启后（reap 会把非终态任务判失败）可凭记录忠实重提。顺手清掉 51 个旧版遗留的 `*_markdown.status.json` 僵尸文件

- **kombu 隐性死依赖**：`services/provider.py` 顶部 `from kombu import uuid` 是 celery 时代遗留的唯一真实引用（其余 celery/redis/amqp/billiard/kombu/vine 零引用已从 requirements 移除、venv 已卸载），换标准库 `uuid4().hex`

- **B 站扫码登录 + Cookie 自动续期**：手动粘贴的裸 SESSDATA 没有 refresh_token，B 站服务端一失效就只能反复手动更新——现在「设置 → 下载配置 → B 站」支持扫码登录（passport qrcode/generate+poll），登录即保存完整 Cookie 并拿到 refresh_token，此后每轮自动化/检查登录前按 B 站官方 Cookie 刷新协议自动续期（RSA-OAEP correspondPath → correspond 页取 refresh_csrf → cookie/refresh 换发 → confirm/refresh 作废旧凭据，12h 节流），登录态可无限保持。手动粘贴完整 Cookie 串时自动解析 `ac_time_value` 作为续期凭据；「检查并续期」按钮可手动触发。协议参考 bilibili-API-collect（原仓库已下架，取自备份仓库文档），零新增依赖（pycryptodomex/qrcode.react 均已在依赖内）

- **长视频喂图增强（时间对齐分批 + 预算上调）**：视频理解的「AI 吃图」总天花板从硬编码 600 帧/150 张拼图提到 2400/500（`FRAME_BUDGET`/`IMAGE_BUDGET` 环境变量可调），≤4h 视频保持设定采样间隔不再被拉长。喂图策略默认改为 `SUMMARY_FEED_STRATEGY=segmented`（时间对齐分批）：转写按时间轴切 N 个连续窗口、拼图按时间占比摊进各窗，每一批同时含「这段的转写 + 这段的截图」——旧行为按字节切块时转写几乎永远整个进第 1 块，超出的拼图全部溢出成无上下文的纯图块；每批仍 ≤20 张拼图（上游实测稳定上限），批次串行调用 + 断点续跑，总结死线按批次数自适应放宽（`SUMMARIZE_LADDER_DEADLINE_PER_CHUNK`）。降级阶梯（原样→减帧→纯文本）与无截图场景行为不变

### Fixed

- **长视频截图时间戳消失/乱序**：抽帧文件名正则 `\d{2}` 只认两位分钟，≥100 分钟视频的帧排序 key 全变 inf（拼图顺序乱、时间戳角标不画）；现支持 1-4 位分钟数。尾组不足一格不再整组丢弃（旧逻辑让视频结尾几分钟完全没有截图），按列数收窄行数生成小拼图。拼图预算超限时优先拉采样间隔、保持 2×2 大格子（单格 783×441），仅当间隔要超设定值 2 倍才升 3×3——回应「长视频把拼图尺寸弄得很大」

- **测试全量运行互相污染**：test_groq_proxy_fallback 运行时把 `sys.modules["app.utils.openai_client"]` 永久替换成桩模块、test_video_reader_dedupe 收集期替换 `sys.modules["ffmpeg"]`——后收集的测试（loopback 代理回归、max_frames 兼容）按字符串 target 打补丁会落在桩上。相关测试改为在收集期绑定真模块对象、对模块自身持有的引用打补丁

- **超长视频分段转写**：262 分钟音频压到下限码率仍 60.1MB 实锤根因——`compress_audio` 只降码率不降采样率，mp3 在 44.1kHz 的编码器下限是 32k，78 分钟以上的音频永远进不了转写引擎 18MB 上限。两级修复：(1) 压缩降采样到 16kHz（whisper 原生采样率，质量无损），单文件容量 78 → 约 157 分钟；(2) 仍超限时等长硬切 N 段逐段转写再按名义起点平移合并（ffmpeg `-ss/-t -c copy` 零转码切割；段级缓存断点续跑、合并后清理；段进度「转写中（第 i/N 段）」实时透出到界面）。`Transcriber` 基类新增 `max_audio_size_bytes` 上限声明（本地引擎 None 不受影响），下游 prompt/原片跳转/向量索引零改动。已实机验证：262 分钟 → 2 段 → 9130 片段时间戳 0→15748s 无缝衔接

- **自动化检查轮整轮失败的告警通知**：轮级失败（Cookie 失效/网络中断/前置校验不过）此前只写日志——fetch 在提交任何任务之前就抛出，走不到汇总通知路径，自动化停摆完全静默（2026-10-10 Cookie -101 连败 1 小时+ 零邮件）。现新增 `send_round_failure_alert`：基础节流 30 分钟 + 同文案 6 小时抑制（文案变化立即告警），轮成功时清告警记忆

- **MCP Server（外部 AI harness 接入）**：新增 stdio MCP 服务器（`backend/mcp_server.py`），Codex / ZCode 等外部 AI 工具可直接读取笔记库并调用核心功能。检索优先哲学：`search_notes` 走新增的 `POST /api/chat/search` 纯本地向量原文块检索（零在线 API），由 harness 自身模型推理作答；内置在线 API 问答降为兜底。11 个工具覆盖读（列表/正文/转写/检索/索引状态）、写（生成/重试/导出/补索引）与删除（需显式 confirm）。本机回环免鉴权，远程 Worker 经 Tailscale + 配对 token 可用（`BILINOTE_BASE_URL` / `BILINOTE_TOKEN`）。见 [docs/mcp.md](./docs/mcp.md)

- **AI 问答行内引用角标**：回答正文按来源编号标注可点击的 `[n]` 角标（与「引用来源」列表一一对应），工具查询到的原文证据同样编号入列——回答与来源不再各说各话。跳转全链路修复：全屏问答/手机窄屏下点引用自动切回正文视图再定位；长视频时间戳（`101:52` / `H:MM:SS`）可定位；来源笔记已删除时提示而非掀掉工作台；单笔记提问的来源徽章可点击；`intro`/开头块定位到笔记顶部；徽章时间显示改为 `M:SS` / `H:MM:SS`
  - 后端配套修复：跨查距离闸基准改回「词面命中候选」（旧实现遍历全体候选，无关最近候选会误杀真命中，引用被砍到只剩保底单条）；全局索引缺失时自动重建/回退（旧实现静默返回零来源）；时间字段容错不再 500
  - **词面补充召回通道**：默认英文嵌入模型对中文语义排序接近失效（逐字命中的块可排到 176/7323 名，连精确章节标题都进不了前 200），语义召回池里根本没有正确块——「问价格答没有价格」由此而来。现按问题关键串（CJK 滑窗、泛词剔除）用 `$contains` 做确定性补充召回，与语义候选同场词面重排竞争；候选池按文本去重（历史重建留下的重复副本不再重复出徽章）

### Fixed

- **OpenCode Go 订阅 400 MissingSessionID**：OpenCode Go 官方要求每个请求带稳定 `x-opencode-session` 会话头（用于路由与 prompt 缓存），Claude Code/Codex/ZCode 等客户端原生携带，而 BiliNote 的裸 OpenAI SDK 调用不带 → 网关 400 拒绝。供应商新增「自定义请求头」配置（SQLite 自动迁移 + UI JSON 编辑），在 `build_openai_client(default_headers=...)` 一处注入，问答/笔记生成/连通性测试/模型列表全部生效；Responses 协议经适配层透传同样受益。OpenCode Go 供应商已在部署时配好该头
- **本地代理进程死亡拖垮全链路（自动化 ProxyError 连败）**：应用启动时按 proxy.json 把代理注入进程环境变量（为 HuggingFace 下载），本地代理退出后 watchlater 等 trust_env=True 的 requests 与 yt-dlp 全部撞 ProxyError 10061（B 站直连本是通的）——2026-10-11 13:42 起自动化每 5 分钟连败。三层加固：① B 站 API 调用（稍后再看/元信息/字幕/passport 登录续期）统一走 `bilibili_session()` 强制直连永不代理；② 代理注入环境变量前先 TCP 探活，死代理不注入；③ LLM 客户端对配置代理探活失败时降级直连并告警。另：自动化失败轮现在也更新 last_round_ts（此前只记成功轮，监控时间线失真）
- **本地网关被系统代理误伤（502 全灭）**：后端进程若被带 `HTTP_PROXY` 的终端拉起（继承环境变量），`ProxyConfigManager` 的环境回退会把「走代理」决策套到 `http://127.0.0.1:8787`（本地 muse 网关）上头——httpx 经系统代理访问回环地址被代理层回 502 空响应（curl 同路径却正常；2026-10-11 实锤：23:49 起全部总结失败、网关日志里没有请求）。现对回环 base_url（127.0.0.1/localhost/::1）跳过一切代理注入恒直连；远端地址仍保留环境变量回退语义（docker/CLI 场景不回归）

- **转写引擎直连被拒（403）自动降级系统代理**：中转站对国内直连 IP 一律返回 403（同一密钥走系统代理即 200），而应用 OpenAI 客户端默认绕系统代理直连（防 Clash 对流式请求假死），转写被误伤——自动化每轮重试全部失败、列表持续显示「失败」。现遇 403 自动用系统代理重建客户端重试一次（转写是一次性大请求，无流式假死风险）

- 「设置 → 通用」新增**「按住左键拖动滚动」开关**（默认开=现状）：关闭后修饰键互换——左键拖动恢复为原生选择文字，改用 Shift+左键拖动滚动；习惯「左键就是选字」的用户无需再按住 Shift

## [2.5.0] - 2026-10-07

> Fork 增强版首个版本（基于上游 2.4.5）。逐项变更见 2026-09-24 以来的 git 提交历史；
> 部署与使用文档见 [docs/deploy-windows-installed.md](./docs/deploy-windows-installed.md) /
> [docs/usage-guide-fork.md](./docs/usage-guide-fork.md)。

### Added

- **触摸式阅读滚动**：滚轮惯性平滑（消除一格一顿刻度感），按住左键拖动 1:1 跟随 + 松手速度连续摩擦滑行（iOS decelerationRate 同族模型，滑行初速=松手手速，滑行中再按即停）；「设置 → 通用」开关 + 三档手感（跟手/适中/动量），Shift+拖保留文字选择，拖动与点击按 6px 位移阈值区分
- **Worker / Viewer 分离**：后端直服手机网页版，Tailscale 直连 + token 配对免公网，远端配置总开关、连接自检与配对前预检
- **多格式导出**：笔记导出 PDF / Word / 长图 / 摘要海报（桌面端走 dialog 另存为，绕开 WebView2 对 blob 的拦截）；思维导图主题切换与 SVG 导出
- **剪贴板监控**：复制视频链接切回窗口自动弹窗回填；可选后台轮询与 OS 系统通知（Tauri onFocusChanged 门控 + AUMID 快捷方式）
- **AI 问答工作台**：三列并排布局，引用徽章点击定位正文章节（高亮 2 秒）或原文对应时间点

### Changed

- 生成历史视频级去重：同一视频只保留一张卡，重复生成收纳为多版本；前后端双向清理镜像残留
- 总结降级阶梯：原样→减帧→纯文本逐级重试，卡片实时显示当前策略，25 分钟总死线兜底不再无声卡死
- 自动化韧性：轮前 ffmpeg 预检、失败任务 30 分钟冷却、通知节流 30 分钟、「立即运行一轮」显示每个视频的提交/跳过原因
- 桌面启动：端口预检 + 占用者点名、孤儿后端健康探测收编（根治假「后端已退出」横幅）、老笔记标题截断自愈
- 手机端 UX：历史全量回填+懒加载、免 http 前缀、本机地址面板、深链直开全量可用

### Fixed

- 超长音频转写报 Connection error：Groq 25MB 上限，按时长自适应码率压缩 + 临时文件泄漏
- 多 P 视频 `_pN` 后缀导致自动化每轮重复总结同一视频：id 归一化 + 存量数据清洗
- 打包白壳 exe（BiliNoteBackend.spec pathex/collect_all 静默回退）导致 AI 问答、chromadb 等整块功能全灭
- 任务状态机三故障：失败重试被拦、僵尸 PENDING 任务、terminated 红横幅卡死
- 长视频 Groq 30MB 超限、上游抖动（网关空回/SSL）重试与末次换链路
- 导图空分支（斜体星号残留 + markmap 空岔）、问答分页、自动化 Cookie 跨轮状态残留等

## [2.4.4] - 2026-06-23

### Security

- **升级 Starlette 0.46.1 → 0.47.2 修复 CVE-2025-54121**（[GHSA-2c2j-9gv5-cj73](https://github.com/advisories/GHSA-2c2j-9gv5-cj73)，#411）：旧版在解析 multipart 表单中的大文件时，`SpooledTemporaryFile` 从内存溢写到磁盘的 rollover 在事件循环线程内**同步执行**，攻击者可借大文件上传阻塞事件循环造成拒绝服务（DoS）。新版把该写入移到线程池。FastAPI 同步升级 0.115.12 → 0.116.2（其 starlette 约束由 `<0.47.0` 放宽，以容纳修复版本），与现有 pydantic / anyio / python-multipart 均兼容。

## [2.4.3] - 2026-06-23

### Fixed

- **Whisper `large-v3-turbo` 模型无法下载**（#402）：内置映射指向的 `Systran/faster-whisper-large-v3-turbo` 仓库已从 HuggingFace 下架（返回 401/404），点击下载会静默失败、状态一直显示「未下载」。改用社区维护的 CT2 转换版 `deepdml/faster-whisper-large-v3-turbo-ct2`（直链可达、含 `model.bin`，与 faster-whisper 的 `large-v3-turbo` 等价）。
- **模型下载失败时前端无任何提示**（#402 衍生）：`/transcriber_models_status` 此前只回传 `downloading`/`downloaded`，后台下载失败状态被丢弃。现新增 `model_download_state` 统一维护下载状态与失败原因，状态接口新增 `failed` 字段并透传 `error`；前端模型列表展示「下载失败」徽标 + 错误详情，按钮变为「重试」，并对新出现的失败弹出提示。

## [2.4.2] - 2026-06-17

### Fixed

- **Docker 部署打开显示 nginx 欢迎页**：`nginx/default.conf` 被 docker-compose（多容器）与 `Dockerfile.complete`（单镜像）共用，但两种模式对 `location /` 的需求相反（多容器需反代独立的 frontend 容器，单镜像需直接服务本地静态文件），导致其中一种部署方式总会回退到 nginx 默认欢迎页。现拆分为两份配置：`nginx/default.conf`（compose，反代 frontend 容器）与新增的 `nginx/standalone.conf`（单镜像，静态前端 + 本地 backend 代理）；`Dockerfile.complete` 改用后者并删除 Debian 默认站点，两种部署方式均恢复正常。

## [2.4.1] - 2026-06-17

### Added

- **YouTube Shorts 链接支持**：后端 URL 校验与 video id 提取支持 `youtube.com/shorts/<id>` 形态，Shorts 链接可正常提交生成笔记（#381）。

### Fixed

- **B 站 412（wbi/playurl 风控）**：B 站 `x/player/wbi/playurl` 网关新增 `dm_img_list`/`dm_img_str`/`dm_cover_img_str`/`dm_img_inter` + `web_location` 风控校验，缺失即返回 HTTP 412。多数视频网页内嵌 playinfo、yt-dlp 不调此 API；而网页不内嵌 playinfo、必须走 API 的视频（如 BV1X9L16oEgB）会撞上风控，刷新 cookie 无效、yt-dlp（含最新版）上游尚未适配。现于 wbi 签名前注入哑值 dm_img 风控参数（形态对齐 yt-dlp 自身 arc/search 用法）恢复 200（#410）。
- **B 站分 P 视频字幕取错集**：分 P 视频提交 `?p=N` 时，字幕优先链路未透传 p 参数，始终取第 1 集 cid，导致笔记内容与实际下载的 p=N 音频不一致。现从 `data.pages[N-1]` 取对应分 P 的 cid（#409）。

## [2.4.0] - 2026-06-07

### Added

- **可配置 Whisper 模型**：转写设置支持自定义 Whisper 模型与名称映射，可指定自定义 HuggingFace repo 或本地路径（新增 `backend/app/transcriber/whisper_models.py` + 测试），转写设置页可选择 / 配置模型。
- **关注公众号获取交流群**：关于页群二维码改为公众号二维码，关注公众号后回复「交流群」即可获取最新群二维码，避免群码过期失效。

### Fixed

- **浏览器扩展**：修复标题显示异常，优化脑图（Markmap）导出。

### Docs

- **GPU/CUDA 部署**：README 补全 GPU/CUDA 部署说明。

## [2.3.4] - 2026-05-27

### Added

- **一对一搭建服务二维码**：新增「BiliNote AI 笔记系统一对一搭建服务」二维码（README + 关于页），扫码加微信、备注「搭建服务」即可咨询。

## [2.3.3] - 2026-05-22

### Fixed

- **预构建 Docker 镜像数据持久化**：文档的 `docker run` 只挂了 `data/`（媒体缓存），而 SQLite 数据库（LLM 供应商配置 + 笔记历史）和笔记文件不在该卷下，导致删除 / 升级容器时丢失配置与历史。现将数据库重定向到 `/app/backend/data/bili_note.db`、笔记到 `data/note_results`（随 data 卷持久化）；README 更新为挂载 `data` / `config` / `static` / `models` 四个数据卷，并提示**勿**挂整个 `/app/backend`（命名卷会固化镜像内代码，导致 `docker pull` 升级不生效）。`docker-compose` 路径本就正确（`./backend:/app` 整目录绑挂），未受影响。

## [2.3.2] - 2026-05-22

### Fixed

- **后端启动崩溃（Docker）**：`python:3.11-slim` 基础镜像升级到 Debian 13 / glibc 2.41 后，`ctranslate2` 4.5.0 预编译库带「可执行栈」标记被 glibc 拒绝加载（`cannot enable executable stack ... Invalid argument`）。由于 `from faster_whisper import WhisperModel` 在顶层 import，import 失败直接拖垮整个后端启动 → 容器反复重启。升级 `ctranslate2` 4.5.0→4.6.0（wheel 加入 `noexecstack` 链接标志，从二进制层根治）
- **whisper 模型误报「离线模式找不到模型」**：下载（modelscope 自定义目录）与加载（faster-whisper HF cache）布局不一致导致命不中缓存。统一为下载 / 加载 / 完整性检测 / 损坏自愈都走 HF cache 布局，并向后兼容老 modelscope 目录
- **桌面端构建产物版本恒为 2.0.0**：Release 工作流在 `pnpm tauri build` 前从 git tag 注入版本号到 `tauri.conf.json`，使产物版本与 Release 版本对齐

## [2.3.1] - 2026-05-22

### Changed

- **更新微信交流群二维码**：旧二维码即将失效，替换 README 中 5 个交流群（群 1-5）的入群二维码。

## [2.3.0] - 2026-05-14

主线：一波部署与运行时韧性专项——Docker / 桌面端 / 在线引擎三端的"装不上、起不来、跑一半挂"问题集中清理，并新增全局代理与转写模型就绪门禁。

### Added

- **全局代理**：新增 `ProxyConfigManager`（`config/proxy.json` 持久化 + `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 环境变量兜底）。一处配置同时作用于 LLM API、转写 API（Groq 等）、yt-dlp 视频下载、youtube-transcript-api 字幕拉取。前端「设置 → 下载配置」页新增代理卡片，会显示当前实际生效值（含 env 兜底来源提示）
- **转写模型就绪门禁**：`/generate_note` 在排队前检查本地转写引擎（fast-whisper / mlx-whisper）的模型是否已下载完整，未就绪直接拦截并返回 `reason=transcriber_model_not_ready`，不再让任务静默卡在首次大文件下载；前端引导用户去「音频转写配置」页下载
- **桌面端后端健康监控韧性**：Tauri 侧 spawn sidecar 后以 HTTP 探针轮询 `/api/sys_check` 判就绪并 emit `backend-ready`；`RunEvent::Exit` 钩子在 app 退出前 kill sidecar，杜绝孤儿进程占用 8483 端口；启动失败对话框展示原因 + 最近 stderr + 一键重启 / 复制日志
- `/sys_health` 重构为结构化健康响应 `{backend, ffmpeg, db, whisper_model}`；部署监控页显示 Whisper 模型本地下载状态
- 所有 Dockerfile 新增 `BASE_REGISTRY` build-arg，国内拉不到 docker.io 时可换 daocloud 等镜像源

### Fixed

- **whisper 模型损坏自愈**：`model.bin` 截断 / 损坏导致 `Unable to open file 'model.bin'` 死循环——加载失败时删除损坏目录、重新下载、重试一次；mlx-whisper 同样按 `config.json` 判定完整性
- **空 API Key 天书报错**：空 key 会让 httpx 拼出非法 header `Bearer ` 并抛 `LocalProtocolError: Illegal header value b'Bearer '`。新增 `build_openai_client` 在入口校验，给出「xxx 的 API Key 未配置」的清晰提示
- **新模型 temperature 不兼容**：OpenAI o1 / o3 / gpt-5 系列拒绝自定义 `temperature`，命中后就地去掉该参数重试，不消耗重试预算
- **桌面端「后端加载中」死循环**：`useCheckBackend` 重写——60s 总超时取代 `while(true)` 无限轮询，订阅 Tauri `backend-ready` / `backend-terminated` / `backend-startup-timeout` 事件；裸 `fetch` 探测避免启动期 toast 叠堆
- **CORS 漏配桌面端 origin**：补全 `tauri://localhost` / `https://tauri.localhost`，修桌面端 fetch 拿到 200 却被浏览器 CORS 拒绝读响应（表现为"连不上后端"但后端日志全 200）
- `/api/api/sys_health` 双 `/api` 前缀导致健康检查 404
- `docker-compose` 的 `restart: on-failure:3` 改为 `unless-stopped`，避免短暂崩溃后容器被永久打死；GPU compose 补齐 `healthcheck` / `restart` / `mem_limit`
- `Dockerfile.complete` 的 supervisord 用 `%(ENV_*)s` 透传环境变量给 backend 子进程（此前只白名单 2 个，`docker run -e` 配的变量后端看不到）
- `.env.example`：修正 `VITE_API_BASE_URL` 端口（8000→8483）、`WHISPER_MODEL_SIZE`（medium→tiny，首次启动不被 ~1.5GB 下载卡住）
- Onboarding：第 1 步后端连通检测改为自动重试 + Tauri 事件触发 + 手动重检按钮；第 2 步撞预置供应商名时改为更新已存在供应商而非报错
- 模型供应商列表卡片整行可点击切换（此前仅 icon 区域响应）
- `connect_test` 改用真实 chat completion 探测而非 `/v1/models`（后者在 key 无 inference 权限 / 供应商不实现该端点时会误判）

### Internal

- `backend/main.py` lifespan 拆为 `[startup 1/5]…[startup 5/5]` 分段日志，启动期异常可一眼定位死在哪一步
- `request.ts` 新增 `suppressToast` 配置位，预期内的失败（如 onboarding 撞名重试）不弹全局红 toast
- `CLAUDE.md` 勘误：移除不存在的 `app/messaging/` / `app/i18n/` / `worker_registry.py` 描述，修正 `events/` 路径，补 `pytest` / 前端 `typecheck` 命令

## [2.2.3] - 2026-05-09

### Fixed

- 前端 vite build 在 Docker / Tauri CI 中失败：`Rollup failed to resolve import '@tauri-apps/api/event'`。v2.2.0 加的 P1/P2 桌面端组件用了 `await import('@tauri-apps/api/event')` 与 `'@tauri-apps/api/core'`，但 `@tauri-apps/api` 只是 `@tauri-apps/plugin-shell` 的间接依赖，没在 `BillNote_frontend/package.json` 直接声明，Rollup 在 production build 时静态分析报"无法解析"
  - `BillNote_frontend/package.json`：把 `@tauri-apps/api` 加为直接依赖（`^2.10.1`，与 lockfile 中已有的 transitive 版本一致）
  - 本地 `DOCKER_BUILD=1 pnpm run build` 复现 + 验证修复

## [2.2.2] - 2026-05-09

补 v2.2.1 漏掉的 Tauri 桌面端 build 修复。

### Fixed

- 桌面端 Tauri 构建失败：v2.2.1 的 hotfix 只修了 Docker 镜像构建里的 pnpm 版本，`main.yml` 的 `pnpm/action-setup@v4 with: version: 'latest'` 没改，于是桌面端 build 仍然在 `Install frontend dependencies` 步报 `ERR_UNKNOWN_BUILTIN_MODULE: No such built-in module: node:sqlite`（pnpm 11 要求 Node 22+，但 main.yml 用的 node 20）。pin 到 `9.15.0`，与 Docker 侧一致。

## [2.2.1] - 2026-05-09

补 v2.2.0 ghcr.io 镜像构建失败。

### Fixed

- Docker 镜像构建失败：`v2.2.0` tag 触发的 ghcr.io 推送在 frontend-builder 第 5/7 步 `pnpm install --frozen-lockfile` 报 `ERR_UNKNOWN_BUILTIN_MODULE`。根因：`corepack prepare pnpm@latest` 拉到了 pnpm 11.0.9，而 pnpm 11 要求 Node 22+，跟我们的 `node:20-alpine` 不兼容。
  - `Dockerfile.complete` 与 `BillNote_frontend/Dockerfile` 的 pnpm 版本 pin 到 `9.15.0`（lockfile 由 pnpm 9 生成，匹配 Node 20）

## [2.2.0] - 2026-05-09

主线：浏览器插件功能与 web 端 NoteForm 完整对齐；桌面客户端 UX 与错误恢复一波重炼。

### Added — 浏览器插件

- 笔记选项与 web 端 NoteForm 完整对齐：
  - `style` 由自由文本改成 9 个预设下拉（minimal / detailed / academic / tutorial / xiaohongshu / life_journal / task_oriented / business / meeting_minutes），与 backend `prompt_builder.note_styles` 严格匹配（之前自由文本不命中 enum 等于没传——隐性 bug）
  - `format` 完整 4 个 checkbox（toc / link / screenshot / summary，原来只有 screenshot/link）
  - `extras` 文本框：拼接到 prompt 末尾的 ad-hoc 提示
- 多模态视频理解：`video_understanding` 开关 + `video_interval`（1-30 秒）+ `grid_size`（[r,c]，1-10），抽帧拼图喂视觉模型，提示需选视觉模型才生效

### Added — 桌面客户端

- **首启 4 步引导**（`/onboarding`）：后端连通性自检 → LLM 供应商 + 模型 → 转写引擎选择（默认推荐 Groq）→ Cookie 同步说明。完成后 `localStorage('bilinote-onboarded')` 标记，纯 web 端不打扰
- **Sidecar 健康度面板**：右下角浮动状态点（绿/黄/红，5s 轮询 `/sys_health`），点开抽屉看最近 200 行后端日志、一键重启后端（新增 Tauri command `restart_backend_sidecar`）、复制日志
- **启动期路径诊断**：Tauri `setup` 中检测安装路径含非 ASCII / 含空格 / 父目录不可写时，emit `backend-warning` 让前端顶端横幅显式告警，主动暴露 README 长期文字警告但无防御的"中文路径"等坑

### Changed

- Whisper 默认模型 size 从 `medium`（~1.5GB）改为 `tiny`（~75MB）：新装用户没主动设置时不再卡在首次大模型下载；高精度可在「音频转写配置」页主动切
- 切到 `fast-whisper` / `mlx-whisper` 且当前 size 未下载时，「音频转写配置」页保存前 confirm 体积提示，并推荐改用在线引擎
- Tauri sidecar 启动逻辑抽出 `spawn_backend_sidecar()`；child handle 存进 `SidecarHandle` state 以支持后续 restart
- sidecar stdout/stderr emit 时不再用 `format!("'{}'", ...)` 包引号，原文直传（前端 hook 兼容旧格式兜底剥引号）

### Fixed

- WhisperTranscriber 在半成品模型目录上死循环报 `Unable to open file 'model.bin'`：判定从「目录存在」改为「`model.bin` 落盘」，半成品目录会被识别并重新下载（PR `fix/backend-deploy-resilience`）
- `/api/deploy_status` 在没装 torch 的部署上 `ModuleNotFoundError: No module named 'torch'` 500：torch 改 try/except，未装时返回 `{available: false, torch_installed: false}`；transcriber 配置 + ffmpeg 也都裹 try，单项失败不再打死整个监控页（同上 PR）
- `routers/config._check_whisper_model_exists` 同步改用 `model.bin` 判定，避免「已下载」状态在监控页误报

## [2.1.4] - 2026-05-07

CI 工程化修复，无运行时行为变化。

### Internal

- 桌面端 Tauri 构建矩阵去掉 Linux（`ubuntu-22.04 / x86_64-unknown-linux-gnu`）。Linux 桌面端构建持续 17m+，且无对应分发渠道；Linux 用户继续可以走 Docker 镜像 (`ghcr.io/jefferyhcool/bilinote`)
- commitlint workflow 去掉无效的 `firstParent` input（wagoid/commitlint-github-action@v6 不支持，被忽略并打 warn）
- 规范 release merge commit 标题：`chore(release): vX.Y.Z`（合 master）/ `chore(release): merge release/X.Y.Z back into develop`（回灌 develop），让 commitlint 能正确识别。`RELEASING.md` §3 与 `CONTRIBUTING.md` §6.3 同步更新

## [2.1.3] - 2026-05-07

### Fixed

- DeepSeek 等非多模态供应商被 400 拒绝（issue #282）：`UniversalGPT.create_messages` 与 `_build_merge_messages` 此前**无条件**把 content 拼成 OpenAI 多模态数组 `[{"type":"text",...}]`，DeepSeek `deepseek-chat` 等模型不识别 `image_url` 变体直接报 `invalid_request_error`。`GPTFactory.from_config` 一律实例化 `UniversalGPT`，所以问题覆盖**所有**通过模型设置页接入的非多模态供应商，不止 DeepSeek。
  - 现按 `video_img_urls` 是否非空切换 content 形态：有图保留多模态数组（视觉模型不退化），无图退回 string。合并阶段历来不带图，统一改 string。
  - 与同包内 `deepseek_gpt.py` / `openai_gpt.py` / `qwen_gpt.py` 的 message builder 行为对齐。
  - 新增 `backend/tests/test_universal_gpt_content_format.py` 6 个 case 回归覆盖（含 `image_url` 字面 not-in JSON 断言）。

感谢 @voidborne-d 的修复（#345）。

## [2.1.2] - 2026-05-07

补 v2.1.1 上 ghcr.io 镜像构建失败的坑。

### Fixed

- Docker 镜像构建失败：v2.1.1 tag 触发的 ghcr.io 推送在 frontend-builder 第 7/7 步 `pnpm run build` 挂掉（vite `loadConfigFromBundledFile` 加载 `@tailwindcss/vite` plugin 时 1.5s 内异常退出）。
  - `Dockerfile.complete` 与 `BillNote_frontend/Dockerfile` 升 `node:18-alpine` → `node:20-alpine`：Tailwind v4 已不再支持 Node 18，Vite 6 也推荐 Node 20+
  - `Dockerfile.complete` 的 frontend 阶段同时复制 `pnpm-lock.yaml` 并改用 `--frozen-lockfile`，杜绝每次构建重解析 semver 拉到比本地新的 native dep
  - `BillNote_frontend/pnpm-lock.yaml` 强制入库（之前一直未提交，导致 CI / 本地依赖图持续漂移）
- README 联系社区段补上微信群二维码（之前只写"年会恢复更新以后放出最新社区地址"）

## [2.1.1] - 2026-05-07

工程化与文档收尾，无运行时行为变化。

### Added

- [`CONTRIBUTING.md`](./CONTRIBUTING.md) — 贡献指南，落地简化 Git Flow（master + develop + 短生命周期分支）+ 提交规范 + 合并规范
- [`RELEASING.md`](./RELEASING.md) — 发版手册（Release Manager 视角），含 release/* 流程 + 各商店人工上传步骤 + 自动发布所需 secrets
- `.github/ISSUE_TEMPLATE/{config,bug_report,feature_request}.yml` — 表单形式的 issue 模板，按工作区分类
- `.github/pull_request_template.md` — PR 模板，把 CONTRIBUTING §5.2 落成 checklist
- `.commitlintrc.json` + `.github/workflows/commitlint.yml` — commitlint CI（PR + push develop/master 时校验，自定义 type 白名单，兼容中文 subject）
- `.github/workflows/release-extension.yml` — `v*` tag push 时自动构建插件 .zip / .xpi / .crx 并挂到对应 GitHub Release（商店自动发布以注释形式预留）

### Changed

- 关于页二维码改为 `import @/assets/wechat.png`，不再依赖腾讯云 COS CDN，更新只需替换文件 + 跑构建
- 群聊 QR 替换为最新版本（`doc/wechat.png` + `BillNote_frontend/src/assets/wechat.png`）

### Removed

- 关于页 QQ 群联系方式（号 785367111，已不再活跃维护）
- 旧版 `.md` 格式 issue 模板（被新 yml 表单模板取代）

## [2.1.0] - 2026-05-07

本次发布的主线是**浏览器插件**和 **B 站字幕优先链路**。配合一些后端 / 前端体验修复。

### Added — 浏览器插件 (`BillNote_extension/`)

全新 Chrome / Edge / Firefox MV3 扩展。Vue 3 + Vite + UnoCSS，骨架基于 vitesse-webext。

- **入口四件套**
  - 工具栏 popup：识别当前 tab → 一键提交，紧凑展示标题 + 封面 + 进度
  - 视频页悬浮按钮：仅在支持平台注入，点击即触发任务
  - 右键菜单"用 BiliNote 总结此视频"：限定 4 个支持域名
  - 侧边栏（side panel）：详情视图 + 三模式切换
- **侧边栏三视图**
  - Markdown：渲染笔记，复制 / 下载 .md
  - 思维导图：基于 markmap-lib + markmap-view 的可缩放 mind map
  - AI 问答：复用后端 RAG `/chat/index`、`/chat/status`、`/chat/ask` 三件套，自动索引 + 多轮历史
- **设置页五大块**（搬入 web 端全部配置能力，今后插件即配置中心）
  - 通用：后端地址、连通性测试、默认供应商 / 模型 / 画质 / 截图 / 跳转 / 风格
  - 模型供应商：完整 CRUD、启用切换、连接测试、模型增删
  - 音频转写配置：fast-whisper / mlx-whisper / Groq / 必剪 / 快手 切换、Whisper 模型大小、本地下载状态、触发下载
  - 下载配置：每平台 cookie 显示、浏览器一键同步、手动粘贴
  - 部署监控：后端 / FFmpeg / CUDA / Whisper 状态总览
- **浏览器 cookie 直通**：`chrome.cookies.getAll` 一键把 `.bilibili.com` 等域 cookie 同步到后端 `/api/update_downloader_cookie`
- **B 站字幕浏览器抓取**：插件直接调 player API 拿字幕，借 host_permissions 自动带本地登录态 cookie，绕过 CORS；随提交以 `prefetched_transcript` 字段附给后端，后端跳过 `download_subtitles` + 音频转写，直接进 GPT 总结

### Added — 后端

- `BilibiliSubtitleFetcher`（`app/downloaders/bilibili_subtitle.py`）：直接调 B 站 player API 拿字幕，作为非插件场景下 yt-dlp 路径的更可靠替代
- `VideoRequest.prefetched_transcript` 字段：客户端预取的字幕直接落到 `<task_id>_transcript.json`，NoteGenerator cache-hit 自动复用

### Added — 前端 Web

- Zustand persist 迁移到 IndexedDB（#318）

### Changed

- 后端 CORS：从静态 origin 列表改为 regex，兼容 `chrome-extension://`、`moz-extension://`、`localhost`、`tauri.localhost`
- mlx-whisper 仓库 ID 改用 `MLX_MODEL_MAP`：`whisper-{size}-mlx` 命名（`large-v3-turbo` 例外），不再 hardcode 出 404
- BilibiliDownloader 从 `CookieConfigManager` 读取 cookie 并注入 yt-dlp cookiefile（#333）
- CLAUDE.md 补充 v2.0.0 引入的子系统说明（RAG / Chat、可选 Nacos+RabbitMQ、i18n、cookie/transcriber 管理器）以及浏览器插件 workspace

### Fixed

- AILogo：自定义供应商（`logo='custom'`）走兜底渲染时不再误报 `console.error`，未匹配的名称降级为 warn
- SettingPage `Model.tsx` 双栏布局加 `min-h-0 overflow-y-auto`：供应商列表过长时无法滚动
- 供应商开关切换不能实时生效（#336）
- `/get_all_providers` 中 301 行历史伪内置脏数据清理 + `add_provider` 加防御（强制 `type='custom'`、同名查重、错误向上抛）
- `/api/task_status` 拆 ResponseWrapper：插件侧进度条因未拆 `data` 全灰；同时把 `R.error` 翻译为 `status:'FAILED'`，避免 UI 卡在轮询循环
- ESLint / ESM `__dirname` 在 production build 中未定义（多个 docker / vite 配置修复）
- GitHub Actions 构建错误 + apt-get 安装失败 + 删除仓库内 ffmpeg 二进制
- 渲染时剥掉 backend 注入的 `> 来源链接：URL` 行（与 web 端 MarkdownViewer 一致），导出文件保留原行便于溯源
- 侧边栏布局收紧：完成后不再渲染 8 段进度条；标题压成单行；视图切换 + 复制 / 下载并入一行；历史任务从底部 details 改为顶栏下拉

### Internal

- 新增分支策略：`develop` / `release/x.y.z` / `master` git-flow
- 备份 backend SQLite DB 前 / 清理脏数据后均落盘存档
