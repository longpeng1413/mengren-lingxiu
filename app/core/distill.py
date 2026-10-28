"""老师蒸馏（猛人灵修版）：从媒体库里一位灵修 / 心灵成长老师的课程和文字资料，自动生成老师工作台要的三份文件。

和猛人学习的蒸馏（按交易课设计：观点 + 条件 + 类型）不同，灵修老师最重要的是：原话、比喻和故事、
怎么回应学员的困惑（问答）、语气和节奏。所以逐节提取多了三类条目，合并和画像也按灵修老师的样子写。

来源可以是：
- 有字幕的音视频（字幕文件和猛人学习共用，在猛人学习里识别好的课直接能用），出处写「S03 12:05」；
- 文字资料：文章、对话记录、书（媒体库里的 txt / md / docx / pdf / epub…，用 book / ebook 读），
  按段落编号（每个非空行算一段；PDF 按页），出处写「S03 第12段」（PDF「S03 第5页」）。

流程：
1. 逐节提取观点库：每份资料单独调一次 AI（太长分段），AI 只返回结构化的条目
   （观点 / 修法 / 定义、原话、比喻和故事、问答、老师自述、术语、可以实践的、存疑）；编号、出处格式、表格由程序来写，
   出处超出资料长度的标「待核对」。做完一份存一份（`mentors/老师id/distill.json`），中途断了下次接着做。
2. 合并成课程蒸馏：观点库不大就整份交给 AI；太大先按顺序分组写小结，再合并。AI 写的编号在观点库里找不到的，程序在文末标出来。
3. 老师画像：第一节「给 AI 的规则」由程序固定写（对话时 AI 严格按它来），后面几节 AI 依据课程蒸馏、原话、比喻和故事、问答写。
4. 写进 Obsidian 库的 `灵修老师/老师名/`（用户 2026-10-28 定：灵修老师放另一个库）；覆盖之前先备份到
   `%APPDATA%\\猛人灵修\\backups\\蒸馏\\老师名-时间\\`（每位老师留 20 份）。上次写完之后在 Obsidian 里改过的，开始前先问用户。

以后老师又有新课：加进来再蒸馏，只提取新资料和改过的资料，再重新合并。编号 S01… 一旦分配就不变
（对话、对话精选里的出处靠它）。
"""
import hashlib
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import config, doc as docs, llm, mentor, tasks
from .log import log

MENTOR_ROOT = '灵修老师'  # 蒸馏结果写在 Obsidian 库的「灵修老师/老师名/」
FILES = {'atoms': '01、观点库（逐节提取）.md', 'distill': '02、课程蒸馏.md', 'persona': '03、老师画像（对话用）.md'}
CHUNK_CHARS = 50000      # 一份资料超过这么多字，分几段提取
MERGE_LIMIT = 90000      # 观点库不超过这么多字，整份交给 AI 合并；超过就先分组小结
GROUP_CHARS = 60000      # 分组小结时每组大约多少字
PERSONA_ATOMS = 60000    # 写老师画像时，观点不超过这么多字就整份附上，超过只附课程蒸馏里引用到的
PERSONA_LIST = 20000     # 写老师画像时，原话、比喻和故事、问答、自述每一类最多附多少字
RETRIES = 3
RETRY_WAIT = (10, 60, 180)
BACKUP_KEEP = 20
PROMPT_VER = 2           # 提示词改了就加 1，合并、画像会重做（逐节提取不受影响）。2：灵修版
KINDS = ('观点', '修法', '定义')
KIND_ALIAS = {'方法': '修法', '练习': '修法', '修持': '修法', '做法': '修法'}
# 编号的字母：观点没有字母（S03-02），A 自述，Y 原话（语录），B 比喻和故事，W 问答
PREFIX = {'自述': 'A', '原话': 'Y', '比喻和故事': 'B', '问答': 'W'}

_running = {}            # 老师id → 正在蒸馏的 job
_lock = threading.Lock()


class DistillError(Exception):
    pass


# ------------------------------------------------------------- 提示词 ----
EXTRACT_SYSTEM = """你在帮用户做「灵修老师蒸馏」的第一步：把一位灵修 / 心灵成长老师的一份资料（一节课的字幕，或一篇文章、一段对话记录），逐条拆成「观点库」。
用户以后会让 AI 依据这份观点库扮演这位老师，和他探讨人生、修行、情绪和关系里的困惑，所以忠实是第一位的；
而且对灵修老师来说，他的原话、比喻和故事、他怎么回应一个人的困惑，比道理本身更重要，都要尽量留下来。

规则：
1. 只写老师在这份资料里讲过的。不补充、不评价，不替老师总结出他没说的结论，不用你自己的灵性知识去补。老师没讲明的前提写「未讲明」。
2. 每条都要有出处，必须是资料里真实出现的位置，不许编：
   - 字幕资料：写原文前面方括号里的时间，mm:ss 或 mm:ss–mm:ss（超过一小时写 h:mm:ss）；
   - 文字资料（文章、对话记录、书）：写原文前面方括号里的段落号，「第12段」或「第12–14段」（PDF 是「第5页」）。
3. 尽量贴近老师原意；关键的话用「」引用原话（可以去掉口头禅，不改意思）。
4. 「观点」一条只讲一件事。「类型」只有三种：观点（看法、主张、对生命和修行的理解）、修法（具体的练习、做法、观察和觉察的方法，写清怎么做）、定义（老师对某个说法、概念的解释）。
5. 「原话」：挑最能代表他的、以后模仿他说话要用的句子，一字照录（只去掉口头禅和明显的识别错字），写清这句话在讲什么。一般 5～15 句。
6. 「比喻和故事」：老师用的比喻、举的例子、讲的故事（包括他自己的、学员的、经典里的），把内容写完整（以后要能复述出来），再写他用它来讲什么。
7. 「问答」：有人提问、倾诉，老师回应的地方（课上学员提问、连麦、答疑，对话记录里的一问一答）。写清学员问了什么（他的处境和困惑）、老师怎么回应（尽量用老师原话，保留回应的步骤：先说了什么、再说了什么）、回应方式（例如：先接纳情绪 / 先反问 / 直接点破 / 讲故事 / 带着做一个练习 / 把问题拉回当下或身体，语气是温和、严厉还是幽默）。对话记录类资料，问答是最重要的部分，不要漏。
8. 只有老师用了绝对化的说法（一定、绝对、唯一、永远、根本不可能……）时，「说得很满」才填 true；一般的强调不算。
   「条件」只写老师自己讲到的前提、适用的人和阶段、例外；没有就填 —。
9. 老师讲自己的（经历、修行经历、师承、立场、风格、著作、课程安排、说以后要讲什么），放进「自述」，一律保留，不评价。
10. 口述前后矛盾、字幕明显识别错、意思拿不准的，放进「存疑」，写清楚哪个位置、哪里不对；不要擅自改。
11. 不要漏：主要内容都要覆盖到。观点一般 15～40 条，内容多的可以更多；没有问答、比喻的资料，对应的列表就空着。
12. 「课程重点」是别人整理的，只能帮你看结构，内容以原文为准。

只返回 JSON，格式：
{"说明": "一两句话：这份资料讲什么、是什么形式（讲课、答疑、文章、对话记录）",
 "观点": [{"内容": "…", "条件": "老师讲到的前提或适用范围；没有写 —；没讲明写 未讲明", "出处": "01:31–02:20", "类型": "观点", "说得很满": false}],
 "原话": [{"原话": "一字照录的原话", "在讲什么": "…", "出处": "05:10"}],
 "比喻和故事": [{"内容": "比喻或故事的完整内容", "用来讲什么": "…", "出处": "12:00–13:40"}],
 "问答": [{"问": "学员问了什么（处境和困惑）", "答": "老师怎么回应，尽量原话，保留步骤", "回应方式": "先接纳，再反问……", "出处": "20:15–24:00"}],
 "自述": [{"内容": "…", "出处": "00:02–00:19"}],
 "术语": [{"术语": "…", "意思": "老师的意思（按他的说法）", "出处": "03:35"}],
 "实践": [{"内容": "可以拿去做的练习或觉察", "依据": [3, 5]}],
 "存疑": ["写位置和原因，例如：04:46 这句识别得不通顺……"]}
其中「依据」是「观点」列表里的第几条（从 1 开始）。文字资料把上面的时间换成「第12段」这样的段落号。"""

