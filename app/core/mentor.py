"""老师工作台（2026-10-24，用户要求「AI 按老师画像，忠实地用老师的知识和我对话」）。

一位「老师」= Obsidian 里 `灵修老师/老师名/` 文件夹（猛人灵修；distill.MENTOR_ROOT），里面有蒸馏好的：
- `…观点库….md`：逐节提取的表（编号 S01-05 观点、S01-Y1 原话、S01-B1 比喻和故事、S01-W1 问答、S01-A1 自述；
  出处 S01 05:19 是音视频的时间，S01 第12段 是文字资料的段落），开头「来源」表把 S01… 对应到课程笔记或文字资料；
- `…老师画像….md`：给 AI 的扮演规则和老师的信念、怎么回应人、语录、比喻和故事、风格（第一节就是规则）；
- `…课程蒸馏….md`（可选）：整体体系。
这些文件以 Obsidian 里的为准，软件每次读最新的，不另存副本（和术语表文件一样）。

对话时交给 AI 的资料：老师画像 + 课程蒸馏 + 观点库（少就全给，多就挑和问题相关的）+ 和问题相关的原文片段
（音视频来自和猛人学习共用的字幕文件，带时间；文字资料按段落），所以回答里的出处能在界面上点开：
音视频跳到那节课那一秒，文字资料显示那一段原文、可以打开那份资料。
挑「相关」用的是本机的字词重合打分，不联网、不花钱。

对话记录存在 `%APPDATA%\\猛人灵修\\mentors\\老师id\\chats\\`（用户的资料，不自动删；界面上能删单个对话或全部）；
用户点「收藏」的回答追加到老师文件夹里的 `对话精选.md`，写之前备份到 `backups\\对话精选`（留 30 份）。
"""
import json
import math
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from . import book, config, doc as docs

MENTOR_DIR = config.DATA_DIR / 'mentors'
FAVORITES = '对话精选.md'
HISTORY_TURNS = 12          # 带上最近几条对话
ATOMS_ALL_LIMIT = 40000     # 观点库不超过这么多字就整份交给 AI，超过就挑相关的
ATOMS_PICK = 40
WINDOWS_PICK = 10
WINDOW_SECONDS = 45         # 字幕按约 45 秒一段切成原文片段
WINDOW_CHARS = 400          # 文字资料按约 400 字（几段连在一起）切成原文片段
SRC_RE = re.compile(r'^\|\s*(S\d{2,3})\s*\|\s*(.*?)\s*\|\s*(\[\[.+?\]\]|[^|\n]*?)\s*\|', re.M)  # 第三格是 [[笔记]]，没剪藏的课是说明文字
ROW_RE = re.compile(r'^\|\s*(S\d{2,3}-[A-Za-z0-9]+)\s*\|(.+)\|\s*$', re.M)
TIME_RE = re.compile(r'(S\d{2,3})\s+(\d{1,2}:\d{2}(?::\d{2})?)')
PARA_RE = re.compile(r'(S\d{2,3})\s+第\s*(\d{1,5})(?:\s*[–\-~～]\s*\d{1,5})?\s*([段页])')
KIND_OF = {'A': '自述', 'Y': '原话', 'B': '比喻和故事', 'W': '问答'}   # 编号里的字母（distill.PREFIX 反过来）
NOTE_LABEL = {'原话': '在讲：', '比喻和故事': '用来讲：', '问答': '回应方式：'}  # 每种条目第二格叫什么


class MentorError(Exception):
    pass


# ------------------------------------------------------------------ 工具 ----
def _ts(t):
    t = int(t)
    return f'{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}' if t >= 3600 else f'{t // 60:02d}:{t % 60:02d}'


def parse_time(s):
    parts = [int(x) for x in str(s).split(':')]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]


def _norm(p):
    return os.path.normcase(os.path.abspath(str(p or '')))


def _same(a, b):
    return _norm(a) == _norm(b)


def _read(path):
    return config.read_text_file(path) if path and Path(path).is_file() else ''


def _pick_file(folder, word):
    hits = sorted(p for p in Path(folder).glob('*.md') if word in p.name)
    return hits[0] if hits else None


def files_of(folder):
    """老师文件夹里的三份文件（找不到的是 None）。"""
    return {'persona': _pick_file(folder, '老师画像'), 'atoms': _pick_file(folder, '观点库'), 'distill': _pick_file(folder, '课程蒸馏')}


