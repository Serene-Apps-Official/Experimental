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

Audio assets:
    Put your custom sound files in an "assets" folder next to this file:
        assets/kbc_intro_main.mp3     -> plays on the lobby / splash screen
        assets/intro_start_play.mp3   -> plays once, when host hits "Begin Question 1"
        assets/ticktick.mp3           -> loops softly while a question timer is running
        assets/lock_option.mp3        -> plays when host locks the contestant's answer
        assets/next_question.mp3      -> plays on the transition to the next question
        assets/wrong_answer.mp3       -> plays on an incorrect answer / game over
    Any cue without a matching file above (suspense, correct answers, lifelines,
    the final win) falls back to the built-in synthesized tones further down.
"""

import streamlit as st
import streamlit.components.v1 as components
import sqlite3
import json
import time
import math
import random
import os
import base64

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

# st.fragment (with run_every) needs Streamlit >= ~1.37. If someone deploys
# on an older pinned version, fall back to whole-page fast polling instead
# of crashing on an AttributeError.
HAS_FRAGMENT = hasattr(st, "fragment")


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
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

# ---- Custom audio files (your own recordings) mapped to game cues. ----
# Key = cue name used by trigger_sound(); value = filename inside ASSETS_DIR.
# A cue not listed here (or whose file is missing on disk) simply falls
# back to the synthesized tone in CUE_DEFINITIONS -- nothing breaks if a
# file hasn't been uploaded yet.
CUSTOM_AUDIO_FILES = {
    "lobby_intro": "kbc_intro_main.mp3",     # splash / lobby screen loop
    "start_play": "intro_start_play.mp3",    # "Begin Question 1" one-shot
    "lock": "lock_option.mp3",               # answer locked in
    "next_question": "next_question.mp3",    # advancing to next question
    "wrong": "wrong_answer.mp3",             # incorrect answer / game over
}
AMBIENT_TICKTICK_FILE = "ticktick.mp3"       # loops while the timer is live


@st.cache_data(show_spinner=False)
def _load_audio_b64(filename: str):
    """Read an audio file from the assets folder and cache it as a base64
    data URI so we don't re-read/re-encode multi-MB files on every rerun.
    Returns None if the file isn't there yet, so missing assets degrade
    gracefully instead of crashing the app."""
    path = os.path.join(ASSETS_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:audio/mp3;base64,{b64}"

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
    "revealed_options": [],  # indices of options shown on display so far (computed from options_reveal_started_at)
    "options_reveal_started_at": None,  # wall-clock time host pressed "Show Options"; drives the 1s-apart auto-reveal
    "celebration_tier": None,  # "small" | "medium" | "big" -- set on a correct answer, drives visual/audio intensity
    "timer_seconds": 0,
    "timer_started_at": None,
    "sound_cue": None,
    "sound_cue_id": 0,
    "last_updated": time.time(),
}

# ---- KBC's actual timer rule: 30s for Q1-5, 45s for Q6-10, untimed Q11-15 ----
def timer_duration_for_question(q_index_zero_based: int) -> int:
    """Returns the countdown length in seconds for a given question,
    0-based index. Returns 0 for questions that are untimed (Q11-15)."""
    q_number = q_index_zero_based + 1
    if q_number <= 5:
        return 30
    elif q_number <= 10:
        return 45
    return 0  # Q11-15 are untimed in real KBC


# ---- Celebration tier: how big the correct-answer payoff should feel,
# scaled to how much money is on the line. Ties directly into which sound
# cue and visual effect fire on a correct answer. ----
def celebration_tier(q_index_zero_based: int) -> str:
    q_number = q_index_zero_based + 1
    if q_number <= 5:
        return "small"
    elif q_number <= 10:
        return "medium"
    return "big"  # Q11-15, lakhs to crores


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


def advance_auto_reveal_and_timer(state: dict) -> dict:
    """Computes how many options should be visible right now based on
    elapsed time since 'options_reveal_started_at' (1 second apart, KBC
    style), and auto-starts the countdown timer the instant the 4th option
    appears. Called on every read from both admin and display so the two
    stay in sync without needing a background job. Persists the computed
    values so the "timer auto-started" transition only fires once.
    """
    started_at = state.get("options_reveal_started_at")
    if not started_at:
        return state  # host hasn't pressed "Show Options" yet

    elapsed = time.time() - started_at
    # Option i becomes visible once `elapsed` passes i seconds: A at 0s,
    # B at 1s, C at 2s, D at 3s.
    should_be_revealed = [i for i in range(4) if elapsed >= i]
    # Never un-reveal something already shown (defensive; elapsed only grows,
    # but this also protects against eliminated options from 50:50 which
    # manage their own reveal set separately).
    current_revealed = set(state.get("revealed_options", []))
    updated_revealed = sorted(current_revealed | set(should_be_revealed))

    needs_write = False
    patch = {}
    if updated_revealed != state.get("revealed_options", []):
        patch["revealed_options"] = updated_revealed
        needs_write = True

    all_four_shown = len(updated_revealed) >= 4
    timer_not_started_yet = state.get("timer_started_at") is None
    q_index = state.get("current_question_index", 0)
    duration = timer_duration_for_question(q_index)

    if all_four_shown and timer_not_started_yet and duration > 0 and state.get("status") == "question":
        # Auto-start the countdown the instant the last option appears,
        # matching real KBC's clock behavior.
        patch["timer_started_at"] = time.time()
        patch["timer_seconds"] = duration
        needs_write = True

    if needs_write:
        state = set_state(patch)
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
# Short tone/chord sequences via the Web Audio API. Not a reproduction of
# the real show's copyrighted score -- just synthesized stings for each
# game moment, plus a continuous soft ambient pad while a question is live.
#
# A "note" can be a single frequency (int) or a chord (list of frequencies
# played together) for a fuller sound than a single sine tone.

CUE_DEFINITIONS = {
    "question": [(440, 120, 40), (660, 160, 0)],
    "lock": [(520, 90, 30), (520, 90, 0)],
    "suspense": [(300, 220, 60), (340, 220, 60), (380, 260, 0)],
    # Correct-answer cues scale with how much money is riding on the
    # question, matching the "bigger celebration for bigger prizes" request.
    "correct_small": [([523, 659], 160, 30), ([659, 831], 220, 0)],
    "correct_medium": [([392, 523, 659], 150, 20), ([523, 659, 831], 150, 20), ([659, 831, 1046], 280, 0)],
    "correct_big": [
        ([392, 523, 659], 130, 10), ([440, 587, 740], 130, 10), ([523, 659, 831], 130, 10),
        ([659, 831, 1046], 260, 60), ([784, 988, 1245], 260, 60), ([1046, 1318, 1568], 500, 0),
    ],
    "wrong": [(300, 180, 40), (220, 260, 0)],
    "lifeline": [(392, 100, 20), (523, 100, 20), (659, 160, 0)],
    "win": [
        ([523, 659, 784], 140, 20), ([659, 831, 1046], 140, 20), ([784, 988, 1245], 140, 20),
        ([1046, 1318, 1568], 320, 80), ([784, 988, 1245], 140, 20), ([1046, 1318, 1568, 2093], 500, 0),
    ],
    "quit": [(500, 160, 40), (400, 160, 40), (330, 260, 0)],
}


def _notes_to_freq_list(note):
    """A note is either a single frequency or a chord (list of frequencies).
    Normalize to a list either way."""
    return note if isinstance(note, list) else [note]


def _build_js(sequence):
    steps = []
    t = 0
    for note, dur_ms, gap_ms in sequence:
        freqs = _notes_to_freq_list(note)
        for freq in freqs:
            steps.append(f"playTone({freq}, {t/1000}, {dur_ms/1000});")
        t += dur_ms + gap_ms
    return "\n".join(steps)


def play_cue(cue_name: str, cue_id: int):
    """Render an invisible component that plays the given cue exactly once
    per unique cue_id (Streamlit skips re-rendering identical component
    calls, so a changing cue_id is what makes a repeat cue actually fire).

    If a custom audio file is mapped for this cue (CUSTOM_AUDIO_FILES) and
    exists on disk, it plays that file directly. Otherwise it falls back
    to the synthesized tone sequence in CUE_DEFINITIONS."""
    custom_file = CUSTOM_AUDIO_FILES.get(cue_name)
    data_uri = _load_audio_b64(custom_file) if custom_file else None

    if data_uri:
        html = f"""
        <div style="display:none" data-cue-id="{cue_id}"></div>
        <script>
        (function() {{
            try {{
                const audio = new Audio("{data_uri}");
                audio.volume = 1.0;
                audio.play().catch(e => console.log('Autoplay blocked, cue:', e));
            }} catch (e) {{
                console.log('Sound cue skipped:', e);
            }}
        }})();
        </script>
        """
        components.html(html, height=0, width=0)
        return

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
                gain.gain.exponentialRampToValueAtTime(0.22, ctx.currentTime + startAt + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + startAt + dur);
                osc.connect(gain).connect(ctx.destination);
                osc.start(ctx.currentTime + startAt);
                osc.stop(ctx.currentTime + startAt + dur + 0.05);
            }}
            {js_calls}
        }} catch (e) {{
            console.log('Sound cue skipped:', e);
        }}
    }})();
    </script>
    """
    components.html(html, height=0, width=0)