GROUP_SYSTEM = """你在帮用户做「灵修老师蒸馏」。下面是这位老师一部分资料的观点库（逐节提取，每条有编号：S03-02 是观点，S03-Y1 是原话，S03-B1 是比喻和故事，S03-W1 是问答，S03-A1 是老师自述）。
请把这几份资料合并写成一份小结，后面会和其他几组的小结再合并成整体的蒸馏。

规则：只用观点库里的内容，不补充、不评价；每一条结论后面标观点库编号，例如（S03-02、S05-W1），编号必须是观点库里有的；
老师说得很满的保留 ⚠️；老师自述保留，标「老师自述：」；前后说法不一致的两边都写；老师没讲明的写「（未讲明）」；
有代表性的原话照录、用「」括起来。

用 Markdown，按这几个小标题写：
### 这几份资料的主线
### 核心观点（合并后）
### 修法
### 反复强调的
### 常用比喻和故事
### 他怎么回应学员（问答里看得出的方式）
### 前后不一致或有张力的
### 没讲到的（老师说以后讲、或明确不讲）
### 可以实践的"""

MERGE_SYSTEM = """你在帮用户做「灵修老师蒸馏」的第二步：把逐节提取的观点库，合并成这位老师整体的「课程蒸馏」。
用户会把它和老师画像一起交给 AI，让 AI 扮演这位老师和他对话；他也会自己读它，弄懂老师的整个教导。

规则：
1. 只用给你的资料里的内容。不补充、不评价，不写老师没讲的，不用你自己的灵性知识去补；老师没讲明的写「（未讲明）」。
2. 每一条都标观点库编号，例如（S03-02、S05-Y1、S06-W2）。编号必须是资料里有的，不许编。
3. 按老师自己的逻辑组织，不按课次；可以合并重复、调整顺序。
4. 老师说得很满的，保留 ⚠️ 提醒。老师自述（经历、修行经历、立场、课程安排）保留，标「老师自述：」。
5. 有代表性的原话照录，用「」括起来，标编号；不要把老师的话改写成书面腔。
6. 前后说法不一致的，两边都写出来，放进第七节，交给用户判断，不替老师圆。
7. 写的是「老师怎么说」，不是正确答案，也不是用户的结论。

用 Markdown，从下面第一个小标题开始写（不要写总标题，不要写开场白），小标题照抄：
## 一、一句话
（一两句话说清这位老师教的是什么；再写他说适合谁、不适合谁，有原话依据的才写）
## 二、核心教导体系（按他自己的逻辑，不按课次）
（用 ```text 代码块画一张从上到下的结构图，每一层标编号）
## 三、核心观点（合并后）
（按主题分成几组，用 ### A. … 这样的小标题；每条一句话加粗要点，后面标编号）
## 四、修法（具体怎么练）
（每个练习写清：做什么、怎么做、什么时候做、要注意什么，标编号）
## 五、他反复强调的
（在几份资料里都讲过的，写清在哪几份讲过）
## 六、常用比喻和故事
（用表格：比喻或故事 | 用来讲什么 | 编号）
## 七、前后说法不一致或有张力的地方（待你判断）
## 八、没讲到的（他说以后讲、或明确不讲）
## 九、可以拿去实践的（汇总）"""

