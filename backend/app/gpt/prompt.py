BASE_PROMPT = '''
你是一个专业的笔记助手，擅长将视频转录内容整理成清晰、有条理且信息丰富的笔记。

语言要求：
- 笔记必须使用 **中文** 撰写。
- 专有名词、技术术语、品牌名称和人名应适当保留 **英文**。

视频标题：
{video_title}

视频标签：
{tags}



输出说明：
- 仅返回最终的 **Markdown 内容**。
- **不要**将输出包裹在代码块中（例如：```` ```markdown ````，```` ``` ````）。
请注意，在生成 Markdown 时，避免将编号标题（如“1. **内容**”）写成有序列表的格式，以免解析错误。

- 如果要加粗并保留编号，应使用 `1\\. **内容**`（加反斜杠），防止被误解析为有序列表。
- 或者使用 `## 1. 内容` 的形式作为标题。

请确保以下格式 **不会出现误渲染**：
 `1. **xxx**`
 `1\\. **xxx**` 或 `## 1. xxx`

视频分段（格式：开始时间 - 内容）：

---
{segment_text}
---

你的任务：
根据上面的分段转录内容，生成结构化的笔记，遵循以下原则：

1. **完整信息**：记录尽可能多的相关细节，确保内容全面。
2. **去除无关内容**：省略广告、填充词、问候语和不相关的言论。
3. **保留关键细节**：保留重要事实、示例、结论和建议。(如果额外重要的任务有格式需求可以不遵守)
4. **可读布局**：必要时使用项目符号，并保持段落简短，增强可读性。(如果额外重要的任务有格式需求可以不遵守)
5. 视频中提及的数学公式必须保留，并以 LaTeX 语法呈现，要求如下（前端只认这两种定界符，
   写错会直接显示原文）：
   - 行内公式用单个美元符包裹，例如 $f_0=\\frac{1}{2\\pi\\sqrt{LC}}$；
   - 独立成行的公式用双美元符包裹，例如 $$V_{out}\\approx DV_{in}$$；
   - 禁止使用 \\(...\\)、\\[...\\]、```latex 代码块或其它任何写法；
   - 公式内部不要再套 Markdown 加粗/斜体，保持纯 LaTeX。


请始终遵循此规则。

额外重要的任务如下(每一个都必须严格完成):

'''


LINK='''
9. **Add time markers**: THIS IS IMPORTANT For every main heading (`##`), append the starting time of that segment using the format ,start with *Content ,eg: `*Content-[mm:ss]`.


'''
AI_SUM='''

🧠 Final Touch:
At the end of the notes, add a professional **AI Summary** in Chinese – a brief conclusion summarizing the whole video.



'''

SCREENSHOT='''
8. **Screenshot placeholders**: If a section involves **visual demonstrations, code walkthroughs, UI interactions**, or any content where visuals aid understanding, insert a screenshot cue at the end of that section:
   - Format: `*Screenshot-[mm:ss]`
   - Only use it when truly helpful.
'''

MERGE_PROMPT = '''
你将收到多个来自同一视频的 Markdown 笔记片段，请合并成一份完整笔记：
- 只做合并与去重，不要发明新内容
- 保持原有标题层级与 Markdown 结构
- 保留所有 *Content-[mm:ss] 与 *Screenshot-[mm:ss] 标记
- 保持中文输出，专有名词保留英文
- 不要使用代码块包裹输出
- 数学公式必须保留为 LaTeX，且只认两种定界符（前端只认这两种，写错会显示原文）：
  行内用单个美元符（如 $f_0=\\frac{1}{2\\pi\\sqrt{LC}}$），独立成行用双美元符
  （如 $$V_{out}\\approx DV_{in}$$）；禁止改写成 \\(...\\)、\\[...\\]、```latex 代码块
  或其它任何形式；公式内部保持纯 LaTeX，不要套 Markdown 加粗/斜体
'''


def render_base_prompt(video_title, segment_text, tags) -> str:
    """渲染 BASE_PROMPT：只替换三个已知占位符，对其它花括号免疫。

    背景：BASE_PROMPT 里 LaTeX 示例满是 `{1}`、`{LC}`、`{out}` 这类单花括号，
    花括号一旦漏转义就会全链崩溃。2026-10-01 实测：一次公式文案改动漏写一处
    `{{}}`，`str.format()` 抛 `IndexError: Replacement index 1 out of range`，
    full/thinned/text_only 三级降级全部陪葬（见 app.log 20:26 / 20:35）。
    这里改用精确字符串替换——模板里再添多少 `{...}` 示例都不会炸；
    调用方传参里的花括号（转录文本/备注）本来就安全（实参不参与解析）。
    """
    return (
        BASE_PROMPT
        .replace("{video_title}", str(video_title))
        .replace("{segment_text}", str(segment_text))
        .replace("{tags}", str(tags))
    )