def play_ambient_loop(active: bool, tension: str = "low"):
    """Continuous soft background sound while a question is live.

    If ticktick.mp3 is present in the assets folder, that file is looped
    (this is the normal path once you've uploaded your own ticking-clock
    recording). Otherwise this falls back to the synthesized detuned-pad
    tone from before. Re-invoking this with active=True is a no-op if a
    loop is already running (tracked via a global on window), so it
    doesn't restart/click every 0.35s poll tick. tension affects
    pitch/brightness for the synthesized fallback, and playback rate for
    the real file: 'low' during Q1-5, 'mid' during Q6-10, 'high' during
    Q11-15 (subtle, ambient -- not meant to be loud or distracting).
    """
    tension_settings = {
        "low": {"base": 110, "detune": 4, "gain": 0.035, "rate": 1.0, "vol": 0.5},
        "mid": {"base": 146, "detune": 6, "gain": 0.045, "rate": 1.08, "vol": 0.6},
        "high": {"base": 196, "detune": 9, "gain": 0.055, "rate": 1.18, "vol": 0.7},
    }
    cfg = tension_settings.get(tension, tension_settings["low"])

    ticktick_uri = _load_audio_b64(AMBIENT_TICKTICK_FILE)
    if ticktick_uri:
        html = f"""
        <div style="display:none"></div>
        <script>
        (function() {{
            try {{
                if (!window.__sfqTicktick) {{ window.__sfqTicktick = {{ audio: null, playing: false }}; }}
                const state = window.__sfqTicktick;
                const shouldPlay = {str(active).lower()};
                const rate = {cfg['rate']};
                const vol = {cfg['vol']};

                if (!shouldPlay) {{
                    if (state.playing && state.audio) {{
                        const a = state.audio;
                        const fadeStep = () => {{
                            a.volume = Math.max(0, a.volume - 0.08);
                            if (a.volume > 0) {{ setTimeout(fadeStep, 40); }}
                            else {{ a.pause(); state.playing = false; }}
                        }};
                        fadeStep();
                    }}
                    return;
                }}

                if (state.playing && state.audio) {{
                    state.audio.playbackRate = rate;
                    state.audio.volume = vol;
                    return;  // already running, just retune -- don't restart/click
                }}

                const audio = new Audio("{ticktick_uri}");
                audio.loop = true;
                audio.playbackRate = rate;
                audio.volume = vol;
                audio.play().catch(e => console.log('Ambient autoplay blocked:', e));
                state.audio = audio;
                state.playing = true;
            }} catch (e) {{
                console.log('Ambient loop (file) skipped:', e);
            }}
        }})();
        </script>
        """
        components.html(html, height=0, width=0)
        return

    html = f"""
    <div style="display:none"></div>
    <script>
    (function() {{
        try {{
            if (!window.__sfqAmbient) {{ window.__sfqAmbient = {{ ctx: null, nodes: [], playing: false }}; }}
            const state = window.__sfqAmbient;
            const shouldPlay = {str(active).lower()};

            if (!shouldPlay) {{
                if (state.playing && state.ctx) {{
                    state.nodes.forEach(n => {{ try {{ n.gainNode.gain.exponentialRampToValueAtTime(0.0001, state.ctx.currentTime + 0.6); }} catch(e) {{}} }});
                    setTimeout(() => {{
                        state.nodes.forEach(n => {{ try {{ n.osc.stop(); }} catch(e) {{}} }});
                        state.nodes = [];
                        state.playing = false;
                    }}, 650);
                }}
                return;
            }}

            if (state.playing) return;  // already running, don't restart/click

            const ctx = state.ctx || new (window.AudioContext || window.webkitAudioContext)();
            state.ctx = ctx;
            const base = {cfg['base']};
            const detune = {cfg['detune']};
            const targetGain = {cfg['gain']};
            const freqs = [base, base * 1.5, base * 2 + detune];

            state.nodes = freqs.map((f, i) => {{
                const osc = ctx.createOscillator();
                const gainNode = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.value = f;
                gainNode.gain.setValueAtTime(0.0001, ctx.currentTime);
                gainNode.gain.exponentialRampToValueAtTime(targetGain / (i + 1), ctx.currentTime + 1.2);
                osc.connect(gainNode).connect(ctx.destination);
                osc.start();
                return {{ osc, gainNode }};
            }});
            state.playing = true;
        }} catch (e) {{
            console.log('Ambient loop skipped:', e);
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
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=Space+Grotesk:wght@400;500;600;700&display=swap');

:root {
    --sfq-void: #05070f;
    --sfq-panel: #0c1330;
    --sfq-panel-light: rgba(20, 27, 58, 0.55);
    --sfq-glass-border: rgba(232, 199, 102, 0.22);
    --sfq-gold: #e8c766;
    --sfq-gold-soft: rgba(232, 199, 102, 0.14);
    --sfq-teal: #7dd3c0;
    --sfq-coral: #ef6f6f;
    --sfq-ink: #f5f3ea;
    --sfq-ink-dim: #a9b0c8;
}

.stApp {
    background:
        radial-gradient(ellipse 45% 35% at 20% 15%, rgba(232, 199, 102, 0.10), transparent 60%),
        radial-gradient(ellipse 40% 40% at 85% 20%, rgba(125, 211, 192, 0.08), transparent 60%),
        radial-gradient(ellipse 50% 45% at 50% 90%, rgba(99, 102, 241, 0.10), transparent 65%),
        var(--sfq-void);
    background-attachment: fixed;
    font-family: 'Space Grotesk', sans-serif;
    color: var(--sfq-ink);
}

#MainMenu, footer, header {visibility: hidden;}
.block-container { padding-top: 1.4rem; padding-bottom: 2rem; max-width: 900px; }

/* ---- Title ---- */
.sfq-title {
    text-align: center; font-family: 'Fraunces', serif; font-weight: 600;
    font-size: 2.3rem; letter-spacing: 0.01em; color: var(--sfq-ink);
    margin-bottom: 0.2rem;
}
.sfq-title .accent { color: var(--sfq-gold); font-style: italic; }
.sfq-subtitle {
    text-align: center; color: var(--sfq-gold); font-size: 0.95rem; font-weight: 500;
    margin-bottom: 1.2rem; min-height: 1.3rem; letter-spacing: 0.02em;
}

/* ---- Glass panel base ---- */
.sfq-glass {
    background: var(--sfq-panel-light);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    border: 1px solid var(--sfq-glass-border);
    border-radius: 22px;
}

/* ---- Header strip: name + prize badge + ladder toggle ---- */
.sfq-header-name {
    font-size: 0.92rem; color: var(--sfq-ink-dim); font-weight: 500;
    display: flex; align-items: center; height: 100%;
}
.sfq-header-name b { color: var(--sfq-ink); font-weight: 600; }
.sfq-prize-badge {
    background: var(--sfq-panel-light); backdrop-filter: blur(14px);
    border: 1px solid var(--sfq-glass-border); border-radius: 14px;
    padding: 0.5rem 0.9rem; text-align: center;
}
.sfq-prize-badge .label {
    font-size: 0.6rem; color: var(--sfq-ink-dim); letter-spacing: 0.1em; text-transform: uppercase;
}
.sfq-prize-badge .amount {
    font-family: 'Fraunces', serif; font-size: 1.15rem; font-weight: 600; color: var(--sfq-gold);
}

/* ---- Question card ---- */
@keyframes sfq-rise-in {
    from { opacity: 0; transform: translateY(34px) scale(0.98); }
    to { opacity: 1; transform: translateY(0) scale(1); }
}
@keyframes sfq-slide-left-in {
    from { opacity: 0; transform: translateX(-28px); }
    to { opacity: 1; transform: translateX(0); }
}
.sfq-qnumber {
    text-align: center; color: var(--sfq-gold); font-weight: 600; font-size: 0.8rem;
    letter-spacing: 0.14em; text-transform: uppercase; margin-bottom: 0.7rem;
}
.sfq-question-card {
    background: var(--sfq-panel-light);
    backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
    border: 1px solid var(--sfq-glass-border);
    border-radius: 26px; padding: 2.1rem 2.2rem;
    box-shadow: 0 8px 40px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.04);
    margin-bottom: 1.4rem; min-height: 110px;
    display: flex; align-items: center; justify-content: center; text-align: center;
    transition: border-color 0.4s ease, box-shadow 0.4s ease;
}
.sfq-question-card.animate-in { animation: sfq-rise-in 0.6s cubic-bezier(0.22, 1, 0.36, 1) both; }
.sfq-question-text {
    font-family: 'Fraunces', serif; font-size: 1.5rem; font-weight: 500;
    line-height: 1.5; color: var(--sfq-ink);
}
.sfq-question-card.suspense {
    border-color: rgba(232, 199, 102, 0.55);
    box-shadow: 0 0 60px rgba(232, 199, 102, 0.22), inset 0 1px 0 rgba(255,255,255,0.06);
    animation: sfq-heartbeat 0.8s ease-in-out infinite;
}
@keyframes sfq-heartbeat {
    0%, 100% { transform: scale(1); filter: brightness(1); }
    50% { transform: scale(1.015); filter: brightness(1.08); }
}

/* ---- Options ---- */
.sfq-option {
    background: var(--sfq-panel-light);
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    border: 1px solid rgba(169, 176, 200, 0.18);
    border-radius: 16px; padding: 1rem 1.4rem; margin-bottom: 0.65rem;
    font-size: 1.02rem; font-weight: 500; color: var(--sfq-ink);
    display: flex; align-items: center; gap: 1rem;
    transition: border-color 0.35s ease, box-shadow 0.35s ease, opacity 0.35s ease, filter 0.35s ease;
}
.sfq-option.animate-in { animation: sfq-slide-left-in 0.45s cubic-bezier(0.22, 1, 0.36, 1) both; }
.sfq-option-letter {
    background: var(--sfq-gold-soft); color: var(--sfq-gold);
    width: 30px; height: 30px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 0.85rem; flex-shrink: 0;
    border: 1px solid rgba(232, 199, 102, 0.35);
    font-family: 'Space Grotesk', sans-serif;
}
.sfq-option.eliminated { opacity: 0.12; filter: grayscale(1); }
.sfq-option.selected {
    border-color: var(--sfq-gold);
    box-shadow: 0 0 0 1px var(--sfq-gold), 0 0 24px rgba(232, 199, 102, 0.25);
    animation: sfq-pulse-select 1.1s ease-in-out infinite;
}
@keyframes sfq-pulse-select { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.012); } }
.sfq-option.correct {
    border-color: var(--sfq-teal);
    background: rgba(125, 211, 192, 0.12);
    box-shadow: 0 0 0 1px var(--sfq-teal), 0 0 30px rgba(125, 211, 192, 0.25);
    animation: sfq-correct-pop 0.5s cubic-bezier(0.34, 1.56, 0.64, 1);
}
@keyframes sfq-correct-pop {
    0% { transform: scale(1); } 40% { transform: scale(1.03); } 100% { transform: scale(1); }
}
.sfq-option.correct .sfq-option-letter { background: var(--sfq-teal); color: #05170f; border-color: var(--sfq-teal); }

/* ---- Celebration tiers: intensity scales with the prize on the line ---- */
.sfq-celebrate-medium {
    animation: sfq-glow-wash 1.1s ease-out;
}
@keyframes sfq-glow-wash {
    0% { box-shadow: 0 0 0 rgba(125, 211, 192, 0); }
    30% { box-shadow: 0 0 90px rgba(125, 211, 192, 0.35); }
    100% { box-shadow: 0 0 0 rgba(125, 211, 192, 0); }
}
.sfq-celebrate-big {
    animation: sfq-glow-wash-big 1.6s ease-out;
    position: relative;
}
@keyframes sfq-glow-wash-big {
    0% { box-shadow: 0 0 0 rgba(232, 199, 102, 0); }
    25% { box-shadow: 0 0 140px rgba(232, 199, 102, 0.5); }
    100% { box-shadow: 0 0 0 rgba(232, 199, 102, 0); }
}
.sfq-confetti-piece {
    position: fixed;
    top: -20px;
    width: 9px; height: 16px;
    opacity: 0.9;
    animation: sfq-confetti-fall linear forwards;
    pointer-events: none;
    z-index: 999;
}
@keyframes sfq-confetti-fall {
    0% { transform: translateY(0) rotate(0deg); opacity: 0.95; }
    100% { transform: translateY(105vh) rotate(540deg); opacity: 0.3; }
}
.sfq-option.incorrect {
    border-color: var(--sfq-coral);
    background: rgba(239, 111, 111, 0.12);
    box-shadow: 0 0 0 1px var(--sfq-coral), 0 0 30px rgba(239, 111, 111, 0.22);
    animation: sfq-shake 0.5s ease;
}
@keyframes sfq-shake {
    0%, 100% { transform: translateX(0); }
    20% { transform: translateX(-6px); } 40% { transform: translateX(6px); }
    60% { transform: translateX(-4px); } 80% { transform: translateX(4px); }
}
.sfq-option.incorrect .sfq-option-letter { background: var(--sfq-coral); color: #200404; border-color: var(--sfq-coral); }
.sfq-option.suspense-dim { opacity: 0.3; filter: grayscale(0.5); }
.sfq-option.eliminated.suspense-dim { opacity: 0.12; filter: grayscale(1); }

/* ---- Ladder drawer (slides from the right edge) ---- */
.sfq-ladder-panel {
    background: rgba(8, 12, 32, 0.85);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    border: 1px solid var(--sfq-glass-border);
    border-radius: 20px; padding: 1rem 0.8rem;
    display: flex; flex-direction: column-reverse;
    animation: sfq-drawer-in 0.4s cubic-bezier(0.22, 1, 0.36, 1) both;
    margin-bottom: 1rem;
}
@keyframes sfq-drawer-in {
    from { opacity: 0; transform: translateY(-14px); }
    to { opacity: 1; transform: translateY(0); }
}
.sfq-ladder-row {
    display: flex; justify-content: space-between; padding: 0.42rem 0.8rem; border-radius: 10px;
    font-size: 0.83rem; font-weight: 500; color: var(--sfq-ink-dim); margin-bottom: 2px;
    font-family: 'Space Grotesk', sans-serif;
}
.sfq-ladder-row.safe { color: var(--sfq-gold); font-weight: 700; }
.sfq-ladder-row.current {
    background: rgba(232, 199, 102, 0.14); color: var(--sfq-ink);
    box-shadow: inset 0 0 0 1px rgba(232, 199, 102, 0.4); transform: scale(1.02);
}
.sfq-ladder-row.cleared { color: var(--sfq-teal); opacity: 0.65; }

/* ---- Lifeline dock (bottom, horizontal) ---- */
.sfq-lifeline-dock { display: flex; gap: 0.7rem; justify-content: center; margin: 1.3rem 0 0.6rem; }
.sfq-lifeline-chip {
    background: var(--sfq-panel-light); backdrop-filter: blur(14px);
    border: 1px solid var(--sfq-glass-border); border-radius: 50px;
    padding: 0.5rem 1rem; display: flex; align-items: center; gap: 0.5rem;
    font-size: 0.85rem; font-weight: 500; color: var(--sfq-ink);
    transition: opacity 0.4s ease;
}
.sfq-lifeline-chip.used { opacity: 0.22; filter: grayscale(1); }

/* ---- Phone-a-friend bubble ---- */
.sfq-phone-bubble {
    background: rgba(232, 199, 102, 0.08);
    backdrop-filter: blur(14px);
    border: 1px solid var(--sfq-glass-border); border-radius: 18px;
    padding: 1.1rem 1.4rem; margin: 1rem 0;
    font-size: 1rem; font-style: italic; color: var(--sfq-ink); text-align: center;
    animation: sfq-rise-in 0.5s cubic-bezier(0.22, 1, 0.36, 1) both;
}

/* ---- Suspense dots ---- */
.sfq-suspense-wrap { text-align: center; padding: 1.2rem; }
.sfq-suspense-text {
    font-size: 1rem; font-weight: 600; letter-spacing: 0.1em; color: var(--sfq-gold);
    text-transform: uppercase; font-family: 'Space Grotesk', sans-serif;
}
.sfq-suspense-dots span {
    display: inline-block; width: 9px; height: 9px; margin: 0 5px; border-radius: 50%;
    background: var(--sfq-gold); animation: sfq-dot-bounce 1.1s ease-in-out infinite;
}
.sfq-suspense-dots span:nth-child(2) { animation-delay: 0.15s; }
.sfq-suspense-dots span:nth-child(3) { animation-delay: 0.3s; }
@keyframes sfq-dot-bounce {
    0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
    30% { transform: translateY(-9px); opacity: 1; }
}

/* ---- Timer ring ---- */
.sfq-timer-wrap { display: flex; justify-content: center; margin-bottom: 1rem; animation: sfq-rise-in 0.4s both; }
.sfq-timer-box { position: relative; width: 78px; height: 78px; }
.sfq-timer-box svg { transform: rotate(-90deg); }
.sfq-timer-track { fill: none; stroke: rgba(232, 199, 102, 0.12); stroke-width: 5; }
.sfq-timer-progress {
    fill: none; stroke: var(--sfq-gold); stroke-width: 5; stroke-linecap: round;
    transition: stroke-dashoffset 0.9s linear, stroke 0.3s ease;
}
.sfq-timer-progress.urgent { stroke: var(--sfq-coral); }
.sfq-timer-number {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-family: 'Fraunces', serif; font-size: 1.4rem; font-weight: 600; color: var(--sfq-gold);
}
.sfq-timer-number.urgent { color: var(--sfq-coral); animation: sfq-tick 1s ease-in-out infinite; }
@keyframes sfq-tick { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.15); } }

/* ---- Result banners ---- */
.sfq-result-banner {
    text-align: center; padding: 2.4rem 1.6rem; border-radius: 24px;
    font-family: 'Fraunces', serif; font-size: 1.7rem; font-weight: 600; margin: 1rem 0;
    backdrop-filter: blur(18px);
    animation: sfq-rise-in 0.7s cubic-bezier(0.22, 1, 0.36, 1) both;
}
.sfq-result-win {
    background: rgba(125, 211, 192, 0.12); border: 1px solid var(--sfq-teal); color: #ddfaf3;
    box-shadow: 0 0 60px rgba(125, 211, 192, 0.25);
}
.sfq-result-lost {
    background: rgba(239, 111, 111, 0.12); border: 1px solid var(--sfq-coral); color: #ffe4e4;
    box-shadow: 0 0 60px rgba(239, 111, 111, 0.2);
}
.sfq-result-quit {
    background: rgba(232, 199, 102, 0.12); border: 1px solid var(--sfq-gold); color: #fff3d6;
    box-shadow: 0 0 60px rgba(232, 199, 102, 0.2);
}

/* ---- Restyle native Streamlit buttons on the display page (ladder toggle)
   so nothing reads as default Streamlit chrome ---- */
.stButton > button {
    background: var(--sfq-panel-light) !important;
    backdrop-filter: blur(14px);
    border: 1px solid var(--sfq-glass-border) !important;
    border-radius: 14px !important;
    color: var(--sfq-ink) !important;
    font-family: 'Space Grotesk', sans-serif !important;
    font-weight: 500 !important;
    transition: border-color 0.25s ease, transform 0.15s ease, box-shadow 0.25s ease !important;
}
.stButton > button:hover {
    border-color: var(--sfq-gold) !important;
    box-shadow: 0 0 18px rgba(232, 199, 102, 0.18) !important;
    transform: translateY(-1px);
}
.stButton > button:active { transform: translateY(0); }

/* ---- Splash / lobby screen ---- */
@keyframes sfq-splash-glow {
    0%, 100% { opacity: 0.55; transform: scale(1); }
    50% { opacity: 1; transform: scale(1.04); }
}
@keyframes sfq-splash-title-in {
    0% { opacity: 0; letter-spacing: 0.4em; filter: blur(6px); }
    100% { opacity: 1; letter-spacing: 0.05em; filter: blur(0); }
}
@keyframes sfq-splash-ring-spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
}
@keyframes sfq-splash-fade-up {
    from { opacity: 0; transform: translateY(18px); }
    to { opacity: 1; transform: translateY(0); }
}
@keyframes sfq-splash-shimmer {
    0% { background-position: -200% 0; }
    100% { background-position: 200% 0; }
}
.sfq-splash-wrap {
    position: relative;
    min-height: 62vh;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    text-align: center; padding: 2rem 1rem;
    overflow: hidden;
}
.sfq-splash-ring {
    position: absolute; width: 380px; height: 380px; border-radius: 50%;
    border: 1px solid rgba(232, 199, 102, 0.18);
    animation: sfq-splash-ring-spin 22s linear infinite, sfq-splash-glow 4s ease-in-out infinite;
}
.sfq-splash-ring.ring-2 { width: 480px; height: 480px; animation-duration: 30s, 5s; border-color: rgba(125, 211, 192, 0.14); }
.sfq-splash-ring.ring-3 { width: 280px; height: 280px; animation-duration: 16s, 3.4s; }
.sfq-splash-logo {
    font-family: 'Fraunces', serif; font-weight: 700; letter-spacing: 0.05em;
    font-size: clamp(2.4rem, 7vw, 4.2rem); color: var(--sfq-ink);
    animation: sfq-splash-title-in 1.4s cubic-bezier(0.22, 1, 0.36, 1) both;
    position: relative; z-index: 1;
    background: linear-gradient(100deg, var(--sfq-ink) 40%, var(--sfq-gold) 50%, var(--sfq-ink) 60%);
    background-size: 250% 100%;
    -webkit-background-clip: text; background-clip: text; color: transparent;
    animation: sfq-splash-title-in 1.4s cubic-bezier(0.22, 1, 0.36, 1) both,
               sfq-splash-shimmer 3.6s linear 1.4s infinite;
}
.sfq-splash-logo .accent { font-style: italic; }
.sfq-splash-tagline {
    font-size: clamp(0.85rem, 2vw, 1.05rem); color: var(--sfq-gold);
    letter-spacing: 0.32em; text-transform: uppercase; margin-top: 0.8rem;
    animation: sfq-splash-fade-up 1s ease 0.6s both;
    position: relative; z-index: 1;
}
.sfq-splash-sub {
    margin-top: 1.6rem; color: var(--sfq-ink-dim); font-size: 0.95rem;
    animation: sfq-splash-fade-up 1s ease 1s both;
    position: relative; z-index: 1;
}
.sfq-splash-dots { display: flex; gap: 8px; margin-top: 1.4rem; justify-content: center;
    animation: sfq-splash-fade-up 1s ease 1.2s both; position: relative; z-index: 1; }
.sfq-splash-dots span {
    width: 7px; height: 7px; border-radius: 50%; background: var(--sfq-gold);
    animation: sfq-dot-bounce 1.3s ease-in-out infinite;
}
.sfq-splash-dots span:nth-child(2) { animation-delay: 0.15s; }
.sfq-splash-dots span:nth-child(3) { animation-delay: 0.3s; }
</style>
"""