def parse_sources(md):
    """观点库开头的来源表 → [{sid, title, note}]（没剪藏的课 note 是空的）"""
    out = []
    for m in SRC_RE.finditer(md):
        link = re.match(r'\[\[(.+?)\]\]', m.group(3))
        out.append({'sid': m.group(1), 'title': m.group(2).strip(), 'note': link.group(1).split('|')[0].strip() if link else ''})
    return out


def parse_atoms(md):
    """观点库里的每一行，按编号的字母认是哪张表：
    观点（S01-03 | 内容 | 条件 | 出处 | 类型）、原话（S01-Y1 | 原话 | 在讲什么 | 出处）、比喻和故事（S01-B1 | 内容 | 用来讲什么 | 出处）、
    问答（S01-W1 | 学员问 | 老师怎么回应 | 回应方式 | 出处）、自述（S01-A1 | 内容 | 出处）。
    t 是音视频的秒数，p 是文字资料的第几段（页），unit 是「段」或「页」。"""
    out = {}
    for m in ROW_RE.finditer(md):
        cells = [c.strip() for c in m.group(2).split('|')]
        aid = m.group(1)
        letter = re.sub(r'\d', '', aid.split('-', 1)[1]).upper()
        kind = KIND_OF.get(letter)
        if kind == '问答' and len(cells) >= 4:
            text, cond, src = f'问：{cells[0]}　答：{cells[1]}', cells[2], cells[3]
        elif kind in ('原话', '比喻和故事') and len(cells) >= 3:
            text, cond, src = cells[0], cells[1], cells[2]
        elif kind and len(cells) >= 2:  # 自述（和猛人学习蒸馏的一样）
            text, cond, src = cells[0], '', cells[1]
        elif not kind and len(cells) >= 4:
            text, cond, src, kind = cells[0], cells[1], cells[2], cells[3]
        else:
            continue
        tm, pm = TIME_RE.search(src), PARA_RE.search(src)
        out[aid] = {'id': aid, 'sid': aid.split('-')[0], 'text': text, 'cond': '' if cond in ('—', '-') else cond, 'src': src, 'kind': kind,
                    't': parse_time(tm.group(2)) if tm else None, 'p': int(pm.group(2)) if pm and not tm else None,
                    'unit': pm.group(3) if pm and not tm else ''}
    return out


def atom_line(a):
    """交给 AI 的一行：- [编号]（出处，类型）内容　条件：…"""
    return (f'- [{a["id"]}]（{a["src"]}，{a["kind"]}）{a["text"]}'
            + (f'　{NOTE_LABEL.get(a["kind"], "条件：")}{a["cond"]}' if a['cond'] else ''))


# --------------------------------------------------------- 文字资料 ----
_units_cache = {}


def is_text(path):
    """文字资料（文章、对话记录、书）：按段落作出处；其余是音视频（按字幕时间）。"""
    return Path(str(path or '')).suffix.lower() in book.DOC_EXT


def text_units(path):
    """文字资料按段落编号：每个非空行算一段（对话记录一般一行一句）；PDF 按页（PDF 的换行不是分段）。
    返回 {title, unit: '段' | '页', units: [{n, text}]}，按文件修改时间缓存（最多 30 份）。"""
    p = os.path.abspath(str(path))
    st = os.stat(p)
    key = (p.lower(), st.st_mtime, st.st_size)
    hit = _units_cache.get(key)
    if hit:
        return hit
    info = book.read(p)
    units = []
    if info.get('kind') == 'pdf':
        unit = '页'
        for pg in info['pages']:
            text = re.sub(r'\s+', ' ', pg.get('text') or '').strip()
            if text:
                units.append({'n': pg['n'], 'text': text})
    else:
        unit = '段'
        for pg in info['pages']:
            for line in (pg.get('text') or '').split('\n'):
                line = line.strip()
                if line:
                    units.append({'n': len(units) + 1, 'text': line})
    out = {'title': Path(p).stem, 'unit': unit, 'units': units}
    if len(_units_cache) >= 30:
        _units_cache.clear()
    _units_cache[key] = out
    return out


def _frontmatter_video(note_path):
    head = _read(note_path)[:3000]
    m = re.search(r'^视频文件:\s*"?(.+?)"?\s*$', head, re.M)
    return os.path.normpath(m.group(1)) if m else ''


