"""受约束的 AI 编导：用户事实 → 两镜带货脚本。

红线：只使用用户给出的信息；不编造价格/折扣/销量/功效；字幕禁止数字（价格由系统贴片展示）；
禁止极限词与未经审核的功效宣称。LLM 失败显式抛错，绝不回退到模板冒充。
"""
import re

from pydantic import BaseModel, field_validator

from . import llm
from .llm import call_json

# 与 webapp.server.validate_facts 同源的宣称红线，覆盖 LLM 产出全文
CLAIM_PATTERN = re.compile(
    r'减脂|减肥|燃脂|瘦身|治疗|治愈|降血糖|抗癌|最强|最好|最低|第一|绝对|国家级|顶级|'
    r'提神|抗疲劳|根治|秒瘦|神效|万能|彻底|百分百|100%'
)
DIGITS = re.compile(r'[0-9０-９]')


class ShotScript(BaseModel):
    scene: str
    action: str
    mood: str = ''
    expression: str = ''
    product_entry: str = ''
    shot_size: str
    camera_move: str

    @field_validator('scene')
    @classmethod
    def _meaningful(cls, v: str) -> str:
        v = v.strip()
        if not 2 <= len(v) <= 100:
            raise ValueError('镜头描述须为 2–100 字的具体画面')
        return v

    @field_validator('action')
    @classmethod
    def _action_beats(cls, v: str) -> str:
        v = v.strip()
        if not 10 <= len(v) <= 130:
            raise ValueError('动作须为 10–130 字的分步动作链（①②③节拍）')
        return v

    @field_validator('mood', 'expression')
    @classmethod
    def _mood(cls, v: str) -> str:
        v = (v or '').strip()
        if len(v) > 60:
            raise ValueError('人物状态/面部表演须为 60 字内')
        return v

    @field_validator('product_entry')
    @classmethod
    def _entry(cls, v: str) -> str:
        v = (v or '').strip()
        if len(v) > 40:
            raise ValueError('商品进入方式须为 40 字内')
        return v

    @field_validator('shot_size', 'camera_move')
    @classmethod
    def _short(cls, v: str) -> str:
        v = v.strip()
        if not 2 <= len(v) <= 20:
            raise ValueError('景别/运镜须为 2–20 字')
        return v


class Creative(BaseModel):
    product_seen: str = ""
    hook: str
    cta: str
    points_shown: list[str] = []
    shots: list[ShotScript]

    @field_validator('points_shown')
    @classmethod
    def _points(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in (v or []) if s.strip()]
        if len(cleaned) > 3:
            raise ValueError('卖点证据链最多 3 条')
        return cleaned

    @field_validator('product_seen')
    @classmethod
    def _seen(cls, v: str) -> str:
        v = (v or '').strip()
        if len(v) > 60:
            raise ValueError('商品识别须为 60 字内的客观描述')
        return v

    @field_validator('hook')
    @classmethod
    def _hook(cls, v: str) -> str:
        v = v.strip()
        if not 4 <= len(v) <= 30:
            raise ValueError('钩子字幕须为 4–30 字')
        return v

    @field_validator('cta')
    @classmethod
    def _cta(cls, v: str) -> str:
        v = v.strip()
        if not 2 <= len(v) <= 24:
            raise ValueError('收尾字幕须为 2–24 字')
        return v

    @field_validator('shots')
    @classmethod
    def _two_shots(cls, v: list[ShotScript]) -> list[ShotScript]:
        if len(v) != 2:
            raise ValueError('必须恰好两个镜头')
        return v

    @field_validator('hook', 'cta', 'shots')
    @classmethod
    def _no_claims(cls, v):
        texts = v if isinstance(v, list) else [v]
        for shot in texts:
            text = shot if isinstance(shot, str) else shot.model_dump_json()
            if CLAIM_PATTERN.search(text):
                raise ValueError('产出包含未经审核的功效或绝对化宣称')
        return v

    @field_validator('hook', 'cta')
    @classmethod
    def _no_digits(cls, v: str) -> str:
        if DIGITS.search(v):
            raise ValueError('字幕禁止出现数字：价格由系统贴片展示，其他数字属于编造风险')
        return v


