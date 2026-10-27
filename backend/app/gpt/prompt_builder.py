from app.gpt.prompt import render_base_prompt

note_formats = [
    {'label': '目录', 'value': 'toc'},
    {'label': '原片跳转', 'value': 'link'},
    {'label': '原片截图', 'value': 'screenshot'},
    {'label': 'AI总结', 'value': 'summary'}
]

note_styles = [
    {'label': '精简', 'value': 'minimal'},
    {'label': '详细', 'value': 'detailed'},
    {'label': '学术', 'value': 'academic'},
    {"label": '教程',"value": 'tutorial', },
    {'label': '小红书', 'value': 'xiaohongshu'},
    {'label': '生活向', 'value': 'life_journal'},
    {'label': '任务导向', 'value': 'task_oriented'},
    {'label': '商业风格', 'value': 'business'},
    {'label': '会议纪要', 'value': 'meeting_minutes'}
]


# 生成 BASE_PROMPT 函数
def generate_base_prompt(
    title,
    segment_text,
    tags,
    _format=None,
    style=None,
    extras=None,
    screenshot_quota=None,
):
    # 生成 Base Prompt 开头部分（render_base_prompt 对模板内花括号免疫，
    # LaTeX 示例再添多少 `{...}` 都不会触发 str.format 崩溃，见 prompt.py 注释）
    prompt = render_base_prompt(
        video_title=title,
        segment_text=segment_text,
        tags=tags
    )

    # 添加用户选择的格式
    if _format:
        prompt += "\n" + "\n".join(
            [get_format_function(f, screenshot_quota=screenshot_quota) for f in _format]
        )

    # 根据用户选择的笔记风格添加描述（未知风格返回空串时不追加兜底，
    # 避免无风格任务的 prompt 凭空多一句）
    if style:
        style_text = get_style_format(style)
        if style_text:
            prompt += "\n" + style_text + "\n" + STYLE_FORMULA_GUARD

    # 添加额外内容
    if extras:
        prompt += f"\n{extras}"
    return prompt


# 获取格式函数
def get_format_function(format_type, screenshot_quota=None):
    format_map = {
        'toc': get_toc_format,
        'link': get_link_format,
        'screenshot': lambda: get_screenshot_format(screenshot_quota),
        'summary': get_summary_format
    }
    return format_map.get(format_type, lambda: '')()


# 风格描述的处理
def get_style_format(style):
    style_map = {
        'minimal': '1. **精简信息**: 仅记录最重要的内容，简洁明了。',
        'detailed': '2. **详细记录**: 包含完整的内容和每个部分的详细讨论。需要尽可能多的记录视频内容，最好详细的笔记',
        'academic': '3. **学术风格**: 适合学术报告，正式且结构化。',
        'xiaohongshu': '''4. **小红书风格**: 
### 擅长使用下面的爆款关键词：
好用到哭，大数据，教科书般，小白必看，宝藏，绝绝子神器，都给我冲,划重点，笑不活了，YYDS，秘方，我不允许，压箱底，建议收藏，停止摆烂，上天在提醒你，挑战全网，手把手，揭秘，普通女生，沉浸式，有手就能做吹爆，好用哭了，搞钱必看，狠狠搞钱，打工人，吐血整理，家人们，隐藏，高级感，治愈，破防了，万万没想到，爆款，永远可以相信被夸爆手残党必备，正确姿势

### 采用二极管标题法创作标题：
- 正面刺激法:产品或方法+只需1秒 (短期)+便可开挂（逆天效果）
- 负面刺激法:你不XXX+绝对会后悔 (天大损失) +(紧迫感)
利用人们厌恶损失和负面偏误的心理

### 写作技巧
1. 使用惊叹号、省略号等标点符号增强表达力，营造紧迫感和惊喜感。
2. **使用emoji表情符号，来增加文字的活力**
3. 采用具有挑战性和悬念的表述，引发读、“无敌者好奇心，例如“暴涨词汇量”了”、“拒绝焦虑”等
4. 利用正面刺激和负面激，诱发读者的本能需求和动物基本驱动力，如“离离原上谱”、“你不知道的项目其实很赚”等
5. 融入热点话题和实用工具，提高文章的实用性和时效性，如“2023年必知”、“chatGPT狂飙进行时”等
6. 描述具体的成果和效果，强调标题中的关键词，使其更具吸引力，例如“英语底子再差，搞清这些语法你也能拿130+”
7. 使用吸引人的标题：''',

        'life_journal': '5. **生活向**: 记录个人生活感悟，情感化表达。',
        'task_oriented': '6. **任务导向**: 强调任务、目标，适合工作和待办事项。',
        'business': '7. **商业风格**: 适合商业报告、会议纪要，正式且精准。',
        'meeting_minutes': '8. **会议纪要**: 适合商业报告、会议纪要，正式且精准。',
        "tutorial":"9.**教程笔记**:尽可能详细的记录教程,特别是关键点和一些重要的结论步骤"
    }
    return style_map.get(style, '')