def _find_notes(vault, names):
    """在 Obsidian 库里按文件名找课程笔记 → {名字: 路径}。整个库只翻一遍、找齐就停；
    先翻设置里的剪藏文件夹（课程笔记一般在那里）。以前每节课翻一遍整个库，导入 5 节课要 8 秒。"""
    want = {(n + '.md').lower(): n for n in names if n}
    found = {}
    if not vault or not os.path.isdir(vault) or not want:
        return found
    sub = (config.load().get('obsidian') or {}).get('folder') or ''
    first = os.path.join(vault, sub) if sub and os.path.isdir(os.path.join(vault, sub)) else ''
    for top in [first, vault] if first else [vault]:
        for root, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if not d.startswith('.') and not (top == vault and first and _same(os.path.join(root, d), first))]
            for f in files:
                n = want.get(f.lower())
                if n and n not in found:
                    found[n] = Path(root) / f
            if len(found) == len(want):
                return found
    return found


def _find_note(vault, name):
    return _find_notes(vault, [name]).get(name)


def _vault_of(folder):
    """老师文件夹所在的 Obsidian 库：设置里填了就用；没填就往上找有 .obsidian 的那一层。"""
    v = (config.load().get('obsidian') or {}).get('vault') or ''
    if v and os.path.normcase(os.path.abspath(folder)).startswith(os.path.normcase(os.path.abspath(v))):
        return v
    p = Path(folder).resolve()
    for q in [p, *p.parents]:
        if (q / '.obsidian').is_dir():
            return str(q)
    return v


# ----------------------------------------------------------------- 老师 ----
def _mdir(mid):
    if not re.fullmatch(r'[0-9a-f]{10}', str(mid or '')):
        raise MentorError('没有这位老师')
    return MENTOR_DIR / mid


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, path)


def _load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


MENTOR_ROOT = '灵修老师'  # 和 distill.MENTOR_ROOT 一样（这里不引用 distill，免得互相引用）


def _relocate(m):
    """老师文件夹不在记下的位置了（例如换了 Obsidian 库、库挪了位置）：在设置里的 Obsidian 库的
    「灵修老师/老师名」找，找到就改记录。"""
    f = m.get('folder') or ''
    if not f or os.path.isdir(f):
        return m
    p = Path(f)
    cands = []
    v = (config.load().get('obsidian') or {}).get('vault') or ''
    if v:
        cands.append(Path(v) / MENTOR_ROOT / p.name)
    for c in cands:
        if c.is_dir():
            m['folder'] = str(c)
            _save_json(_mdir(m['id']) / 'mentor.json', {k: x for k, x in m.items() if k != 'chats'})
            break
    return m


def list_mentors(chats=True):
    out = []
    if MENTOR_DIR.is_dir():
        for d in sorted(MENTOR_DIR.iterdir()):
            m = _load_json(d / 'mentor.json')
            if m and m.get('id'):
                m = _relocate(m)
            if m:
                out.append(dict(m, chats=list_chats(m['id'])) if chats else m)
            for t in d.glob('**/*.tmp'):  # 写到一半退出留下的临时文件
                if time.time() - t.stat().st_mtime > 86400:
                    t.unlink(missing_ok=True)
    return sorted(out, key=lambda m: m.get('created', ''))


def update(mid, **kw):
    m = get(mid)
    m.update(kw)
    _save_json(_mdir(mid) / 'mentor.json', m)
    return m


def get(mid):
    m = _load_json(_mdir(mid) / 'mentor.json')
    if not m:
        raise MentorError('没有这位老师')
    return _relocate(m)


def add(folder):
    """导入一个已经蒸馏好的老师文件夹。要有观点库和老师画像；来源表里的课程笔记要能在 Obsidian 库里找到。"""
    folder = os.path.abspath(str(folder or ''))
    if not os.path.isdir(folder):
        raise MentorError('找不到这个文件夹')
    f = files_of(folder)
    if not f['atoms'] or not f['persona']:
        raise MentorError('这个文件夹里要有「观点库」和「老师画像」两份文件（文件名里带这几个字），先完成蒸馏再导入')
    for m in list_mentors(chats=False):
        if _same(m['folder'], folder):
            raise MentorError(f'「{m["name"]}」已经导入过了')
    m = {'id': uuid.uuid4().hex[:10], 'name': Path(folder).name, 'folder': folder, 'created': time.strftime('%Y-%m-%d %H:%M:%S')}
    m['sources'] = resolve_sources(folder)
    _save_json(_mdir(m['id']) / 'mentor.json', m)
    return m