PERSONA_SYSTEM = """你在帮用户做「灵修老师蒸馏」的第三步：写这位灵修 / 心灵成长老师的「老师画像」。
用户会把画像交给 AI，让 AI 扮演这位老师（数字分身）和他交流；扮演规则已经写好了（第一节），你写后面几节。
灵修老师的分身好不好，关键不在道理，而在像不像他：他怎么接住一个人、怎么说话、用什么比喻和故事。所以第四、六、七、八节要写得最具体。

规则：
1. 只依据给你的资料（课程蒸馏、原话、比喻和故事、问答、老师自述、术语、观点库），不补充、不评价，不写资料里没有的经历和观点。
2. 每条后面标出处：观点库编号写成［S02-21］［S02-Y3］［S02-W1］，音视频时间写成［S02 19:54］，文字资料写成［S03 第12段］，必须是资料里有的。
3. 老师的原话用「」引用，照录。
4. 写具体、能用：AI 要靠它学会这位老师怎么想、怎么回应人、怎么说，所以多写他回应的顺序和分寸、常用的比喻和故事、口头禅和说话节奏。

用 Markdown，从下面第一个小标题开始写（不要写总标题、不要写第一节），小标题照抄：
## 二、他是谁
（身份、修行和教学的背景、教什么、资料由哪几部分组成；只写他自己说过的）
## 三、他的核心信念（回答问题时的底层立场）
（编号列表，8～12 条）
## 四、他怎么回应一个人的困惑（模仿这个顺序和分寸）
（从问答里总结：先接纳情绪还是先点破，会不会反问、怎么反问，什么时候讲故事、什么时候带着做练习，怎么把话题引回当下、身体或觉察，对痛苦的人和对钻牛角尖的人有什么不同，语气是温和、严厉还是幽默；每条带一两个问答编号做例子）
## 五、他怎么思考一个问题
## 六、语录（最能代表他的原话）
（10～20 句，照录，每句标出处）
## 七、他常用的比喻和故事（回答时可以用，标出处）
（用表格：比喻或故事 | 用来讲什么 | 出处；故事写成能直接复述的一两句）
## 八、说话风格
（语气、节奏、句子长短、口头禅、常用的词、怎么称呼学员、会不会用玩笑和反问，各举原话为例）
## 九、他反对什么、常提醒什么
## 十、他没讲、或明确不碰的（回答时要说明）
## 十一、回答示范
（举 3 个用户可能问的问题，按第一节的规则写老师会怎么答，带出处、用他的语气：
 其中一个是情绪很痛苦的提问（例如「我很痛苦，觉得活着没意思」），示范先关心、接住他，按第一节第 7 条建议找身边的人和专业心理援助；
 一个是资料里没讲的问题，示范怎么标【AI 推测：…】）"""


def persona_rules(name, n, first='S01', last='S01'):
    """老师画像第一节：给 AI 的规则（程序固定写，对话时 AI 严格按它来）。"""
    return f"""## 一、你的身份和规则（给 AI）

你现在是灵修 / 心灵成长老师「**{name}**」的数字分身，是 AI 根据他的资料模拟的，不是他本人。你的全部知识来自他的 {n} 份资料（{first}～{last}：课程、文章、对话记录，见观点库）。用户想向你请教、倾诉困惑、和你探讨修行，你要**忠实地用他的教导、思路、语气和回应方式**来回答。

### 必须遵守

1. **只用资料里的教导说话，并标出处。**资料里讲过的内容，在句末标出处，格式 `［S02 19:54］`（音视频时间）、`［S03 第12段］`（文字资料段落，PDF 是页）或 `［S02-21］`（观点库编号；`S02-Y3` 是原话，`S02-B1` 是比喻和故事，`S02-W1` 是问答）。关键说法尽量引用他的原话，用「」括起来。
2. **超出资料的问题，可以给看法，但必须单独标注：**
   > 【AI 推测：{name}可能的看法，不是他资料里的内容】……
   推测必须从他讲过的教导推出来，并说清是从哪几条推的（例如「从 S02-03、S04-15 推测」）。推不出来就说「资料里没讲，没法替他说」。
3. **不掺你（AI）自己的观点**，也不拿别的老师、别的传统的说法来补。用户明确要求「AI 你自己怎么看」时，另起一段：
   > 【AI 自己的看法，不代表{name}】……
4. **资料里明确没讲、他说以后再讲的话题**（见第十节），要照实说没讲，再按规则 2 决定要不要给推测。
5. **他不懂、不碰的东西不要装懂**（见第十节）。
6. **你是 AI 模拟的，不是{name}本人。**用户问「你是不是老师本人」「你是真人吗」时照实说；不要编造资料里没有的个人经历，不替他做承诺（例如答应见面、收徒、给个人加持或保证效果）。
7. **对方流露严重的痛苦、绝望，或自伤、轻生的念头时：**先关心他、接住他的情绪，不急着讲道理；明确建议他马上联系身边信任的人，和专业的心理援助（例如当地的心理援助热线、医院的心理科或精神科；有紧急危险时打当地急救电话）。不要只用灵性道理带过，不要说「这是你的业力、功课、考验」这类让人更自责的话，也不要劝他停药或停止治疗。
8. **他的立场不等于正确**：用户问「他说得对不对」时，按规则 3 给 AI 自己的看法并标注。
9. **像{name}那样说话**：用他的语气、节奏和常用的词；能用他的原话、比喻和故事就用（照录、标出处）。先回应这个人（他的处境和感受），再讲道理，最后可以给一个马上能做的练习或觉察。不要 AI 腔：不用「首先、其次、综上所述」，不说空泛的安慰话，不列一大串要点（除非他自己就这样讲）。"""


# --------------------------------------------------------------- 工具 ----
def _sha(*parts):
    return hashlib.sha1('\x00'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:16]


def _cell(s):
    return re.sub(r'\s+', ' ', str(s or '')).replace('|', '｜').strip() or '—'


def _ts(t):
    return mentor._ts(t)


def _times(src):
    return [mentor.parse_time(x) for x in re.findall(r'\d{1,2}:\d{2}(?::\d{2})?', str(src or ''))]


_PARA = re.compile(r'(\d{1,5})\s*[段页]?\s*(?:[–\-~～至到]\s*第?\s*(\d{1,5}))?')


def _fix_src(sid, src, duration=0, unit=None, size=0):
    """出处统一写成「S01 05:19–07:08」（文字资料「S01 第12–14段」）；超出这份资料长度的标出来。"""
    src = re.sub(r'^\s*S\d{2,3}\s*', '', str(src or '')).strip()
    if unit:
        m = _PARA.search(src)
        if not m:
            return f'{sid}（位置未标）', False
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        a, b = min(a, b), max(a, b)
        bad = a < 1 or (bool(size) and b > size)
        where = f'第{a}{unit}' if a == b else f'第{a}–{b}{unit}'
        return f'{sid} {where}' + ('（位置待核对）' if bad else ''), not bad
    src = src.replace('-', '–').replace('~', '–').replace('～', '–')
    ts = _times(src)
    if not ts:
        return f'{sid}（时间未标）', False
    bad = bool(duration) and any(t > duration + 5 for t in ts)
    return f'{sid} {src}' + ('（时间待核对）' if bad else ''), not bad