ADMIN_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&display=swap');

:root {
    --sfq-void: #05070f;
    --sfq-panel-light: rgba(20, 27, 58, 0.55);
    --sfq-glass-border: rgba(232, 199, 102, 0.22);
    --sfq-gold: #e8c766;
    --sfq-teal: #7dd3c0;
    --sfq-coral: #ef6f6f;
    --sfq-ink: #f5f3ea;
    --sfq-ink-dim: #a9b0c8;
}

.stApp { font-family: 'Space Grotesk', sans-serif; background: var(--sfq-void); color: var(--sfq-ink); }
#MainMenu, footer, header {visibility: hidden;}
.block-container { padding-top: 1.2rem; max-width: 720px; }

.admin-badge {
    display: inline-block; background: rgba(232, 199, 102, 0.12); color: var(--sfq-gold);
    border: 1px solid var(--sfq-glass-border); padding: 0.25rem 0.8rem; border-radius: 20px;
    font-size: 0.7rem; font-weight: 700; letter-spacing: 0.06em; margin-bottom: 0.6rem;
    text-transform: uppercase;
}
.admin-qcard {
    background: var(--sfq-panel-light); backdrop-filter: blur(14px);
    border: 1px solid var(--sfq-glass-border); border-radius: 18px;
    padding: 1.1rem 1.3rem; margin-bottom: 0.9rem; line-height: 1.6;
}
.admin-status-pill {
    display: inline-block; padding: 0.28rem 0.85rem; border-radius: 20px;
    font-size: 0.72rem; font-weight: 700; background: rgba(125, 211, 192, 0.12); color: var(--sfq-teal);
    border: 1px solid rgba(125, 211, 192, 0.35); letter-spacing: 0.03em;
}