def resolve_sources(folder):
    """来源表 S01… → 课程笔记 → 笔记属性里的「视频文件」（找不到的标出来）。
    在软件里蒸馏的老师（distill.json 里记着每节课的视频）：笔记找不到、或者还没剪藏，就用记下的视频。"""
    md = _read(files_of(folder)['atoms']) if Path(folder).is_dir() else ''
    srcs = parse_sources(md)
    notes = _find_notes(_vault_of(folder), [s['note'] for s in srcs])
    known = {}
    for m in list_mentors(chats=False):
        if _same(m['folder'], folder):
            known = {x['sid']: x['path'] for x in (_load_json(_mdir(m['id']) / 'distill.json') or {}).get('lessons') or []}
    out = []
    for s in srcs:
        note = notes.get(s['note'])
        video = _frontmatter_video(note) if note else ''
        if not (video and os.path.isfile(video)) and known.get(s['sid']):
            video = os.path.normpath(known[s['sid']])
        out.append(dict(s, notePath=str(note) if note else '', path=video, found=bool(video and os.path.isfile(video)), text=bool(video) and is_text(video)))
    return out


def refresh(mid):
    m = get(mid)
    m['sources'] = resolve_sources(m['folder'])
    _save_json(_mdir(mid) / 'mentor.json', m)
    return m


def _rmtree(d):
    """删文件夹；文件正被别的程序（杀毒软件扫描、刚写完）占着时等一下再试，最多约 3 秒。"""
    for k in range(8):
        try:
            shutil.rmtree(d)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if k == 7:
                raise
            time.sleep(0.4)


def remove(mid):
    """删掉软件里这位老师和他的对话记录；Obsidian 里的文件不动。"""
    d = _mdir(mid)
    if d.is_dir():
        _rmtree(d)


def overview(mid):
    """打开一位老师时界面要的：来源（出处跳视频用）、观点编号和出处（出处显示原文用）。"""
    m = get(mid)
    f = files_of(m['folder'])
    atoms = parse_atoms(_read(f['atoms']))
    return {'mentor': m, 'files': {k: str(v) if v else '' for k, v in f.items()},
            'atoms': {k: {'src': v['src'], 'text': v['text'], 't': v['t'], 'p': v['p'], 'sid': v['sid'], 'kind': v['kind']} for k, v in atoms.items()}}


# ------------------------------------------------------------ 找相关资料 ----
def _grams(text):
    t = re.sub(r'[\s\W_]+', '', str(text).lower())
    return {t[i:i + 2] for i in range(len(t) - 1)} or ({t} if t else set())


def win_label(w):
    """原文片段的出处：音视频「S02 01:00–01:45」，文字资料「S03 第12–14段」。"""
    if w.get('unit'):
        return f'{w["sid"]} 第{w["start"]}{w["unit"]}' if w['start'] == w['end'] else f'{w["sid"]} 第{w["start"]}–{w["end"]}{w["unit"]}'
    return f'{w["sid"]} {_ts(w["start"])}–{_ts(w["end"])}'


def _text_windows(s):
    try:
        t = text_units(s['path'])
    except Exception:  # noqa: BLE001  读不出就跳过
        return []
    out, cur = [], None
    for u in t['units']:
        if cur is None:
            cur = {'sid': s['sid'], 'start': u['n'], 'end': u['n'], 'text': u['text'], 'unit': t['unit']}
        else:
            cur['end'] = u['n']
            cur['text'] += '\n' + u['text']
        if len(cur['text']) >= WINDOW_CHARS or t['unit'] == '页':
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


def _windows(m):
    """原文切成片段：字幕约 45 秒一段 [{sid, start, end, text}]（秒）；文字资料约 400 字一段（start / end 是段号，unit 是「段」或「页」）。"""
    out = []
    for s in m.get('sources') or []:
        if not s.get('path'):
            continue
        if is_text(s['path']):
            out += _text_windows(s)
            continue
        try:
            d = docs.load(s['path']) or {}
        except Exception:  # noqa: BLE001  某节课字幕读不出就跳过
            continue
        cur = None
        for seg in d.get('segments') or []:
            if cur is None:
                cur = {'sid': s['sid'], 'start': float(seg['start']), 'end': float(seg['end']), 'text': seg['text']}
            else:
                cur['end'] = float(seg['end'])
                cur['text'] += seg['text']
            if cur['end'] - cur['start'] >= WINDOW_SECONDS:
                out.append(cur)
                cur = None
        if cur:
            out.append(cur)
    return out