def _src_of(x, src, duration=0):
    """一份资料（x）里一条的出处；x 记着是不是文字资料（unit）、有多少段（size）。"""
    return _fix_src(x['sid'], src, duration, x.get('unit'), x.get('size') or 0)


def _state_path(mid):
    return mentor._mdir(mid) / 'distill.json'


def load_state(mid):
    return mentor._load_json(_state_path(mid), None) or {'lessons': [], 'cache': {}, 'written': {}}


def save_state(mid, st):
    mentor._save_json(_state_path(mid), st)


def _vault():
    v = (config.load().get('obsidian') or {}).get('vault') or ''
    if not v or not Path(v).is_dir():
        raise DistillError(f'先在「设置 → Obsidian」里选好 Obsidian 库的位置，蒸馏结果要写进库里的「{MENTOR_ROOT}」文件夹')
    return v


def target_folder(name):
    name = re.sub(r'[\\/:*?"<>|]', '', str(name or '')).strip()
    if not name:
        raise DistillError('请填老师的名字')
    return Path(_vault()) / MENTOR_ROOT / name


def _lesson_doc(path):
    """一份资料的内容：音视频是字幕文件（和猛人学习共用）；文字资料是读出来的段落 {kind: 'text', units, unit, …}。"""
    if not path or not Path(path).is_file():
        return None
    if mentor.is_text(path):
        try:
            t = mentor.text_units(path)
        except Exception as e:  # noqa: BLE001  读不出的文档当作没有内容
            log.warning('蒸馏：读不出文字资料 %s：%s', path, e)
            return None
        return {'kind': 'text', 'title': t['title'], 'units': t['units'], 'unit': t['unit'], 'reviewed': True, 'duration': 0}
    d = docs.load(path)
    return d if d and d.get('kind') != 'book' else None


def _has_content(d):
    return bool(d and (d.get('segments') or d.get('units')))


def _note_name(d):
    p = ((d or {}).get('clip') or {}).get('path') or ''
    return Path(p).stem if p and Path(p).is_file() else ''


def _hash_doc(d):
    if d.get('units'):
        return _sha(*(u['text'] for u in d['units']))
    return _sha(*(s.get('text', '') for s in d.get('segments') or []))


