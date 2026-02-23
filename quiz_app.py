import random
import time
import re
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Dict, Optional, Set

import pandas as pd
import streamlit as st
import gspread
import warnings
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r".*Method signature's arguments 'range_name' and 'values' will change their order.*"
)

DEFAULT_EXCEL = "TOEIC_frequent_words.xlsx"
CHOICES_N = 4
AUTO_NEXT_SECONDS = 1.0  # 正誤表示後の自動遷移秒


@dataclass
class Item:
    word: str
    jp: str
    pos: str = ""
    category: str = ""
    example: str = ""


def load_items(excel_path: str, sheet_name: str) -> List[Item]:
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    df = df.rename(columns={c: str(c).strip() for c in df.columns})

    if "Word" not in df.columns:
        raise ValueError("Word 列が見つかりません。")
    if "日本語訳" not in df.columns:
        raise ValueError("日本語訳 列が見つかりません。")

    for c in ["Word", "日本語訳", "品詞", "カテゴリ", "例文（英）"]:
        if c not in df.columns:
            df[c] = ""

    df = df.dropna(subset=["Word", "日本語訳"]).copy()

    df["Word"] = df["Word"].astype(str).str.strip()
    df["日本語訳"] = df["日本語訳"].astype(str).str.strip()
    df["品詞"] = df["品詞"].astype(str).replace({"nan": ""}).str.strip()
    df["カテゴリ"] = df["カテゴリ"].astype(str).replace({"nan": ""}).str.strip()
    df["例文（英）"] = df["例文（英）"].astype(str).replace({"nan": ""}).str.strip()

    items: List[Item] = []
    for _, r in df.iterrows():
        w = r["Word"]
        j = r["日本語訳"]
        if not w or not j:
            continue
        items.append(
            Item(
                word=w,
                jp=j,
                pos=r["品詞"],
                category=r["カテゴリ"],
                example=r["例文（英）"],
            )
        )

    # Word で重複除去（大文字小文字無視）
    seen = set()
    uniq: List[Item] = []
    for it in items:
        k = it.word.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq


def ensure_state():
    if "rng_seed" not in st.session_state:
        st.session_state.rng_seed = random.randint(1, 10**9)
    if "rng" not in st.session_state:
        st.session_state.rng = random.Random(st.session_state.rng_seed)

    if "vocab" not in st.session_state:
        st.session_state.vocab = []

    if "q" not in st.session_state:
        st.session_state.q = None
    if "answered" not in st.session_state:
        st.session_state.answered = False

    # 通常スコア
    if "score" not in st.session_state:
        st.session_state.score = 0
    if "total" not in st.session_state:
        st.session_state.total = 0

    # 復習スコア
    if "review_score" not in st.session_state:
        st.session_state.review_score = 0
    if "review_total" not in st.session_state:
        st.session_state.review_total = 0

    if "wrong_log" not in st.session_state:
        st.session_state.wrong_log = []


    # 復習対象（完全復習）
    if "review_set" not in st.session_state:
        st.session_state.review_set = set()  # Set[str] lowercased word

    if "q_id" not in st.session_state:
        st.session_state.q_id = 0

    if "last_result" not in st.session_state:
        st.session_state.last_result = None

    if "mode" not in st.session_state:
        st.session_state.mode = "normal"  # normal / review

    if "direction" not in st.session_state:
        st.session_state.direction = "en2ja"  # en2ja / ja2en

    # 自動遷移の二重実行防止
    if "auto_advanced_for" not in st.session_state:
        st.session_state.auto_advanced_for = None




def _get_gsheets_cfg() -> dict:
    return st.secrets["connections"]["gsheets"]


def _get_gspread_client():
    sa_info = dict(_get_gsheets_cfg()["service_account"])
    # Streamlit secrets may provide AttrDict-like objects; dict() makes it plain mapping.
    return gspread.service_account_from_dict(sa_info)


def _get_gspread_worksheet(ws_name: str):
    cfg = _get_gsheets_cfg()
    spreadsheet_ref = cfg.get("spreadsheet", "")
    client = _get_gspread_client()
    if not spreadsheet_ref:
        raise ValueError("connections.gsheets.spreadsheet が未設定です")
    if str(spreadsheet_ref).startswith("http"):
        sh = client.open_by_url(spreadsheet_ref)
    else:
        sh = client.open_by_key(str(spreadsheet_ref))
    try:
        ws = sh.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ws_name, rows=1000, cols=2)
        ws.update("A1:A1", [["Word"]])
    return ws