def rank(query, items, key, k):
    """按和问题的字词重合打分，取前 k 个（常见的字词权重低）。"""
    q = _grams(query)
    if not q or not items:
        return []
    grams = [_grams(key(x)) for x in items]
    n = len(items)
    df = {}
    for g in grams:
        for w in g & q:
            df[w] = df.get(w, 0) + 1
    scored = []
    for x, g in zip(items, grams):
        hit = g & q
        if hit:
            s = sum(math.log(1 + n / df[w]) for w in hit) / (1 + math.log(1 + len(g)))
            scored.append((s, x))
    scored.sort(key=lambda p: -p[0])
    return [x for _, x in scored[:k]]


CHAT_RULES = (
    '1. 严格按上面「老师画像」第一节的规则回答，用老师的思路、语气和回应方式（见画像「他怎么回应一个人的困惑」「说话风格」）：先听懂这个人、回应他的处境和感受，再讲道理。\n'
    '2. 出处只能写成 ［S02 19:54］（音视频原文时间）、［S03 第12段］（文字资料的段落，PDF 是页）或 ［S02-21］（观点库编号，S02-Y3 原话、S02-B1 比喻和故事、S02-W1 问答），而且只能引用上面给出的编号、时间和段落，不许编造出处。\n'
    '3. 资料里没讲的，按规则标【AI 推测：……不是他资料里的内容】，说清从哪几条推出来；推不出来就照实说资料里没讲。\n'
    '4. 用户没要求时，不加 AI 自己的观点；用户要求时另起一段标【AI 自己的看法，不代表老师】。\n'
    '5. 用中文；能用老师的原话、比喻和故事就用（照录，标出处），不要把他的话改写成书面腔或 AI 腔，不列一大串要点；可以在最后给一个马上能做的练习或觉察。\n'
    '6. 对方流露严重痛苦、绝望或自伤、轻生的念头时，先关心、接住他，明确建议他联系身边信任的人和专业心理援助（当地心理援助热线、医院心理科；有紧急危险打当地急救电话），不要只用灵性道理带过。\n'
    '7. 被问到你是不是老师本人时照实说：你是 AI 按老师的资料模拟的，不是他本人。'
)


def build_messages(m, history, question, extra=''):
    """交给 AI 的资料和对话。返回 (messages, 用了哪些资料的说明)。extra：圆桌讨论时追加的说明（只代表这一位老师等）。"""
    f = files_of(m['folder'])
    persona, distill, atoms_md = _read(f['persona']), _read(f['distill']), _read(f['atoms'])
    if not persona or not atoms_md:
        raise MentorError('老师文件夹里的「老师画像」或「观点库」不见了，请检查 Obsidian 里的文件')
    atoms = list(parse_atoms(atoms_md).values())
    last_q = next((h['content'] for h in reversed(history or []) if h.get('role') == 'user'), '')
    query = question + ' ' + last_q
    total = sum(len(a['text']) + len(a['cond']) for a in atoms)
    picked = atoms if total <= ATOMS_ALL_LIMIT else rank(query, atoms, lambda a: a['text'] + a['cond'], ATOMS_PICK)
    wins = rank(query, _windows(m), lambda w: w['text'], WINDOWS_PICK)
    wins.sort(key=lambda w: (w['sid'], w['start']))
    names = '\n'.join(f'- {s["sid"]}：{s["title"]}' for s in m.get('sources') or [])
    atom_lines = '\n'.join(atom_line(a) for a in picked)
    win_lines = '\n'.join(f'- [{win_label(w)}] {w["text"]}' for w in wins) or '（没有找到特别相关的原文）'
    system = (
        f'{persona}\n\n---\n# 资料（编号对应）\n{names}\n\n'
        + (f'---\n# 课程蒸馏（整体体系）\n{distill}\n\n' if distill else '')
        + f'---\n# 观点库（{"全部" if picked is atoms else "和这个问题相关的部分"}，编号可作出处）\n{atom_lines}\n\n'
        f'---\n# 和这个问题相关的原文（音视频的字幕片段、文字资料的段落）\n{win_lines}\n\n'
        '---\n# 这一次回答的要求\n' + CHAT_RULES
        + (f'\n\n---\n{extra}' if extra else '')
    )
    messages = [{'role': 'system', 'content': system}]
    for h in (history or [])[-HISTORY_TURNS:]:
        if h.get('role') in ('user', 'assistant') and h.get('content'):
            messages.append({'role': h['role'], 'content': h['content']})
    messages.append({'role': 'user', 'content': question})
    used = {'atoms': len(picked), 'atoms_total': len(atoms), 'windows': len(wins), 'chars': len(system)}
    return messages, used


