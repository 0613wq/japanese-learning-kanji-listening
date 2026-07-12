# ═══════════════════════════════════════════════════
# 日语单词听力练习系统 v5.1
#   ✧ 词类分离（汉字词 / 动词）
#   ✧ 临时单词表（集中复习卡词）
#   ✧ 释义显示 + AI 提示词生成
#   ✧ GitHub Gist 云端同步
#
# v5.1 修复：
#   1. 【核心】Streamlit Cloud 段错误的根因是 requirements.txt 未锁版本，
#      老版 streamlit==1.39 被 uv 装上了全新的 pyarrow==25（二进制不兼容），
#      进程启动即 Segmentation fault。→ 必须同时更新 requirements.txt（见文件）。
#   2. AI 释义页「全选/清空」按钮逻辑：原来在 multiselect 实例化之后才改
#      picked，导致界面与实际选词不一致；现改为在控件实例化前写入 state。
#   3. 练习会话：若所选词全部已是 1 级（掌握），初始队列为空会白屏卡死；
#      现在会自动跳过空组 / 直接进入完成页。
#   4. 词汇编辑表：保存/重分组/清临时后给 data_editor 换 key，
#      避免旧编辑状态叠加到新数据上造成错乱。
#   5. 循环朗读页并发提示与实际并发数(6)不一致的文案修正。
# ═══════════════════════════════════════════════════
import streamlit as st
import streamlit.components.v1 as components
import edge_tts
import asyncio
import io
import base64
import random
import datetime
import requests
import re
import csv as pycsv
import io as pyio
import pandas as pd