h1, h2, h3, .stMarkdown h3 { color: var(--sfq-ink); font-family: 'Space Grotesk', sans-serif; }

/* ---- Restyle every native Streamlit control so nothing looks stock ---- */
.stButton > button {
    background: var(--sfq-panel-light) !important;
    backdrop-filter: blur(14px);
    border: 1px solid var(--sfq-glass-border) !important;
    border-radius: 12px !important;
    color: var(--sfq-ink) !important;
    font-family: 'Space Grotesk', sans-serif !important;
    font-weight: 500 !important;
    transition: border-color 0.2s ease, transform 0.12s ease, box-shadow 0.2s ease !important;
}
.stButton > button:hover {
    border-color: var(--sfq-gold) !important;
    box-shadow: 0 0 16px rgba(232, 199, 102, 0.18) !important;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, rgba(232, 199, 102, 0.25), rgba(232, 199, 102, 0.1)) !important;
    border-color: var(--sfq-gold) !important;
    color: var(--sfq-gold) !important;
}
.stTextInput > div > div > input, .stTextArea textarea {
    background: var(--sfq-panel-light) !important;
    border: 1px solid var(--sfq-glass-border) !important;
    border-radius: 10px !important;
    color: var(--sfq-ink) !important;
}
.streamlit-expanderHeader {
    background: var(--sfq-panel-light) !important;
    border-radius: 12px !important;
    color: var(--sfq-ink) !important;
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
        classes = "sfq-ladder-row"
        if q_number in SAFE_LEVELS:
            classes += " safe"
        if status not in ("lobby",) and i == current_index:
            classes += " current"
        elif status in ("revealed", "won") and i < current_index:
            classes += " cleared"
        rows += f'<div class="{classes}"><span>{q_number}</span><span>{amount}</span></div>'
    return f'<div class="sfq-ladder-panel">{rows}</div>'


def render_option_letter(i):
    return ["A", "B", "C", "D"][i]


def render_timer_ring(seconds_left: float, total_seconds: float):
    if total_seconds <= 0:
        return ""
    radius = 34
    circumference = 2 * math.pi * radius
    frac = max(0.0, min(1.0, seconds_left / total_seconds))
    offset = circumference * (1 - frac)
    urgent = seconds_left <= 5
    urgent_class = " urgent" if urgent else ""
    display_seconds = max(0, math.ceil(seconds_left))
    return f"""
    <div class="sfq-timer-wrap">
        <div class="sfq-timer-box">
            <svg width="78" height="78">
                <circle class="sfq-timer-track" cx="39" cy="39" r="{radius}"></circle>
                <circle class="sfq-timer-progress{urgent_class}" cx="39" cy="39" r="{radius}"
                    stroke-dasharray="{circumference:.1f}"
                    stroke-dashoffset="{offset:.1f}"></circle>
            </svg>
            <div class="sfq-timer-number{urgent_class}">{display_seconds}</div>
        </div>
    </div>
    """


def render_suspense_overlay():
    return """
    <div class="sfq-suspense-wrap">
        <div class="sfq-suspense-text">Lock kiya jaaye?</div>
        <div class="sfq-suspense-dots"><span></span><span></span><span></span></div>
    </div>
    """


def render_confetti_burst():
    """CSS-only confetti burst for the 'big' celebration tier (Q11-15,
    lakhs-to-crores territory). Random-ish but deterministic-enough pure
    Python generation of falling pieces, styled in the gold/teal palette
    to stay consistent with the rest of the theme."""
    colors = ["#e8c766", "#7dd3c0", "#f5f3ea", "#efb35a"]
    pieces = []
    for i in range(28):
        left = (i * 37) % 100
        delay = (i % 7) * 0.12
        duration = 2.2 + (i % 5) * 0.3
        color = colors[i % len(colors)]
        rotate_start = (i * 53) % 360
        pieces.append(
            f'<div class="sfq-confetti-piece" style="left:{left}%; '
            f'animation-duration:{duration}s; animation-delay:{delay}s; '
            f'background:{color}; transform:rotate({rotate_start}deg);"></div>'
        )
    return "".join(pieces)


def render_splash_screen():
    """Animated splash/lobby screen shown before the host begins Question 1.
    Purely decorative (rings, shimmering title, breathing dots) -- the
    actual game state transition happens when the host clicks
    'Begin Question 1' in the admin panel."""
    return """
    <div class="sfq-splash-wrap">
        <div class="sfq-splash-ring"></div>
        <div class="sfq-splash-ring ring-2"></div>
        <div class="sfq-splash-ring ring-3"></div>
        <div class="sfq-splash-logo">Serene <span class="accent">Falah</span> Quiz</div>
        <div class="sfq-splash-tagline">The Show Begins Shortly</div>
        <div class="sfq-splash-sub">Get ready, the game is about to start...</div>
        <div class="sfq-splash-dots"><span></span><span></span><span></span></div>
    </div>
    """


def play_lobby_intro_loop(active: bool):
    """Loops kbc_intro_main.mp3 softly while the splash/lobby screen is
    showing. Idempotent like play_ambient_loop -- safe to call on every
    poll tick without restarting/clicking the audio. Silently does
    nothing if the file hasn't been uploaded to assets/ yet."""
    intro_uri = _load_audio_b64(CUSTOM_AUDIO_FILES.get("lobby_intro", ""))
    if not intro_uri:
        return
    html = f"""
    <div style="display:none"></div>
    <script>
    (function() {{
        try {{
            if (!window.__sfqLobbyIntro) {{ window.__sfqLobbyIntro = {{ audio: null, playing: false }}; }}
            const state = window.__sfqLobbyIntro;
            const shouldPlay = {str(active).lower()};

            if (!shouldPlay) {{
                if (state.playing && state.audio) {{
                    const a = state.audio;
                    const fadeStep = () => {{
                        a.volume = Math.max(0, a.volume - 0.06);
                        if (a.volume > 0) {{ setTimeout(fadeStep, 40); }}
                        else {{ a.pause(); state.playing = false; }}
                    }};
                    fadeStep();
                }}
                return;
            }}

            if (state.playing) return;  // already looping, don't restart/click

            const audio = new Audio("{intro_uri}");
            audio.loop = true;
            audio.volume = 0.75;
            audio.play().catch(e => console.log('Lobby intro autoplay blocked:', e));
            state.audio = audio;
            state.playing = true;
        }} catch (e) {{
            console.log('Lobby intro loop skipped:', e);
        }}
    }})();
    </script>
    """
    components.html(html, height=0, width=0)


def render_display():
    st.markdown(DISPLAY_CSS, unsafe_allow_html=True)
    st.markdown('<div class="sfq-title">Serene <span class="accent">Falah</span> Quiz</div>', unsafe_allow_html=True)

    try:
        if HAS_FRAGMENT:
            _render_live_game_area_fragment()
        else:
            # Fallback for older Streamlit without st.fragment: fast whole-page
            # polling. Less smooth (the title above will also flash on each
            # rerun) but still far more responsive than a 1s refresh.
            if HAS_AUTOREFRESH:
                st_autorefresh(interval=350, key="display_refresh_fallback")
            _render_live_game_area()
    except Exception as e:
        # Surface the real error instead of a silent blank screen. If the
        # display ever looks completely empty, this box is what should show
        # up instead -- copy the message shown here when reporting a bug.
        st.error(f"Display failed to render: {type(e).__name__}: {e}")
        import traceback
        st.code(traceback.format_exc())


def _render_live_game_area():
    state = get_state()
    state = advance_auto_reveal_and_timer(state)
    status = state["status"]
    q_index = state["current_question_index"]
    bank = state["active_question_bank"]
    name = state.get("contestant_name") or "Contestant"
    revealed_options = state.get("revealed_options", [])

    last_played = st.session_state.get("last_sound_cue_id", 0)
    current_cue_id = state.get("sound_cue_id", 0)
    if current_cue_id != last_played and state.get("sound_cue"):
        play_cue(state["sound_cue"], current_cue_id)
        st.session_state["last_sound_cue_id"] = current_cue_id

    # Splash screen: before the host begins Question 1, show the animated
    # welcome screen with the intro track looping, and skip the rest of
    # the in-game layout (header/prize badge/lifeline dock don't make
    # sense before the game has actually started).
    play_lobby_intro_loop(status == "lobby")
    if status == "lobby":
        st.markdown(render_splash_screen(), unsafe_allow_html=True)
        return

    # Continuous soft ambient sound while a question is actively being
    # played (not during lobby, suspense, or after reveal/win/lose --
    # those get their own one-shot cues instead). play_ambient_loop is
    # idempotent, so calling it every 0.35s tick doesn't restart or click
    # the audio.
    in_suspense_now = state.get("reveal_stage") == "suspense"
    ambient_should_play = (
        status == "question"
        and revealed_options
        and not in_suspense_now
        and not state.get("revealed_correct")
    )
    if q_index <= 4:
        tension = "low"
    elif q_index <= 9:
        tension = "mid"
    else:
        tension = "high"
    play_ambient_loop(ambient_should_play, tension=tension)

    # Detect whether the question itself (not the options) actually changed
    # since the last poll tick, so the question-card slide-in animation
    # fires once on a genuine transition rather than replaying every 0.35s
    # the fragment reruns.
    transition_key = (status, q_index)
    is_new_transition = st.session_state.get("last_transition_key") != transition_key
    st.session_state["last_transition_key"] = transition_key
    animate_class = " animate-in" if is_new_transition else ""

    # Track which individual options have already played their entrance
    # animation, so revealing option C doesn't replay A and B's slide-in.
    prev_revealed = st.session_state.get("last_revealed_options", [])
    newly_revealed = [i for i in revealed_options if i not in prev_revealed]
    st.session_state["last_revealed_options"] = list(revealed_options)

    # ---- Header row: contestant name on the left, ladder toggle on the right ----
    st.session_state.setdefault("show_ladder_panel", False)
    header_col1, header_col2, header_col3 = st.columns([2, 1, 1])
    with header_col1:
        st.markdown(f'<div class="sfq-header-name">Playing: <b>&nbsp;{name}</b></div>', unsafe_allow_html=True)
    with header_col2:
        current_prize = format_money(PRIZE_LADDER[q_index]) if q_index < len(PRIZE_LADDER) else format_money(PRIZE_LADDER[-1])
        st.markdown(
            f'<div class="sfq-prize-badge"><div class="label">Playing For</div>'
            f'<div class="amount">{current_prize}</div></div>',
            unsafe_allow_html=True,
        )
    with header_col3:
        label = "Hide Milestones" if st.session_state["show_ladder_panel"] else "🏆 Milestones"
        if st.button(label, use_container_width=True, key="ladder_toggle_btn"):
            st.session_state["show_ladder_panel"] = not st.session_state["show_ladder_panel"]
            if HAS_FRAGMENT:
                st.rerun(scope="fragment")
            else:
                st.rerun()

    if st.session_state["show_ladder_panel"]:
        st.markdown(render_ladder(q_index, status), unsafe_allow_html=True)

    lifelines = state.get("lifelines_used", {})
    icons = {"fifty_fifty": "50:50", "phone_a_friend": "📞 Phone", "flip_question": "🔄 Flip"}
    lifeline_html = '<div class="sfq-lifeline-dock">'
    for key, icon in icons.items():
        used = lifelines.get(key, False)
        cls = "sfq-lifeline-chip used" if used else "sfq-lifeline-chip"
        lifeline_html += f'<div class="{cls}">{icon}</div>'
    lifeline_html += '</div>'
    st.markdown(lifeline_html, unsafe_allow_html=True)

    if status in ("question", "locked") and revealed_options:
        remaining = timer_remaining(state)
        total = state.get("timer_seconds", 0)
        if total > 0:
            st.markdown(render_timer_ring(remaining, total), unsafe_allow_html=True)

    if status in ("question", "locked", "revealed"):
        if q_index < len(bank):
            q = bank[q_index]
            in_suspense = state.get("reveal_stage") == "suspense"
            card_class = "sfq-question-card suspense" if in_suspense else "sfq-question-card"
            card_class += animate_class

            # Celebration effect on a correct reveal, intensity scaled to
            # how much money was on the line. Fires once per reveal (not
            # replayed on every 0.35s poll) using the same "new transition"
            # tracking pattern as the question slide-in.
            tier = state.get("celebration_tier")
            show_celebration = (
                status == "revealed" and state.get("revealed_correct")
                and tier in ("medium", "big")
            )
            celebration_key = (q_index, tier, state.get("revealed_correct"))
            is_new_celebration = show_celebration and st.session_state.get("last_celebration_key") != celebration_key
            if show_celebration:
                st.session_state["last_celebration_key"] = celebration_key
            if is_new_celebration:
                card_class += f" sfq-celebrate-{tier}"

            st.markdown(
                f'<div class="sfq-qnumber">Question {q_index + 1} '
                f'&nbsp;·&nbsp; For {format_money(PRIZE_LADDER[q_index])}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="{card_class}"><div class="sfq-question-text">'
                f'{q["question"]}</div></div>',
                unsafe_allow_html=True,
            )

            if is_new_celebration and tier == "big":
                st.markdown(render_confetti_burst(), unsafe_allow_html=True)

            if in_suspense:
                st.markdown(render_suspense_overlay(), unsafe_allow_html=True)

            if state.get("phone_a_friend_active") or state.get("phone_a_friend_text"):
                txt = state.get("phone_a_friend_text") or "Connecting the call..."
                st.markdown(f'<div class="sfq-phone-bubble">📞 "{txt}"</div>', unsafe_allow_html=True)

            if revealed_options:
                eliminated = set(state.get("eliminated_options", []))
                selected = state.get("selected_option")
                revealed = state.get("revealed_correct")
                correct_idx = q["correct_index"]

                for i, opt in enumerate(q["options"]):
                    if i not in revealed_options:
                        continue  # this option hasn't been shown yet -- skip entirely
                    classes = "sfq-option"
                    if i in newly_revealed:
                        classes += " animate-in"
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
                        f'<span class="sfq-option-letter">{render_option_letter(i)}</span>'
                        f'<span>{opt}</span></div>',
                        unsafe_allow_html=True,
                    )
        else:
            st.info("Waiting for next question...")

    elif status == "won":
        amt = format_money(state.get("final_amount", PRIZE_LADDER[-1]))
        st.markdown(f'<div class="sfq-result-banner sfq-result-win">🏆 {name} wins {amt}!</div>',
                    unsafe_allow_html=True)

    elif status == "lost":
        amt = format_money(state.get("final_amount", 0))
        st.markdown(f'<div class="sfq-result-banner sfq-result-lost">Game over — {name} takes home {amt}</div>',
                    unsafe_allow_html=True)

    elif status == "quit":
        amt = format_money(state.get("final_amount", 0))
        st.markdown(f'<div class="sfq-result-banner sfq-result-quit">{name} walks away with {amt}!</div>',
                    unsafe_allow_html=True)


