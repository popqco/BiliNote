# 原片截图密度：同类项目调研与自适应方案

> 2026-10-28。背景：BiliNote 当前用「固定平均间隔（默认 90s，可配 20–900s）+ 最小间隔 +
> H2 小节配额 + 全篇上限」稀释 LLM 写出的 `*Screenshot-[mm:ss]*` 标记。用户反馈视频的
> 信息密度不是线性的：教程/幻灯片段画面变化密集，谈话/口播段几乎没有可截画面，固定
> 间隔在密集段漏帧、在稀疏段过采样。

## 结论

1. **固定平均间隔不是业界主流。** 开源/工业界普遍采用「视觉变化或幻灯片检测产生候选
   → 去重 → 按预算选帧」，而不是按时间均匀切。
2. 与 BiliNote 最接近、最低风险的做法：**复用现有 6s 采样帧和 dHash，加上转写文本密度
   与 LLM 语义标记，做「候选生成 → 打分 → 预算分配 → 选择 → LLM 角色」五步**。Phase-1
   不需要 PySceneDetect / OpenCV / Tesseract。
3. 推荐的核心思想来自 QCA / SLICE / FOCUS：先估计每个时间块的信息密度，再按密度分配
   截图预算；同时保留覆盖下限与最小间隔，避免「全挤在密集段」。
4. 「平均间隔」应降级为**预算换算器**：`目标张数 ≈ 时长 / 平均间隔`，但截图不再等间隔
   分布。UI 保留「密度档位、最小间隔、覆盖下限、单篇上限」。
5. B 站「高能进度条」暂不可依赖：实测官方 `videoshot` 接口的 `pvdata` 是预览雪碧图
   时间戳的二进制 URL，不是热力图曲线；YouTube「most replayed」是 innertube 非公开
   数据，只能做低权重 tie-breaker。

## 同类项目

