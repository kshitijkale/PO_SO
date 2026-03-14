import os
import streamlit as st
from pathlib import Path

from data_parser import get_available_runs, LogParser

from tabs.dashboard import render_dashboard
from tabs.iterations import render_iterations
from tabs.memory import render_memory
from tabs.llm_calls import render_llm_calls
from tabs.candidates import render_candidates

# Set page config
st.set_page_config(
    page_title="GEPA Log Explorer",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded",
)

def main():
    st.sidebar.title("🧬 GEPA Log Explorer")
    
    # Run Selection
    base_dir = st.sidebar.text_input("Outputs Directory", value="outputs")
    available_runs = get_available_runs(base_dir)
    
    if not available_runs:
        st.warning(f"No runs found in `{base_dir}/`. Please make sure you have run GEPA with `research_mode=True`.")
        return
        
    selected_run = st.sidebar.selectbox("Select Run", options=available_runs)
    run_dir = Path(base_dir) / selected_run
    
    st.sidebar.markdown("---")
    
    # Tab Selection
    tabs = {
        "Dashboard": render_dashboard,
        "Iterations": render_iterations,
        "Memory & Lessons": render_memory,
        "LLM Calls": render_llm_calls,
        "Candidates": render_candidates,
    }
    
    selected_tab = st.sidebar.radio("Navigation", list(tabs.keys()))
    
    st.sidebar.markdown("---")
    st.sidebar.info(f"**Current Run:**\n`{selected_run}`")
    
    # Initialize parser
    parser = LogParser(str(run_dir))
    
    # Render selected tab
    tabs[selected_tab](parser)

if __name__ == "__main__":
    main()
