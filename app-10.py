"""
KBC-style live quiz — single-file build for Streamlit Cloud.

Deploy this ONE file as your app's main file. Two views, selected by URL:

  https://your-app.streamlit.app/            -> contestant/TV display (public)
  https://your-app.streamlit.app/?view=admin -> host control panel (password gated)

Set the admin password via Streamlit Cloud's Settings -> Secrets:
    KBC_ADMIN_PASSWORD = "your-chosen-password"
(falls back to "changeme123" locally if no secret is set -- change this
before running with a real contestant).

Requirements (put in requirements.txt alongside this file):
    streamlit>=1.32.0
    streamlit-autorefresh>=1.0.1
"""

import streamlit as st
import streamlit.components.v1 as components
import sqlite3
import json
import time
import math
import random
import os

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False


# =====================================================================
# SECTION 1: GAME STATE (shared SQLite-backed state)
# =====================================================================
# Both views talk only to the functions in this section. It's backed by
# a single-row SQLite table on disk. The admin view writes a row; the
# display view polls (via streamlit_autorefresh) and re-reads it every
# ~1s, so an admin button tap shows up on the display within about a
# second -- same trick as a live-updating bar chart from a voting app.
# This relies on Streamlit Cloud running your app as a single instance
# (the default), so both browser tabs hit the same file on the same
# machine. Only one game runs at a time.

DB_PATH = os.path.join(os.path.dirname(__file__), "kbc_state.db")

PRIZE_LADDER = [
    1_000, 2_000, 3_000, 5_000, 10_000,
    20_000, 40_000, 80_000, 1_60_000, 3_20_000,
    6_40_000, 12_50_000, 25_00_000, 50_00_000, 7_00_00_000,
]
SAFE_LEVELS = {5, 10}  # 1-indexed question numbers that are "safe" (Q5 -> ₹10,000, Q10 -> ₹3,20,000)


def format_money(amount: int) -> str:
    return f"₹{amount:,}"


def guaranteed_amount(current_question_index: int) -> int:
    """Amount the contestant walks away with if they quit or answer wrong,
    based on the last SAFE level cleared. current_question_index is 0-based
    (index of the question about to be / just been played)."""
    last_cleared_q_number = current_question_index
    safe_amount = 0
    for lvl in sorted(SAFE_LEVELS):
        if last_cleared_q_number >= lvl:
            safe_amount = PRIZE_LADDER[lvl - 1]
    return safe_amount


def timer_remaining(state: dict) -> float:
    """Seconds left on the countdown, computed from wall-clock time so it
    stays accurate regardless of how often the display polls."""
    started_at = state.get("timer_started_at")
    total = state.get("timer_seconds", 0)
    if not started_at or not total:
        return 0
    elapsed = time.time() - started_at
    remaining = total - elapsed
    return max(0.0, remaining)


DEFAULT_STATE = {
    "status": "lobby",  # lobby | question | locked | revealed | won | quit | lost
    "current_question_index": 0,
    "active_question_bank": [],
    "used_flip_pool": [],
    "selected_option": None,
    "revealed_correct": False,
    "reveal_stage": None,  # None | "suspense" | "done"
    "eliminated_options": [],
    "lifelines_used": {"fifty_fifty": False, "phone_a_friend": False, "flip_question": False},
    "phone_a_friend_active": False,
    "phone_a_friend_text": "",
    "contestant_name": "",
    "final_amount": 0,
    "show_options": False,
    "timer_seconds": 0,
    "timer_started_at": None,
    "sound_cue": None,
    "sound_cue_id": 0,
    "last_updated": time.time(),
}


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS game (id INTEGER PRIMARY KEY, state TEXT)")
    conn.commit()
    return conn


def init_state_if_needed():
    conn = _get_conn()
    cur = conn.execute("SELECT state FROM game WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO game (id, state) VALUES (1, ?)", (json.dumps(DEFAULT_STATE),))
        conn.commit()
    conn.close()