| 项目 | 方法/参数 | 强项 | 局限 |
|---|---|---|---|
| [video-context](https://github.com/LaloHao/video-context) · [how-it-works](https://github.com/LaloHao/video-context/blob/master/docs/how-it-works.md) | ffmpeg `scene > 0.25` 选帧，且至少每 60s 一帧；9×8 灰度缩略图；64-bit dHash 与上一保留帧 Hamming ≤ 4 则丢弃；Tesseract OCR 可选 | 极简、本地、确定性好，与 BiliNote 现有 dHash 思路几乎一致 | 场景阈值固定；60s 保底会产生冗余；只和上一保留帧比；无内容预算 |
| [video-slide-extractor](https://github.com/itsyujay/video-slide-extractor) | 默认 1 fps；160×90；8×8 block，`blockDelta=14`，`changedRatio=0.02`；activity mask `activeFrac=0.5`、`maxMaskFrac=0.35`；Otsu/median+2MAD 校准 | 零依赖；屏蔽 webcam/cursor；benchmark 明确 | 手持/游戏/电影/白板弱；overlay 持续时默认阈值会泛滥 |
| [PySceneDetect](https://www.scenedetect.com/docs/latest/api/detectors.html) | `ContentDetector`: HSV 加权像素差，threshold 27.0，min_scene_len 15；`AdaptiveDetector`: rolling average，adaptive_threshold 3.0，window_width 2，min_content_val 15；`HashDetector`: DCT 感知哈希；`ThresholdDetector`: 亮度淡入淡出 | 成熟、参数明确、可解释 | 检测「镜头切换」≠「值得记笔记」；无转写语义 |
| [Azure AI Video Indexer](https://learn.microsoft.com/en-us/azure/azure-video-indexer/scene-shot-keyframe-detection-insight) | scene 按跨镜头颜色一致性；shot 检测突变/渐变；keyframe 选择考虑模糊度；shot type 按首帧人脸尺度/位置 | 工业化、结构化输出、质量意识 | 云服务；固定机位效果差；非开源 |
| [Katna](https://github.com/keplerlab/Katna) | LUV 绝对差、亮度、熵/对比度过滤；K-means 聚类直方图；Laplacian 方差选最清晰帧 | 通用关键帧质量不错 | 无语义；不针对讲座 |
| [SVLA toolkit](https://github.com/travisseng/svla-toolkit) | scene detection + Tesseract/Surya OCR + YOLOv8 slide detector + Sentence-BERT/FAISS 对齐 | 端到端讲座理解 | 依赖重（torch/ultralytics AGPL/Surya GPL/CUDA） |
| [Lecture2Note (ICME 2019)](https://ieeexplore.ieee.org/document/8784916/) | 从 slide 提取视觉实体，与描述性语音语义关联，布局优化成笔记 | 「视觉实体 + 语音对齐」经典思路 | 未找到官方开源代码，细节仅按摘要 |
| [MaViLS](https://github.com/andererka/MaViLS) | 转写 + OCR + 图像特征；动态规划对齐，jump_penalty 0.1；distiluse + swiftformer-xs | 已知 slides 时对齐强；抑制跳页 | 需要已知 PDF slides |
| [video2slides](https://github.com/binh234/video2slides) | OpenCV 背景减除/帧差；pHash/dHash/aHash 后处理；相似度阈值 90–100% | 简单幻灯片提取 | 全背景动画失败；约 0.5× 视频时长 |
| [video2ppt](https://github.com/Wangxs404/video2ppt) | 固定间隔 1s（推荐 5s） | 快、简单 | 纯固定间隔，冗余极高 |
| [lore-engine](https://github.com/Slydite/lore-engine) | pHash 8×8 DCT；temporal diversity；全局去重；`screenshots_per_minute=3`；`hash_similarity_threshold=5` | 视频→Markdown 笔记完整 | 密度仍是固定每分钟 3 张 |

## 论文/算法

| 工作 | 方法 | 对 BiliNote 的启示 |
|---|---|---|
| [FOCUS (arXiv 2510.27280)](https://arxiv.org/abs/2510.27280) | 视频切 clip 作为 bandit arms；经验均值 + Bernstein 置信半径；两阶段选 top arms，再在 arm 内选帧；示例 64 帧；报告 <2% 帧 | 「按不确定性/价值分配预算」，而非均匀 |
| [AKS (CVPR 2025)](https://arxiv.org/abs/2502.21271) | 关键帧选择 = 与 prompt 相关性 + 对视频覆盖度联合优化 | 目标函数可直接借用：相关性 + 覆盖 |
| [QCA](https://github.com/hktk07/QCA) | 分 temporal segments；每段贡献 = query-frame 匹配 + 内容偏差；归一化贡献决定预算；段内 anchor + 多样性迭代；`num_segments=16`、`total_keep=64` | 最接近「按密度分配」的公式 |
| [CLIP-It](https://arxiv.org/abs/2107.00650) | CLIP 图文嵌入 + 帧打分 transformer；帧分数→shot 分数；knapsack 选 shot | 「帧分数 → shot 分数 → 预算背包」结构 |
| [SLICE (IEEE Access 2026)](https://doi.org/10.1109/access.2026.3680314) | 参数-free；对数尺度平滑；按 Information Density Partitioning / Semantic Energy Mass 分配 token 预算 | 密度分配思想；精确公式未核实 |
| [K-frames (arXiv 2510.13891)](https://arxiv.org/abs/2510.13891) | 场景驱动，预测语义连贯 clip，支持 any-k | 「按 clip 选而非按帧」避免时间碎片化 |

## 他们使用的信号

| 信号 | 代表 | 做法 | BiliNote 可行性 |
|---|---|---|---|
| 视觉变化/镜头切换 | video-context、PySceneDetect、video-slide-extractor、Azure | ffmpeg scene 分数；HSV 加权差；block diff；颜色一致性 | Phase-1：dHash + 8×8 block diff |
| 稳定幻灯片/静态屏幕 | video-slide-extractor、video2slides、SVLA | 连续帧变化低于阈值形成 dwell；背景减除；slide detector | Phase-1：dwell + edge density |
| OCR/文字存在 | video-context、SVLA、MaViLS、Lecture2Note | Tesseract/Surya 读屏；OCR 变化；与转写关键词匹配 | Phase-2，只对候选帧跑 |
| 人脸/说话头 | Azure shot type、Katna | 人脸尺度/位置；face detection | Phase-2，降权 |
| 转写文本密度 | Lecture2Note、MaViLS | 语音与 slide 实体对齐 | Phase-1，BiliNote 已有 timestamped transcript |
| 音频能量/事件 | 通用 highlight | 语速、停顿、音乐/掌声 | 对讲座弱；Phase-2 可选 |
| 平台热图 | YouTube most replayed；B 站高能进度条 | YouTube innertube heatmap 100 点；B 站需另查 | Phase-2 低权重；B 站未验证 |
| LLM/VLM 打分 | FOCUS、AKS、QCA、CLIP-It | 相关性；frame scoring；bandit | Phase-1：LLM marker；Phase-2：VLM 复核 top 候选 |
| 预算分配 | SLICE、QCA、FOCUS、AKS | 按信息质量/密度比例；bandit；relevance+coverage | Phase-1：mass-proportional + 覆盖下限 |

**关键结论**：绝大多数「幻灯片提取」工具只看视觉变化；真正把「转写语义 + 视觉 + 预算」
合在一起的很少。BiliNote 已有转写和 LLM，这是做自适应选帧的天然优势。

## 推荐方案

### 原则

1. **视觉变化是候选，不是结论。** 变化大的帧可能是转场、模糊、镜头晃动；要结合稳定度、
   清晰度、文字密度。
2. **转写是「为什么值得截」的语义证据，不是「画面一定值得截」的证明。** 加视觉 gate，
   避免播客被文本密度带偏。
3. **预算按信息密度分配，但保留覆盖下限。** 长视频的安静但重要段落不能被完全跳过。
4. **LLM 不做算术。** LLM 只标「可见指代」和视觉类型；张数/间隔由确定性 planner 决定。
5. **确定性优先。** 无随机、稳定排序、可 dump 特征、可 golden test。

### 流水线

```
已有 6s 采样帧 + dHash
  ├─ 转写 segments（start/end/text）
  ├─ LLM Screenshot markers（±5s）
  └─ H2 小节边界
        ▼
[1] 候选生成：change / dwell / text / marker / coverage
[2] 特征与打分：V, L, T, M, Q → score
[3] 预算分配：覆盖保留 + 按 block mass 分配
[4] 选择与后处理：min gap、per-minute cap、dedup、max gap
[5] 最终截图：回到原视频按 timestamp 截取
```

### 候选生成

对每个已采样帧 `i`（时间 `t_i`，dHash `d_i`）：

- `h_i = Hamming(d_i, d_{i-1}) / 64`
- `dwell_i`：连续 `h < 0.04` 的帧数 × 采样间隔
- `E_i`：160×90 灰度图的平均梯度幅值（文字/UI/幻灯片代理）
- `Q_i`：Laplacian 方差（清晰度）+ 亮度惩罚

候选类型（并集，按帧去重）：

1. **change**：`h_i` 局部极大且 `h_i >= max(0.08, median(h) + 1.5*MAD(h))`
2. **dwell**：stable run（`h < 0.04` 连续 ≥2 个采样）取中间帧；run ≥20s 取「中间 + 边缘密度最高」
3. **text**：每 30s 窗口转写密度最高的 segment，吸附到最近采样帧（±3s）
4. **marker**：LLM marker 吸附到最近采样帧（±5s）
5. **coverage**：每个 5 分钟桶没有候选时，取该桶 `score` 最高的帧
6. 可选 **scene**：ffmpeg `scdet` / `select='gt(scene,0.25)'` 边界作为 change 补充

去重：同一帧合并 reasons；相邻候选时间差 < min_gap 时先保留分数高的。

### 特征与打分

按整段视频做稳健归一化：

```
n(x) = clip((x - P10) / (P90 - P10), 0, 1)
n_delta(x) = clip((x - P50) / (P95 - P50), 0, 1)
```

逐候选：

```
V_i = 0.70 * n_delta(h_i) + 0.30 * novelty_i
B_i = n(E_i)
S_i = clip(dwell_i / 20, 0, 1)
L_i = 0.65 * B_i + 0.35 * S_i
T_i = 0.35 * speech + 0.30 * keyword + 0.20 * entity + 0.15 * topic_novelty
M_i = 1 if LLM marker within ±5s else 0
Q_i = 0.70 * sharpness + 0.30 * brightness_penalty

score = 0.35*V + 0.20*L + 0.20*T + 0.15*M + 0.10*Q
```

- `novelty`：±120s 内最近帧的 Hamming 距离 / 16（奖励「和近两分钟不一样」）
- `speech`：±15s 窗口字符/秒 ÷ 全片中位数
- `keyword`：窗口内 top-5 TF-IDF 权重和 ÷ P90
- `entity`：数字、拉丁词、`%`、`=`、`()`、`→`、代码标点每 100 字符 ÷ P90
- `topic_novelty = 1 - Jaccard(tokens_i, tokens_{i-1})`
- `brightness_penalty`：过暗/过曝 0.5，正常 1

**视觉 gate**：

```
eligible = (V >= 0.15) or (L >= 0.35) or (M == 1)
```

只有 eligible 候选参与密度预算；不 eligible 的只能用于覆盖下限。

### 预算分配

```
B = clamp(round(duration_min * density), 3, max_count)
density ∈ {3, 6, 10} 张 / 10 分钟   # 精简 / 标准 / 详细
# 兼容旧设置：B = clamp(round(duration / screenshot_interval), 3, max_count)
B_cov = min(0.30*B, number_of_5min_buckets)
B_density = B - B_cov
```

每个 5 分钟桶至少 1 张（有 eligible 时）。把视频切成 30s block：

```
mass_b = sum(max(0, score_i - 0.35)) + 0.05
mass'_b = GaussianSmooth(mass_b, sigma=max(1, ln(N_b)))
q_b = B_density * mass'_b^1.2 / sum(mass'_b^1.2)
```

再做：

- 每 30s block 上限 `max(1, ceil(per_minute_cap/2))`，`per_minute_cap` 默认 2
- 每 H2 小节上限 `min(12, max(2, ceil(0.35*B)))`
- largest remainder 取整，保证总和 = B_density

**talking-head 自动降级**：

```
R = fraction(frames with L_i > 0.5 or V_i > 0.15)
if R < 0.15:
    B = min(B, max(4, ceil(duration_min / 10)))
```

2 小时播客最多约 12 张，而不是 80 张。

### 选择与后处理

```
rank_i = score_i + 0.10*clip(gap_i/120, 0, 1) - 0.25*dup_i
```

- 满足 `t_i - last_t >= min_gap`、block/H2 配额、总数 < B
- 平局按时间早者优先，保证确定性

后处理：

1. **max gap**：某段 >240s 没截图且总数 < max_count，插入该段最高分；否则替换最密
   block 中最低分。
2. **全局去重**：与已选帧 Hamming ≤2 且时间差 >300s，只有转写关键词 Jaccard <0.3
   （上下文不同）才保留。
3. **稳定帧吸附**：最终 timestamp 在 ±6s 内找 `h_next` 低、清晰度高的帧，优先转场后
   2–4s。
4. **质量兜底**：清晰度处于全片后 10% 且有更清晰候选时替换。

### LLM 角色

Phase-1 只做两件事，不让 LLM 数张数：

1. **语义 marker**：只在「转写明确指向可见内容」时输出，带类型与原因：
   `*Screenshot-[mm:ss]* type=slide|code|ui|formula|chart|demo why=...`
   提示词明确写「不要为了凑数量而标记；确定性 planner 决定最终张数」。
2. **小节视觉类型（可选）**：给每个 H2 输出 `slides|ui|talking_head|whiteboard|gameplay|other`，
   planner 微调权重（slides：L+0.10；ui：V+0.10；talking_head：L−0.15、触发降预算）。

Phase-2 可选：把 top 20 候选（时间戳 + 一句转写 + 缩略图）给 VLM 做 keep/drop JSON。

### 参数起始值

| 参数 | 默认 | 范围 | 作用 |
|---|---|---|---|
| `adaptive_screenshots` | true | bool | 总开关 |
| `screenshot_density` | standard（6/10min） | 3/6/10 | 总预算 |
| `screenshot_max_count` | 100 | 3–200 | 硬上限 |
| `screenshot_min_gap` | 15s | 5–60s | 最小间隔 |
| `coverage_floor` | 1/5min | 0–1 | 覆盖下限 |
| `max_gap` | 240s | 120–600s | 最大空档 |
| `per_minute_cap` | 2 | 1–4 | 密集段限流 |
| `per_section_cap` | `min(12, max(2, ceil(0.35B)))` | 2–12 | 每 H2 上限 |
| `gamma` | 1.2 | 1.0–1.6 | 预算集中度 |
| `dedup_hamming` | 4 | 2–8 | 近重复阈值 |
| `visual_gate` | V≥0.15 或 L≥0.35 或 M=1 | — | 防文本密度带偏 |
| `talking_head_ratio` | 0.15 | 0.1–0.3 | 自动降预算 |

### 边界情况

| 场景 | 信号 | 策略 |
|---|---|---|
| 播客/谈话头 | 低 V、低 L、高 S、高 T | 自动降预算到约 1/10min；优先强调句 + 镜头切换 |
| 教程/快速 UI | 高 V、高 L、低 S | 局部峰值 + min_gap + 每分钟 2 张；选变化后稳定帧 |
| 幻灯片讲座 | 转场高 V、高 L、高 S | 每个 stable run 1 张；全局 dHash 去重；H2 配额 |
| 白板 | 中 V、中 L、高 S | 选笔画结束后的稳定帧；长 dwell |
| 游戏/手持 | 高 V、高 L、低 S | 降视觉权重；靠转写/LLM；质量过滤 |
| 短文本/无转写 | 视觉 only | V+L+S；覆盖兜底；不足回退固定间隔 |
| 重复幻灯片 | dHash 距离小 | 全局去重；间隔 >5min 且上下文不同才保留 |
| <2min | 候选少 | 回退 1/20s，最多 6 张 |
| 采样失败/无帧 | 无视觉 | 回退旧版平均间隔 |

## 如何替换「平均间隔」

**低风险迁移**：

- Phase-1 保留 `screenshot_interval`，但只用于换算总预算：
  `B = clamp(round(duration / interval), 3, max_count)`。
- 截图不再按间隔均匀分布，由 planner 按密度分配。
- UI 把「截图平均间隔」改成「截图密度」三档（精简/标准/详细），显示派生值：
  「≈ 每 3.5 分钟 1 张；实际按内容密度分布，预计 36 张」。
- 保留「单篇截图上限」（3–200）。
- 新增「最小间隔」（5–60s，默认 15s）。
- 新增「覆盖下限」（每 5 分钟至少 1 张，默认开）。
- 高级：每小节上限（默认 8 或 35%）、每分钟上限（默认 2）。
- 保留「固定间隔（旧版）」开关。

## Phase-1 / Phase-2

### Phase-1（已实现，2026-10-28）

落地位置：`backend/app/utils/screenshot_planner.py` + `note._insert_screenshots`。

**2026-10-28 用户定版：不保留任何截图开关。** 表单里的「截图密度」、设置里的
「截图编排 / 截图预算间隔 / 单篇截图上限」全部移除；生成时一律走自适应编排，
内部基准为「每 90 秒换算 1 张、单篇上限 60 张」（`ADAPTIVE_INTERVAL` /
`ADAPTIVE_MAX_COUNT`），仅供预算换算与维护快照，用户不可调。planner 异常时
静默回退旧版间隔抽稀，保证生成不中断。

实现要点（相对上文方案的取舍）：

1. **候选只有 LLM 标记。** 帧驱动的「无标记区域补图」需要可靠的正文插入锚点，
   否则图片与相邻段落语义错位；留到 Phase-2 先做 VLM/OCR 或与 H2 时间范围对齐。
2. **采样帧语义**：VideoReader 对相邻完全相同帧做了 MD5 去重，保留帧多为画面
   变化点；`dwell` 同时看「到下一保留帧的间隔」和「下一帧的变化量」，兼容压缩
   噪声导致的伪变化帧。
3. **打分**使用候选集内 P10–P90 / P50–P95 稳健归一化，无帧时视觉项中性 0.5，
   只靠文本与预算，避免视频理解降级时整篇无图。
4. **预算**仍由 `时长 / screenshot_interval` 换算（间隔彻底降级为换算器）；
   30s 块 mass（`sum(max(0, score-0.35)) + 0.05`）经三角核平滑后按 `^1.2`
   最大余数分配；5 分钟桶覆盖下限占预算 30%；每小节上限改为「平均份额 ×3」
   （单小节笔记直接拿满），每分钟最多 2 张；配额取整/限流剩余预算按分数补齐。
5. **时间戳只在 ±2s 内吸附**到更稳定的采样帧（标记本身是模型读拼图给的，
   大幅位移会让图与正文错位）。
6. 任何异常（帧目录缺失、PIL/numpy 异常）都回退旧版 `select_markers`，不阻断生成。

### Phase-1（建议 1–2 周，无新重依赖）

1. 新增纯函数 `screenshot_planner`：候选、特征、打分、预算、选择、后处理。
2. 复用现有采样帧/dHash/缩略图；PIL/numpy 算 block diff、edge、sharpness；无 numpy 时
   降级 dHash-only。
3. 接入转写密度（语速、TF-IDF、数字/实体、主题新颖度）和 LLM marker（±5s）。
4. 配置：`adaptive_screenshots`、`screenshot_density`、`screenshot_min_gap`、
   `coverage_floor`、`per_section_cap`；`screenshot_interval` 仅作预算换算。
5. 提示词改造：LLM 只标「可见指代」，不按数量凑。
6. **Shadow 模式**：先不改变最终输出，只记录旧/新方案差异和特征 JSON；观察 1–2 周
   再切换默认。
7. Golden tests + 5 类真实视频 A/B（讲座/教程/播客/游戏/白板）。

### Phase-2（2–4 周，按需加依赖）

1. **OCR**：只对候选帧跑 Tesseract/PaddleOCR；文字面积、文字变化、与转写关键词匹配；
   OCR 文本相似度 >0.9 视为重复 slide。
2. **人脸/说话头**：OpenCV/MediaPipe/小 ONNX；face area ratio >0.15 且低 edge 降权 0.3。
3. **场景检测**：优先 ffmpeg `scdet`（无 Python 依赖）；更细再上 PySceneDetect。
4. **VLM 复核**：只对 top 2B 候选，问「是否含 slide/图表/代码/公式/UI」，0–3 分，按 dHash 缓存。
5. **Embedding/CLIP**：用现有 embedding/vector DB 或 CLIP，算「H2 标题/转写窗口 ↔ 候选帧」
   相似度，作为 relevance 特征。
6. **热图**：YouTube most replayed 仅作低权重 tie-breaker；B 站先验证接口，未验证前不接入。
7. **MaViLS 式对齐**：仅当用户提供课件/PDF 时启用。

## 未验证与风险

- **SLICE**：IEEE PDF 抓取失败，`σ=ln(N)` / Information Density Partitioning /
  Semantic Energy Mass 的精确公式未核实，只按摘要描述。
- **Lecture2Note**：未找到官方开源代码，方法细节只能按摘要理解。
- **B 站高能进度条**：实测 `videoshot` 的 `pvdata` 是预览雪碧图时间戳二进制 URL，不是
  热力图曲线；是否有其他公开接口未验证，不要作为 Phase-1/2 的确定信号。
- **YouTube most replayed**：innertube `heatmap` 为非公开接口，可能变化；只能低权重。
- **FOCUS/QCA/K-frames**：论文指标未独立复现。
- **风险**：dHash 对渐进变化（白板、视频播放）不敏感；edge density 会被复杂背景干扰；
  转写密度可能高估「讲得多但没画面」；LLM marker 可能偏向「说了」而非「值得看」。
  缓解：视觉 gate、覆盖下限、每段上限、Phase-2 OCR/人脸/VLM 复核。

## 主要来源

- video-context: https://github.com/LaloHao/video-context
- video-context how-it-works: https://github.com/LaloHao/video-context/blob/master/docs/how-it-works.md
- video-slide-extractor: https://github.com/itsyujay/video-slide-extractor
- PySceneDetect detectors: https://www.scenedetect.com/docs/latest/api/detectors.html
- Azure AI Video Indexer: https://learn.microsoft.com/en-us/azure/azure-video-indexer/scene-shot-keyframe-detection-insight
- Katna: https://github.com/keplerlab/Katna
- Lecture2Note: https://ieeexplore.ieee.org/document/8784916/
- SVLA toolkit: https://github.com/travisseng/svla-toolkit
- MaViLS: https://github.com/andererka/MaViLS
- video2slides: https://github.com/binh234/video2slides
- video2ppt: https://github.com/Wangxs404/video2ppt
- lore-engine: https://github.com/Slydite/lore-engine
- FOCUS: https://arxiv.org/abs/2510.27280
- AKS: https://arxiv.org/abs/2502.21271
- QCA: https://github.com/hktk07/QCA
- CLIP-It: https://arxiv.org/abs/2107.00650
- SLICE: https://doi.org/10.1109/access.2026.3680314
- K-frames: https://arxiv.org/abs/2510.13891
- YouTube heatmap issue: https://github.com/yt-dlp/yt-dlp/issues/3888
- Bilibili videoshot API: https://api.bilibili.com/x/player/videoshot?bvid=BV1GJ411x7h7&cid=137649199&index=1
- Awesome video-to-slides: https://github.com/itsyujay/awesome-video-to-slides