def quote(mid, sid, t, p=None):
    """出处那一秒前后的原话（界面上鼠标停在出处上时显示）；文字资料（p 是第几段 / 页）是那一段和后面两段。"""
    m = get(mid)
    s = next((x for x in m.get('sources') or [] if x['sid'] == sid), None)
    if not s or not s.get('path'):
        return {'title': s['title'] if s else sid, 'text': '', 'path': ''}
    if is_text(s['path']):
        try:
            tu = text_units(s['path'])
        except Exception:  # noqa: BLE001
            return {'title': s['title'], 'text': '', 'path': s['path'], 'text_source': True}
        p = int(p or 1)
        lines = [f'[第{u["n"]}{tu["unit"]}] {u["text"]}' for u in tu['units'] if p <= u['n'] < p + 3]
        return {'title': s['title'], 'text': '\n'.join(lines), 'path': s['path'], 'text_source': True}
    d = docs.load(s['path']) or {}
    segs = d.get('segments') or []
    i = max((k for k, x in enumerate(segs) if float(x['start']) <= float(t) + 0.5), default=0)
    lines = [f'[{_ts(x["start"])}] {x["text"]}' for x in segs[i:i + 4]]
    return {'title': s['title'], 'text': '\n'.join(lines), 'path': s['path']}


# ----------------------------------------------------------------- 对话 ----
def _cpath(mid, cid):
    if not re.fullmatch(r'[0-9a-f]{12}', str(cid or '')):
        raise MentorError('没有这个对话')
    return _mdir(mid) / 'chats' / f'{cid}.json'


def list_chats(mid):
    d = _mdir(mid) / 'chats'
    out = []
    if d.is_dir():
        for p in d.glob('*.json'):
            c = _load_json(p)
            if c:
                out.append({'id': c['id'], 'title': c.get('title') or '新对话', 'updated': c.get('updated', ''), 'count': len(c.get('messages') or [])})
    return sorted(out, key=lambda c: c['updated'], reverse=True)


def load_chat(mid, cid):
    c = _load_json(_cpath(mid, cid))
    if not c:
        raise MentorError('这个对话不见了')
    return c


def new_chat(mid, title=''):
    get(mid)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    c = {'id': uuid.uuid4().hex[:12], 'title': title or '新对话', 'created': now, 'updated': now, 'messages': []}
    _save_json(_cpath(mid, c['id']), c)
    return c


def append(mid, cid, *msgs):
    """往对话里加消息（读最新的再写，界面和后台不会互相覆盖）。第一句问题当标题。"""
    c = load_chat(mid, cid)
    for msg in msgs:
        c['messages'].append(dict(msg, time=msg.get('time') or time.strftime('%Y-%m-%d %H:%M:%S')))
    if c.get('title') in ('', '新对话'):
        q = next((x['content'] for x in c['messages'] if x['role'] == 'user'), '')
        c['title'] = re.sub(r'\s+', ' ', q).strip()[:30] or '新对话'
    c['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _save_json(_cpath(mid, cid), c)
    return c


def rename_chat(mid, cid, title):
    c = load_chat(mid, cid)
    c['title'] = str(title or '').strip()[:60] or c['title']
    _save_json(_cpath(mid, cid), c)
    return c


def delete_chat(mid, cid):
    _cpath(mid, cid).unlink(missing_ok=True)


def delete_all_chats(mid):
    d = _mdir(mid) / 'chats'
    n = len(list(d.glob('*.json'))) if d.is_dir() else 0
    if d.is_dir():
        _rmtree(d)
    return n


def search(mid, q):
    """在这位老师的所有对话里找（标题和内容）。"""
    q = str(q or '').strip().lower()
    out = []
    for c in list_chats(mid):
        if not q:
            out.append(c)
            continue
        full = load_chat(mid, c['id'])
        if q in c['title'].lower() or any(q in (x.get('content') or '').lower() for x in full['messages']):
            out.append(c)
    return out


# ------------------------------------------------------- 收藏到 Obsidian ----
def _backup(path):
    folder = config.DATA_DIR / 'backups' / '对话精选'
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, folder / f'{Path(path).parent.name}-{time.strftime("%Y%m%d-%H%M%S")}.md')
    olds = sorted(folder.glob(f'{Path(path).parent.name}-*.md'))
    for old in olds[:-30]:
        old.unlink(missing_ok=True)