def _get_gsheets_worksheet_base() -> str:
    """secrets.toml で指定された worksheet 名のベース（未指定なら 'wrong_log'）。"""
    try:
        return st.secrets["connections"]["gsheets"].get("worksheet", "wrong_log")
    except Exception:
        return "wrong_log"

def worksheet_name_for_direction(direction: str) -> str:
    base = _get_gsheets_worksheet_base()
    suffix = "en_ja" if direction == "en2ja" else "ja_en"
    return f"{base}_{suffix}"


def require_gsheets_config() -> None:
    """Google Sheets 接続設定があるかを確認。なければエラーで停止。"""
    try:
        _ = st.secrets["connections"]["gsheets"]["service_account"]["client_email"]
        # worksheet は未指定でもよい
    except Exception:
        st.error(
            "Google Sheets 接続設定が見つかりません。"
            " `.streamlit/secrets.toml`（ローカル）または Community Cloud の Secrets に "
            "`[connections.gsheets]` と `[connections.gsheets.service_account]` を設定してください。"
        )
        st.stop()

def load_wrong_log_from_gsheets() -> List[Dict]:
    """間違いログを Google Sheets（2タブ: en_ja / ja_en, Wordのみ）から読み込む。"""
    try:
        out: List[Dict] = []
        for direction in ["en2ja", "ja2en"]:
            ws = _get_gspread_worksheet(worksheet_name_for_direction(direction))
            values = ws.get_all_records()
            if not values:
                continue
            for row in values:
                word = str(row.get("Word") or "").strip()
                if not word:
                    continue
                out.append({"Word": word, "Direction": direction})
        st.session_state["gsheets_last_error"] = ""
        return out
    except Exception as e:
        st.session_state["gsheets_last_error"] = f"{type(e).__name__}: {e}"
        return []


def _unique_words_for_direction(log: List[Dict], direction: str) -> List[str]:
    seen = set()
    words: List[str] = []
    for r in log:
        if not isinstance(r, dict):
            continue
        rdir = str(r.get("Direction", "")).strip()
        if rdir != direction:
            continue
        w = str(r.get("Word", "")).strip()
        k = w.lower()
        if not k or k in seen:
            continue
        seen.add(k)
        words.append(w)
    words.sort(key=lambda s: s.lower())
    return words