def get_state() -> dict:
    init_state_if_needed()
    conn = _get_conn()
    cur = conn.execute("SELECT state FROM game WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    if row is None:
        return dict(DEFAULT_STATE)
    return json.loads(row[0])


def set_state(patch: dict):
    """Merge patch into current state and persist."""
    state = get_state()
    state.update(patch)
    state["last_updated"] = time.time()
    conn = _get_conn()
    conn.execute("UPDATE game SET state = ? WHERE id = 1", (json.dumps(state),))
    conn.commit()
    conn.close()
    return state


def trigger_sound(cue: str, extra_patch: dict = None):
    """Set a one-shot sound cue plus bump its id so the display can tell a
    fresh cue apart from one it already played."""
    state = get_state()
    patch = dict(extra_patch or {})
    patch["sound_cue"] = cue
    patch["sound_cue_id"] = state.get("sound_cue_id", 0) + 1
    return set_state(patch)


def reset_game(question_list, contestant_name=""):
    fresh = dict(DEFAULT_STATE)
    fresh["active_question_bank"] = question_list
    fresh["contestant_name"] = contestant_name
    fresh["last_updated"] = time.time()
    init_state_if_needed()  # ensure row id=1 exists before we try to UPDATE it
    conn = _get_conn()
    conn.execute("UPDATE game SET state = ? WHERE id = 1", (json.dumps(fresh),))
    conn.commit()
    conn.close()
    return fresh


# =====================================================================
# SECTION 2: QUESTION BANK
# =====================================================================
# MAIN_LADDER: exactly 15 questions mapped 1:1 to PRIZE_LADDER (easy -> hard).
# FLIP_POOL: extra questions used only when the host triggers "Flip the
# Question" to swap the current question without changing the prize level.

MAIN_LADDER = [
    {"id": "m1", "question": "Which planet is known as the 'Red Planet'?",
     "options": ["Venus", "Mars", "Jupiter", "Saturn"], "correct_index": 1},
    {"id": "m2", "question": "How many continents are there on Earth?",
     "options": ["5", "6", "7", "8"], "correct_index": 2},
    {"id": "m3", "question": "Who wrote the Indian national anthem 'Jana Gana Mana'?",
     "options": ["Bankim Chandra Chatterjee", "Rabindranath Tagore", "Sarojini Naidu", "Muhammad Iqbal"],
     "correct_index": 1},
    {"id": "m4", "question": "Which is the largest ocean on Earth?",
     "options": ["Atlantic Ocean", "Indian Ocean", "Arctic Ocean", "Pacific Ocean"], "correct_index": 3},
    {"id": "m5", "question": "The Taj Mahal is located in which Indian city?",
     "options": ["Delhi", "Jaipur", "Agra", "Lucknow"], "correct_index": 2},
    {"id": "m6", "question": "Which gas do plants primarily absorb from the atmosphere for photosynthesis?",
     "options": ["Oxygen", "Nitrogen", "Carbon Dioxide", "Hydrogen"], "correct_index": 2},
    {"id": "m7", "question": "Who was the first Prime Minister of independent India?",
     "options": ["Sardar Vallabhbhai Patel", "Jawaharlal Nehru", "Dr. Rajendra Prasad", "Lal Bahadur Shastri"],
     "correct_index": 1},
    {"id": "m8", "question": "Which is the longest river in the world?",
     "options": ["Amazon River", "Yangtze River", "Nile River", "Ganges River"], "correct_index": 2},
    {"id": "m9", "question": "The Great Barrier Reef is located off the coast of which country?",
     "options": ["Brazil", "Australia", "Thailand", "South Africa"], "correct_index": 1},
    {"id": "m10", "question": "Which Mughal emperor built the Red Fort in Delhi?",
     "options": ["Akbar", "Humayun", "Shah Jahan", "Aurangzeb"], "correct_index": 2},
    {"id": "m11", "question": "In which year did India's Chandrayaan-3 successfully land near the Moon's south pole?",
     "options": ["2019", "2021", "2023", "2024"], "correct_index": 2},
    {"id": "m12", "question": "Which Indian classical dance form originated in the state of Kerala?",
     "options": ["Bharatanatyam", "Kathakali", "Odissi", "Manipuri"], "correct_index": 1},
    {"id": "m13", "question": "The 'Battle of Plassey', which laid the foundation of British rule in India, was fought in which year?",
     "options": ["1757", "1764", "1857", "1600"], "correct_index": 0},
    {"id": "m14", "question": "Which scientist proposed the theory of general relativity?",
     "options": ["Isaac Newton", "Niels Bohr", "Albert Einstein", "Max Planck"], "correct_index": 2},
    {"id": "m15", "question": "As per the Constitution of India (Article 1), India, that is Bharat, shall be a Union of which of the following?",
     "options": ["States only", "States and Union Territories", "Provinces", "Princely States"], "correct_index": 1},
]

FLIP_POOL = [
    {"id": "f1", "question": "Which is the national animal of India?",
     "options": ["Lion", "Elephant", "Bengal Tiger", "Leopard"], "correct_index": 2},
    {"id": "f2", "question": "How many players are there in a cricket team on the field at one time?",
     "options": ["9", "10", "11", "12"], "correct_index": 2},
    {"id": "f3", "question": "Which Indian state is known as the 'Land of Five Rivers'?",
     "options": ["Haryana", "Punjab", "Rajasthan", "Gujarat"], "correct_index": 1},
    {"id": "f4", "question": "Who is known as the 'Father of the Nation' in India?",
     "options": ["Jawaharlal Nehru", "Bhagat Singh", "Mahatma Gandhi", "Subhas Chandra Bose"], "correct_index": 2},
    {"id": "f5", "question": "Which metal is liquid at room temperature?",
     "options": ["Iron", "Mercury", "Aluminium", "Zinc"], "correct_index": 1},
    {"id": "f6", "question": "The 'Sabarmati Ashram', associated with Mahatma Gandhi, is located in which city?",
     "options": ["Surat", "Ahmedabad", "Vadodara", "Rajkot"], "correct_index": 1},
    {"id": "f7", "question": "Which is the smallest country in the world by area?",
     "options": ["Monaco", "San Marino", "Vatican City", "Liechtenstein"], "correct_index": 2},
    {"id": "f8", "question": "Who composed India's national song 'Vande Mataram'?",
     "options": ["Rabindranath Tagore", "Bankim Chandra Chatterjee", "Iqbal", "Subramania Bharati"], "correct_index": 1},
    {"id": "f9", "question": "Which international organization has its headquarters in Geneva, Switzerland?",
     "options": ["United Nations (HQ)", "World Health Organization", "IMF", "World Bank"], "correct_index": 1},
    {"id": "f10", "question": "The 'Sepoy Mutiny' / First War of Independence in India took place in which year?",
     "options": ["1757", "1857", "1919", "1942"], "correct_index": 1},
    {"id": "f11", "question": "Who was awarded the first Nobel Prize in Literature from Asia?",
     "options": ["Mahatma Gandhi", "Rabindranath Tagore", "C.V. Raman", "R.K. Narayan"], "correct_index": 1},
    {"id": "f12", "question": "Which is the only Indian classical dance form that finds mention in the ancient text 'Natya Shastra' as its foundation for most others?",
     "options": ["Kathak", "Bharatanatyam", "Odissi", "Kuchipudi"], "correct_index": 1},
    {"id": "f13", "question": "The Treaty of Versailles, which formally ended World War I, was signed in which year?",
     "options": ["1917", "1918", "1919", "1920"], "correct_index": 2},
    {"id": "f14", "question": "Which particle, predicted by Peter Higgs, was confirmed by CERN in 2012?",
     "options": ["Neutrino", "Higgs Boson", "Quark", "Positron"], "correct_index": 1},
    {"id": "f15", "question": "Under the Indian Constitution, which Article deals with the abolition of untouchability?",
     "options": ["Article 14", "Article 15", "Article 17", "Article 21"], "correct_index": 2},
]


def build_active_bank():
    ladder = []
    for i, q in enumerate(MAIN_LADDER):
        qc = dict(q)
        qc["ladder_index"] = i
        ladder.append(qc)
    return ladder


# =====================================================================
# SECTION 3: SOUND CUES (synthesized tones, no external audio files)
# =====================================================================
# Short sine-wave tone sequences via the Web Audio API. Not a reproduction
# of the real show's copyrighted score -- just short synthesized stings for
# each game moment.

CUE_DEFINITIONS = {
    "question": [(440, 120, 40), (660, 160, 0)],
    "lock": [(520, 90, 30), (520, 90, 0)],
    "suspense": [(300, 220, 60), (340, 220, 60), (380, 260, 0)],
    "correct": [(523, 130, 30), (659, 130, 30), (784, 260, 0)],
    "wrong": [(300, 180, 40), (220, 260, 0)],
    "lifeline": [(392, 100, 20), (523, 100, 20), (659, 160, 0)],
    "win": [(523, 140, 20), (659, 140, 20), (784, 140, 20), (1046, 300, 80), (784, 140, 20), (1046, 420, 0)],
    "quit": [(500, 160, 40), (400, 160, 40), (330, 260, 0)],
}


def _build_js(sequence):
    steps = []
    t = 0
    for freq, dur_ms, gap_ms in sequence:
        steps.append(f"playTone({freq}, {t/1000}, {dur_ms/1000});")
        t += dur_ms + gap_ms
    return "\n".join(steps)


def play_cue(cue_name: str, cue_id: int):
    """Render an invisible component that plays the given cue exactly once
    per unique cue_id (Streamlit skips re-rendering identical component
    calls, so a changing cue_id is what makes a repeat cue actually fire)."""
    sequence = CUE_DEFINITIONS.get(cue_name)
    if not sequence:
        return
    js_calls = _build_js(sequence)
    html = f"""
    <div style="display:none" data-cue-id="{cue_id}"></div>
    <script>
    (function() {{
        try {{
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            function playTone(freq, startAt, dur) {{
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.value = freq;
                gain.gain.setValueAtTime(0.0001, ctx.currentTime + startAt);
                gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + startAt + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + startAt + dur);
                osc.connect(gain).connect(ctx.destination);
                osc.start(ctx.currentTime + startAt);
                osc.stop(ctx.currentTime + startAt + dur + 0.05);
            }}
            {js_calls}
        }} catch (e) {{
            console.log('KBC sound cue skipped:', e);
        }}
    }})();
    </script>
    """
    components.html(html, height=0, width=0)


# =====================================================================
# SECTION 4: STYLES
# =====================================================================

DISPLAY_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800&display=swap');

.stApp {
    background: radial-gradient(ellipse at top, #0a1642 0%, #050a24 55%, #020412 100%);
    font-family: 'Poppins', sans-serif;
    color: #f5f0e0;
}
#MainMenu, footer, header {visibility: hidden;}
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1100px; }

.kbc-title {
    text-align: center; font-size: 2rem; font-weight: 800; letter-spacing: 0.06em;
    background: linear-gradient(135deg, #ffd76a 0%, #ffb347 45%, #ff9d2e 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
    margin-bottom: 0.1rem; text-shadow: 0 0 40px rgba(255, 183, 71, 0.25);
}
.kbc-subtitle {
    text-align: center; color: #9fb3d9; font-size: 0.85rem; font-weight: 500;
    margin-bottom: 1.4rem; letter-spacing: 0.03em;
}

.kbc-question-card {
    background: linear-gradient(135deg, rgba(20, 30, 70, 0.85), rgba(10, 16, 45, 0.9));
    border: 2px solid rgba(255, 183, 71, 0.45); border-radius: 20px; padding: 1.8rem 2rem;
    box-shadow: 0 0 40px rgba(255, 183, 71, 0.12), inset 0 0 30px rgba(255, 183, 71, 0.04);
    margin-bottom: 1.3rem; min-height: 100px; display: flex; align-items: center;
    justify-content: center; text-align: center;
}
.kbc-question-text { font-size: 1.35rem; font-weight: 700; line-height: 1.5; color: #fff9ec; }
.kbc-question-number {
    text-align: center; color: #ffb347; font-weight: 700; font-size: 0.95rem;
    letter-spacing: 0.05em; margin-bottom: 0.6rem;
}

.kbc-option {
    background: linear-gradient(135deg, rgba(30, 42, 90, 0.75), rgba(15, 22, 55, 0.85));
    border: 1.5px solid rgba(120, 150, 220, 0.35); border-radius: 40px; padding: 0.95rem 1.6rem;
    margin-bottom: 0.7rem; font-size: 1.05rem; font-weight: 600; color: #e8edf9;
    display: flex; align-items: center; gap: 0.9rem; transition: all 0.3s ease;
}
.kbc-option-letter {
    background: rgba(255, 183, 71, 0.15); color: #ffb347; width: 30px; height: 30px;
    border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 0.85rem; flex-shrink: 0; border: 1.5px solid rgba(255, 183, 71, 0.4);
}
.kbc-option.eliminated { opacity: 0.15; filter: grayscale(1); }
.kbc-option.selected {
    border-color: #ffd76a; background: linear-gradient(135deg, rgba(255, 183, 71, 0.28), rgba(255, 157, 46, 0.2));
    box-shadow: 0 0 24px rgba(255, 183, 71, 0.35); animation: kbc-pulse 1s ease-in-out infinite;
}
.kbc-option.correct {
    border-color: #4ade80; background: linear-gradient(135deg, rgba(74, 222, 128, 0.3), rgba(34, 197, 94, 0.2));
    box-shadow: 0 0 30px rgba(74, 222, 128, 0.4);
}
.kbc-option.correct .kbc-option-letter { background: #22c55e; color: #05170a; border-color: #22c55e; }
.kbc-option.incorrect {
    border-color: #f87171; background: linear-gradient(135deg, rgba(248, 113, 113, 0.3), rgba(239, 68, 68, 0.2));
    box-shadow: 0 0 30px rgba(248, 113, 113, 0.4);
}
.kbc-option.incorrect .kbc-option-letter { background: #ef4444; color: #200404; border-color: #ef4444; }

@keyframes kbc-pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.015); } }

.kbc-ladder {
    background: linear-gradient(135deg, rgba(10, 16, 45, 0.9), rgba(5, 8, 25, 0.95));
    border: 1.5px solid rgba(120, 150, 220, 0.25); border-radius: 16px; padding: 0.9rem 0.7rem;
    display: flex; flex-direction: column-reverse;
}
.kbc-ladder-row {
    display: flex; justify-content: space-between; padding: 0.4rem 0.7rem; border-radius: 8px;
    font-size: 0.82rem; font-weight: 600; color: #7f8cad; margin-bottom: 2px;
}
.kbc-ladder-row.safe { color: #ffd76a; font-weight: 800; }
.kbc-ladder-row.current {
    background: linear-gradient(90deg, rgba(255, 183, 71, 0.25), rgba(255, 183, 71, 0.05));
    color: #fff9ec; box-shadow: 0 0 16px rgba(255, 183, 71, 0.2); transform: scale(1.03);
}
.kbc-ladder-row.cleared { color: #4ade80; opacity: 0.6; }

.kbc-lifelines { display: flex; gap: 0.7rem; justify-content: center; margin-bottom: 1.2rem; }
.kbc-lifeline-icon {
    width: 56px; height: 56px; border-radius: 50%;
    background: linear-gradient(135deg, rgba(30, 42, 90, 0.8), rgba(15, 22, 55, 0.9));
    border: 2px solid rgba(255, 183, 71, 0.4); display: flex; align-items: center;
    justify-content: center; font-size: 1.4rem;
}
.kbc-lifeline-icon.used { opacity: 0.2; filter: grayscale(1); border-color: #555; }

.kbc-phone-bubble {
    background: linear-gradient(135deg, rgba(255, 183, 71, 0.18), rgba(255, 157, 46, 0.08));
    border: 1.5px solid rgba(255, 183, 71, 0.4); border-radius: 18px; padding: 1.1rem 1.4rem;
    margin: 1rem 0; font-size: 1rem; font-style: italic; color: #fff3dc; text-align: center;
}

.kbc-timer-wrap { display: flex; justify-content: center; margin-bottom: 1rem; }
.kbc-timer-svg-box { position: relative; width: 84px; height: 84px; }
.kbc-timer-svg-box svg { transform: rotate(-90deg); }
.kbc-timer-track { fill: none; stroke: rgba(255,183,71,0.15); stroke-width: 6; }
.kbc-timer-progress {
    fill: none; stroke: #ffb347; stroke-width: 6; stroke-linecap: round;
    transition: stroke-dashoffset 0.9s linear, stroke 0.3s ease;
}
.kbc-timer-progress.urgent { stroke: #f87171; }
.kbc-timer-number {
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    display: flex; align-items: center; justify-content: center; font-size: 1.5rem;
    font-weight: 800; color: #ffd76a;
}
.kbc-timer-number.urgent { color: #f87171; animation: kbc-tick 1s ease-in-out infinite; }
@keyframes kbc-tick { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.15); } }

.kbc-suspense-wrap { text-align: center; padding: 1.4rem; animation: kbc-heartbeat 0.7s ease-in-out infinite; }
.kbc-suspense-text {
    font-size: 1.1rem; font-weight: 700; letter-spacing: 0.08em; color: #ffd76a; text-transform: uppercase;
}
.kbc-suspense-dots span {
    display: inline-block; width: 10px; height: 10px; margin: 0 5px; border-radius: 50%;
    background: #ffb347; animation: kbc-dot-bounce 1.1s ease-in-out infinite;
}
.kbc-suspense-dots span:nth-child(2) { animation-delay: 0.15s; }
.kbc-suspense-dots span:nth-child(3) { animation-delay: 0.3s; }
@keyframes kbc-dot-bounce {
    0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
    30% { transform: translateY(-10px); opacity: 1; }
}
@keyframes kbc-heartbeat {
    0%, 100% { transform: scale(1); filter: brightness(1); }
    50% { transform: scale(1.03); filter: brightness(1.15); }
}
.kbc-question-card.suspense {
    border-color: rgba(255, 183, 71, 0.8);
    box-shadow: 0 0 60px rgba(255, 183, 71, 0.35), inset 0 0 40px rgba(255, 183, 71, 0.08);
    animation: kbc-heartbeat 0.7s ease-in-out infinite;
}
.kbc-option.suspense-dim { opacity: 0.35; filter: grayscale(0.6); transition: opacity 0.4s ease; }
.kbc-option.eliminated.suspense-dim { opacity: 0.15; filter: grayscale(1); }

.kbc-result-banner {
    text-align: center; padding: 2.2rem 1.5rem; border-radius: 20px; font-size: 1.6rem;
    font-weight: 800; margin: 1rem 0;
}
.kbc-result-win {
    background: linear-gradient(135deg, rgba(74, 222, 128, 0.25), rgba(34, 197, 94, 0.12));
    border: 2px solid #4ade80; color: #d3ffe6;
}
.kbc-result-lost {
    background: linear-gradient(135deg, rgba(248, 113, 113, 0.25), rgba(239, 68, 68, 0.12));
    border: 2px solid #f87171; color: #ffe0e0;
}
.kbc-result-quit {
    background: linear-gradient(135deg, rgba(255, 183, 71, 0.25), rgba(255, 157, 46, 0.12));
    border: 2px solid #ffb347; color: #fff3dc;
}
</style>
"""

ADMIN_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800&display=swap');
.stApp { font-family: 'Poppins', sans-serif; background: #0d1224; color: #eef1fa; }
#MainMenu, footer, header {visibility: hidden;}
.block-container { padding-top: 1rem; max-width: 720px; }
.admin-badge {
    display: inline-block; background: rgba(255,183,71,0.15); color: #ffb347;
    border: 1px solid rgba(255,183,71,0.4); padding: 0.2rem 0.7rem; border-radius: 20px;
    font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em; margin-bottom: 0.6rem;
}
.admin-qcard {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px; padding: 1rem 1.2rem; margin-bottom: 0.8rem;
}
.admin-status-pill {
    display: inline-block; padding: 0.25rem 0.8rem; border-radius: 20px;
    font-size: 0.75rem; font-weight: 700; background: rgba(74,222,128,0.15); color: #4ade80;
    border: 1px solid rgba(74,222,128,0.4);
}
</style>
"""


# =====================================================================
# SECTION 5: DISPLAY VIEW (contestant/TV screen)
# =====================================================================

def render_ladder(current_index, status):
    rows = ""
    for i in range(len(PRIZE_LADDER) - 1, -1, -1):
        q_number = i + 1
        amount = format_money(PRIZE_LADDER[i])
        classes = "kbc-ladder-row"
        if q_number in SAFE_LEVELS:
            classes += " safe"
        if status not in ("lobby",) and i == current_index:
            classes += " current"
        elif status in ("revealed", "won") and i < current_index:
            classes += " cleared"
        rows += f'<div class="{classes}"><span>{q_number}</span><span>{amount}</span></div>'
    return f'<div class="kbc-ladder">{rows}</div>'


def render_option_letter(i):
    return ["A", "B", "C", "D"][i]


def render_timer_ring(seconds_left: float, total_seconds: float):
    if total_seconds <= 0:
        return ""
    radius = 36
    circumference = 2 * math.pi * radius
    frac = max(0.0, min(1.0, seconds_left / total_seconds))
    offset = circumference * (1 - frac)
    urgent = seconds_left <= 5
    urgent_class = " urgent" if urgent else ""
    display_seconds = max(0, math.ceil(seconds_left))
    return f"""
    <div class="kbc-timer-wrap">
        <div class="kbc-timer-svg-box">
            <svg width="84" height="84">
                <circle class="kbc-timer-track" cx="42" cy="42" r="{radius}"></circle>
                <circle class="kbc-timer-progress{urgent_class}" cx="42" cy="42" r="{radius}"
                    stroke-dasharray="{circumference:.1f}"
                    stroke-dashoffset="{offset:.1f}"></circle>
            </svg>
            <div class="kbc-timer-number{urgent_class}">{display_seconds}</div>
        </div>
    </div>
    """


def render_suspense_overlay():
    return """
    <div class="kbc-suspense-wrap">
        <div class="kbc-suspense-text">Lock kiya jaaye?</div>
        <div class="kbc-suspense-dots"><span></span><span></span><span></span></div>
    </div>
    """


def render_display():
    st.markdown(DISPLAY_CSS, unsafe_allow_html=True)

    if HAS_AUTOREFRESH:
        st_autorefresh(interval=1000, key="display_refresh")

    state = get_state()
    status = state["status"]
    q_index = state["current_question_index"]
    bank = state["active_question_bank"]

    last_played = st.session_state.get("last_sound_cue_id", 0)
    current_cue_id = state.get("sound_cue_id", 0)
    if current_cue_id != last_played and state.get("sound_cue"):
        play_cue(state["sound_cue"], current_cue_id)
        st.session_state["last_sound_cue_id"] = current_cue_id

    name = state.get("contestant_name") or "Contestant"
    st.markdown('<div class="kbc-title">KAUN BANEGA CROREPATI</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="kbc-subtitle">Playing: {name}</div>', unsafe_allow_html=True)

    col_main, col_ladder = st.columns([2.3, 1], gap="large")

    with col_ladder:
        st.markdown(render_ladder(q_index, status), unsafe_allow_html=True)

    with col_main:
        lifelines = state.get("lifelines_used", {})
        icons = {"fifty_fifty": "50:50", "phone_a_friend": "📞", "flip_question": "🔄"}
        lifeline_html = '<div class="kbc-lifelines">'
        for key, icon in icons.items():
            used = lifelines.get(key, False)
            cls = "kbc-lifeline-icon used" if used else "kbc-lifeline-icon"
            lifeline_html += f'<div class="{cls}">{icon}</div>'
        lifeline_html += '</div>'
        st.markdown(lifeline_html, unsafe_allow_html=True)

        if status in ("question", "locked") and state.get("show_options"):
            remaining = timer_remaining(state)
            total = state.get("timer_seconds", 0)
            if total > 0:
                st.markdown(render_timer_ring(remaining, total), unsafe_allow_html=True)

        if status == "lobby":
            st.markdown(
                '<div class="kbc-question-card"><div class="kbc-question-text">'
                'Welcome! The game will begin shortly...</div></div>',
                unsafe_allow_html=True,
            )

        elif status in ("question", "locked", "revealed"):
            if q_index < len(bank):
                q = bank[q_index]
                in_suspense = state.get("reveal_stage") == "suspense"
                card_class = "kbc-question-card suspense" if in_suspense else "kbc-question-card"

                st.markdown(
                    f'<div class="kbc-question-number">QUESTION {q_index + 1} '
                    f'&nbsp;·&nbsp; FOR {format_money(PRIZE_LADDER[q_index])}</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div class="{card_class}"><div class="kbc-question-text">'
                    f'{q["question"]}</div></div>',
                    unsafe_allow_html=True,
                )

                if in_suspense:
                    st.markdown(render_suspense_overlay(), unsafe_allow_html=True)

                if state.get("phone_a_friend_active") or state.get("phone_a_friend_text"):
                    txt = state.get("phone_a_friend_text") or "Connecting the call..."
                    st.markdown(f'<div class="kbc-phone-bubble">📞 "{txt}"</div>', unsafe_allow_html=True)

                if state.get("show_options"):
                    eliminated = set(state.get("eliminated_options", []))
                    selected = state.get("selected_option")
                    revealed = state.get("revealed_correct")
                    correct_idx = q["correct_index"]

                    for i, opt in enumerate(q["options"]):
                        classes = "kbc-option"
                        if i in eliminated:
                            classes += " eliminated"
                        if revealed:
                            if i == correct_idx:
                                classes += " correct"
                            elif i == selected and i != correct_idx:
                                classes += " incorrect"
                        elif selected == i:
                            classes += " selected"
                        elif in_suspense and i != selected:
                            classes += " suspense-dim"

                        st.markdown(
                            f'<div class="{classes}">'
                            f'<span class="kbc-option-letter">{render_option_letter(i)}</span>'
                            f'<span>{opt}</span></div>',
                            unsafe_allow_html=True,
                        )
            else:
                st.info("Waiting for next question...")

        elif status == "won":
            amt = format_money(state.get("final_amount", PRIZE_LADDER[-1]))
            st.markdown(f'<div class="kbc-result-banner kbc-result-win">🏆 {name} WINS {amt}! 🏆</div>',
                        unsafe_allow_html=True)

        elif status == "lost":
            amt = format_money(state.get("final_amount", 0))
            st.markdown(f'<div class="kbc-result-banner kbc-result-lost">Game Over — {name} takes home {amt}</div>',
                        unsafe_allow_html=True)

        elif status == "quit":
            amt = format_money(state.get("final_amount", 0))
            st.markdown(f'<div class="kbc-result-banner kbc-result-quit">{name} walks away with {amt}!</div>',
                        unsafe_allow_html=True)


# =====================================================================
# SECTION 6: ADMIN VIEW (host control panel)
# =====================================================================

ADMIN_PASSWORD = st.secrets.get("KBC_ADMIN_PASSWORD", "changeme123")


def check_password():
    if st.session_state.get("admin_authed"):
        return True
    st.markdown(ADMIN_CSS, unsafe_allow_html=True)
    st.markdown("### 🔒 Host Admin Login")
    pw = st.text_input("Password", type="password")
    if st.button("Enter"):
        if pw == ADMIN_PASSWORD:
            st.session_state["admin_authed"] = True
            st.rerun()
        else:
            st.error("Incorrect password")
    return False


def render_admin():
    st.markdown(ADMIN_CSS, unsafe_allow_html=True)

    if not check_password():
        return

    st.markdown('<span class="admin-badge">HOST CONTROL PANEL</span>', unsafe_allow_html=True)
    st.title("🎙️ KBC Admin")

    state = get_state()
    status = state["status"]

    with st.expander("🎬 New Game Setup", expanded=(status == "lobby")):
        name = st.text_input("Contestant name", value=state.get("contestant_name", ""))
        if st.button("🔄 Start New Game", use_container_width=True):
            bank = build_active_bank()
            reset_game(bank, contestant_name=name)
            st.success("New game started. Display screen reset to lobby.")
            st.rerun()

    st.markdown(f'<span class="admin-status-pill">STATUS: {status.upper()}</span>', unsafe_allow_html=True)
    st.write("")

    if status == "lobby":
        st.info("Start a new game above. The display screen will show 'Welcome' until you begin Question 1.")
        if st.button("▶️ Begin Question 1", use_container_width=True, type="primary"):
            fresh = get_state()
            if fresh.get("active_question_bank"):
                trigger_sound("question", {"status": "question"})
                st.rerun()
            else:
                st.warning("Start a new game first.")
        return

    bank = state["active_question_bank"]
    q_index = state["current_question_index"]

    if status in ("won", "lost", "quit"):
        amt = format_money(state.get("final_amount", 0))
        st.markdown(f"## Game ended — {amt}")
        if st.button("🔄 Reset for Next Contestant", use_container_width=True):
            st.session_state["admin_authed"] = True
            bank2 = build_active_bank()
            reset_game(bank2)
            st.rerun()
        return

    if q_index >= len(bank):
        st.warning("No more questions in bank.")
        return

    q = bank[q_index]
    st.markdown(
        f'<div class="admin-qcard">'
        f'<b>Q{q_index+1} — {format_money(PRIZE_LADDER[q_index])}</b><br><br>'
        f'{q["question"]}<br><br>'
        + "<br>".join(
            f'{"✅ " if i == q["correct_index"] else "▫️ "}{"ABCD"[i]}. {opt}'
            for i, opt in enumerate(q["options"])
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    if status == "question" and not state.get("show_options"):
        if st.button("👁️ Show Options", use_container_width=True, type="primary"):
            trigger_sound("question", {"show_options": True})
            st.rerun()

    if state.get("show_options"):
        if state.get("reveal_stage") is None and state.get("selected_option") is None:
            st.write("**⏱️ Countdown Timer**")
            tcol1, tcol2, tcol3 = st.columns(3)
            timer_running = state.get("timer_started_at") is not None
            with tcol1:
                if st.button("Start 30s", use_container_width=True, disabled=timer_running):
                    set_state({"timer_seconds": 30, "timer_started_at": time.time()})
                    st.rerun()
            with tcol2:
                if st.button("Start 45s", use_container_width=True, disabled=timer_running):
                    set_state({"timer_seconds": 45, "timer_started_at": time.time()})
                    st.rerun()
            with tcol3:
                if st.button("Stop Timer", use_container_width=True, disabled=not timer_running):
                    set_state({"timer_started_at": None, "timer_seconds": 0})
                    st.rerun()

        st.write("**Contestant's answer:**")
        opt_cols = st.columns(4)
        eliminated = set(state.get("eliminated_options", []))
        for i in range(4):
            with opt_cols[i]:
                disabled = i in eliminated
                if st.button(f"Lock {'ABCD'[i]}", key=f"lock_{i}", disabled=disabled, use_container_width=True):
                    trigger_sound("lock", {
                        "selected_option": i, "status": "locked",
                        "timer_started_at": None, "timer_seconds": 0,
                    })
                    st.rerun()

        if state.get("selected_option") is not None:
            st.write(f"Locked answer: **{'ABCD'[state['selected_option']]}**")
            rcol1, rcol2, rcol3 = st.columns(3)
            with rcol1:
                if state.get("reveal_stage") != "suspense" and not state.get("revealed_correct"):
                    if st.button("🥁 Build Suspense", use_container_width=True):
                        trigger_sound("suspense", {"reveal_stage": "suspense"})
                        st.rerun()
            with rcol2:
                if st.button("✅ Reveal Answer", use_container_width=True, type="primary"):
                    is_correct = state["selected_option"] == q["correct_index"]
                    cue = "correct" if is_correct else "wrong"
                    trigger_sound(cue, {"status": "revealed", "revealed_correct": True, "reveal_stage": "done"})
                    st.rerun()
            with rcol3:
                if st.button("➡️ Next Question", use_container_width=True):
                    is_correct = state["selected_option"] == q["correct_index"]
                    already_revealed = state.get("revealed_correct", False)
                    if not is_correct:
                        final_amt = guaranteed_amount(q_index)
                        if already_revealed:
                            set_state({"status": "lost", "final_amount": final_amt})
                        else:
                            trigger_sound("wrong", {"status": "lost", "final_amount": final_amt})
                    elif q_index + 1 >= len(bank):
                        if already_revealed:
                            set_state({"status": "won", "final_amount": PRIZE_LADDER[-1]})
                        else:
                            trigger_sound("win", {"status": "won", "final_amount": PRIZE_LADDER[-1]})
                    else:
                        set_state({
                            "status": "question",
                            "current_question_index": q_index + 1,
                            "selected_option": None,
                            "revealed_correct": False,
                            "reveal_stage": None,
                            "eliminated_options": [],
                            "show_options": False,
                            "phone_a_friend_active": False,
                            "phone_a_friend_text": "",
                            "timer_started_at": None,
                            "timer_seconds": 0,
                        })
                    st.rerun()

    st.divider()

    st.markdown("### 🎯 Lifelines")
    lifelines = state.get("lifelines_used", {})
    lcol1, lcol2, lcol3 = st.columns(3)

    with lcol1:
        disabled = lifelines.get("fifty_fifty", False)
        if st.button("50:50", disabled=disabled, use_container_width=True):
            wrong_indices = [i for i in range(4) if i != q["correct_index"]]
            to_remove = random.sample(wrong_indices, 2)
            lifelines["fifty_fifty"] = True
            trigger_sound("lifeline", {
                "eliminated_options": to_remove, "lifelines_used": lifelines, "show_options": True,
            })
            st.rerun()

    with lcol2:
        disabled = lifelines.get("phone_a_friend", False)
        if st.button("📞 Phone", disabled=disabled, use_container_width=True):
            lifelines["phone_a_friend"] = True
            trigger_sound("lifeline", {
                "phone_a_friend_active": True, "lifelines_used": lifelines, "phone_a_friend_text": "",
            })
            st.rerun()

    with lcol3:
        disabled = lifelines.get("flip_question", False)
        if st.button("🔄 Flip", disabled=disabled, use_container_width=True):
            used_ids = {qq["id"] for qq in bank} | set(state.get("used_flip_pool", []))
            candidates = [fq for fq in FLIP_POOL if fq["id"] not in used_ids]
            if candidates:
                new_q = dict(random.choice(candidates))
                new_bank = list(bank)
                new_bank[q_index] = new_q
                lifelines["flip_question"] = True
                used_pool = state.get("used_flip_pool", []) + [new_q["id"]]
                trigger_sound("lifeline", {
                    "active_question_bank": new_bank,
                    "lifelines_used": lifelines,
                    "used_flip_pool": used_pool,
                    "selected_option": None,
                    "eliminated_options": [],
                    "show_options": False,
                    "status": "question",
                    "timer_started_at": None,
                    "timer_seconds": 0,
                })
                st.rerun()
            else:
                st.warning("No more spare questions available to flip to.")

    if state.get("phone_a_friend_active"):
        st.write("**Type what the 'friend' says (shown live on display):**")
        friend_text = st.text_area("Friend's response", value=state.get("phone_a_friend_text", ""), key="friend_input")
        if st.button("📤 Send to Display", use_container_width=True):
            set_state({"phone_a_friend_text": friend_text})
            st.rerun()
        if st.button("Close Phone Call", use_container_width=True):
            set_state({"phone_a_friend_active": False})
            st.rerun()

    st.divider()

    st.markdown("### 🚪 Contestant Options")
    guaranteed = guaranteed_amount(q_index)
    if st.button(f"🏳️ Contestant Quits (walks away with {format_money(guaranteed)})", use_container_width=True):
        trigger_sound("quit", {"status": "quit", "final_amount": guaranteed})
        st.rerun()


# =====================================================================
# SECTION 7: ROUTER (main entry point)
# =====================================================================

def main():
    view = st.query_params.get("view", "display")
    if view == "admin":
        st.set_page_config(page_title="KBC Admin Control", layout="centered")
        render_admin()
    else:
        st.set_page_config(page_title="KBC Quiz — Live", layout="wide", initial_sidebar_state="collapsed")
        render_display()


if __name__ == "__main__":
    main()