class ProductSeen(BaseModel):
    """视觉分析师的产出：只陈述看到的事实。"""
    product_seen: str

    @field_validator('product_seen')
    @classmethod
    def _factual(cls, v: str) -> str:
        v = (v or '').strip()
        if not 4 <= len(v) <= 60:
            raise ValueError('商品识别须为 4–60 字的客观描述')
        return v


class PlanResult(BaseModel):
    creative: Creative
    vision_used: bool
    note: str = ""


PERCEIVE_SYSTEM = """你是视觉商品分析师，为带货视频管线提供商品客观事实。
观察图片中的商品，只描述你直接看到的：品类、容器/包装形态、颜色、最显眼的可见标识文字、质地细节。
禁止推测品牌背景、成分、功效、价格、人群。
product_seen 必须在 40 字以内（超过会被系统拒收），删掉修饰语，只留关键事实。
JSON 字符串值内禁止出现英文双引号和换行（描述标识文字时直接写出文字本身，不加任何引号）。
只输出一个 JSON 对象，不要 markdown 代码块：{"product_seen": "..."}"""


DIRECTOR_SYSTEM = """你是一位拥有三十年经验的资深带货视频编导：从电视购物时代做到短视频时代，经手的带货广告超过三千条，深谙什么让观众停下拇指、什么促成下单。
你的职业信条：
- 前 3 秒定生死——钩子必须是"看得见的痛点场景"，不是形容词堆砌；
- 带货片的魂是"情绪转变写在脸上"——观众买的不只是商品，是"用完之后更好的那个自己"。第二镜必须给到特写级的面部表演指令：喝到/用到的瞬间，眼睛重新亮起来、嘴角上扬、眉毛舒展、呼吸变畅快，随后干劲十足地回到手里的活。转变没有"写在脸上"，这条片子就是废片；
- 动笔前先入戏：在心里把这两镜亲自演一遍——我累的时候真会这样吗？喝到一口好喝的冰饮，我的脸先有什么变化？写不出真实的表情就推倒重写；
- 商品是主角的"转机"，不是背景道具——它出现之前，观众已经在这个场景里等它；
- 卖点是画面的证据链：用户给出的每个卖点，必须转译成镜头拍得到的演示动作（如"冰感"→大口畅饮、杯壁水珠、呼出一口爽气；"方便"→单手可拿、随拿随走），并用字幕口语化点出；
- 动作必须写成"分步动作链"：action 用①②③标出时间顺序的 2–4 个动作节拍，每个节拍写明动作主体与身体部位。交接类动作（递东西、拿东西）必须按"递—接—退"三拍拆解：例：①同事的手从画外把杯子递到桌面 ②主人公右手伸出，握住杯身接过 ③同事的手撤出画面 ④主人公举杯到嘴边喝下。先接稳、再做下一个动作；严禁让递与接的两只手在画面里混同、变形或跳变；
- 消耗品的"真实消耗"是带货片可信度的命门：饮品类第二镜的饮用必须是画面主体动作——举杯到嘴边（或含住吸管）→真实喝下 1–2 大口、吞咽可见→放下，饮用占本镜至少 2 秒；绝不允许"拿起又放下、只摆放或轻扶杯子"冒充喝过。动作链里必须明确写"这一口喝掉杯中约三分之一，放下杯子时液面明显降至三分之二处，冰块随液面下移、杯壁挂上饮痕"；带盖吸管杯则写明吸管含入口中、杯身倾斜饮用、放下后吸管挂奶痕——量的减少必须看得见；
- 道具纪律：商品是画面中唯一的杯/瓶/容器——scene 和桌面上禁止出现"空杯子"或其他同类容器（空杯会被观众当成第二件商品或残次品），中性道具只保留键盘、文件、便签这类办公用品；
- 你深知广告法红线，三十年零违规，从不越线；
- 你的最终交付物是给本地视频生成模型（MiniMax H3）的镜头提示词素材：你写下的每个画面要求，都必须是镜头拍得到的具体动作、表情与状态，绝不写模型执行不了的抽象概念。

任务：基于商品真实信息，策划一条 10 秒竖屏带货视频的两镜脚本。
输出 JSON：{"hook": "...", "cta": "...", "points_shown": [...], "shots": [{"scene": "...", "action": "...", "mood": "...", "expression": "...", "product_entry": "...", "shot_size": "...", "camera_move": "..."}, ...]}
硬性要求：
1. 叙事弧结构（不可缺）：
   - 第一镜（0-5s）困境+转机：前 2–3 秒画面中【没有商品】，只有目标人群在真实场景里的生活化疲惫或枯燥状态；镜头后段商品才从画外进入（同事递到面前、外卖放到桌上、自己从包里拿出等），形成"转机"。
   - 第二镜（5-10s）转变：表情与状态的反差是重头戏——使用瞬间面部先亮（眼神聚焦睁大有神/嘴角明显上扬/满足地呼一口气），随后干劲十足地回到手中的事，商品正面特写清晰，结尾自然收向行动引导。表情幅度要"观众一眼看得出变化"，但仍属生活化范围。
2. expression 字段是特写级的面部表演指令（60 字内，如镜1"眉心微蹙、眼皮沉、目光涣散"；镜2"喝下一秒眼睛亮起来、嘴角上扬、长舒一口气"），两镜 expression 必须构成可拍摄的表情反差。
3. mood 写人物身体与情绪状态（例："疲惫：肩膀垮、动作迟缓" / "回神：腰背挺直、节奏恢复"）。
4. 卖点规则：用户给出了明确卖点 → 逐条转译成镜头拍得到的演示动作，points_shown 原文照填；用户【没有】给出卖点 → 你必须以三十年电商经验，从视觉分析师描述的商品形态与品类**推断 2–3 个"看得见的卖点"**（从可见特征推导：冰块→冰爽畅饮、单手可握→便携随拿、独立小包→随手卫生），points_shown 里每条加"推断:"前缀。推断卖点同样只能演示画面可见的事实，禁止编造成分、参数、功效、背书。
5. 真实感铁律：表演生活化、克制、纪录片质感——微表情与日常小动作（撑额头、眯眼、缓慢眨眼、后仰靠背、有气无力地敲键盘、揉肩）；【禁止】打哈欠、瞪眼、张大嘴、夸张肢体等戏剧化 AI 味表演。"眼里有光"靠眼神聚焦与面部松弛自然呈现，不靠瞪眼。
6. product_entry：第一镜写商品进入方式与时机（例："第3秒同事从画外递入"）；第二镜写"全程"。
7. hook 4-30 字，cta 2-24 字。只用用户信息与视觉分析师提供的商品事实，禁止编造价格/折扣/销量/功效/排行；禁止极限词（最/第一/绝对/国家级/顶级）；禁止提神、减脂等功效宣称；字幕禁止任何数字（价格走贴片）。
8. 商品形态必须与视觉分析师描述完全一致（杯装就是杯、盒装就是盒）；第二镜商品全程清晰。
9. scene 按用户信息推断真实场景；人物用泛化人群（"年轻白领""健身青年"）。
10. shot_size 中文景别（特写/近景/中景/全景），camera_move 中文运镜（固定/缓推/横移/手持轻晃）。
11. 镜头描述（scene）控制在 100 字内；action 必须是 ①②③ 分步动作链（130 字内），每拍写明是谁的哪个身体部位在动，交接动作按"递—接—退"三拍、先接稳再下一步。action 只写画面动作与可见状态，【禁止】写"字幕："等任何文字说明——字幕由系统统一烧录，写进动作会被模型画到画面里。
12. 只输出 JSON 对象。"""