# 风格公式兜底：无论切到哪种笔记风格，都追加一句公式定界符提醒。
# 背景：公式规则只在 BASE_PROMPT 第 5 条出现一次，风格文案越长（如小红书的爆款词表）
# 越容易把它淹没，模型就退回习惯性的 \(...\) / \[...\] 写法，前端直接显示原文
# （2026-10-01 用户实拍：LTspice 笔记公式全是原文）。style 追加在 base 之后，
# 这句兜底离模型最近，且只做提醒不涉及花括号，无 .format 风险。
STYLE_FORMULA_GUARD = (
    '另：视频中的数学公式必须保留为 LaTeX，且只用两种定界符——'
    '行内用单个美元符（如 $f_0=\\frac{1}{2\\pi\\sqrt{LC}}$），'
    '独立成行用双美元符（如 $$V_{out}\\approx DV_{in}$$）；'
    '禁止 \\(...\\)、\\[...\\]、```latex 代码块或其它写法。'
)


# 格式化输出内容
def get_toc_format():
    return '''
    9. **目录**: 自动生成一个基于 `##` 级标题的目录。不需要插入原片跳转
    '''


def get_link_format():
    return '''
    10. **原片跳转**: 为每个主要章节添加时间戳，使用格式 `*Content-[mm:ss]`。 
    重要：**始终**在章节标题前加上 `*Content` 前缀，例如：`AI 的发展史 *Content-[01:23]`。一定是标题在前 插入标记在后
    '''


def get_screenshot_format(quota=None):
    """原片截图格式说明（2026-10-27 重写）。

    旧版只有「认真分析每个图片…插入最合适的内容」——模型把网格里每一格都
    当成必须交代的时间点，实测单篇最多 790 张截图（图片语法占正文 64%）。
    新版给出明确的取舍标准与数量配额；后端 screenshot_policy 还会做确定性
    兜底，模型不配合也不会失控。
    """
    quota_line = ""
    if quota:
        quota_line = (
            f"\n- **数量配额**：本段最多 {int(quota)} 张截图。宁可少截，也不要为了凑数"
            "把口播画面、重复画面塞进来；没有值得截的画面就一张都不要插。"
        )
    return f'''
11. **原片截图**：你收到的图片是网格，每格是视频里一个时间点（左上角有 mm:ss）。
网格只是候选画面，**不是每格都要截**。只有画面本身承载了转写没讲到的信息时才插入：
- **值得截**：PPT/板书、代码、图表曲线、参数表、软件界面与操作步骤、实物细节、演示结果。
- **不要截**：说话人头像/纯口播画面、空镜、转场、纯字幕卡、与上下文无关的画面、
  同一画面换个角度或停留几秒的重复帧。
- 同一张画面只截一次；前后几格是同一场景时，只保留信息最全的一张。
- 每个二级章节通常 1~2 张；只有逐步演示类内容（教程/操作）才适当多截。{quota_line}
- 格式必须逐字遵守，否则系统无法解析：标记单独一行，紧接下一行写一句不超过 20 字的图注。
  示例：
  `*Screenshot-[03:24]*`
  `图注：三相电流的相位关系`

    '''


def get_summary_format():
    return '''
    12. **AI总结**: 在笔记末尾加入简短的AI生成总结,并且二级标题 就是 AI 总结 例如 ## AI 总结。
    '''