def _fav_block(c, i):
    msgs = c['messages']
    a = msgs[i]
    q = next((msgs[k]['content'] for k in range(i - 1, -1, -1) if msgs[k]['role'] == 'user'), '')
    quoted = '\n'.join('> ' + line if line.strip() else '>' for line in q.strip().splitlines())
    return (f'\n\n---\n\n## {re.sub(chr(10), " ", q.strip())[:40]}\n\n'
            f'<small>{a.get("time", "")[:16]} · {a.get("model", "")} · 对话「{c.get("title", "")}」</small>\n\n'
            f'**问：**\n\n{quoted}\n\n**答：**\n\n{a["content"].strip()}\n')


def _fav_write(m, blocks):
    """把几条问答追加到老师文件夹里的「对话精选.md」（写之前备份一次）。"""
    path = Path(m['folder']) / FAVORITES
    if path.is_file():
        _backup(path)
        old = _read(path).rstrip()
    else:
        atoms = files_of(m['folder'])['atoms']
        old = (f'---\n老师: {m["name"]}\n类型: 对话精选\ntags:\n  - 灵修蒸馏\n---\n\n# {m["name"]}：对话精选\n\n'
               f'> 在猛人灵修「老师工作台」里和{m["name"]}的老师画像对话时，收藏的回答。出处 ［S02 19:54］ 对应 [[{atoms.stem if atoms else "01、观点库（逐节提取）"}]] 里的资料编号（［S03 第12段］是文字资料的第几段）。'
               f'回答是 AI 按老师画像写的，不等于老师原话，也不是你的结论。')
    path.write_text(old + ''.join(blocks), encoding='utf-8')
    return path


def save_favorite(mid, cid, index):
    """把第 index 条（AI 的回答）连同它前面的问题，追加到老师文件夹里的「对话精选.md」。"""
    m = get(mid)
    c = load_chat(mid, cid)
    msgs = c['messages']
    if not (0 <= int(index) < len(msgs)) or msgs[int(index)]['role'] != 'assistant':
        raise MentorError('只能收藏老师的回答')
    path = _fav_write(m, [_fav_block(c, int(index))])
    msgs[int(index)]['saved'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _save_json(_cpath(mid, cid), c)
    return dict(favorites(mid), chat=c, count=1)


def save_all(mid, cid):
    """一键收藏整个对话：还没收藏的回答按顺序一次写进去，已经收藏的不重复。"""
    m = get(mid)
    c = load_chat(mid, cid)
    todo = [i for i, x in enumerate(c['messages']) if x['role'] == 'assistant' and not x.get('saved') and (x.get('content') or '').strip()]
    if not todo:
        return dict(favorites(mid), chat=c, count=0)
    _fav_write(m, [_fav_block(c, i) for i in todo])
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    for i in todo:
        c['messages'][i]['saved'] = now
    _save_json(_cpath(mid, cid), c)
    return dict(favorites(mid), chat=c, count=len(todo))


def favorites(mid):
    """对话精选在哪、有没有、在 Obsidian 里打开的链接（库外面的文件没有链接，就在资源管理器里显示）。"""
    m = get(mid)
    path = Path(m['folder']) / FAVORITES
    vault = _vault_of(m['folder'])
    uri = ''
    if vault and _norm(path).startswith(_norm(vault).rstrip('\\/') + os.sep):
        from urllib.parse import quote as q
        rel = os.path.relpath(path, vault).replace('\\', '/')
        uri = f'obsidian://open?vault={q(Path(vault).name)}&file={q(rel)}'
    return {'file': str(path), 'exists': path.is_file(), 'uri': uri}