# Polling interval for the live game area. 0.35s feels close to instant to a
# human watching a screen, while still being cheap (a small SQLite read).
# Wrapping _render_live_game_area in a fragment means ONLY this part reruns
# on each tick -- the title/CSS rendered in render_display() above stay
# untouched, so there's no whole-page flash the way a 1s full-page
# st_autorefresh caused. This is the same trick that makes a live
# vote-count bar chart feel instant: fast polling scoped to just the piece
# that actually changes.
if HAS_FRAGMENT:
    _render_live_game_area_fragment = st.fragment(run_every=0.35)(_render_live_game_area)


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
    st.title("🎙️ Serene Falah Quiz — Admin")

    state = get_state()
    status = state["status"]

    # Auto-refresh the admin panel (slower than the display's 0.35s -- this
    # page has many buttons, and a host doesn't need sub-second precision on
    # the *status text*, just enough to watch the auto-reveal/timer progress
    # without manually refreshing). Only runs while something is actually
    # progressing automatically, to avoid needless reruns the rest of the time.
    auto_progressing = (
        state.get("options_reveal_started_at") is not None
        and len(state.get("revealed_options", [])) < 4
    ) or (
        state.get("timer_started_at") is not None and timer_remaining(state) > 0
    )
    if auto_progressing and HAS_AUTOREFRESH:
        st_autorefresh(interval=500, key="admin_progress_refresh")

    state = advance_auto_reveal_and_timer(state)

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
        st.info("Start a new game above. The display screen will show the splash/welcome screen until you begin Question 1.")
        if st.button("▶️ Begin Question 1", use_container_width=True, type="primary"):
            fresh = get_state()
            if fresh.get("active_question_bank"):
                # "start_play" plays intro_start_play.mp3 (your custom game-start
                # cue) instead of the generic "question" tone -- this is the
                # one-shot that marks the show actually kicking off.
                trigger_sound("start_play", {"status": "question"})
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

    revealed_options = state.get("revealed_options", [])
    reveal_in_progress = state.get("options_reveal_started_at") is not None

    if status == "question" and not reveal_in_progress:
        st.write("**👁️ Reveal options** — each appears 1 second apart, automatically:")
        if st.button("Show Options", use_container_width=True, type="primary"):
            trigger_sound("question", {"options_reveal_started_at": time.time(), "revealed_options": []})
            st.rerun()

    if reveal_in_progress and len(revealed_options) < 4:
        st.info(f"Revealing options... {len(revealed_options)}/4 shown so far")

    duration = timer_duration_for_question(q_index)
    if revealed_options and len(revealed_options) >= 4:
        if duration > 0:
            timer_running = state.get("timer_started_at") is not None
            if timer_running:
                remaining = timer_remaining(state)
                st.write(f"**⏱️ Timer running automatically — {math.ceil(remaining)}s left** "
                         f"(auto-started when option D appeared, {duration}s total for this question)")
            else:
                st.write("**⏱️ Timer will auto-start once options finish revealing**")
        else:
            st.write("**⏱️ This question is untimed** (Q11-15, same as real KBC)")

        st.write("**Contestant's answer:**")
        opt_cols = st.columns(4)
        eliminated = set(state.get("eliminated_options", []))
        for i in range(4):
            with opt_cols[i]:
                not_yet_shown = i not in revealed_options
                disabled = (i in eliminated) or not_yet_shown
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
                    if is_correct:
                        tier = celebration_tier(q_index)
                        cue = f"correct_{tier}"
                        trigger_sound(cue, {
                            "status": "revealed", "revealed_correct": True, "reveal_stage": "done",
                            "celebration_tier": tier,
                        })
                    else:
                        trigger_sound("wrong", {"status": "revealed", "revealed_correct": True, "reveal_stage": "done"})
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
                        # Genuine "move on to the next question" transition ->
                        # plays next_question.mp3 (your custom cue) instead of
                        # advancing silently.
                        trigger_sound("next_question", {
                            "status": "question",
                            "current_question_index": q_index + 1,
                            "selected_option": None,
                            "revealed_correct": False,
                            "reveal_stage": None,
                            "eliminated_options": [],
                            "revealed_options": [],
                            "options_reveal_started_at": None,
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
            # 50:50 removes two wrong options entirely -- reveal only the
            # two survivors (correct answer + one wrong one) immediately,
            # bypassing the normal 1s-apart auto-reveal. If the auto-reveal
            # sequence was still running when 50:50 was used, stop it here
            # so it can't re-add an eliminated option a moment later.
            survivors = [i for i in range(4) if i not in to_remove]
            trigger_sound("lifeline", {
                "eliminated_options": to_remove, "lifelines_used": lifelines,
                "revealed_options": sorted(set(survivors) | (set(state.get("revealed_options", [])) - set(to_remove))),
                "options_reveal_started_at": None,
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
                    "revealed_options": [],
                    "options_reveal_started_at": None,
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
        st.set_page_config(page_title="Serene Falah Quiz — Admin", layout="centered")
        render_admin()
    else:
        st.set_page_config(page_title="Serene Falah Quiz — Live", layout="wide", initial_sidebar_state="collapsed")
        render_display()


if __name__ == "__main__":
    main()