def save_wrong_log_to_gsheets(log: List[Dict]) -> None:
    """間違いログを Google Sheets に保存（2タブ・Wordのみ）。"""
    try:
        for direction in ["en2ja", "ja2en"]:
            ws_obj = _get_gspread_worksheet(worksheet_name_for_direction(direction))
            words = _unique_words_for_direction(log, direction)
            rows_2d = [["Word"]] + [[w] for w in words]
            ws_obj.clear()
            ws_obj.update(rows_2d)
        st.session_state["gsheets_last_error"] = ""
        st.session_state["gsheets_last_save_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        st.session_state["gsheets_last_error"] = f"{type(e).__name__}: {e}"
def ensure_wrong_log_persistence():
    """セッション開始時に一度だけ Sheets からログを読み込む。"""
    if "wrong_log_loaded" not in st.session_state:
        st.session_state.wrong_log_loaded = False

    if not st.session_state.wrong_log_loaded:
        loaded = load_wrong_log_from_gsheets()
        if loaded:
            if not st.session_state.wrong_log:
                st.session_state.wrong_log = loaded
        st.session_state.wrong_log_loaded = True


def append_wrong_and_save(record: Dict) -> None:
    """ログ追記して Sheets に保存（Word/Directionのみ保持）。"""
    word = str(record.get("Word", "")).strip()
    direction = str(record.get("Direction", "")).strip()
    if not word or direction not in {"en2ja", "ja2en"}:
        return
    st.session_state.wrong_log.append({"Word": word, "Direction": direction})

    # 方向別に同一単語を1件化（常時）
    st.session_state.wrong_log = compact_wrong_log(st.session_state.wrong_log)
    save_wrong_log_to_gsheets(st.session_state.wrong_log)


def remove_wrong_word_and_save(word: str, direction: Optional[str] = None) -> None:
    """指定単語を wrong_log から削除し、Sheets に保存する（復習で正解した時用）。direction指定で片側だけ削除。"""
    key = str(word).strip().lower()
    if not key:
        return
    before = len(st.session_state.wrong_log)
    new_log = []
    for r in st.session_state.wrong_log:
        rw = str(r.get("Word", "")).strip().lower()
        rd = str(r.get("Direction", "")).strip()
        if rw == key and (direction is None or rd == direction):
            continue
        new_log.append(r)
    st.session_state.wrong_log = new_log
    if len(st.session_state.wrong_log) != before:
        save_wrong_log_to_gsheets(st.session_state.wrong_log)

def compact_wrong_log(log: List[Dict]) -> List[Dict]:
    """(Direction, Word) ごとに1件だけ残す（Word/Directionのみ保持）。"""
    grouped: Dict[tuple, Dict] = {}
    for r in log:
        if not isinstance(r, dict):
            continue
        w = str(r.get("Word", "")).strip()
        d = str(r.get("Direction", "")).strip()
        if not w or d not in {"en2ja", "ja2en"}:
            continue
        key = (d, w.lower())
        if key not in grouped:
            grouped[key] = {"Word": w, "Direction": d}
    # 表示/保存の安定性のため方向→Word順
    return sorted(grouped.values(), key=lambda x: (x.get("Direction",""), str(x.get("Word","")).lower()))


def rebuild_review_set_from_wrong_log():
    """現在の出題方向に対応する復習語のみを review_set に再構築する。"""
    vocab_words = {it.word.lower() for it in st.session_state.vocab}
    current_direction = st.session_state.get("direction", "en2ja")
    wrong_words: Set[str] = set()
    for r in st.session_state.wrong_log:
        rdir = str(r.get("Direction", "")).strip()
        if rdir != current_direction:
            continue
        w = str(r.get("Word", "")).strip().lower()
        if w and w in vocab_words:
            wrong_words.add(w)
    st.session_state.review_set = wrong_words


def current_radio_key() -> str:
    return f"choice_q{st.session_state.q_id}"


def make_question(pool: List[Item], all_items: List[Item], rng: random.Random, direction: str) -> Optional[Dict]:
    if not pool:
        return None

    q = rng.choice(pool)

    if direction == "en2ja":
        prompt = q.word
        correct = q.jp
        # 日本語訳の選択肢
        distractor_pool = [it.jp for it in all_items if it.word.lower() != q.word.lower() and it.jp != q.jp]
        # 同一日本語訳が多い場合に備えてユニーク化
        distractor_pool = list(dict.fromkeys(distractor_pool))
    else:
        prompt = q.jp
        correct = q.word
        # 英単語の選択肢
        distractor_pool = [it.word for it in all_items if it.word.lower() != q.word.lower()]
        distractor_pool = list(dict.fromkeys(distractor_pool))

    rng.shuffle(distractor_pool)
    distractors = distractor_pool[: max(0, CHOICES_N - 1)]

    choices = distractors + [correct]
    # もし十分な選択肢が作れない場合（単語数が極端に少ないなど）も落ちないようにする
    choices = list(dict.fromkeys(choices))  # 重複除去（順序維持）
    if len(choices) < 2:
        return None

    rng.shuffle(choices)

    return {
        "item": q,
        "prompt": prompt,
        "choices": choices,
        "correct": correct,
        "direction": direction,
    }


def next_question():
    st.session_state.q_id += 1

    all_items: List[Item] = st.session_state.vocab
    if not all_items:
        st.session_state.q = None
        st.session_state.answered = False
        st.session_state.last_result = None
        st.session_state.auto_advanced_for = None
        return

    if st.session_state.mode == "review":
        pool = [it for it in all_items if it.word.lower() in st.session_state.review_set]
        if not pool:
            st.session_state.q = None
            st.session_state.answered = False
            st.session_state.last_result = None
            st.session_state.auto_advanced_for = None
            return
    else:
        pool = all_items

    q = make_question(pool, all_items, st.session_state.rng, st.session_state.direction)
    st.session_state.q = q
    st.session_state.answered = False
    st.session_state.last_result = None
    st.session_state.auto_advanced_for = None


def reset_quiz(reset_wrong_log: bool = False):
    st.session_state.score = 0
    st.session_state.total = 0
    st.session_state.review_score = 0
    st.session_state.review_total = 0

    if reset_wrong_log:
        st.session_state.wrong_log = []
        st.session_state.review_set = set()
    else:
        rebuild_review_set_from_wrong_log()


    st.session_state.rng_seed = random.randint(1, 10**9)
    st.session_state.rng = random.Random(st.session_state.rng_seed)

    st.session_state.q_id = 0
    next_question()


def grade_current_selection():
    if st.session_state.answered:
        return

    key = current_radio_key()
    selected = st.session_state.get(key)
    if selected is None:
        return

    q = st.session_state.q
    if not q:
        return

    item: Item = q["item"]
    correct = q["correct"]
    direction = q["direction"]

    if st.session_state.mode == "review":
        st.session_state.review_total += 1
    else:
        st.session_state.total += 1

    if selected == correct:
        if st.session_state.mode == "review":
            st.session_state.review_score += 1
            st.session_state.review_set.discard(item.word.lower())
            remove_wrong_word_and_save(item.word, direction)
        else:
            st.session_state.score += 1

        st.session_state.last_result = {
            "correct": True,
            "correct_value": correct,
            "selected": selected,
            "direction": direction,
        }
    else:
        append_wrong_and_save({"Word": item.word, "Direction": direction})
        st.session_state.review_set.add(item.word.lower())
        st.session_state.last_result = {
            "correct": False,
            "correct_value": correct,
            "selected": selected,
            "direction": direction,
        }

    st.session_state.answered = True
    st.session_state.auto_advanced_for = st.session_state.q_id


def do_skip():
    q = st.session_state.q
    if not q:
        return

    item: Item = q["item"]
    correct = q["correct"]
    direction = q["direction"]

    if st.session_state.mode == "review":
        st.session_state.review_total += 1
    else:
        st.session_state.total += 1

    append_wrong_and_save({"Word": item.word, "Direction": direction})
    st.session_state.review_set.add(item.word.lower())

    next_question()


def render_score():
    mode = st.session_state.mode
    direction = st.session_state.direction

    if mode == "review":
        total = st.session_state.review_total
        score = st.session_state.review_score
        remaining = len(st.session_state.review_set)
        label = "復習"
    else:
        total = st.session_state.total
        score = st.session_state.score
        remaining = None
        label = "通常"

    acc = (score / total * 100.0) if total else 0.0
    dir_label = "英→日" if direction == "en2ja" else "日→英"
    st.markdown(f"**{label} / {dir_label}：{score} / {total}（正答率 {acc:.0f}%）**")
    if remaining is not None:
        st.caption(f"復習残り：{remaining}語")


# ===== UI =====
st.set_page_config(page_title="TOEIC Quiz", layout="centered")
ensure_state()
require_gsheets_config()
ensure_wrong_log_persistence()

with st.sidebar:
    st.header("設定")

    excel_path = st.text_input("単語表Excelのパス", value=DEFAULT_EXCEL)
    # Google Sheets への永続保存（UI表示は最小化）
    try:
        # 設定存在チェックのみ。読み込み失敗時は起動時処理側でエラー表示される。
        _ = st.secrets.get("connections", {}).get("gsheets", {})
        # 書き込みエラーだけは表示（最終保存時刻などは非表示）
        last_err = st.session_state.get("gsheets_last_error", "")
        if last_err:
            st.error(f"Sheets書き込みエラー: {last_err}")
    except Exception:
        rebuild_review_set_from_wrong_log()

    st.caption(f"現在のログ件数: {len(st.session_state.get('wrong_log', []))}")


    sheet_names: List[str] = []
    try:
        xls = pd.ExcelFile(excel_path)
        sheet_names = xls.sheet_names
    except Exception as e:
        st.warning(f"Excelが読み込めません: {e}")

    sheet = st.selectbox("シート", options=sheet_names) if sheet_names else None

    st.divider()

    # 出題方向
    dir_label = st.radio(
        "出題方向",
        options=["英→日（英単語→日本語訳）", "日→英（日本語訳→英単語）"],
        index=0 if st.session_state.direction == "en2ja" else 1,
    )
    new_dir = "en2ja" if dir_label.startswith("英→日") else "ja2en"
    if new_dir != st.session_state.direction:
        st.session_state.direction = new_dir
        # 復習モード中は、方向に対応する復習集合へ作り直す
        if st.session_state.mode == "review" and st.session_state.vocab:
            rebuild_review_set_from_wrong_log()
        # vocabがあるときだけ次問を作り直す
        if st.session_state.vocab:
            next_question()

    # 学習モード
    mode_label = st.radio(
        "学習モード",
        options=["通常（全体から出題）", "復習（間違いから出題・正解で消える）"],
        index=0 if st.session_state.mode == "normal" else 1,
    )
    new_mode = "normal" if mode_label.startswith("通常") else "review"
    if new_mode != st.session_state.mode:
        st.session_state.mode = new_mode
        if new_mode == "review":
            rebuild_review_set_from_wrong_log()
        if st.session_state.vocab:
            next_question()

    st.divider()

    if st.button("単語を読み込む"):
        if not sheet:
            st.error("シートを選んでください。")
        else:
            try:
                loaded = load_items(excel_path, sheet)
                if len(loaded) < 2:
                    st.error("単語数が少なすぎます。")
                else:
                    st.session_state.vocab = loaded
                    rebuild_review_set_from_wrong_log()
                    reset_quiz(reset_wrong_log=False)
                    st.success(f"{len(loaded)} 語を読み込みました。")
            except Exception as e:
                st.error(f"読み込みに失敗: {e}")

    if st.button("スコアリセット"):
        if st.session_state.vocab:
            reset_quiz(reset_wrong_log=False)

    if st.button("間違いログ消去"):
        st.session_state.wrong_log = []
        st.session_state.review_set = set()
        save_wrong_log_to_gsheets(st.session_state.wrong_log)
        if st.session_state.vocab:
            next_question()

# ロード待ち
if not st.session_state.vocab:
    st.info("左のサイドバーでExcelを指定し、「単語を読み込む」を押してください。")
    st.stop()

# 初回の問題生成
if st.session_state.q is None:
    if st.session_state.mode == "review":
        rebuild_review_set_from_wrong_log()
    next_question()

# 復習完了
if st.session_state.mode == "review" and (st.session_state.q is None or len(st.session_state.review_set) == 0):
    st.success("復習完了！ 🎉（復習対象がありません）")
    st.info("通常モードで間違えると復習対象が溜まります。")
    st.stop()

# 問題が作れない（選択肢不足など）
if st.session_state.q is None:
    st.error("問題が生成できませんでした（単語数や重複の状況を確認してください）。")
    st.stop()

q = st.session_state.q
item: Item = q["item"]
prompt = q["prompt"]
choices: List[str] = q["choices"]
direction = q["direction"]

answered_count = st.session_state.review_total if st.session_state.mode == "review" else st.session_state.total
st.subheader(f"Q{answered_count + 1}")

render_score()

# プロンプト表示
st.markdown(f"### **{prompt}**")

# 例文は「英→日」のときだけ表示（＝日→英では表示しない）
if direction == "en2ja" and item.example:
    st.caption(f"例文: {item.example}")

# 4択（選択で即判定）
rkey = current_radio_key()
st.radio(
    "答えを選んでください（選択すると判定されます）",
    options=choices,
    index=None,
    key=rkey,
    on_change=grade_current_selection,
    disabled=st.session_state.answered,
)

st.button("スキップ", on_click=do_skip, disabled=st.session_state.answered)

# 回答後 → 表示 → 自動で次へ
if st.session_state.answered and st.session_state.last_result:
    res = st.session_state.last_result
    if res["correct"]:
        st.success("正解！")
    else:
        st.error("不正解…")

    if direction == "en2ja":
        st.write(f"**正解:** {res['correct_value']}（日本語訳）")
    else:
        st.write(f"**正解:** {res['correct_value']}（英単語）")

    if st.session_state.auto_advanced_for == st.session_state.q_id:
        bar = st.progress(0)
        steps = 10
        for i in range(steps):
            bar.progress(int((i + 1) / steps * 100))
            time.sleep(AUTO_NEXT_SECONDS / steps)

        next_question()
        st.rerun()