def plan(sentence: str, price: str | None, image_data_url: str | None = None) -> PlanResult:
    """两段式编导流水线：
    ① 眼睛（Ling VL）：只看图，输出商品客观事实；
    ② 笔杆子（DeepSeek 资深编导）：基于事实+用户信息做创意策划。
    任一环节失败显式降级/报错，绝不静默编造。"""
    notes = []
    product_seen = ''
    vision_used = False

    if image_data_url and llm.vision_ready():
        try:
            seen = call_json('perceive', PERCEIVE_SYSTEM, '识别图中的商品。', ProductSeen,
                             fallback=False, image_data_url=image_data_url)
            product_seen = seen.product_seen
            vision_used = True
        except Exception:
            notes.append('看图识别失败，本次策划未使用图片信息（已明确标注）')
    elif image_data_url:
        notes.append('视觉模型未配置，本次策划未使用图片信息')

    user = f'商品真实信息：{sentence}'
    if product_seen:
        user += f'\n视觉分析师看到的商品形态（镜头必须与此一致，不得生成其他形态）：{product_seen}'
    if price:
        user += f'\n（用户提供了展示价格 {price} 元，将由系统以贴片展示，字幕中不要写价格）'

    creative = None
    try:
        creative = call_json('creative', DIRECTOR_SYSTEM, user, Creative, fallback=False)
    except llm.LLMError as e:
        # DeepSeek 不可用（如余额不足）：备用模型接管策划，明确标注不静默
        notes.append(f'DeepSeek 不可用（{str(e)[:80]}），本次策划由备用视觉模型接管，文案质量可能略降')
        creative = call_json('creative', DIRECTOR_SYSTEM, user, Creative,
                             fallback=False, use_vision=True)
    creative.product_seen = product_seen
    return PlanResult(creative=creative, vision_used=vision_used, note='；'.join(notes))


