import streamlit as st
import pandas as pd
from difflib import HtmlDiff

def render_iterations(parser):
    st.title("🔄 Iteration Explorer")
    
    summary_df = parser.get_summary()
    if summary_df.empty:
        st.warning("No summary.jsonl data found to determine iterations.")
        return
        
    iterations = summary_df["iteration"].tolist()
    
    # Select Iteration
    selected_iter = st.select_slider("Select Iteration", options=iterations, value=iterations[-1])
    
    events = parser.get_events_for_iteration(selected_iter)
    if not events:
        st.info(f"No detailed log events found for iteration {selected_iter}.")
        return
        
    st.header(f"Iteration {selected_iter} Details")
    
    # Extract key events
    eval_pre = next((e for e in events if e.get("event_type") == "evaluation_end" and e.get("capture_traces") is True), None)
    eval_post = next((e for e in events if e.get("event_type") == "evaluation_end" and e.get("capture_traces") is False), None)
    decision = next((e for e in events if e.get("event_type") in ("candidate_accepted", "candidate_rejected")), None)
    proposal = next((e for e in events if e.get("event_type") == "proposal_end"), None)
    
    # High-level outcome
    if decision:
        is_accepted = decision["event_type"] == "candidate_accepted"
        color = "green" if is_accepted else "red"
        st.markdown(f"**Outcome:** :{color}[{decision['event_type'].replace('_', ' ').upper()}]")
        if not is_accepted:
            st.markdown(f"**Reason:** {decision.get('reason', 'N/A')}")
    
    # Scores
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Before Mutation")
        if eval_pre:
            scores = eval_pre.get("scores", [])
            st.metric("Subsample Score", f"{sum(scores):.4f}" if scores else "N/A")
            with st.expander("Per-example scores"):
                st.write(scores)
        else:
            st.write("No pre-mutation evaluation found.")
            
    with col2:
        st.subheader("After Mutation")
        if eval_post:
            scores = eval_post.get("scores", [])
            st.metric("Subsample Score", f"{sum(scores):.4f}" if scores else "N/A")
            with st.expander("Per-example scores"):
                st.write(scores)
        else:
            st.write("No post-mutation evaluation found.")
            
    st.markdown("---")
    
    # Proposed Changes
    if proposal and proposal.get("new_instructions"):
        st.header("Proposed Changes")
        for comp, new_text in proposal["new_instructions"].items():
            st.subheader(f"Component: `{comp}`")
            # We don't have the explicit 'old text' in proposal_end cleanly unless we fetch candidate_selected
            cand_selected = next((e for e in events if e.get("event_type") == "candidate_selected"), None)
            old_text = ""
            if cand_selected and cand_selected.get("candidate"):
                old_text = cand_selected["candidate"].get(comp, "")
                
            if old_text:
                # Simple diff
                diff = HtmlDiff().make_table(old_text.splitlines(), new_text.splitlines(), "Old", "New", context=True)
                st.components.v1.html(diff, height=400, scrolling=True)
            else:
                st.code(new_text)
                
    # Raw Events
    with st.expander("View Raw Events JSON"):
        st.json(events)
