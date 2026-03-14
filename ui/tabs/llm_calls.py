import streamlit as st

def render_llm_calls(parser):
    st.title("🤖 LLM Calls Inspector")
    
    calls = parser.list_llm_calls()
    if not calls:
        st.info("No LLM calls found in the `llm_calls/` directory.")
        return
        
    # Create filters
    iterations = sorted(list(set(c["iteration"] for c in calls if c["iteration"] is not None)))
    components = sorted(list(set(c["component_name"] for c in calls if c["component_name"] is not None)))
    
    col1, col2 = st.columns(2)
    with col1:
        selected_iter = st.selectbox("Filter by Iteration", options=["All"] + iterations)
    with col2:
        selected_comp = st.selectbox("Filter by Component", options=["All"] + components)
        
    # Apply filters
    filtered_calls = calls
    if selected_iter != "All":
        filtered_calls = [c for c in filtered_calls if c["iteration"] == selected_iter]
    if selected_comp != "All":
        filtered_calls = [c for c in filtered_calls if c["component_name"] == selected_comp]
        
    st.markdown(f"Found {len(filtered_calls)} calls.")
    
    for call in filtered_calls:
        it = call.get("iteration")
        comp = call.get("component_name")
        with st.expander(f"Iter {it} - {comp} ({call.get('model_id')}, {call.get('latency_ms', 0):.0f}ms)"):
            details = parser.get_llm_call_details(it, comp)
            
            tab_prompt, tab_response, tab_meta = st.tabs(["Prompt", "Response", "Metadata"])
            
            with tab_prompt:
                if details["prompt"]:
                    st.code(details["prompt"], language="text")
                else:
                    st.warning("Prompt file not found.")
                    
            with tab_response:
                if details["response"]:
                    st.code(details["response"], language="text")
                else:
                    st.warning("Response file not found.")
                    
            with tab_meta:
                st.json(call["data"])