st.set_page_config(
    page_title="日语听力练习",
    page_icon="🇯🇵",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
  .word-box, .meaning-box {
      text-align: center; font-weight: 700;
      color: #2c3e50;
      padding: 22px 10px 18px;
      min-height: 110px; line-height: 1.15;
      font-family: 'Helvetica Neue', Arial, 'Hiragino Sans', sans-serif;
      border: 2px solid #e6ebf1; border-radius: 12px;
      margin: 4px 2px; background: #fafbfc;
      display: flex; align-items: center; justify-content: center;
  }
  .word-box    { font-size: 52px; letter-spacing: 3px; }
  .meaning-box { font-size: 26px; letter-spacing: 1px; color:#34495e; }
  .word-box.hidden, .meaning-box.hidden {
      color: #ecf0f1; text-shadow: 0 0 24px #bdc3c7;
  }
  div.stButton > button { border-radius: 10px; }
  audio { width: 100% !important; }
  .cat-pill {
      display:inline-block; padding:2px 10px; border-radius:12px;
      background:#eef3f9; color:#2c3e50; font-size:12px; margin-left:6px;
  }
</style>
""", unsafe_allow_html=True)

VOICES = {
    "🎀 七海 Nanami（女声·自然）": "ja-JP-NanamiNeural",
    "🎵 圭太 Keita（男声·自然）":  "ja-JP-KeitaNeural",
}
SPEEDS = {
    "🐢 0.75× 慢速": "-25%",
    "🐇 0.9×  稍慢": "-10%",
    "▶  1.0×  正常": "+0%",
    "⚡ 1.15× 稍快": "+15%",
    "🚀 1.3×  快速": "+30%",
}

# ── 中文语音（用于循环朗读时读释义）──
CN_VOICES = {
    "🎀 晓晓 Xiaoxiao（女声·自然）": "zh-CN-XiaoxiaoNeural",
    "🎵 云希 Yunxi（男声·自然）":   "zh-CN-YunxiNeural",
    "🎀 晓伊 Xiaoyi（女声·柔和）":   "zh-CN-XiaoyiNeural",
    "🎵 云健 Yunjian（男声·沉稳）":  "zh-CN-YunjianNeural",
}

# ── 词类定义 ──
CATEGORIES = {
    "kanji": "🈶 汉字词",
    "verb":  "🏃 动词",
}
CAT_NAMES_CN = {"kanji": "汉字词", "verb": "动词"}

GIST_FILENAME    = "japanese_words.csv"
RECORDS_FILENAME = "practice_records.json"


# ═══════════════════════════════════════════════════
# GitHub Gist 工具函数
# ═══════════════════════════════════════════════════
def _gist_cfg() -> tuple[str | None, str | None]:
    try:
        token   = st.secrets["github"]["token"]
        gist_id = st.secrets["github"].get("gist_id", "")
        return token, gist_id or None
    except Exception:
        return None, None


def _gist_enabled() -> bool:
    token, _ = _gist_cfg()
    return bool(token)


def _gist_headers() -> dict:
    token, _ = _gist_cfg()
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


def gist_find(token: str) -> dict | None:
    page = 1
    while True:
        r = requests.get(
            "https://api.github.com/gists",
            headers=_gist_headers(),
            params={"per_page": 100, "page": page},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json()
        if not items:
            return None
        for item in items:
            if GIST_FILENAME in item.get("files", {}):
                return {"id": item["id"], "updated_at": item["updated_at"]}
        page += 1


def gist_load(gist_id: str) -> tuple[str, str]:
    """返回 (csv_text, records_json_text)；缺失的文件返回空串。"""
    r = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers=_gist_headers(),
        timeout=10,
    )
    r.raise_for_status()
    files = r.json().get("files", {})
    csv_text     = ""
    records_text = ""
    if GIST_FILENAME in files:
        raw = requests.get(files[GIST_FILENAME]["raw_url"], timeout=10)
        raw.raise_for_status()
        csv_text = raw.text
    if RECORDS_FILENAME in files:
        raw = requests.get(files[RECORDS_FILENAME]["raw_url"], timeout=10)
        raw.raise_for_status()
        records_text = raw.text
    return csv_text, records_text


def gist_save(csv_text: str, records_json: str, gist_id: str | None = None) -> str:
    payload = {
        "description": "日语单词听力练习 — 词库自动备份",
        "files": {
            GIST_FILENAME:    {"content": csv_text},
            RECORDS_FILENAME: {"content": records_json or "[]"},
        },
    }
    if gist_id:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers=_gist_headers(),
            json=payload,
            timeout=10,
        )
    else:
        payload["public"] = False
        r = requests.post(
            "https://api.github.com/gists",
            headers=_gist_headers(),
            json=payload,
            timeout=10,
        )
    r.raise_for_status()
    return r.json()["id"]


def do_gist_load():
    token, gist_id = _gist_cfg()
    if not token:
        st.error("❌ 未配置 GitHub Token，请查看侧边栏说明")
        return

    with st.spinner("☁️ 正在从 GitHub Gist 加载..."):
        try:
            if not gist_id:
                info = gist_find(token)
                if not info:
                    st.warning(f"⚠️ 在你的 Gist 里没有找到 `{GIST_FILENAME}`，请先保存一次")
                    return
                gist_id = info["id"]
            csv_text, records_text = gist_load(gist_id)
        except Exception as e:
            st.error(f"❌ 加载失败：{e}")
            return

    store = st.session_state.store
    n     = store.import_csv(csv_text)
    # 覆盖式加载 records（避免累积旧记录）
    try:
        import json as _j
        parsed = _j.loads(records_text) if records_text.strip() else []
        if isinstance(parsed, list):
            store.records = parsed
    except Exception:
        pass
    n_rec = len(store.records)

    st.session_state.gist_id        = gist_id
    st.session_state.gist_last_sync = datetime.datetime.now().strftime("%H:%M:%S")
    if n:
        st.success(f"✅ 已加载 {n} 个新词  ·  同步记录 {n_rec} 条")
    else:
        st.info(f"✅ 已同步 ·  记录 {n_rec} 条（词库已是最新）")
    st.rerun()


def do_gist_save():
    token, cfg_gist_id = _gist_cfg()
    if not token:
        st.error("❌ 未配置 GitHub Token，请查看侧边栏说明")
        return

    store = st.session_state.store
    if not store.words and not store.records:
        st.warning("⚠️ 词库为空，无需保存")
        return

    gist_id = st.session_state.get("gist_id") or cfg_gist_id

    with st.spinner("☁️ 正在保存到 GitHub Gist..."):
        try:
            if not gist_id:
                info = gist_find(token)
                gist_id = info["id"] if info else None
            new_id = gist_save(store.export_csv(), store.export_records_json(), gist_id)
        except Exception as e:
            st.error(f"❌ 保存失败：{e}")
            return

    st.session_state.gist_id        = new_id
    st.session_state.gist_last_sync = datetime.datetime.now().strftime("%H:%M:%S")

    if not gist_id:
        st.success(f"✅ 已新建 Secret Gist！")
        st.info(
            f"💡 **可选加速**：把下面这个 Gist ID 填入 secrets.toml 的 `gist_id`。\n\n`{new_id}`"
        )
    else:
        st.success(f"✅ 已保存 {len(store.words)} 词  ·  {len(store.records)} 条记录到 Gist")


# ═══════════════════════════════════════════════════
# WordStore  ——  支持 category / meaning / in_temp
# ═══════════════════════════════════════════════════
def _ensure_fields(w: dict) -> dict:
    w.setdefault('reading',    '')
    w.setdefault('meaning',    '')
    w.setdefault('category',   'kanji')
    w.setdefault('in_temp',    False)
    w.setdefault('added_date', datetime.date.today().isoformat())
    w.setdefault('long_level', 0)
    return w


class WordStore:
    def __init__(self):
        self.words = []
        self.records = []   # 难词记录：[{id, title, category, words, source, created}, ...]

    # ── 分类过滤 ──
    def by_category(self, cat):
        return [w for w in self.words if w.get('category', 'kanji') == cat]

    def _exists(self, cat):
        return {w['word'] for w in self.words if w.get('category', 'kanji') == cat}

    def _next_grp(self, gs, cat):
        n = len(self.by_category(cat))
        return (n // gs) + 1

    # ── 增删 ──
    def add_text(self, text, gs, cat):
        ex = self._exists(cat)
        added = 0
        today = datetime.date.today().isoformat()
        for line in text.splitlines():
            w = line.strip()
            if w and w not in ex:
                self.words.append({
                    'word': w, 'reading': '', 'meaning': '',
                    'group': self._next_grp(gs, cat),
                    'long_level': 0,
                    'added_date': today,
                    'category': cat,
                    'in_temp': False,
                })
                ex.add(w)
                added += 1
        return added

    def import_csv(self, text, gs=33):
        added = 0
        reader = pycsv.reader(pyio.StringIO(text))
        ex_by_cat = {}
        for row in reader:
            if not row:
                continue
            if row[0].strip().lower() == 'word':
                continue
            word = row[0].strip()
            if not word:
                continue
            try:
                group = int(row[1].strip()) if len(row) > 1 and row[1].strip() else 1
            except Exception:
                group = 1
            try:
                level = max(0, min(3, int(row[2].strip()))) if len(row) > 2 and row[2].strip() else 0
            except Exception:
                level = 0
            date    = row[3].strip() if len(row) > 3 and row[3].strip() else datetime.date.today().isoformat()
            reading = row[4].strip() if len(row) > 4 else ''
            meaning = row[5].strip() if len(row) > 5 else ''
            cat     = row[6].strip() if len(row) > 6 and row[6].strip() else 'kanji'
            if cat not in CATEGORIES:
                cat = 'kanji'
            try:
                in_temp = bool(int(row[7])) if len(row) > 7 and row[7].strip() else False
            except Exception:
                in_temp = False

            ex = ex_by_cat.setdefault(cat, self._exists(cat))
            if word in ex:
                continue
            self.words.append({
                'word': word, 'reading': reading, 'meaning': meaning,
                'group': group, 'long_level': level, 'added_date': date,
                'category': cat, 'in_temp': in_temp,
            })
            ex.add(word)
            added += 1
        return added

    def export_csv(self):
        out = pyio.StringIO()
        writer = pycsv.writer(out)
        writer.writerow(['word', 'group', 'long_level', 'added_date',
                         'reading', 'meaning', 'category', 'in_temp'])
        for w in self.words:
            writer.writerow([
                w['word'], w['group'], w['long_level'], w['added_date'],
                w.get('reading', ''), w.get('meaning', ''),
                w.get('category', 'kanji'), int(bool(w.get('in_temp', False))),
            ])
        return out.getvalue()

    # ── 查询 & 更新 ──
    def _find(self, word, cat):
        for w in self.words:
            if w['word'] == word and w.get('category', 'kanji') == cat:
                return w
        return None

    def get_long(self, word, cat):
        w = self._find(word, cat)
        return w['long_level'] if w else 0

    def update_long(self, word, cat, level):
        w = self._find(word, cat)
        if w:
            w['long_level'] = level

    def update_word_meaning(self, word, cat, meaning):
        w = self._find(word, cat)
        if w:
            w['meaning'] = meaning
            return True
        return False

    def toggle_temp(self, word, cat, val=None):
        w = self._find(word, cat)
        if w:
            w['in_temp'] = val if val is not None else not w.get('in_temp', False)
            return w['in_temp']
        return False

    def clear_temp(self, cat):
        n = 0
        for w in self.words:
            if w.get('category', 'kanji') == cat and w.get('in_temp', False):
                w['in_temp'] = False
                n += 1
        return n

    def delete_words(self, word_set, cat):
        self.words = [w for w in self.words
                      if not (w['word'] in word_set and w.get('category', 'kanji') == cat)]

    def regroup(self, gs, cat, start_gid=1):
        cat_words = [w for w in self.words if w.get('category', 'kanji') == cat]
        for i, w in enumerate(cat_words):
            w['group'] = start_gid + (i // gs)

    def get_groups(self, cat):
        gs = {}
        for w in self.by_category(cat):
            gs.setdefault(w['group'], []).append(w)
        return dict(sorted(gs.items()))

    def filter(self, levels, groups, cat, only_temp=False):
        return [w for w in self.by_category(cat)
                if w['long_level'] in levels and w['group'] in groups
                and (not only_temp or w.get('in_temp', False))]

    def stats(self, cat):
        cw = self.by_category(cat)
        lv = [0, 0, 0, 0]
        for w in cw:
            lv[w['long_level']] += 1
        temp = sum(1 for w in cw if w.get('in_temp', False))
        return {'total': len(cw), 'levels': lv,
                'groups': len(set(w['group'] for w in cw)),
                'temp': temp,
                'records': sum(1 for r in self.records if r.get('category') == cat)}

    # ── 难词记录 ──
    RECORD_LIMIT = 50

    def add_record(self, category, words, source, title=None):
        """新增一条记录，enforce 上限 50（超出删最旧）。"""
        import uuid as _uuid
        now = datetime.datetime.now()
        rec = {
            'id':       f"rec_{now.strftime('%Y%m%d_%H%M%S')}_{_uuid.uuid4().hex[:6]}",
            'title':    title or _default_record_title(category, words, source),
            'category': category,
            'words':    list(words),
            'source':   dict(source or {}),
            'created':  now.isoformat(timespec='seconds'),
        }
        self.records.append(rec)
        # 上限：按创建时间倒序保留最新 N 条
        if len(self.records) > self.RECORD_LIMIT:
            self.records.sort(key=lambda r: r.get('created', ''), reverse=True)
            self.records = self.records[:self.RECORD_LIMIT]
        return rec

    def delete_record(self, rec_id):
        before = len(self.records)
        self.records = [r for r in self.records if r.get('id') != rec_id]
        return before - len(self.records)

    def get_records(self, category=None):
        recs = self.records if category is None \
               else [r for r in self.records if r.get('category') == category]
        return sorted(recs, key=lambda r: r.get('created', ''), reverse=True)

    def export_records_json(self):
        import json as _j
        return _j.dumps(self.records, ensure_ascii=False, indent=2)

    def import_records_json(self, text):
        import json as _j
        if not (text or '').strip():
            return 0
        try:
            data = _j.loads(text)
            if not isinstance(data, list):
                return 0
        except Exception:
            return 0
        existing = {r.get('id') for r in self.records}
        added = 0
        for rec in data:
            if not isinstance(rec, dict): continue
            rid = rec.get('id')
            if not rid or rid in existing: continue
            if not rec.get('words'): continue
            self.records.append(rec)
            existing.add(rid)
            added += 1
        return added


# ── 记录标题辅助 ──
def _format_groups(gids):
    """[1,2,3,5,7,8] -> '1-3,5,7-8'，无则返回 '全部'。"""
    if not gids:
        return "全部"
    gs = sorted(set(int(g) for g in gids))
    ranges = []
    start = prev = gs[0]
    for g in gs[1:]:
        if g == prev + 1:
            prev = g
        else:
            ranges.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = g
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)


def _format_levels(lvs):
    """[0,1,2,3] -> '0-3' ; [1,3] -> '1,3'。"""
    if not lvs:
        return "全部"
    ls = sorted(set(int(l) for l in lvs))
    if ls == list(range(ls[0], ls[-1] + 1)):
        return f"{ls[0]}-{ls[-1]}" if ls[0] != ls[-1] else str(ls[0])
    return ",".join(str(l) for l in ls)


def _default_record_title(category, words, source):
    parts = [CAT_NAMES_CN.get(category, category)]
    if source.get('from_record'):
        parts.append("← 记录再录")
    if source.get('only_temp'):
        parts.append("临时列表")
    if source.get('groups'):
        parts.append(f"组[{_format_groups(source['groups'])}]")
    if source.get('levels'):
        parts.append(f"等级[{_format_levels(source['levels'])}]")
    parts.append(f"{len(words)}词")
    parts.append(datetime.datetime.now().strftime("%m-%d %H:%M"))
    return " · ".join(parts)


# ═══════════════════════════════════════════════════
# SessionManager  ——  练习会话
# ═══════════════════════════════════════════════════
_INIT_STATE    = {0: 3, 1: 1, 2: 2, 3: 3}
_REAPPEAR      = {3: [('near', 4, 8), ('medium', 14, 22)], 2: [('medium', 10, 17)], 1: []}
_CARRYOVER_SLOTS = {3: 2, 2: 1}
FAIL_THRESHOLD = 3


class SessionManager:
    def __init__(self, words, ordered=False):
        self.ordered   = ordered
        self.word_map  = {w['word']: w for w in words}
        self.state     = {w['word']: _INIT_STATE[w['long_level']] for w in words}
        self.done      = {w['word']: (w['long_level'] == 1) for w in words}
        self.dgr       = {w['word']: {} for w in words}
        self.fail_cnt  = {w['word']: 0 for w in words}
        self.history   = {w['word']: [] for w in words}
        gmap = {}
        for w in words:
            gmap.setdefault(w['group'], []).append(w['word'])
        self.group_order = sorted(gmap.keys())
        self.gmap        = gmap
        self.g_idx       = 0
        self.carryover   = {}
        self.last_carryover = []
        self.queue       = []
        self.q_pos       = 0
        self.in_loop     = False
        self._build_queue()
        # 修复：若起始组全部已是 1 级掌握（队列为空），自动推进，
        # 否则界面会白屏卡死在空队列上
        while not self.queue and not self.is_done():
            if self._advance() == 'session_done':
                break

    def _build_queue(self):
        gid  = self.group_order[self.g_idx]
        main = [w for w in self.gmap[gid] if not self.done[w]]
        if not self.ordered:
            random.shuffle(main)
        combined = list(main)
        for w, slots in self.carryover.items():
            for _ in range(slots):
                combined.insert(random.randint(0, len(combined)), w)
        self.carryover = {}
        self.queue = combined
        self.q_pos = 0

    def current_word(self):
        return self.queue[self.q_pos] if self.q_pos < len(self.queue) else None

    def _reinsert(self, word):
        for (_, lo, hi) in _REAPPEAR.get(self.state[word], []):
            dist = max(3, random.randint(lo, hi))
            pos  = min(self.q_pos + dist, len(self.queue))
            self.queue.insert(pos, word)

    def rate(self, word, button):
        self.history[word].append(button)
        if button == 3:
            self.fail_cnt[word] = self.fail_cnt.get(word, 0) + 1
        curr = self.state[word]
        if button < curr:
            self.state[word] = button
            self.dgr[word]   = {}
        elif button > curr:
            self.dgr[word][button] = self.dgr[word].get(button, 0) + 1
            if self.dgr[word][button] >= 3:
                self.state[word] = min(3, curr + 1)
                self.dgr[word]   = {}
        else:
            self.dgr[word] = {}
        if self.state[word] == 1:
            self.done[word] = True
        else:
            self.done[word] = False
            self._reinsert(word)
        self.q_pos += 1
        if self.is_done():
            return 'session_done'
        if self.q_pos >= len(self.queue):
            return self._advance()
        return 'continue'

    def _advance(self):
        if self.in_loop:
            undone = [w for w, d in self.done.items() if not d]
            if undone:
                random.shuffle(undone)
                self.queue = undone
                self.q_pos = 0
                return 'continue'
            return 'session_done'
        gid = self.group_order[self.g_idx]
        new_carry = {}
        self.last_carryover = []
        for w in self.gmap[gid]:
            st_val = self.state[w]
            if not self.done[w] and (st_val in _CARRYOVER_SLOTS or
                                     self.fail_cnt.get(w, 0) >= FAIL_THRESHOLD):
                slots = _CARRYOVER_SLOTS.get(st_val, 1)
                new_carry[w] = slots
                self.last_carryover.append(w)
        self.g_idx += 1
        if self.g_idx < len(self.group_order):
            self.carryover = new_carry
            self._build_queue()
            return 'group_done'
        else:
            undone = [w for w, d in self.done.items() if not d]
            if undone:
                self.in_loop = True
                self.last_carryover = list(undone)
                random.shuffle(undone)
                self.queue = undone
                self.q_pos = 0
                return 'group_done'
            return 'session_done'

    def is_done(self):
        return all(self.done.values())

    def prev(self):
        if self.q_pos > 0:
            self.q_pos -= 1
            return True
        return False

    def skip(self):
        word = self.current_word()
        if word:
            self._reinsert(word)
        self.q_pos += 1
        if self.is_done():
            return 'session_done'
        if self.q_pos >= len(self.queue):
            return self._advance()
        return 'continue'

    def current_gid(self):
        if self.in_loop or self.g_idx >= len(self.group_order):
            return None
        return self.group_order[self.g_idx]

    def word_detail(self, word):
        return {'state':    self.state.get(word, 1),
                'hist':     self.history.get(word, []),
                'dgr_cnt':  sum(self.dgr.get(word, {}).values()),
                'fail_cnt': self.fail_cnt.get(word, 0)}

    def stats(self):
        done  = sum(1 for d in self.done.values() if d)
        total = len(self.done)
        stuck = sum(1 for w in self.fail_cnt
                    if self.fail_cnt[w] >= FAIL_THRESHOLD)
        return {'done': done, 'total': total,
                'undone_count': total - done,
                'queue_rem':    max(0, len(self.queue) - self.q_pos),
                'in_loop':      self.in_loop,
                'gid':          self.current_gid(),
                'stuck':        stuck}

    def stuck_words(self):
        return [w for w in self.fail_cnt
                if self.fail_cnt[w] >= FAIL_THRESHOLD]


# ═══════════════════════════════════════════════════
# 相似音排序
# ═══════════════════════════════════════════════════
def _word_reading(w):
    r = w.get('reading', '').strip()
    return r if r else w['word']


def sort_words_by_similarity(words):
    def _key(w):
        r = _word_reading(w)
        return (r, w['word'])
    return sorted(words, key=_key)


# ═══════════════════════════════════════════════════
# TTS —— 用 asyncio.run() 保证事件循环干净关闭
# 关键：不再用「新线程 + 新事件循环」的旧模式（会累积 aiohttp socket 泄漏，
# 在 Streamlit Cloud 上导致段错误）。asyncio.run() 每次调用都是完整生命周期，
# aiohttp 的 WebSocket 会被正确关闭。
# ═══════════════════════════════════════════════════
import json as _json
import threading as _threading


def _run_async(coro_factory, timeout=60):
    """
    在同步代码里跑 async。coro_factory 是「零参可调用对象」，返回一个新协程。
    优先用 asyncio.run；如果当前线程已有 loop（罕见）就退回线程模式。
    传 factory 而不是 coroutine 是因为 coroutine 单次使用，回退需要新的。
    """
    # 首选路径：asyncio.run 会创建独立循环、跑完自动清理所有 aiohttp 资源
    try:
        return asyncio.run(coro_factory())
    except RuntimeError:
        # 例外：当前线程已经在跑 loop（Streamlit 内部有时会），退回线程
        pass
    except Exception:
        return None

    # 后备路径：新线程 + 新循环，注意 join 后一定 close
    result_box = [None]
    def _worker():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result_box[0] = loop.run_until_complete(coro_factory())
        except Exception:
            pass
        finally:
            try:
                # 确保所有挂起任务被取消（不然 aiohttp 会留下悬空的 socket）
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            finally:
                loop.close()
    t = _threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result_box[0]


async def _tts_stream_async(text, voice, rate):
    """单条 TTS：文字 → mp3 bytes。异常时返回空串。"""
    try:
        com = edge_tts.Communicate(text, voice, rate=rate)
        buf = io.BytesIO()
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()
    except Exception:
        return b""


def render_audio(data, word="", autoplay=False):
    if not data:
        st.caption("⚠️ 音频未生成，请点「🔊 重播」重试")
        return
    b64     = base64.b64encode(data).decode()
    play_js = "audio.play().catch(function(){});" if autoplay else ""
    size_kb = len(data) // 1024
    html = f"""<!DOCTYPE html><html><head>
<style>body{{margin:0;padding:0;background:transparent;}}audio{{width:100%;border-radius:8px;display:block;}}p{{font-size:11px;color:#888;margin:2px 0 0;font-family:sans-serif;}}</style>
</head><body>
<audio controls id="jp-audio"><source src="data:audio/mpeg;base64,{b64}" type="audio/mpeg"></audio>
<p>📦 {size_kb} KB · {word}</p>
<script>var audio=document.getElementById('jp-audio');audio.load();{play_js}</script>
</body></html>"""
    components.html(html, height=75, scrolling=False)


@st.cache_data(max_entries=50, show_spinner=False)
def get_audio(word, voice, rate):
    result = _run_async(lambda: _tts_stream_async(word, voice, rate), timeout=15)
    return result if result else b""


# ═══════════════════════════════════════════════════
# 循环朗读 — 批量音频生成
# ═══════════════════════════════════════════════════
# 并发数关键：从 12 降到 6，减轻 Streamlit Cloud 的 socket 压力
_LOOP_TTS_CONCURRENCY = 6


async def _gen_batch_async(tasks, concurrency=_LOOP_TTS_CONCURRENCY):
    """tasks: [{key, text, voice, rate}]. 返回 {key: bytes}."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(t):
        async with sem:
            data = await _tts_stream_async(t['text'], t['voice'], t['rate'])
            return t['key'], data

    results = await asyncio.gather(*[_one(t) for t in tasks], return_exceptions=True)
    out = {}
    for r in results:
        if isinstance(r, tuple):
            out[r[0]] = r[1]
    return out


def _gen_batch(tasks, timeout=120):
    """同步版：批量并发生成，返回 {key: bytes}。"""
    result = _run_async(lambda: _gen_batch_async(tasks), timeout=timeout)
    return result if result else {}


def _cn_text_from_meaning(meaning: str, mode: str) -> str:
    """把 '吃/食用/接受' 按 mode 变成 TTS 文本。"""
    m = (meaning or '').strip()
    if not m or mode == 'none':
        return ''
    parts = [p.strip() for p in m.split('/') if p.strip()]
    if not parts:
        return ''
    if mode == 'first_only':
        return parts[0]
    return '、'.join(parts)


def generate_loop_audio(words, jp_voice, jp_rate, cn_voice, cn_rate, cn_mode):
    """给一批词生成 JP + CN 音频，返回 list[{word, meaning, cn_text, jp_b64, cn_b64}]。"""
    tasks = []
    cn_text_map = {}
    for w in words:
        tasks.append({'key': f"jp::{w['word']}", 'text': w['word'],
                      'voice': jp_voice, 'rate': jp_rate})
        cn_text = _cn_text_from_meaning(w.get('meaning', ''), cn_mode)
        cn_text_map[w['word']] = cn_text
        if cn_text:
            tasks.append({'key': f"cn::{w['word']}", 'text': cn_text,
                          'voice': cn_voice, 'rate': cn_rate})

    if not tasks:
        return []

    # chunk_size 只影响进度更新粒度；实际并发由 _LOOP_TTS_CONCURRENCY(=6) 控制
    chunk_size = 12
    all_result = {}
    prog = st.progress(0.0, text=f"🎵 生成音频 0/{len(tasks)}")
    for i in range(0, len(tasks), chunk_size):
        chunk = tasks[i:i + chunk_size]
        chunk_result = _gen_batch(chunk)
        all_result.update(chunk_result)
        done = min(i + chunk_size, len(tasks))
        prog.progress(done / len(tasks),
                      text=f"🎵 生成音频 {done}/{len(tasks)}")
    prog.empty()

    result = []
    for w in words:
        jp = all_result.get(f"jp::{w['word']}", b"")
        cn = all_result.get(f"cn::{w['word']}", b"")
        result.append({
            'word':    w['word'],
            'meaning': w.get('meaning', '') or '',
            'cn_text': cn_text_map.get(w['word'], ''),
            'in_temp': bool(w.get('in_temp', False)),
            'jp_b64':  base64.b64encode(jp).decode() if jp else '',
            'cn_b64':  base64.b64encode(cn).decode() if cn else '',
        })
    return result


# ── HTML+JS 播放器 ──────────────────────────────────
_LOOP_PLAYER_HTML = r"""<!DOCTYPE html>
<html><head><style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 14px; background: transparent;
    font-family: 'Helvetica Neue', Arial, 'Hiragino Sans', 'Microsoft YaHei', sans-serif;
    color: #2c3e50;
  }
  .display {
    text-align: center; padding: 24px 12px;
    border: 2px solid #e6ebf1; border-radius: 12px;
    background: #fff; margin-bottom: 12px;
  }
  .word-text {
    font-size: 42px; font-weight: 700; letter-spacing: 3px;
    min-height: 54px; line-height: 1.15; margin-bottom: 10px;
    transition: color 0.2s;
  }
  .meaning-text {
    font-size: 22px; color: #5a6c7d; min-height: 30px;
    letter-spacing: 1px; transition: color 0.2s;
  }
  .word-text.playing  { color: #ff4b4b; }
  .meaning-text.playing { color: #3498db; }
  .hidden-text {
    color: #ecf0f1 !important; text-shadow: 0 0 24px #bdc3c7;
  }
  .info {
    text-align: center; color: #7f8c9b; font-size: 13px;
    margin-bottom: 8px;
  }
  .progress-bar {
    height: 4px; background: #e6ebf1; border-radius: 2px;
    margin-bottom: 12px; overflow: hidden;
  }
  .progress-fill {
    height: 100%; background: #ff4b4b; transition: width 0.3s;
  }
  .controls {
    display: flex; gap: 8px; justify-content: center; margin-bottom: 12px;
  }
  .controls button {
    padding: 10px 18px; font-size: 16px; border: 1px solid #d5dce4;
    border-radius: 10px; background: #fff; cursor: pointer;
    transition: all 0.15s; min-width: 56px;
  }
  .controls button:hover { background: #eef3f9; }
  .controls button.play {
    background: #ff4b4b; color: #fff; border-color: #ff4b4b; min-width: 100px;
  }
  .controls button.play:hover { background: #e04040; }
  .toggles {
    display: flex; gap: 18px; justify-content: center; flex-wrap: wrap;
    font-size: 13px; color: #5a6c7d;
  }
  .toggles label { cursor: pointer; user-select: none; }
</style></head>
<body>
  <div class="display">
    <div id="wordText" class="word-text">点击「开始」</div>
    <div id="meaningText" class="meaning-text">&nbsp;</div>
  </div>
  <div class="info"><span id="info">准备中...</span></div>
  <div class="progress-bar"><div id="progFill" class="progress-fill" style="width:0%"></div></div>
  <div class="controls">
    <button id="btnPrev" title="上一词">⏮</button>
    <button id="btnPlay" class="play" title="播放/暂停">▶ 开始</button>
    <button id="btnNext" title="下一词">⏭</button>
  </div>
  <div class="controls" style="margin-top:-4px;">
    <button id="btnRemove" title="剔除当前词（已听懂）">❌ 剔除当前词</button>
    <button id="btnSave" title="把剩余未剔除词复制为 JSON">💾 保存记录</button>
  </div>
  <div id="removedBox" style="text-align:center; font-size:12px; color:#7f8c9b; margin:8px 0; min-height:22px;">
    <div id="removedText">已剔除 0 词  ·  剩余 全部</div>
    <div id="removedList" style="font-size:11px; margin-top:4px; max-height:70px; overflow-y:auto;"></div>
  </div>
  <div class="toggles">
    <label><input type="checkbox" id="tglText" checked> 显示单词与释义</label>
    <label><input type="checkbox" id="tglAuto" checked> 结束后自动下一轮</label>
  </div>
  <audio id="player" preload="auto"></audio>
<script>
(function() {
  const DATA = __DATA_JSON__;
  const words = DATA.words;
  const cfg   = DATA.config;
  const N = words.length;

  function shuffle(arr) {
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
  }

  let order      = shuffle([...Array(N).keys()]);
  let wordIdx    = 0;
  let cycleIdx   = 0;
  let subIdx     = 0;   // 0 = JP, 1 = CN
  let playing    = false;
  let loopCount  = 1;
  let advTimer   = null;

  const player       = document.getElementById('player');
  const btnPlay      = document.getElementById('btnPlay');
  const btnPrev      = document.getElementById('btnPrev');
  const btnNext      = document.getElementById('btnNext');
  const wordTextEl   = document.getElementById('wordText');
  const meaningTextEl= document.getElementById('meaningText');
  const infoEl       = document.getElementById('info');
  const progFill     = document.getElementById('progFill');
  const tglText      = document.getElementById('tglText');
  const tglAuto      = document.getElementById('tglAuto');
  const btnRemove    = document.getElementById('btnRemove');
  const btnSave      = document.getElementById('btnSave');
  const removedTextEl= document.getElementById('removedText');
  const removedListEl= document.getElementById('removedList');

  // 剔除的词（"已听懂"），保存记录时只存剩余的词
  const removedSet = new Set();

  function updateRemovedUI() {
    const n = removedSet.size;
    const remain = N - n;
    removedTextEl.innerHTML =
      '已剔除 <b>' + n + '</b> 词  ·  剩余 <b>' + remain + '</b> 词' +
      (n > 0 ? '　（点词名可撤销）' : '');
    if (n === 0) {
      removedListEl.innerHTML = '<span style="color:#bdc3c7">（点 ❌ 剔除已听懂的词）</span>';
    } else {
      const parts = [];
      removedSet.forEach(function(w) {
        parts.push('<span style="color:#95a5a6;cursor:pointer;text-decoration:line-through" ' +
                   'onclick="window.__undoRemove(\'' + w.replace(/'/g, "\\'") + '\')">' + w + '</span>');
      });
      removedListEl.innerHTML = parts.join('　');
    }
  }

  window.__undoRemove = function(word) {
    if (!removedSet.has(word)) return;
    removedSet.delete(word);
    // 重新塞回 order 里随机位置
    const idx = words.findIndex(function(w) { return w.word === word; });
    if (idx >= 0 && !order.includes(idx)) {
      const insertPos = Math.floor(Math.random() * (order.length + 1));
      order.splice(insertPos, 0, idx);
    }
    updateRemovedUI();
    updateDisplay();
  };

  function currentWord() { return words[order[wordIdx]]; }

  function updateDisplay() {
    const w = currentWord();
    if (!w) return;
    wordTextEl.textContent    = w.word;
    meaningTextEl.textContent = w.meaning || '（无释义）';

    let wCls = 'word-text';
    let mCls = 'meaning-text';
    if (playing) {
      if (subIdx === 0) wCls += ' playing';
      else              mCls += ' playing';
    }
    if (!tglText.checked) { wCls += ' hidden-text'; mCls += ' hidden-text'; }
    wordTextEl.className    = wCls;
    meaningTextEl.className = mCls;

    infoEl.innerHTML =
      '第 <b>' + loopCount + '</b> 轮  ·  ' +
      (wordIdx + 1) + ' / ' + order.length +
      '  ·  ' + (subIdx === 0 ? '🇯🇵 日语' : '🇨🇳 中文') +
      '  ·  第 ' + (cycleIdx + 1) + '/' + cfg.repeat + ' 次';
    progFill.style.width = ((wordIdx / Math.max(order.length, 1)) * 100).toFixed(1) + '%';
    updateRemovedUI();
  }

  function currentB64() {
    const w = currentWord();
    if (!w) return '';
    return subIdx === 0 ? w.jp_b64 : w.cn_b64;
  }

  function playCurrent() {
    if (!playing) return;
    const b64 = currentB64();
    if (!b64) {
      // 无音频（比如无释义又要读中文），直接推进
      onAudioDone();
      return;
    }
    try {
      player.src = 'data:audio/mpeg;base64,' + b64;
      player.play().catch(function(e) { console.warn('play failed', e); });
    } catch (e) { console.warn(e); }
  }

  function onAudioDone() {
    if (!playing) return;
    // 阶段 1: JP 刚播完
    if (subIdx === 0) {
      const w = currentWord();
      if (w && cfg.cn_mode !== 'none' && w.cn_b64) {
        subIdx = 1;
        advTimer = setTimeout(function() {
          updateDisplay();
          playCurrent();
        }, cfg.inner_gap_ms);
        return;
      }
      // 无中文，落到下面推进逻辑
    }
    // 阶段 2: CN 播完（或跳过）— 该词的本轮结束
    cycleIdx += 1;
    if (cycleIdx < cfg.repeat) {
      subIdx = 0;
      advTimer = setTimeout(function() {
        updateDisplay();
        playCurrent();
      }, cfg.inner_gap_ms);
      return;
    }
    // 该词全部重复完成 —— 下一个词
    cycleIdx = 0;
    subIdx   = 0;
    wordIdx += 1;
    if (wordIdx >= order.length) {
      // 一轮结束
      if (!tglAuto.checked) {
        playing = false;
        btnPlay.textContent = '▶ 开始';
        wordIdx = 0;
        order = shuffle(activeIndices());
        updateDisplay();
        return;
      }
      loopCount += 1;
      wordIdx = 0;
      order = shuffle(activeIndices());
      if (order.length === 0) {
        // 所有词都被剔除了，停止
        playing = false;
        btnPlay.textContent = '✅ 全部剔除';
        wordTextEl.textContent    = '🎉 已剔除全部词';
        meaningTextEl.textContent = '点「💾 保存记录」';
        updateRemovedUI();
        return;
      }
    }
    advTimer = setTimeout(function() {
      updateDisplay();
      playCurrent();
    }, cfg.word_gap_ms);
  }

  function activeIndices() {
    const out = [];
    for (let i = 0; i < N; i++) {
      if (!removedSet.has(words[i].word)) out.push(i);
    }
    return out;
  }

  player.addEventListener('ended', onAudioDone);
  player.addEventListener('error', function() {
    // 单条失败，别卡死
    console.warn('audio error, skipping');
    setTimeout(onAudioDone, 100);
  });

  btnPlay.addEventListener('click', function() {
    if (order.length === 0) return;
    playing = !playing;
    if (playing) {
      btnPlay.textContent = '⏸ 暂停';
      updateDisplay();
      playCurrent();
    } else {
      btnPlay.textContent = '▶ 继续';
      player.pause();
      if (advTimer) clearTimeout(advTimer);
      updateDisplay();
    }
  });

  btnPrev.addEventListener('click', function() {
    if (advTimer) clearTimeout(advTimer);
    player.pause();
    cycleIdx = 0; subIdx = 0;
    wordIdx = Math.max(0, wordIdx - 1);
    updateDisplay();
    if (playing) playCurrent();
  });

  btnNext.addEventListener('click', function() {
    if (advTimer) clearTimeout(advTimer);
    player.pause();
    cycleIdx = 0; subIdx = 0;
    wordIdx += 1;
    if (wordIdx >= order.length) {
      loopCount += 1;
      wordIdx = 0;
      order = shuffle(activeIndices());
    }
    updateDisplay();
    if (playing) playCurrent();
  });

  tglText.addEventListener('change', updateDisplay);

  // ❌ 剔除当前词
  btnRemove.addEventListener('click', function() {
    if (order.length === 0) return;
    const cur = currentWord();
    if (!cur) return;
    removedSet.add(cur.word);
    // 从 order 里去掉，重排 wordIdx
    order = order.filter(function(i) { return !removedSet.has(words[i].word); });
    if (order.length === 0) {
      playing = false;
      btnPlay.textContent = '✅ 全部剔除';
      player.pause();
      if (advTimer) clearTimeout(advTimer);
      wordTextEl.textContent    = '🎉 已剔除全部词';
      meaningTextEl.textContent = '点「💾 保存记录」';
      updateRemovedUI();
      return;
    }
    if (wordIdx >= order.length) wordIdx = 0;
    cycleIdx = 0; subIdx = 0;
    player.pause();
    if (advTimer) clearTimeout(advTimer);
    updateDisplay();
    if (playing) {
      advTimer = setTimeout(function() { playCurrent(); }, cfg.word_gap_ms);
    }
  });

  // 💾 保存记录（把剩余未剔除词复制为 JSON）
  btnSave.addEventListener('click', async function() {
    const remaining = [];
    for (let i = 0; i < N; i++) {
      if (!removedSet.has(words[i].word)) remaining.push(words[i].word);
    }
    const payload = JSON.stringify({
      remaining:     remaining,
      removed_count: removedSet.size,
      total_count:   N,
    });
    let ok = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(payload);
        ok = true;
      }
    } catch (e) { /* fallthrough */ }
    if (!ok) {
      window.prompt('请手动复制下方 JSON，然后粘贴到下面的文本框：', payload);
    } else {
      const orig = btnSave.textContent;
      btnSave.textContent = '✅ 已复制  →  粘贴到下方保存';
      setTimeout(function() { btnSave.textContent = orig; }, 2000);
    }
  });

  // 初始视图
  updateDisplay();
  wordTextEl.textContent    = '点击「▶ 开始」启动';
  meaningTextEl.textContent = '共 ' + N + ' 词';
})();
</script>
</body></html>"""


def render_loop_player(word_data, config):
    """渲染 JS 循环播放器。"""
    payload = _json.dumps({'words': word_data, 'config': config}, ensure_ascii=False)
    html = _LOOP_PLAYER_HTML.replace('__DATA_JSON__', payload)
    components.html(html, height=520, scrolling=False)


# ═══════════════════════════════════════════════════
# AI 提示词工具
# ═══════════════════════════════════════════════════
def _make_ai_prompt(words, category):
    cat_name = CAT_NAMES_CN.get(category, '日语词')
    header = (
        f"我在学习日语{cat_name}。请为下面每个日语词提供简短的中文释义。规则：\n"
        f"1. 每个词给 1-3 个最常用的中文含义，多个含义之间用【/】分隔（如：食べる = 吃/食用）。\n"
        f"2. 每个含义不超过 8 字。如果只有一个常用含义就只给一个，不要凑数。\n"
        f"3. 严格按【单词 = 释义1/释义2/释义3】格式，一行一个，不要加序号、括号或多余解释。\n"
        f"4. 释义部分不要出现【=】或【:】等符号。\n\n"
    )
    return header + "\n".join(words)


def _parse_ai_response(text):
    """解析 AI 回复。支持：单词=释义 / 单词：释义 / 单词 - 释义。"""
    result = {}
    seps = ['＝', '=', '：', ':', ' — ', '——', ' - ', '\t']
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 去掉行首序号（1. / 1、 / (1) / -/ * 等）
        line = re.sub(r'^[\d]+[\.\)、]\s*', '', line)
        line = re.sub(r'^[\-\*・•●○]\s*', '', line)
        for sep in seps:
            if sep in line:
                parts = line.split(sep, 1)
                w = parts[0].strip().strip('「」『』【】()（）')
                m = parts[1].strip().strip('「」『』【】()（）')
                if w and m:
                    result[w] = m
                break
    return result


# ═══════════════════════════════════════════════════
# Session State 初始化
# ═══════════════════════════════════════════════════
def _init():
    defaults = {
        'store':            WordStore(),
        'session':          None,
        'phase':            'main',
        'category':         'kanji',
        'show_word':        False,
        'voice':            'ja-JP-NanamiNeural',
        'speed':            '+0%',
        'gs':               33,
        'batch_size':       20,
        'autoplay':         False,
        'rv_idx':           0,
        'cur_audio':        b'',
        'last_audio_word':  '',
        'last_file_id':     '',
        'last_file_result': None,
        # 编辑表版本号：保存后 +1 用于重置 data_editor 的内部状态
        'editor_ver':       0,
        # Gist 状态
        'gist_id':          None,
        'gist_last_sync':   None,
        'gist_auto_loaded': False,
        # 循环朗读
        'loop_audio':         None,
        'loop_config':        None,
        'loop_meta':          None,
        'loop_record_active': None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init()

# ── 启动时自动加载 ──
if (not st.session_state.gist_auto_loaded
        and _gist_enabled()
        and len(st.session_state.store.words) == 0):
    st.session_state.gist_auto_loaded = True
    try:
        token, gist_id = _gist_cfg()
        if not gist_id:
            info = gist_find(token)
            gist_id = info["id"] if info else None
        if gist_id:
            csv_text, records_text = gist_load(gist_id)
            n = st.session_state.store.import_csv(csv_text)
            # 覆盖式加载 records
            try:
                import json as _j
                parsed = _j.loads(records_text) if records_text.strip() else []
                if isinstance(parsed, list):
                    st.session_state.store.records = parsed
            except Exception:
                pass
            n_rec = len(st.session_state.store.records)
            st.session_state.gist_id        = gist_id
            st.session_state.gist_last_sync = datetime.datetime.now().strftime("%H:%M:%S")
            st.toast(f"☁️ 已加载 {n} 个词 · {n_rec} 条记录", icon="✅")
    except Exception:
        pass


# ═══════════════════════════════════════════════════
# 内存管理
# 循环播放的音频数据可能有 20-50 MB（base64），常驻 session_state 会导致
# 每次 rerun 都拖着这坨数据处理，最终触发 Streamlit Cloud 的内存/段错误。
# 只要当前不在循环播放页，就主动清掉。
# ═══════════════════════════════════════════════════
if st.session_state.phase != 'loop_playing' and st.session_state.get('loop_audio'):
    st.session_state.loop_audio  = None
    st.session_state.loop_config = None
    st.session_state.loop_meta   = None


# ═══════════════════════════════════════════════════
# 侧边栏
# ═══════════════════════════════════════════════════
def _sidebar_gist():
    with st.sidebar:
        st.header("🐙 GitHub Gist 同步")
        enabled   = _gist_enabled()
        last_sync = st.session_state.gist_last_sync

        if enabled:
            st.success(f"已连接{'  ·  上次同步：' + last_sync if last_sync else ''}")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("⬇ 加载", use_container_width=True):
                    do_gist_load()
            with c2:
                if st.button("⬆ 保存", use_container_width=True):
                    do_gist_save()
            gid = st.session_state.gist_id
            if gid:
                st.caption(f"Gist ID: `{gid[:12]}…`")
        else:
            st.warning("未配置")
            st.markdown("""
**配置步骤（5 分钟）：**

**1. 生成 GitHub Token**
→ GitHub → Settings → Developer settings
→ Personal access tokens → Tokens (classic)
→ Generate new token
→ 勾选 **`gist`** 权限 → 生成并复制

**2. 新建配置文件**

项目目录里创建 `.streamlit/secrets.toml`：

```toml
[github]
token = "ghp_你的token"
```

**3. 部署到 Streamlit Cloud 时**

App Settings → Secrets → 粘贴相同内容
""")


# ═══════════════════════════════════════════════════
# 主页
# ═══════════════════════════════════════════════════
def _category_switch():
    """顶部词类切换器。切换时清空当前会话。"""
    current = st.session_state.category
    keys    = list(CATEGORIES.keys())
    labels  = [CATEGORIES[k] for k in keys]
    picked = st.radio(
        "词类模式", labels,
        index=keys.index(current),
        horizontal=True,
        key="_cat_radio",
        label_visibility="collapsed",
    )
    new_cat = keys[labels.index(picked)]
    if new_cat != current:
        st.session_state.category = new_cat
        st.session_state.session  = None
        st.session_state.pop("show_word", None)
        st.session_state.last_audio_word = ''
        st.rerun()


def screen_main():
    _sidebar_gist()
    store = st.session_state.store

    st.title("🇯🇵 日语单词听力练习")
    _category_switch()
    cat = st.session_state.category
    s   = store.stats(cat)

    # 顶部云端快捷栏
    if _gist_enabled():
        last_sync = st.session_state.gist_last_sync
        sync_txt  = f"上次同步：{last_sync}" if last_sync else "尚未同步"
        ca, cb, cc = st.columns([2, 1, 1])
        ca.caption(f"🐙 GitHub Gist 已连接  ·  {sync_txt}  ·  当前：{CATEGORIES[cat]}")
        with cb:
            if st.button("⬇ 加载", use_container_width=True, help="从 Gist 加载词库"):
                do_gist_load()
        with cc:
            if st.button("⬆ 保存", use_container_width=True, help="保存词库到 Gist"):
                do_gist_save()
    else:
        st.info(f"💡 当前模式：**{CATEGORIES[cat]}**  ·  在左侧边栏配置 GitHub Token 可实现多设备同步")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("总词数",  s['total'])
    c2.metric("0级 新词", s['levels'][0])
    c3.metric("1级 掌握", s['levels'][1])
    c4.metric("2级 模糊", s['levels'][2])
    c5.metric("3级 重点", s['levels'][3])
    c6.metric("⭐ 临时", s['temp'])

    tab_in, tab_edit, tab_ai, tab_st, tab_loop = st.tabs(
        ["📝 录入词汇", "✏️ 管理词汇", "🤖 AI 释义", "🎧 开始学习", "🔁 循环朗读"]
    )
    with tab_in:   _panel_input()
    with tab_edit: _panel_edit()
    with tab_ai:   _panel_ai()
    with tab_st:   _panel_study()
    with tab_loop: _panel_loop()


# ── 录入面板 ──
def _panel_input():
    store = st.session_state.store
    cat   = st.session_state.category

    st.subheader(f"粘贴添加  →  当前分类：{CATEGORIES[cat]}")
    st.caption("新词会自动归入当前分类。切换到「动词」模式后，添加的都会被记作动词。")
    raw  = st.text_area("每行一个日语词", height=130, label_visibility="collapsed",
                        placeholder=("食べる\n飲む\n勉強する" if cat == 'verb'
                                     else "食べ物\n天気\n図書館\n電車"))
    gs_v = st.number_input("每组词数", min_value=5, max_value=200,
                            value=st.session_state.gs, step=1)

    if st.button("➕ 添加词汇", type="primary"):
        n = store.add_text(raw, int(gs_v), cat)
        st.session_state.gs = int(gs_v)
        if n:
            st.success(f"✅ 添加了 {n} 个新词到「{CAT_NAMES_CN[cat]}」")
            if _gist_enabled():
                do_gist_save()
        else:
            st.warning("⚠️ 没有新词（词可能已存在于该分类）")

    st.divider()

    st.subheader("📂 上传本地 CSV（备用）")
    st.caption("兼容旧版 CSV。旧文件里的词会视为「汉字词」；新版 CSV 会保留 category 列。")
    uploaded = st.file_uploader("选择 CSV 文件", type=["csv"],
                                 label_visibility="collapsed")
    if uploaded is not None:
        file_id = f"{uploaded.name}_{uploaded.size}"
        if st.session_state.last_file_id != file_id:
            try:
                text = uploaded.read().decode("utf-8-sig")
                n    = store.import_csv(text, int(gs_v))
                st.session_state.last_file_id     = file_id
                st.session_state.last_file_result = (uploaded.name, n)
            except Exception as e:
                st.error(f"读取失败：{e}")
        if st.session_state.last_file_result:
            fname, n = st.session_state.last_file_result
            if n:
                st.success(f"✅ 从 {fname} 导入了 {n} 个词")
            else:
                st.info(f"文件 {fname} 中没有新词（可能已全部存在）")

    st.divider()

    st.subheader(f"当前词库 · {CATEGORIES[cat]}")
    groups = store.get_groups(cat)
    if not groups:
        st.caption("（该分类下词库为空）")
    else:
        lv_icon = {0: "⬜", 1: "🟩", 2: "🟨", 3: "🟥"}
        for gid, words in groups.items():
            with st.expander(f"第 {gid} 组 — {len(words)} 词"):
                st.write("　".join(
                    f"{lv_icon[w['long_level']]}"
                    f"{'⭐' if w.get('in_temp') else ''} {w['word']}"
                    for w in words))

        col_dl, col_up = st.columns(2)
        with col_dl:
            st.download_button(
                "⬇ 下载全部 CSV（含所有分类）",
                data=store.export_csv(),
                file_name="japanese_words.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with col_up:
            if _gist_enabled():
                if st.button("🐙 保存到 Gist", use_container_width=True, type="primary"):
                    do_gist_save()


# ── 词汇管理面板 ──
def _panel_edit():
    store = st.session_state.store
    cat   = st.session_state.category
    cat_words = store.by_category(cat)

    if not cat_words:
        st.info(f"「{CAT_NAMES_CN[cat]}」词库为空，请先在「录入词汇」中添加。")
        return

    st.subheader(f"✏️ 编辑词汇  ·  {CATEGORIES[cat]}")
    st.caption("可直接修改読み、释义、分组、等级、临时标记；删除行后点「保存修改」生效。")

    df = pd.DataFrame([{
        '单词':  w['word'],
        '読み':  w.get('reading', ''),
        '释义':  w.get('meaning', ''),
        '分组':  int(w['group']),
        '等级':  int(w['long_level']),
        '临时':  bool(w.get('in_temp', False)),
    } for w in cat_words])

    # 修复：key 里带版本号，保存/重分组后 +1，避免 data_editor
    # 把旧的编辑状态叠加到新数据上
    editor_key = f'word_editor_tbl_{cat}_v{st.session_state.editor_ver}'
    edited = st.data_editor(
        df,
        column_config={
            '单词': st.column_config.TextColumn('单词', disabled=True, width='small'),
            '読み': st.column_config.TextColumn('読み（可选·相似音聚类）', width='medium'),
            '释义': st.column_config.TextColumn('释义（练习时可显示/隐藏）', width='large'),
            '分组': st.column_config.NumberColumn('分组', min_value=1, step=1, width='small'),
            '等级': st.column_config.SelectboxColumn('等级', options=[0, 1, 2, 3], width='small',
                        help='0=新词  1=已掌握  2=模糊  3=重点'),
            '临时': st.column_config.CheckboxColumn('⭐临时', width='small',
                        help='勾选后可在学习时「只练习临时列表」'),
        },
        use_container_width=True,
        num_rows='dynamic',
        height=min(600, 44 + 36 * len(cat_words)),
        key=editor_key,
        hide_index=False,
    )

    col_save, col_clr = st.columns([2, 1])
    with col_save:
        if st.button('💾 保存词汇修改', type='primary', use_container_width=True):
            existing = {w['word']: w for w in cat_words}
            new_cat_words = []
            changed = 0
            for _, row in edited.iterrows():
                word = str(row['单词']).strip() if pd.notna(row['单词']) else ''
                if not word:
                    continue
                orig = existing.get(word)
                if not orig:
                    continue
                rdg = str(row['読み']).strip() if pd.notna(row['読み']) else ''
                mng = str(row['释义']).strip() if pd.notna(row['释义']) else ''
                grp = int(row['分组']) if pd.notna(row['分组']) else orig['group']
                lv  = int(row['等级']) if pd.notna(row['等级']) else orig['long_level']
                tmp = bool(row['临时']) if pd.notna(row['临时']) else bool(orig.get('in_temp', False))
                w2 = orig.copy()
                if (w2.get('reading','') != rdg or w2.get('meaning','') != mng or
                    w2['group'] != grp or w2['long_level'] != lv or
                    bool(w2.get('in_temp', False)) != tmp):
                    changed += 1
                w2['reading'] = rdg
                w2['meaning'] = mng
                w2['group']   = grp
                w2['long_level'] = lv
                w2['in_temp'] = tmp
                new_cat_words.append(w2)
            deleted = len(cat_words) - len(new_cat_words)
            # 合并回全库：其他分类保留不动
            other = [w for w in store.words if w.get('category', 'kanji') != cat]
            store.words = other + new_cat_words
            st.session_state.editor_ver += 1   # 重置编辑器状态
            st.success(f'✅ 保存完成  ·  修改 {changed} 词  ·  删除 {deleted} 词  ·  该分类剩余 {len(new_cat_words)} 词')
            if _gist_enabled():
                do_gist_save()
            st.rerun()

    with col_clr:
        n_temp = sum(1 for w in cat_words if w.get('in_temp', False))
        if st.button(f'🗑 清空临时（{n_temp}）', use_container_width=True,
                     disabled=(n_temp == 0)):
            store.clear_temp(cat)
            st.session_state.editor_ver += 1
            st.success(f'✅ 已清空 {n_temp} 个临时标记')
            if _gist_enabled():
                do_gist_save()
            st.rerun()

    st.divider()
    st.subheader('🔀 批量重新分组')
    st.caption(f'将「{CAT_NAMES_CN[cat]}」的全部词（按现有顺序）重新按指定大小划分。')
    col_gs, col_sg = st.columns(2)
    with col_gs:
        new_gs  = st.number_input('每组词数', min_value=5, max_value=300, value=30, step=5,
                                   key='regrp_gs')
    with col_sg:
        start_g = st.number_input('起始组号', min_value=1, max_value=99, value=1, step=1,
                                   key='regrp_start')
    total_after = len(cat_words)
    n_grps = -(-total_after // int(new_gs))
    st.caption(f"将生成 **{n_grps}** 组，最后一组约 {total_after - (n_grps-1)*int(new_gs)} 词")
    if st.button('🔄 执行重新分组', use_container_width=True):
        store.regroup(int(new_gs), cat, int(start_g))
        st.session_state.editor_ver += 1
        st.success(f'✅ 已分为 {n_grps} 组（组 {start_g} ~ {start_g+n_grps-1}）')
        if _gist_enabled():
            do_gist_save()
        st.rerun()


# ── AI 释义面板 ──
def _panel_ai():
    store = st.session_state.store
    cat   = st.session_state.category
    cat_words = store.by_category(cat)

    st.subheader(f"🤖 AI 生成释义  ·  {CATEGORIES[cat]}")
    st.caption("流程：① 选词 → ② 复制提示词粘到 ChatGPT / Claude / Gemini → ③ 把 AI 的回复贴回下方 → ④ 一键解析入库。")

    if not cat_words:
        st.info("该分类下没有词，请先添加。")
        return

    no_meaning = [w for w in cat_words if not w.get('meaning', '').strip()]
    with_meaning = [w for w in cat_words if w.get('meaning', '').strip()]

    m1, m2 = st.columns(2)
    m1.metric("无释义词", len(no_meaning))
    m2.metric("已有释义", len(with_meaning))

    if not no_meaning:
        st.success("🎉 该分类下所有词都已有释义！")
        with st.expander("查看已有释义（可作检查）"):
            st.dataframe(pd.DataFrame([
                {'单词': w['word'], '释义': w.get('meaning', '')} for w in with_meaning
            ]), use_container_width=True, hide_index=True)
        return

    # ① 选词
    st.markdown("**① 选择要生成释义的词**")
    all_options = [w['word'] for w in no_meaning]
    ms_key = f"ai_pick_{cat}"

    # 修复：「全选/清空」必须在 multiselect 实例化【之前】写入 session_state，
    # 原版在控件之后才改 picked 变量，界面上永远不会更新，且逻辑与显示不一致。
    if st.session_state.pop("_ai_force_all", False):
        st.session_state[ms_key] = list(all_options)
    if st.session_state.pop("_ai_force_none", False):
        st.session_state[ms_key] = []
    if ms_key in st.session_state:
        # 清掉已不在候选列表里的词（比如刚保存过释义的），否则 multiselect 会报错
        _opt_set = set(all_options)
        st.session_state[ms_key] = [w for w in st.session_state[ms_key] if w in _opt_set]
    else:
        st.session_state[ms_key] = all_options[:min(60, len(all_options))]

    picked = st.multiselect(
        "选词",
        all_options,
        key=ms_key,
        label_visibility="collapsed",
    )
    ca, cb, cc = st.columns(3)
    if ca.button("全选", use_container_width=True, key="ai_pick_all"):
        st.session_state["_ai_force_all"] = True
        st.rerun()
    if cb.button("清空", use_container_width=True, key="ai_pick_none"):
        st.session_state["_ai_force_none"] = True
        st.rerun()
    cc.caption(f"已选 {len(picked)} / {len(all_options)}")

    if not picked:
        st.info("请至少选择一个词。")
        return

    # ② 生成提示词
    st.markdown("**② 复制下方提示词，发给 AI**")
    prompt = _make_ai_prompt(picked, cat)
    st.code(prompt, language=None)

    # ③ 粘贴回复
    st.markdown("**③ 把 AI 回复贴到这里**")
    resp = st.text_area(
        "AI 回复",
        key="ai_resp_area",
        height=220,
        label_visibility="collapsed",
        placeholder="食べる = 吃\n飲む = 喝\n勉強する = 学习\n...",
    )

    # 实时预览解析结果
    if resp.strip():
        parsed = _parse_ai_response(resp)
        picked_set = set(picked)
        matched = {w: m for w, m in parsed.items() if w in picked_set}
        extra   = {w: m for w, m in parsed.items() if w not in picked_set}
        missing = [w for w in picked if w not in parsed]

        st.markdown("**④ 解析预览**")
        col_ok, col_miss, col_ex = st.columns(3)
        col_ok.metric("✅ 命中", len(matched))
        col_miss.metric("⚠️ 缺失", len(missing))
        col_ex.metric("❔ 多余", len(extra))

        if matched:
            with st.expander(f"命中 {len(matched)} 个（将入库）", expanded=True):
                st.dataframe(pd.DataFrame([
                    {'单词': w, '释义': m} for w, m in matched.items()
                ]), use_container_width=True, hide_index=True)
        if missing:
            with st.expander(f"⚠️ 缺失 {len(missing)} 个（AI 没返回）"):
                st.write("　".join(missing))
        if extra:
            with st.expander(f"❔ 多余 {len(extra)} 个（不在选词列表内，忽略）"):
                st.write("　".join(extra))

        if st.button(f"💾 保存 {len(matched)} 个释义入库", type="primary",
                     use_container_width=True, disabled=(len(matched) == 0)):
            n = 0
            for w, m in matched.items():
                if store.update_word_meaning(w, cat, m):
                    n += 1
            st.success(f"✅ 已保存 {n} 个释义")
            if _gist_enabled():
                do_gist_save()
            st.rerun()


# ── 学习配置面板 ──
def _panel_study():
    store = st.session_state.store
    cat   = st.session_state.category
    cat_words = store.by_category(cat)

    if not cat_words:
        st.warning(f"⚠️ 「{CAT_NAMES_CN[cat]}」词库为空，请先添加。")
        return

    all_gids = list(store.get_groups(cat).keys())

    # ① 等级筛选
    st.caption("**① 筛选长期等级**")
    lc = st.columns(4)
    lv_labels   = ["0 新词", "1 掌握", "2 模糊", "3 重点"]
    lv_defaults = [True,     False,    True,     True]
    lv_sel = [i for i, (col, lbl, df) in enumerate(zip(lc, lv_labels, lv_defaults))
               if col.checkbox(lbl, value=df, key=f"lv_chk_{i}")]

    # ⭐ 临时列表过滤
    n_temp = sum(1 for w in cat_words if w.get('in_temp', False))
    only_temp = st.checkbox(
        f"⭐ 只练习临时列表词（当前 {n_temp} 个）",
        value=False,
        disabled=(n_temp == 0),
        key="only_temp_chk",
    )

    ws = store.filter(lv_sel, all_gids, cat, only_temp=only_temp)
    if not ws:
        st.warning("⚠️ 无词匹配，请调整筛选条件")
        return
    st.caption(f"共 **{len(ws)}** 词符合条件")

    # 修复：若选出的词全部已是 1 级（掌握），会话会立即结束、原版直接白屏
    if all(w['long_level'] == 1 for w in ws):
        st.warning("⚠️ 所选词全部已是 1 级（掌握），没有需要练习的词。请把等级筛选调整为包含 0/2/3 级。")
        return

    # ② 练习模式 + 批次大小
    st.caption("**② 练习模式与分组大小**")
    col_mode, col_bs = st.columns([2, 1])
    with col_mode:
        mode = st.radio("模式", ["🎧 普通（词库顺序）", "🔀 相似音（按读音排序）"],
                        horizontal=True, label_visibility="collapsed")
    with col_bs:
        bs = st.number_input("每组词数", min_value=5, max_value=300,
                             value=st.session_state.get("batch_size", 20), step=5,
                             key="batch_size_input")
    st.session_state.batch_size = int(bs)
    bs = st.session_state.batch_size

    if "相似音" in mode:
        ordered_ws = sort_words_by_similarity(ws)
    else:
        ordered_ws = list(ws)

    tmp_groups = {}
    for i, w in enumerate(ordered_ws):
        gid = (i // bs) + 1
        tmp_groups.setdefault(gid, []).append(w)

    # ③ 选组
    st.caption("**③ 选择本次练习的组**")
    all_tmp_gids = list(tmp_groups.keys())

    def _grp_label(gid):
        words = tmp_groups[gid]
        preview = "　".join(w['word'] for w in words[:4])
        suffix = "…" if len(words) > 4 else ""
        return f"第{gid}组（{len(words)}词）：{preview}{suffix}"

    gr_sel = st.multiselect(
        "组别", all_tmp_gids, default=all_tmp_gids,
        format_func=_grp_label,
        label_visibility="collapsed",
    )

    if not gr_sel:
        st.warning("⚠️ 请至少选择一组")
        return

    selected_ws_ordered = [w for gid in sorted(gr_sel) for w in tmp_groups[gid]]
    st.info(f"🎯 本次练习 **{len(selected_ws_ordered)}** 词，共 **{len(gr_sel)}** 组  ·  {CATEGORIES[cat]}")

    # ④ 语音 + 开始
    voice_name = st.selectbox("语音", list(VOICES.keys()))

    def _make_batched(words):
        return [{**w, 'group': (i // bs) + 1} for i, w in enumerate(words)]

    if st.button("▶ 开始练习", type="primary", use_container_width=True):
        st.session_state.voice = VOICES[voice_name]
        batched = _make_batched(selected_ws_ordered)
        st.session_state.session = SessionManager(
            batched, ordered=("相似音" in mode))
        st.session_state.pop("show_word", None)
        st.session_state.last_audio_word = ''
        st.session_state.phase = 'session'
        st.rerun()


# ═══════════════════════════════════════════════════
# 循环朗读配置面板
# ═══════════════════════════════════════════════════
def _panel_loop():
    store = st.session_state.store
    cat   = st.session_state.category
    cat_words = store.by_category(cat)

    st.subheader(f"🔁 洗脑循环朗读  ·  {CATEGORIES[cat]}")
    st.caption("生成音频后在浏览器内连续播放（切标签页也不中断）。每轮结束自动重新洗牌。")

    if not cat_words:
        st.warning(f"⚠️ 「{CAT_NAMES_CN[cat]}」词库为空")
        return

    # ── 是否正从"练习记录"进入 ──
    rec_active = st.session_state.get('loop_record_active')
    if rec_active and rec_active.get('category') != cat:
        st.warning(f"📌 当前有一条记录待用（属于「{CAT_NAMES_CN.get(rec_active.get('category'),'?')}」），"
                   f"请切换到对应分类，或点下方按钮取消。")
        if st.button("取消使用记录", key="cancel_rec_cross_cat"):
            st.session_state.loop_record_active = None
            st.rerun()
        rec_active = None

    from_record = False
    if rec_active:
        from_record = True
        st.info(f"📌 **正在使用记录**：{rec_active['title']}")
        rec_words_set = set(rec_active['words'])
        # 只取词库里还存在的词
        ws = [w for w in cat_words if w['word'] in rec_words_set]
        missing = len(rec_words_set) - len(ws)
        if missing > 0:
            st.warning(f"⚠️ 记录中有 {missing} 个词已从词库中删除，将被跳过。")
        if st.button("← 取消使用记录，返回普通筛选", use_container_width=True,
                     key="cancel_rec_use"):
            st.session_state.loop_record_active = None
            st.rerun()
        # 源信息：来自记录
        source = {
            'levels': [], 'groups': [], 'only_temp': False,
            'from_record':       rec_active.get('id'),
            'from_record_title': rec_active.get('title'),
        }
    else:
        # ── ① 筛选范围（正常模式）──
        st.markdown("**① 筛选范围**")
        lc = st.columns(4)
        lv_labels = ["0 新词", "1 掌握", "2 模糊", "3 重点"]
        lv_defaults = [True, True, True, True]
        lv_sel = [i for i, (col, lbl, df) in enumerate(zip(lc, lv_labels, lv_defaults))
                  if col.checkbox(lbl, value=df, key=f"loop_lv_{i}")]

        n_temp = sum(1 for w in cat_words if w.get('in_temp', False))
        only_temp = st.checkbox(
            f"⭐ 只播临时列表词（当前 {n_temp} 个）",
            value=False, disabled=(n_temp == 0), key="loop_only_temp",
        )

        all_gids = list(store.get_groups(cat).keys())
        grp_sel = st.multiselect(
            "组别（不选则全部）", all_gids,
            default=all_gids,
            format_func=lambda g: f"第 {g} 组",
            key="loop_grp_sel",
        )
        if not grp_sel:
            grp_sel = all_gids

        ws = store.filter(lv_sel, grp_sel, cat, only_temp=only_temp)
        source = {
            'levels':    lv_sel,
            'groups':    grp_sel,
            'only_temp': bool(only_temp),
        }

    if not ws:
        st.warning("⚠️ 无词匹配筛选条件")
        return

    n_with_meaning = sum(1 for w in ws if w.get('meaning', '').strip())
    n_no_meaning   = len(ws) - n_with_meaning
    st.info(
        f"🎯 共 **{len(ws)}** 词  ·  {n_with_meaning} 个有释义  ·  {n_no_meaning} 个无释义"
    )
    if n_no_meaning > 0:
        st.caption(f"💡 提示：无释义的词只会播日语。可到「🤖 AI 释义」补齐。")

    # ── ② 播放参数 ──
    st.markdown("**② 播放参数**")

    col_jv, col_cv = st.columns(2)
    with col_jv:
        jp_voice_name = st.selectbox("日语语音", list(VOICES.keys()), key="loop_jp_voice")
    with col_cv:
        cn_voice_name = st.selectbox("中文语音", list(CN_VOICES.keys()), key="loop_cn_voice")

    col_js, col_cs = st.columns(2)
    _speed_keys = list(SPEEDS.keys())
    with col_js:
        jp_spd_name = st.selectbox("日语速度", _speed_keys, index=2, key="loop_jp_speed")
    with col_cs:
        cn_spd_name = st.selectbox("中文速度", _speed_keys, index=2, key="loop_cn_speed")

    col_rp, col_ig, col_wg = st.columns(3)
    with col_rp:
        repeat = st.number_input("每词重复次数",
                                  min_value=1, max_value=10, value=2, step=1,
                                  key="loop_repeat",
                                  help="每个词的「日语→中文」序列会重复播放这么多次")
    with col_ig:
        inner_gap = st.number_input("音频间隔（秒）",
                                     min_value=0.1, max_value=3.0, value=0.4, step=0.1,
                                     key="loop_inner_gap",
                                     help="日语与中文之间、以及同词内多次重复之间的间隔")
    with col_wg:
        word_gap = st.number_input("词间间隔（秒）",
                                    min_value=0.2, max_value=5.0, value=1.0, step=0.1,
                                    key="loop_word_gap",
                                    help="切换到下一个词之前的停顿")

    cn_mode_options = {
        "📢 全读（读全部义项，用「、」连读）": "all",
        "📌 只读第一个义项":                    "first_only",
        "🔇 不读中文（只播日语）":              "none",
    }
    cn_mode_label = st.radio(
        "中文含义读法",
        list(cn_mode_options.keys()),
        index=1,   # 默认「只读第一个」
        key="loop_cn_mode_label",
    )
    cn_mode = cn_mode_options[cn_mode_label]

    # ── ③ 估算 & 启动 ──
    st.markdown("**③ 生成音频并开始**")
    n_audio = len(ws) + (n_with_meaning if cn_mode != 'none' else 0)
    # 修复：估算与提示按真实并发数(_LOOP_TTS_CONCURRENCY=6)计算，原文案写死"12 路"
    est_sec = max(4, int(n_audio * 0.6 / _LOOP_TTS_CONCURRENCY))
    st.caption(
        f"共需生成 **{n_audio}** 个音频片段  ·  预计约 **{est_sec} 秒**"
        f"（并发 {_LOOP_TTS_CONCURRENCY} 路，词数越多耗时越久）"
    )
    if len(ws) > 150:
        st.warning("⚠️ 词数较多：音频会常驻内存，Streamlit Cloud 免费版内存有限，"
                   "建议一次不超过 150 词，否则可能触发资源限制导致应用重启。")

    if st.button("🎵 生成音频并开始循环", type="primary", use_container_width=True):
        with st.spinner(f"🎵 正在生成 {n_audio} 个音频..."):
            audio_data = generate_loop_audio(
                ws,
                VOICES[jp_voice_name], SPEEDS[jp_spd_name],
                CN_VOICES[cn_voice_name], SPEEDS[cn_spd_name],
                cn_mode,
            )
        if not audio_data:
            st.error("❌ 没有生成任何音频")
            return

        audio_data = [d for d in audio_data if d['jp_b64']]
        if not audio_data:
            st.error("❌ 音频生成全部失败，请检查网络后重试")
            return

        st.session_state.loop_audio = audio_data
        st.session_state.loop_config = {
            'repeat':       int(repeat),
            'inner_gap_ms': int(inner_gap * 1000),
            'word_gap_ms':  int(word_gap * 1000),
            'cn_mode':      cn_mode,
        }
        st.session_state.loop_meta = {
            'count':       len(audio_data),
            'category':    cat,
            'jp_voice':    jp_voice_name,
            'cn_voice':    cn_voice_name,
            'jp_speed':    jp_spd_name,
            'cn_speed':    cn_spd_name,
            'cn_mode':     cn_mode_label,
            'started_at':  datetime.datetime.now().strftime("%H:%M:%S"),
            'source':      source,
        }
        # 记录已启用一次，用完清空
        st.session_state.loop_record_active = None
        st.session_state.phase = 'loop_playing'
        st.rerun()

    # ── ④ 练习记录列表（当前分类）──
    st.divider()
    st.subheader("📋 练习记录 · 难词库")
    records = store.get_records(cat)
    if not records:
        st.caption(
            f"（「{CAT_NAMES_CN[cat]}」还没有记录。"
            "循环中剔除已听懂的词 → 点「💾 保存记录」→ 粘贴保存 → 就会出现在这里。）"
        )
    else:
        st.caption(f"共 **{len(records)}** 条  ·  上限 {store.RECORD_LIMIT}  ·  按时间倒序")
        for rec in records:
            with st.expander(rec.get('title', rec.get('id', '未命名'))):
                n_w = len(rec.get('words', []))
                created = rec.get('created', '')[:16].replace('T', ' ')
                st.caption(f"创建于 {created}  ·  共 {n_w} 词  ·  ID `{rec.get('id','')[-10:]}`")
                if n_w > 0:
                    preview = "　".join(rec['words'][:30])
                    if n_w > 30:
                        preview += f"　… （另 {n_w - 30} 词）"
                    st.write(preview)

                cbtn1, cbtn2 = st.columns([3, 1])
                with cbtn1:
                    if st.button("▶ 用这批词开始循环",
                                 key=f"use_rec_{rec['id']}",
                                 type="primary", use_container_width=True):
                        st.session_state.loop_record_active = rec
                        st.rerun()
                with cbtn2:
                    if st.button("🗑 删除",
                                 key=f"del_rec_{rec['id']}",
                                 use_container_width=True):
                        store.delete_record(rec['id'])
                        if _gist_enabled():
                            do_gist_save()
                        st.rerun()


# ═══════════════════════════════════════════════════
# 循环朗读播放界面
# ═══════════════════════════════════════════════════
def screen_loop_playing():
    audio_data = st.session_state.get('loop_audio')
    config     = st.session_state.get('loop_config')
    meta       = st.session_state.get('loop_meta') or {}

    if not audio_data or not config:
        st.session_state.phase = 'main'
        st.rerun()
        return

    cat = meta.get('category', st.session_state.category)
    st.title("🔁 循环朗读中")
    st.caption(
        f"分类：{CATEGORIES.get(cat, cat)}  ·  共 **{meta.get('count', len(audio_data))}** 词  "
        f"·  开始于 {meta.get('started_at', '')}  ·  切标签页也能继续听 🎧"
    )

    with st.expander("📋 本次配置", expanded=False):
        st.write({
            "日语语音":      meta.get('jp_voice'),
            "中文语音":      meta.get('cn_voice'),
            "日语速度":      meta.get('jp_speed'),
            "中文速度":      meta.get('cn_speed'),
            "每词重复次数":  config['repeat'],
            "音频间隔":      f"{config['inner_gap_ms']} ms",
            "词间间隔":      f"{config['word_gap_ms']} ms",
            "中文读法":      meta.get('cn_mode'),
        })

    # 播放器（所有控件在 iframe 内，不会触发 Streamlit 重跑）
    render_loop_player(audio_data, config)

    st.info("💡 **提示**：请务必点击播放器上的「▶ 开始」按钮启动。中途点下方按钮会中断播放。")

    # ── 保存难词记录 ──
    with st.expander("💾 保存难词记录（未剔除的词）", expanded=False):
        st.caption(
            "在播放器里 ❌ 剔除已听懂的词 → 点 「💾 保存记录」→ 粘贴 JSON → 确认标题 → 存。"
            "保存后可在「🔁 循环朗读」页面下方的「练习记录」中随时用这批词重开循环。"
        )

        saved_json = st.text_area(
            "剩余词 JSON",
            key="_loop_save_record_json",
            height=100,
            placeholder='{"remaining": ["食べる", "飲む"], "removed_count": 3, "total_count": 5}',
            label_visibility="collapsed",
        )

        source_for_record = (meta.get('source') or {})
        # 预览解析 + 自动标题
        default_title = ""
        parsed_remaining = None
        parse_err = None
        if (saved_json or "").strip():
            try:
                _data = _json.loads(saved_json)
                parsed_remaining = _data.get('remaining', []) or []
                default_title = _default_record_title(
                    cat, parsed_remaining, source_for_record
                )
            except Exception as e:
                parse_err = str(e)

        if parse_err:
            st.error(f"❌ JSON 解析失败：{parse_err}")
        elif parsed_remaining is not None:
            st.caption(f"📊 解析成功：剩余 **{len(parsed_remaining)}** 词准备存档")

        title_input = st.text_input(
            "记录标题（可编辑）",
            value=default_title,
            key="_loop_save_record_title",
            placeholder="标题会根据剩余词数和来源自动生成",
        )

        if st.button("💾 存为练习记录", type="primary", use_container_width=True,
                     disabled=(parsed_remaining is None or len(parsed_remaining) == 0)):
            try:
                rec = st.session_state.store.add_record(
                    category=cat,
                    words=parsed_remaining,
                    source=source_for_record,
                    title=(title_input.strip() or None),
                )
                if _gist_enabled():
                    do_gist_save()
                st.success(f"✅ 已保存记录：**{rec['title']}**")
                # 清空输入框（删除已实例化控件的 state 是允许的，下次 rerun 会重置）
                st.session_state.pop("_loop_save_record_json", None)
                st.session_state.pop("_loop_save_record_title", None)
            except Exception as e:
                st.error(f"❌ 保存失败：{e}")

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔙 返回配置", use_container_width=True):
            if _gist_enabled():
                do_gist_save()
            st.session_state.phase = 'main'
            st.rerun()
    with c2:
        if st.button("⏹ 结束并清空音频", use_container_width=True, type="secondary"):
            if _gist_enabled():
                do_gist_save()
            st.session_state.phase = 'main'
            st.session_state.loop_audio  = None
            st.session_state.loop_config = None
            st.session_state.loop_meta   = None
            st.rerun()


# ═══════════════════════════════════════════════════
# 练习界面
# ═══════════════════════════════════════════════════
def screen_session():
    sess = st.session_state.session
    if not sess:
        st.session_state.phase = 'main'
        st.rerun()
        return

    cat  = st.session_state.category
    store = st.session_state.store
    s    = sess.stats()
    word = sess.current_word()
    # 修复：原版此处 `if not word: return` 会白屏卡死。
    # 队列空 = 要么全部完成（去完成页），要么状态异常（回主页）。
    if not word:
        if sess.is_done():
            st.session_state.phase = 'done'
        else:
            st.session_state.phase   = 'main'
            st.session_state.session = None
        st.rerun()
        return

    audio_key = f"{word}::{st.session_state.speed}"
    if audio_key != st.session_state.last_audio_word:
        with st.spinner("🎵 加载音频..."):
            st.session_state.cur_audio = get_audio(
                word, st.session_state.voice, st.session_state.speed)
        st.session_state.last_audio_word = audio_key
        st.session_state.autoplay = True

    det   = sess.word_detail(word)
    _sl   = {1: "✅ 认识", 2: "🟡 模糊", 3: "❌ 不会"}
    gstr  = f"第 {s['gid']} 组" if s['gid'] else "🔁 收尾循环"

    fail_tip = ""
    if det['fail_cnt'] >= FAIL_THRESHOLD:
        fail_tip = f" · ⚠️ 卡词（共③ {det['fail_cnt']} 次）"
    elif det['fail_cnt'] > 0:
        fail_tip = f" · ③×{det['fail_cnt']}"
    dgr_tip  = f" · 升难 {det['dgr_cnt']}/3" if det['dgr_cnt'] else ""

    w_obj    = sess.word_map[word]
    is_temp  = bool(w_obj.get('in_temp', False))
    temp_tag = " · ⭐临时" if is_temp else ""

    st.caption(
        f"**{gstr}**  <span class='cat-pill'>{CATEGORIES[cat]}</span>"
        f" · 队列剩余 {s['queue_rem']} · 已通过 {s['done']}/{s['total']}"
        f" · {_sl[det['state']]}{dgr_tip}{fail_tip}{temp_tag}",
        unsafe_allow_html=True,
    )
    st.progress(s['done'] / max(s['total'], 1))

    # ── 双盒子：原词 + 释义（同步显示/隐藏）──
    show_all = st.session_state.get('show_word', False)
    meaning_text = (w_obj.get('meaning', '') or '').strip() or '（无释义）'

    cls = "" if show_all else " hidden"
    wtxt = word if show_all else "？"
    mtxt = meaning_text if show_all else "？"

    col_w, col_m = st.columns(2)
    with col_w:
        st.markdown(f'<div class="word-box{cls}">{wtxt}</div>',
                    unsafe_allow_html=True)
    with col_m:
        st.markdown(f'<div class="meaning-box{cls}">{mtxt}</div>',
                    unsafe_allow_html=True)

    st.toggle("👁 显示单词与释义", key='show_word')

    # ── 音频 & 速度 ──
    autoplay = st.session_state.autoplay
    st.session_state.autoplay = False
    if st.session_state.cur_audio:
        st.audio(st.session_state.cur_audio, format='audio/mpeg', autoplay=autoplay)
    else:
        st.caption("⚠️ 音频未生成，请点「🔊 重播」")

    col_sp, col_rp = st.columns([3, 1])
    with col_sp:
        spd_name = st.selectbox(
            "速度", list(SPEEDS.keys()),
            index=list(SPEEDS.values()).index(st.session_state.speed),
            label_visibility="collapsed",
        )
        new_speed = SPEEDS[spd_name]
        if new_speed != st.session_state.speed:
            st.session_state.speed = new_speed
            st.session_state.last_audio_word = ''
            st.rerun()
    with col_rp:
        if st.button("🔊 重播", use_container_width=True):
            st.session_state.last_audio_word = ''
            st.rerun()

    st.divider()

    # ── 临时列表按钮（独立一行·醒目）──
    temp_label = "⭐ 已在临时列表 · 点击移出" if is_temp else "☆ 加入临时列表"
    if st.button(temp_label, use_container_width=True, type="secondary",
                 key=f"temp_btn_{word}"):
        new_val = store.toggle_temp(word, cat)
        w_obj['in_temp'] = new_val
        st.toast(f"{'⭐ 已加入临时列表' if new_val else '☆ 已从临时列表移出'}")
        st.rerun()

    st.caption("**短期评级** — 立即影响播放队列")

    def do_rate(lv):
        st.session_state.pop("show_word", None)
        result = sess.rate(word, lv)
        if result == 'session_done':
            st.session_state.phase = 'done'
        elif result == 'group_done':
            st.session_state.phase = 'group_done'
        st.rerun()

    bc1, bc2, bc3 = st.columns(3)
    with bc1:
        if st.button("① 认识", use_container_width=True, type="secondary"):
            do_rate(1)
    with bc2:
        if st.button("② 模糊", use_container_width=True):
            do_rate(2)
    with bc3:
        if st.button("③ 不会", use_container_width=True, type="primary"):
            do_rate(3)

    nc1, nc2, nc3 = st.columns(3)
    with nc1:
        if st.button("◀ 上一", disabled=(sess.q_pos == 0), use_container_width=True):
            if sess.prev():
                st.session_state.pop("show_word", None)
                st.rerun()
    with nc2:
        if st.button("跳过 ▶", use_container_width=True):
            result = sess.skip()
            st.session_state.pop("show_word", None)
            if result == 'session_done':
                st.session_state.phase = 'done'
            elif result == 'group_done':
                st.session_state.phase = 'group_done'
            st.rerun()
    with nc3:
        if st.button("⏹ 退出", use_container_width=True):
            if _gist_enabled():
                do_gist_save()
            st.session_state.phase   = 'main'
            st.session_state.session = None
            st.rerun()

    cur_lv = store.get_long(word, cat)
    with st.expander(f"长期评级（当前 {cur_lv} 级）"):
        lv_map = {0: "0 新词", 1: "1 掌握", 2: "2 模糊", 3: "3 重点"}
        lc1, lc2, lc3, lc4 = st.columns(4)
        for lv, col in zip([0, 1, 2, 3], [lc1, lc2, lc3, lc4]):
            btn_type = "primary" if lv == cur_lv else "secondary"
            if col.button(lv_map[lv], key=f"long_{word}_{lv}",
                          use_container_width=True, type=btn_type):
                store.update_long(word, cat, lv)
                sess.word_map[word]['long_level'] = lv
                st.toast(f"✅ 已保存为长期 {lv} 级")
                st.rerun()


# ═══════════════════════════════════════════════════
# 组间过渡
# ═══════════════════════════════════════════════════
def screen_group_done():
    sess = st.session_state.session
    if not sess:
        st.session_state.phase = 'main'
        st.rerun()
        return

    s     = sess.stats()
    carry = sess.last_carryover

    if s['in_loop']:
        st.info(f"🔁 **进入收尾循环** — {len(carry)} 个词将继续出现，直到全部通过")
    else:
        gid = s['gid'] or '（最后）'
        st.info(f"📋 **进入第 {gid} 组** — {len(carry)} 个上组词将随机混入新组")

    st.markdown(f"已通过：**{s['done']}** / {s['total']}")
    if carry:
        st.markdown("**携带词（未完成）：**")
        st.write("　".join(carry))
    else:
        st.success("✅ 上一组词全部通过！")

    if st.button("继续 ▶", type="primary", use_container_width=True):
        st.session_state.phase = 'session'
        st.rerun()
    if st.button("⏹ 退出练习", use_container_width=True):
        if _gist_enabled():
            do_gist_save()
        st.session_state.phase   = 'main'
        st.session_state.session = None
        st.rerun()


# ═══════════════════════════════════════════════════
# 完成界面
# ═══════════════════════════════════════════════════
def screen_done():
    sess = st.session_state.session
    if not sess:
        st.session_state.phase = 'main'
        st.rerun()
        return

    s = sess.stats()
    st.balloons()
    st.success(f"🎉 练习完成！共 {s['total']} 个词全部短期通过 ✓")

    all_words = sorted(sess.word_map.values(), key=lambda w: -w['long_level'])
    rv_words  = [w['word'] for w in all_words]

    if rv_words:
        st.subheader("按长期等级顺序复习")
        idx  = max(0, min(st.session_state.rv_idx, len(rv_words) - 1))
        rw   = rv_words[idx]
        rlv  = sess.word_map[rw]['long_level']
        rlbl = {0: "新词", 1: "已掌握", 2: "模糊", 3: "重点"}[rlv]
        rmn  = sess.word_map[rw].get('meaning', '') or '（无释义）'

        col_w, col_m = st.columns(2)
        with col_w:
            st.markdown(f'<div class="word-box">{rw}</div>', unsafe_allow_html=True)
        with col_m:
            st.markdown(f'<div class="meaning-box">{rmn}</div>', unsafe_allow_html=True)

        st.caption(f"长期 {rlv} 级（{rlbl}）— {idx+1} / {len(rv_words)}")
        if st.button("🔊 播放发音", use_container_width=True):
            audio = get_audio(rw, st.session_state.voice, st.session_state.speed)
            render_audio(audio, word=rw, autoplay=True)
        rc1, rc2 = st.columns(2)
        if rc1.button("◀ 上一个", use_container_width=True):
            if idx > 0:
                st.session_state.rv_idx = idx - 1
                st.rerun()
        if rc2.button("下一个 ▶", use_container_width=True):
            if idx < len(rv_words) - 1:
                st.session_state.rv_idx = idx + 1
                st.rerun()

    st.divider()
    st.subheader("词汇总览")
    lv_names = {0: "新词", 1: "已掌握", 2: "模糊", 3: "重点"}
    for lv in [3, 2, 1, 0]:
        ws = [w['word'] for w in all_words if w['long_level'] == lv]
        if ws:
            st.markdown(f"**{lv}级 {lv_names[lv]}（{len(ws)}个）**")
            st.write("　".join(ws))

    st.divider()

    col_gist, col_dl = st.columns(2)
    with col_gist:
        if _gist_enabled():
            if st.button("🐙 保存到 GitHub Gist", type="primary",
                         use_container_width=True):
                do_gist_save()
        else:
            st.caption("配置 GitHub Token 后可直接同步到云端")
    with col_dl:
        st.download_button(
            "⬇ 下载本地 CSV",
            data=st.session_state.store.export_csv(),
            file_name="japanese_words.csv",
            mime="text/csv",
            use_container_width=True,
        )

    if st.button("🏠 返回主页", use_container_width=True):
        st.session_state.phase   = 'main'
        st.session_state.session = None
        st.session_state.rv_idx  = 0
        st.rerun()


# ═══════════════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════════════
{
    'main':         screen_main,
    'session':      screen_session,
    'group_done':   screen_group_done,
    'done':         screen_done,
    'loop_playing': screen_loop_playing,
}.get(st.session_state.phase, screen_main)()