CAPTION_NOTE = re.compile(r'[（(]\s*字幕\s*[:：][^）)]*[）)]|【\s*字幕\s*[:：][^】]*】')


def shot_prompt(script: ShotScript | dict, continuation: bool = False, person_ref: bool = False) -> str:
    if isinstance(script, dict):
        script = ShotScript.model_validate(script)
    # 编导偶尔把"（字幕：…）"备注混进动作链；字幕由系统烧录，混入会被模型画进画面。
    action = CAPTION_NOTE.sub('', script.action).strip()
    mood = f'人物状态：{script.mood}。' if script.mood else ''
    expression = f'面部表演（特写级）：{script.expression}。' if script.expression else ''
    realness = (
        '纪录片式生活化表演：表情克制自然、动作是日常小动作，'
        '禁止打哈欠、瞪眼、张大嘴等夸张戏剧化表演；真实皮肤质感，画面轻微手持感。'
        '道具纪律：商品是画面中唯一的杯瓶容器，不得出现其他杯子、空杯或饮料。'
    )
    if continuation:
        head = ('<Picture 1> 是商品参考图（锁定商品外观与颜色）。'
                '另附参考视频片段，它仅用于锁定上一镜结束时的人物、服饰与场景状态：'
                '本镜从该状态之后继续，人物、服饰、发型、场景与商品外观严格保持一致。'
                '【硬性限制】参考视频里出现过的"递入/接取"动作在本镜禁止再次发生——'
                '本镜自始至终没有任何手从画外进入，商品从第 0 帧起已在主人公手中或桌面，直接从使用动作开始。')
        entry = '商品从本镜第 0 帧起就在画面中，随动作保持正面清晰。'
    else:
        head = '<Picture 1> 是商品参考图。'
        if person_ref:
            head += ('<Picture 2> 是出镜人物的形象参考照：画面主人公的五官容貌、发型与体态'
                     '必须与 <Picture 2> 中的人物一致（自然相似即可，服饰按场景设定）。')
        entry = (f'商品出现时机：{script.product_entry}；进入之前画面中不得出现该商品。'
                 if script.product_entry and script.product_entry != '全程'
                 else '商品全程在画面中且正面清晰可辨。')
    liquid = ''
    if any(k in action for k in ('喝', '饮', '液面')):
        liquid = ('【饮用动作硬要求——本镜的主体事件】主人公必须真实喝下，完整拍出'
                  '"举杯到嘴边（或含住吸管）→饮用 1–2 大口、吞咽可见→放下"的连续过程，饮用至少持续 2 秒。'
                  '【假喝判定】只把杯子拿起又放回、轻扶杯身、贴脸摆拍而没有任何吞咽，都算不合格。'
                  '【物理连续性硬约束】喝掉约三分之一：放下杯子之后，杯中液面必须明显低于拿起饮用之前'
                  '（约降至三分之二处），冰块随液面下移、杯壁挂饮痕；透明带盖吸管杯则透过杯身可见液体变少、'
                  '吸管上挂奶痕。放下时杯子正对镜头拍到下降后的液面，禁止放下后液面与喝之前相同。')
    return (
        f'{head}{script.scene}。{action}。{liquid}'
        f'{mood}{expression}{entry}'
        f'{realness}{script.shot_size}，镜头{script.camera_move}。'
        f'只展示参考中的同一个商品，保持其真实外观、颜色与包装不变，不新增其他商品或包装文字。'
        f'写实电商商品视频质感，柔和自然光，竖屏，单个连续镜头，画面中不出现任何字幕、贴片或文字。'
    )