# -------------------------------------------------------- 新建 / 加课 ----
def create(name, paths):
    """新建一位老师（或给同一个文件夹的老师加资料）。只记下要用哪些资料，不调用 AI。"""
    folder = target_folder(name)
    paths = _check_paths(paths)
    m = next((x for x in mentor.list_mentors(chats=False) if mentor._same(x['folder'], folder)), None)
    if m is None:
        m = {'id': uuid.uuid4().hex[:10], 'name': folder.name, 'folder': str(folder),
             'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'sources': []}
    m['distilled'] = True
    m.setdefault('distill_done', False)
    mentor._save_json(mentor._mdir(m['id']) / 'mentor.json', {k: v for k, v in m.items() if k != 'chats'})
    add_lessons(m['id'], paths)
    return mentor.get(m['id'])


def _check_paths(paths):
    from . import workflow
    out = []
    for p in paths or []:
        p = str(p)
        if not workflow.allowed(p):
            raise DistillError(f'《{Path(p).stem}》不在媒体库里')
        if not _has_content(_lesson_doc(p)):
            raise DistillError(f'《{Path(p).stem}》里没有读到文字' if mentor.is_text(p) else f'《{Path(p).stem}》还没有字幕，先生成字幕再蒸馏')
        out.append(p)
    if not out:
        raise DistillError('请至少选一份资料（有字幕的课，或文章、对话记录）')
    return out


def add_lessons(mid, paths):
    """加资料：已经在的不重复加；新的按顺序接着编号（S01… 一旦分配不再变）。"""
    st = load_state(mid)
    have = {mentor._norm(x['path']) for x in st['lessons']}
    n = max((int(x['sid'][1:]) for x in st['lessons']), default=0)
    added = 0
    for p in paths:
        if mentor._norm(p) in have:
            continue
        n += 1
        d = _lesson_doc(p) or {}
        x = {'sid': f'S{n:02d}', 'path': str(p), 'title': d.get('title') or Path(p).stem, 'added': time.strftime('%Y-%m-%d %H:%M:%S')}
        if d.get('units'):
            x.update(unit=d['unit'], size=len(d['units']))
        st['lessons'].append(x)
        have.add(mentor._norm(p))
        added += 1
    save_state(mid, st)
    if added:
        mentor.update(mid, distill_done=False)
    return added


def remove_lesson(mid, sid):
    """不再用某份资料（编号空着，不给别的资料用）。"""
    st = load_state(mid)
    st['lessons'] = [x for x in st['lessons'] if x['sid'] != sid]
    save_state(mid, st)
    mentor.update(mid, distill_done=False)


# ---------------------------------------------------------- 现在的状态 ----
def status(mid):
    """界面要的：每份资料提取过没有、改过没有；三份文件写过没有、在 Obsidian 里改过没有。"""
    m = mentor.get(mid)
    st = load_state(mid)
    lessons = []
    for x in st['lessons']:
        d = _lesson_doc(x['path'])
        state = 'missing' if not _has_content(d) else ('todo' if not x.get('data') else
                                                       ('changed' if _hash_doc(d) != x.get('hash') else 'done'))
        lessons.append({'sid': x['sid'], 'title': x['title'], 'path': x['path'], 'state': state, 'text': bool(x.get('unit')),
                        'points': sum(len((x.get('data') or {}).get(k) or []) for k in ('观点', '原话', '比喻和故事', '问答'))})
    return {'mentor': m, 'lessons': lessons, 'conflicts': conflicts(m, st), 'running': running(mid),
            'complete': bool(st.get('done')) and all(x['state'] == 'done' for x in lessons)}


def _paths(m):
    folder = Path(m['folder'])
    have = mentor.files_of(folder) if folder.is_dir() else {}
    return {k: (have.get(k) or folder / FILES[k]) for k in FILES}


def conflicts(m, st=None):
    """要覆盖的文件里，上次写完以后在 Obsidian 里改过的（或者不是软件写的）。"""
    st = st or load_state(m['id'])
    out = []
    for k, p in _paths(m).items():
        if p.is_file() and _sha(mentor._read(p)) != (st.get('written') or {}).get(k):
            out.append(p.name)
    return out


def running(mid):
    with _lock:
        job = _running.get(mid)
        return job.id if job and job.status == 'running' else None


# --------------------------------------------------------------- 调 AI ----
def _ask(job, settings, name, messages, label, json_mode=False, on_piece=None):
    """调一次 AI（边收边显示写了多少字）；临时出错按 workflow 的规则重试。
    on_piece：边写边把文字交出去（圆桌讨论用）；重试前先交一个 None，表示前面交出去的作废。"""
    tries = 0
    while True:
        try:
            parts, n = [], 0
            if on_piece and tries:
                on_piece(None)
            for piece in llm.stream(settings, name, messages, job.cancel, json_mode=json_mode):
                parts.append(piece)
                n += len(piece)
                if on_piece:
                    on_piece(piece)
                if len(parts) % 30 == 0:
                    job.report(job.progress, f'{label}：AI 已写 {n} 字…')
            if job.cancel.is_set():
                raise tasks.WorkError('cancelled', '已取消')
            text = ''.join(parts).strip()
            if not text:
                raise llm.LlmError('AI 没有返回内容')
            return text
        except Exception as e:  # noqa: BLE001
            err = tasks.error_dict(e)
            if job.cancel.is_set() or err['error_code'] == 'cancelled':
                raise tasks.WorkError('cancelled', '已取消')
            if not err['retryable'] or tries >= RETRIES:
                raise
            wait = RETRY_WAIT[min(tries, len(RETRY_WAIT) - 1)]
            tries += 1
            log.warning('蒸馏：%s出错，%s 秒后第 %d 次重试：%s', label, wait, tries, err['message'])
            end = time.monotonic() + wait
            while (left := end - time.monotonic()) > 0:
                job.report(job.progress, f'{label}出错（{err["message"][:60]}），{max(1, round(left))} 秒后第 {tries} 次重试…')
                if job.cancel.wait(min(left, 5)):
                    raise tasks.WorkError('cancelled', '已取消')
            settings = config.load()


def _json(text):
    try:
        data = llm._json_from(text)
    except ValueError:
        raise llm.LlmError('AI 返回的格式不对（不是 JSON），请重试或换一个 AI 服务')
    if not isinstance(data, dict):
        raise llm.LlmError('AI 返回的格式不对，请重试')
    return data


def _sub_lines(segs):
    return [f'[{_ts(float(s["start"]))}] {s["text"]}' for s in segs]


LISTS = ('观点', '原话', '比喻和故事', '问答', '自述', '术语', '存疑')


def extract_lesson(job, settings, name, x, d, frac):
    """一份资料 → 结构化的条目（太长时分几段，合起来）。"""
    text = bool(d.get('units'))
    if text:
        lines = [f'[第{u["n"]}{d["unit"]}] {u["text"]}' for u in d['units']]
        head = f'# 原文（文字资料，[第N{d["unit"]}] 原文；出处写「第N{d["unit"]}」或「第N–M{d["unit"]}」）\n'
    else:
        lines = _sub_lines(d.get('segments') or [])
        head = '# 完整字幕（[时间] 原文）\n'
    chunks, cur, size = [], [], 0
    for ln in lines:
        if cur and size + len(ln) > CHUNK_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append(cur)
    summary = '' if text else (d.get('summary') or '').strip()
    out = {'说明': '', **{k: [] for k in LISTS}, '实践': []}
    for k, ch in enumerate(chunks):
        part = f'（这份资料较长，分 {len(chunks)} 段提取，这是第 {k + 1} 段，只提取这一段里的内容）\n' if len(chunks) > 1 else ''
        user = (f'资料：{x["title"]}（{"文字资料" if text else "音视频课程的字幕"}）\n{part}\n'
                + (f'# 课程重点（别人整理的，只作参考）\n{summary[:20000]}\n\n' if summary and k == 0 else '')
                + head + '\n'.join(ch))
        label = f'提取《{x["title"]}》' + (f'（第 {k + 1}/{len(chunks)} 段）' if len(chunks) > 1 else '')
        job.report(frac, label + '…')
        data = _json(_ask(job, settings, name, [{'role': 'system', 'content': EXTRACT_SYSTEM}, {'role': 'user', 'content': user}], label, json_mode=True))
        base = len(out['观点'])
        for key in LISTS:
            out[key] += [v for v in (data.get(key) or []) if v]
        for p in data.get('实践') or []:
            if isinstance(p, dict) and p.get('内容'):
                refs = [int(i) + base for i in (p.get('依据') or []) if str(i).isdigit()]
                out['实践'].append({'内容': p['内容'], '依据': refs})
        if data.get('说明') and not out['说明']:
            out['说明'] = str(data['说明'])
    if not any(out[k] for k in ('观点', '原话', '比喻和故事', '问答')):
        raise llm.LlmError(f'《{x["title"]}》没有提取出内容，请重试或换一个 AI 服务')
    return out


# ------------------------------------------------------------ 写观点库 ----
def lesson_md(x, duration=0):
    """一份资料的那一部分。返回 (markdown, 条目数, 出处超出资料长度的条数)。"""
    sid, data = x['sid'], x.get('data') or {}
    bad = 0

    def src(v):
        nonlocal bad
        s, ok = _src_of(x, v, duration)
        bad += not ok
        return s

    def items(key, need):
        return [p for p in data.get(key) or [] if isinstance(p, dict) and p.get(need)]
    rows = []
    for i, p in enumerate([p for p in data.get('观点') or [] if isinstance(p, dict)], 1):
        kind = _cell(p.get('类型') or '观点')
        kind = KIND_ALIAS.get(kind, kind)
        if kind not in KINDS:
            kind = '观点'
        rows.append(f'| {sid}-{i:02d} | {_cell(p.get("内容"))} | {_cell(p.get("条件"))} | {src(p.get("出处"))} | {kind}{" ⚠️" if p.get("说得很满") else ""} |')
    out = [f'## {sid}　{x["title"]}', '']
    if data.get('说明'):
        out += [f'> {_cell(data["说明"])}', '']
    if rows:
        out += ['### 观点、修法、定义', '', '| 编号 | 内容 | 条件 / 边界 | 出处 | 类型 |', '| --- | --- | --- | --- | --- |', *rows, '']
    quotes = items('原话', '原话')
    if quotes:
        out += ['### 老师原话（语录）', '', '| 编号 | 原话 | 在讲什么 | 出处 |', '| --- | --- | --- | --- |']
        out += [f'| {sid}-Y{i} | {_cell(p["原话"])} | {_cell(p.get("在讲什么"))} | {src(p.get("出处"))} |' for i, p in enumerate(quotes, 1)]
        out.append('')
    stories = items('比喻和故事', '内容')
    if stories:
        out += ['### 比喻和故事', '', '| 编号 | 比喻或故事 | 用来讲什么 | 出处 |', '| --- | --- | --- | --- |']
        out += [f'| {sid}-B{i} | {_cell(p["内容"])} | {_cell(p.get("用来讲什么"))} | {src(p.get("出处"))} |' for i, p in enumerate(stories, 1)]
        out.append('')
    qas = items('问答', '答')
    if qas:
        out += ['### 问答（学员问，老师怎么回应）', '', '| 编号 | 学员问 | 老师怎么回应（尽量原话） | 回应方式 | 出处 |', '| --- | --- | --- | --- | --- |']
        out += [f'| {sid}-W{i} | {_cell(p.get("问"))} | {_cell(p["答"])} | {_cell(p.get("回应方式"))} | {src(p.get("出处"))} |' for i, p in enumerate(qas, 1)]
        out.append('')
    selfs = items('自述', '内容')
    if selfs:
        out += ['### 老师自述（做老师画像用）', '', '| 编号 | 内容 | 出处 |', '| --- | --- | --- |']
        out += [f'| {sid}-A{i} | {_cell(p["内容"])} | {src(p.get("出处"))} |' for i, p in enumerate(selfs, 1)]
        out.append('')
    terms = items('术语', '术语')
    if terms:
        out += ['### 老师的术语（本份资料）', '', '| 术语 | 老师的意思 | 出处 |', '| --- | --- | --- |']
        out += [f'| {_cell(p["术语"])} | {_cell(p.get("意思"))} | {_src_of(x, p.get("出处"), duration)[0]} |' for p in terms]
        out.append('')
    prac = [p for p in data.get('实践') or [] if isinstance(p, dict)]
    if prac:
        out += ['### 可以拿去实践的', '']
        for p in prac:
            refs = '、'.join(f'{sid}-{i:02d}' for i in p.get('依据') or [] if 1 <= i <= len(rows))
            out.append(f'- {_cell(p["内容"])}' + (f'（{refs}）' if refs else ''))
        out.append('')
    doubts = [str(v).strip() for v in data.get('存疑') or [] if str(v).strip()]
    if doubts:
        out += ['### 存疑和待核对', ''] + [f'- {sid} {re.sub(r"^S\d{2,3}\s*", "", v)}' for v in doubts] + ['']
    return '\n'.join(out), len(rows) + len(quotes) + len(stories) + len(qas), bad


def atoms_md(m, st):
    """整份观点库。返回 (markdown, 正文（给 AI 合并用）, 统计)。"""
    name = m['name']
    src_rows, parts, total, bad, reviewed = [], [], 0, 0, 0
    for x in st['lessons']:
        if not x.get('data'):
            continue
        d = _lesson_doc(x['path']) or {}
        reviewed += bool(d.get('reviewed'))
        note = _note_name(d) or x.get('note') or ''
        x['note'] = note
        where = (f'[[{note}]]' if note else f'（文字资料：{_cell(Path(x["path"]).name)}）' if x.get('unit')
                 else f'（还没剪藏到 Obsidian：{_cell(Path(x["path"]).name)}）')
        src_rows.append(f'| {x["sid"]} | {_cell(x["title"])} | {where} |')
        body, n, b = lesson_md(x, d.get('duration') or 0)
        parts.append(body)
        total += n
        bad += b
    body = '\n\n---\n\n'.join(parts)
    n = len(parts)
    head = (f'---\n老师: {name}\n类型: 观点库\n状态: AI 提取，待作者审核\n{review_key(reviewed, n)}\n生成: 猛人灵修「老师蒸馏」{time.strftime("%Y-%m-%d")}\ntags:\n  - 灵修蒸馏\n---\n\n'
            f'# {name}：观点库（逐节提取）\n\n'
            f'> 这是「{name}」蒸馏的第一步：AI 逐份把老师讲的拆成一条一条，并标出处（由猛人灵修「老师蒸馏」生成）。后面的「课程蒸馏」和「老师画像」都从这里来。\n'
            '> 出处是 AI 依据原文标的，程序只核对了有没有超出资料长度；要核对原话，在老师工作台里点出处跳到那一秒（文字资料显示那一段原文）。\n'
            + review_note(reviewed, n) + '\n'
            '## 来源\n\n| 编号 | 资料 | 笔记 |\n| --- | --- | --- |\n' + '\n'.join(src_rows) + '\n\n'
            '## 怎么读这张表\n\n'
            '- **出处**：`S01 05:19` 表示第 1 份资料（音视频）5 分 19 秒；`S02 第12段` 表示第 2 份资料（文字）第 12 段（每个非空行算一段；PDF 是第几页）。\n'
            '- **编号**：`S01-03` 是观点、修法或定义；`S01-Y2` 是老师原话；`S01-B1` 是比喻和故事；`S01-W1` 是问答（学员问、老师怎么回应）；`S01-A1` 是老师自述。\n'
            '- **类型**：**观点**是老师的看法、主张；**修法**是具体的练习和做法；**定义**是老师对某个说法的解释。\n'
            '- 引号「」里和「原话」表里是老师原话（只做了去口头禅的最小处理）。\n'
            '- ⚠️：老师用了「一定、绝对、唯一」这类说得很满的话，审核时留意它的前提。\n'
            '- 「条件 / 边界」写的是老师自己讲到的前提和适用的人；老师没讲的写「未讲明」，不替老师补。\n'
            '- 「待核对」：AI 标的位置超出了这份资料的长度，可能标错了。\n\n---\n\n')
    return head + body + '\n', body, {'points': total, 'bad_times': bad, 'reviewed': reviewed}


def review_key(reviewed, n):
    """属性里的一行：来源有几份人工校对过（文字资料本来就是原文，算校对过）。"""
    return f'来源人工校对: {reviewed}/{n} 节'


def review_note(reviewed, n):
    if reviewed >= n:
        return ''
    return (f'> 来源里有 {n - reviewed} 节音视频的字幕还没人工校对（老师普通话标准、识别准确时可以不校对）；'
            '提取时发现字幕可能识别错的地方，放进了各节的「存疑和待核对」。\n')


_ID = r'[AYBW]?\d{1,3}'


def _ids_in(text):
    """文字里提到的观点库编号（含「S03-02、03」「S03-Y1、Y2」这种省略写法）。"""
    out = set()
    for m in re.finditer(rf'(S\d{{2,3}})-({_ID})(?!\d)((?:\s*[、,，～~–-]\s*{_ID}(?!\d))*)', text):
        out.add(f'{m.group(1)}-{m.group(2)}')
        for k in re.findall(_ID, m.group(3) or ''):
            out.add(f'{m.group(1)}-{k}')
    return out


def _norm_id(aid):
    sid, _, k = aid.partition('-')
    return f'{sid}-{k[0]}{int(k[1:])}' if k[:1].isalpha() else f'{sid}-{int(k):02d}'


def bad_refs(text, atoms):
    """AI 写的编号里，观点库没有的（区间写法 S03-02～05 只查两头）。"""
    return sorted({a for a in _ids_in(text) if _norm_id(a) not in atoms})


# -------------------------------------------------------------- 整个流程 ----
def start(mid, force=False):
    """开始（或接着）蒸馏。在 Obsidian 里改过要覆盖的文件时返回 conflict，用户确认后 force。"""
    from . import jobs
    m = mentor.get(mid)
    if not m.get('distilled'):
        raise DistillError('这位老师是导入的，不是在软件里蒸馏的；要重新蒸馏，请用「新建老师」从媒体库选资料')
    with _lock:
        job = _running.get(mid)
        if job and job.status == 'running':
            return {'job': job.id}
    st = load_state(mid)
    if not st['lessons']:
        raise DistillError('还没有选资料')
    c = conflicts(m, st)
    if c and not force:
        return {'conflict': c}
    s = config.load()
    llm.check(s, s['chat'])
    job = jobs.start('distill', lambda j: run(mid, j))
    with _lock:
        _running[mid] = job
    return {'job': job.id}


def _capped(lines, limit, what):
    out, size = [], 0
    for k, ln in enumerate(lines):
        if out and size + len(ln) > limit:
            out.append(f'- （还有 {len(lines) - k} 条{what}没附上，太多了）')
            break
        out.append(ln)
        size += len(ln) + 1
    return '\n'.join(out) or '（没有）'


def run(mid, job):
    s = config.load()
    name = s['chat']
    m = mentor.get(mid)
    st = load_state(mid)
    st['done'] = False
    save_state(mid, st)
    t0 = time.time()
    # 1. 逐份提取
    todo = []
    for x in st['lessons']:
        d = _lesson_doc(x['path'])
        if not _has_content(d):
            if not x.get('data'):
                raise DistillError(f'{x["sid"]}《{x["title"]}》的内容找不到了（文件挪了位置或删了？），可以在「更新蒸馏」里去掉它')
            continue
        if d.get('units'):
            x.update(unit=d['unit'], size=len(d['units']))
        if not x.get('data') or _hash_doc(d) != x.get('hash'):
            todo.append((x, d))
    for k, (x, d) in enumerate(todo):
        frac = 0.7 * k / max(1, len(todo))
        job.report(frac, f'逐份提取：第 {k + 1}/{len(todo)} 份《{x["title"]}》…')
        data = extract_lesson(job, s, name, x, d, frac)
        x.update(data=data, hash=_hash_doc(d), done=time.strftime('%Y-%m-%d %H:%M:%S'), model=_model(s, name), title=d.get('title') or x['title'])
        save_state(mid, st)  # 做完一份存一份
    job.report(0.7, '整理观点库…')
    full, body, stat = atoms_md(m, st)
    atoms = mentor.parse_atoms(full)
    cache, used = st.setdefault('cache', {}), set()

    def cached(key, fn):
        used.add(key)
        if key not in cache:
            cache[key] = fn()
            save_state(mid, st)
        return cache[key]

    # 2. 合并成课程蒸馏（太大先分组小结）
    if len(body) <= MERGE_LIMIT:
        material = '# 观点库\n\n' + body
    else:
        groups, cur, size = [], [], 0
        for x in st['lessons']:
            if not x.get('data'):
                continue
            part = lesson_md(x)[0]
            if cur and size + len(part) > GROUP_CHARS:
                groups.append(cur)
                cur, size = [], 0
            cur.append((x, part))
            size += len(part)
        if cur:
            groups.append(cur)
        notes = []
        for k, g in enumerate(groups):
            label = f'分组小结 {k + 1}/{len(groups)}（{g[0][0]["sid"]}～{g[-1][0]["sid"]}）'
            job.report(0.7 + 0.1 * k / len(groups), label + '…')
            text = '\n\n---\n\n'.join(p for _, p in g)
            notes.append(f'## {g[0][0]["sid"]}～{g[-1][0]["sid"]} 小结\n\n' + cached(
                _sha('group', PROMPT_VER, text), lambda: _ask(job, s, name, [{'role': 'system', 'content': GROUP_SYSTEM}, {'role': 'user', 'content': text}], label)))
        material = '# 各组小结（每组几份资料，编号是观点库的）\n\n' + '\n\n'.join(notes)
    names = '\n'.join(f'- {x["sid"]}：{x["title"]}' + ('（文字资料）' if x.get('unit') else '') for x in st['lessons'] if x.get('data'))
    user = f'老师：{m["name"]}\n资料：\n{names}\n\n{material}'
    job.report(0.8, '合并成课程蒸馏…')
    distill = cached(_sha('merge', PROMPT_VER, user), lambda: _ask(job, s, name, [{'role': 'system', 'content': MERGE_SYSTEM}, {'role': 'user', 'content': user}], '合并成课程蒸馏'))
    distill = _from_heading(distill, '## 一')
    # 3. 老师画像：原话、比喻和故事、问答、自述各附一份（灵修老师像不像，主要看这些）
    of = lambda kind: [mentor.atom_line(a) for a in atoms.values() if a['kind'] == kind]  # noqa: E731
    terms = [f'| {_cell(t["术语"])} | {_cell(t.get("意思"))} | {_src_of(x, t.get("出处"))[0]} |'
             for x in st['lessons'] if x.get('data') for t in x['data'].get('术语') or [] if isinstance(t, dict) and t.get('术语')]
    points = [a for a in atoms.values() if a['kind'] in KINDS]
    if sum(len(a['text']) + len(a['cond']) for a in points) > PERSONA_ATOMS:
        ids = {_norm_id(a) for a in _ids_in(distill)}
        points = [a for a in points if a['id'] in ids]
        extra = '# 观点（课程蒸馏里引用到的条目）\n\n'
    else:
        extra = '# 观点（全部）\n\n'
    extra += '\n'.join(mentor.atom_line(a) for a in points)
    puser = (f'老师：{m["name"]}（共 {len(names.splitlines())} 份资料）\n资料：\n{names}\n\n# 课程蒸馏\n\n{distill}\n\n'
             f'# 问答（学员问，老师怎么回应）\n\n{_capped(of("问答"), PERSONA_LIST, "问答")}\n\n'
             f'# 老师原话\n\n{_capped(of("原话"), PERSONA_LIST, "原话")}\n\n'
             f'# 比喻和故事\n\n{_capped(of("比喻和故事"), PERSONA_LIST, "比喻和故事")}\n\n'
             f'# 老师自述（全部）\n\n{_capped(of("自述"), PERSONA_LIST, "自述")}\n\n'
             '# 老师的术语\n\n| 术语 | 老师的意思 | 出处 |\n' + '\n'.join(terms) + f'\n\n{extra}')
    job.report(0.9, '写老师画像…')
    persona = cached(_sha('persona', PROMPT_VER, puser), lambda: _ask(job, s, name, [{'role': 'system', 'content': PERSONA_SYSTEM}, {'role': 'user', 'content': puser}], '写老师画像'))
    persona = _from_heading(persona, '## 二')
    for k in list(cache):
        if k not in used:
            del cache[k]
    # 4. 写进 Obsidian
    job.report(0.98, '写进 Obsidian…')
    have = [x['sid'] for x in st['lessons'] if x.get('data')]
    n = len(have)
    model = _model(s, name)
    bad_d, bad_p = bad_refs(distill, atoms), bad_refs(persona, atoms)
    distill_md = (f'---\n老师: {m["name"]}\n类型: 课程蒸馏\n状态: AI 蒸馏，待作者审核\n{review_key(stat["reviewed"], n)}\n生成: 猛人灵修「老师蒸馏」{time.strftime("%Y-%m-%d")} · {model}\ntags:\n  - 灵修蒸馏\n---\n\n'
                  f'# {m["name"]}：课程蒸馏\n\n'
                  f'> 依据 {n} 份资料（见 [[{Path(FILES["atoms"]).stem}]]）。这里是 AI 把观点库合并后的结果：老师的教导体系、修法、反复强调的、常用的比喻和故事、前后不一致处和没讲到的内容。每条都能顺着编号回到观点库，再回到原文。\n'
                  '> 这里写的是「老师怎么说」，不是正确答案，也不是你的结论。\n\n' + distill.strip() + '\n'
                  + _bad_note(bad_d))
    persona_md = (f'---\n老师: {m["name"]}\n类型: 老师画像\n状态: AI 起草，待作者试用和审核\n{review_key(stat["reviewed"], n)}\n生成: 猛人灵修「老师蒸馏」{time.strftime("%Y-%m-%d")} · {model}\ntags:\n  - 灵修蒸馏\n  - 老师画像\n---\n\n'
                  f'# {m["name"]}：老师画像（对话用）\n\n'
                  f'> 用法：在猛人灵修「老师工作台」里和{m["name"]}对话时，软件会把本文、[[{Path(FILES["atoms"]).stem}]]、[[{Path(FILES["distill"]).stem}]] 和相关的原文一起交给 AI。\n'
                  '> 第一节是给 AI 的规则（软件固定写的），后面几节是 AI 依据资料写的。\n\n---\n\n'
                  + persona_rules(m['name'], n, have[0], have[-1]) + '\n\n---\n\n' + persona.strip() + '\n' + _bad_note(bad_p))
    written = write_files(m, st, {'atoms': full, 'distill': distill_md, 'persona': persona_md})
    st['done'] = True
    st['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    st['model'] = model
    save_state(mid, st)
    mentor.update(mid, distill_done=True, distill_updated=st['updated'], distill_model=model)
    m = mentor.refresh(mid)
    log.info('蒸馏完成：%s，%d 份资料（这次提取 %d 份），%d 条，用时 %.0f 秒', m['name'], n, len(todo), stat['points'], time.time() - t0)
    return {'mentor': m, 'lessons': n, 'extracted': len(todo), 'points': stat['points'], 'bad_times': stat['bad_times'],
            'bad_refs': len(bad_d) + len(bad_p), 'files': written, 'folder': m['folder'], 'seconds': round(time.time() - t0)}


def _from_heading(text, head):
    """AI 有时在前面加开场白或总标题：从第一个指定的小标题开始取。"""
    text = re.sub(r'^\s*```(?:markdown|md)?\s*\n|\n```\s*$', '', text.strip())
    i = text.find(head)
    return text[i:] if i > 0 else text


def _bad_note(bad):
    if not bad:
        return ''
    return (f'\n---\n\n> 程序检查：上面有 {len(bad)} 处编号在观点库里找不到（可能是 AI 写错了），审核时留意：'
            + '、'.join(bad[:30]) + ('…' if len(bad) > 30 else '') + '\n')


def _model(s, name):
    return f'{llm.PROVIDER_NAMES.get(name, name)} · {(s["providers"].get(name) or {}).get("chat_model", "")}'


def write_files(m, st, texts):
    """写三份文件；已有的先整体备份（每位老师留 BACKUP_KEEP 份）。"""
    paths = _paths(m)
    folder = Path(m['folder'])
    folder.mkdir(parents=True, exist_ok=True)
    olds = [p for p in paths.values() if p.is_file()]
    if olds:
        root = config.DATA_DIR / 'backups' / '蒸馏'
        b = root / f'{folder.name}-{time.strftime("%Y%m%d-%H%M%S")}'
        b.mkdir(parents=True, exist_ok=True)
        for p in olds:
            shutil.copy2(p, b / p.name)
        for old in sorted(root.glob(f'{folder.name}-*'))[:-BACKUP_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
    st['written'] = {}
    for k, text in texts.items():
        p = paths[k]
        tmp = p.with_name(p.name + '.tmp')
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(p)
        st['written'][k] = _sha(mentor._read(p))
    return [str(p) for p in paths.values()]
