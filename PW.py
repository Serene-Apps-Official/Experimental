import streamlit as st
import pandas as pd
from datetime import datetime

# Configure page layout and style
st.set_page_config(
    page_title="Tuition Attendance Poll",
    page_icon="📅",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# 1. Initialize Persistent Storage State via Session State
if "poll_data" not in st.session_state:
    st.session_state.poll_data = {
        "Yes, I am coming": 0,
        "No, I am not coming": 0
    }
if "has_voted" not in st.session_state:
    st.session_state.has_voted = False

# Custom Neumorphic Light Theme Styling (Soft Shadows, Smooth Gradients)
neumorphic_css = """
<style>
    /* Global Background Adjustments */
    .stApp {
        background-color: #E0E5EC !important;
        font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
    }
    
    /* Neumorphic Card Container */
    .neumorphic-card {
        background: #E0E5EC;
        border-radius: 20px;
        box-shadow: 9px 9px 16px #A3B1C6, -9px -9px 16px #FFFFFF;
        padding: 30px;
        margin-bottom: 25px;
    }
    
    /* Gradient Blue Typography */
    .gradient-text {
        background: linear-gradient(135deg, #2E72E5 0%, #154294 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 700;
        text-align: center;
        margin: 0;
    }
    
    /* Subtitle / Date styling */
    .date-display {
        color: #616E7C;
        font-size: 1.1rem;
        font-weight: 500;
        text-align: center;
        letter-spacing: 0.5px;
        margin-bottom: 20px;
    }
    
    /* Styled Stat Display for Live Updates */
    .stat-box {
        background: #E0E5EC;
        border-radius: 12px;
        box-shadow: inset 4px 4px 8px #A3B1C6, inset -4px -4px 8px #FFFFFF;
        padding: 15px;
        text-align: center;
        margin-top: 10px;
    }
    
    .stat-count {
        font-size: 1.8rem;
        font-weight: bold;
        color: #2E72E5;
    }
    
    .stat-label {
        font-size: 0.85rem;
        color: #616E7C;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
</style>
"""
st.markdown(neumorphic_css, unsafe_allow_html=True)

# Fetch Current Date Context
current_date = datetime.now().strftime("%B %d, %Y")

# Render Main Interface Inside Neumorphic Container
st.markdown(f"""
<div class="neumorphic-card">
    <h1 class="gradient-text" style="font-size: 2.3rem; margin-bottom: 5px;">Tuition Dashboard</h1>
    <div class="date-display">Date: {current_date}</div>
</div>
""", unsafe_allow_html=True)

# 2. Voting Interface Logic
if not st.session_state.has_voted:
    st.markdown("""
    <div class="neumorphic-card">
        <h2 class="gradient-text" style="font-size: 1.6rem; text-align: left; margin-bottom: 20px;">
            Are you coming today for Tuition?
        </h2>
    </div>
    """, unsafe_allow_html=True)

    # Selection component
    choice = st.radio(
        label="Select your attendance status:",
        options=list(st.session_state.poll_data.keys()),
        label_visibility="collapsed"
    )
    
    st.write("") # Spacer
    
    # Vote submission processing
    if st.button("Submit Attendance", use_container_width=True):
        st.session_state.poll_data[choice] += 1
        st.session_state.has_voted = True
        st.rerun()

# 3. Results & Visualization Rendering
else:
    st.markdown("""
    <div class="neumorphic-card">
        <h2 class="gradient-text" style="font-size: 1.6rem; text-align: left; margin-bottom: 15px;">
            Attendance Confirmed
        </h2>
    </div>
    """, unsafe_allow_html=True)
    
    # Extract metrics
    yes_count = st.session_state.poll_data["Yes, I am coming"]
    no_count = st.session_state.poll_data["No, I am not coming"]
    total_votes = yes_count + no_count

    # Live Metrics Visualizer Card
    st.markdown("<div class='neumorphic-card'>", unsafe_allow_html=True)
    st.markdown("<h3 class='gradient-text' style='font-size: 1.3rem; text-align: left; margin-bottom: 15px;'>Live Response Overview</h3>", unsafe_allow_html=True)

    metric_col1, metric_col2, metric_col3 = st.columns(3)

    with metric_col1:
        st.markdown(f"""
        <div class="stat-box">
            <div class="stat-count">{yes_count}</div>
            <div class="stat-label">Attending</div>
        </div>
        """, unsafe_allow_html=True)

    with metric_col2:
        st.markdown(f"""
        <div class="stat-box">
            <div class="stat-count">{no_count}</div>
            <div class="stat-label">Absent</div>
        </div>
        """, unsafe_allow_html=True)

    with metric_col3:
        st.markdown(f"""
        <div class="stat-box">
            <div class="stat-count">{total_votes}</div>
            <div class="stat-label">Total Logs</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
    
    # Option to reset state (Change Vote)
    if st.button("Change Vote", use_container_width=True):
        # Subtract the previous vote to keep data accurate on change
        st.session_state.has_voted = False
        st.rerun()

