import streamlit as st

def render_candidates(parser):
    st.title("🧬 Candidates")
    
    candidates = parser.list_candidates()
    if not candidates:
        st.info("No candidates found in the `candidates/` directory.")
        return
        
    # List of indices for selection
    indices = [c.get("index") for c in candidates]
    
    st.sidebar.markdown("### Select Candidate")
    selected_idx = st.sidebar.selectbox("Candidate Index", options=indices)
    
    selected_cand = next((c for c in candidates if c.get("index") == selected_idx), None)
    
    if selected_cand:
        st.header(f"Candidate #{selected_cand.get('index')}")
        st.markdown(f"**Created in Iteration:** {selected_cand.get('iteration_created')}")
        st.markdown(f"**Operation:** {selected_cand.get('operation')}")
        st.markdown(f"**Parents:** {selected_cand.get('parent_indices')}")
        
        st.markdown("---")
        st.subheader("Components")
        
        components = selected_cand.get("components", {})
        for comp_name, comp_text in components.items():
            with st.expander(f"`{comp_name}`", expanded=True):
                st.code(comp_text)
                
        with st.expander("Raw JSON"):
            st.json(selected_cand)
