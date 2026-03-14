import streamlit as st

def render_memory(parser):
    st.title("🧠 Memory & Lessons")
    
    tab1, tab2 = st.tabs(["Lessons Learned", "Memory Injections"])
    
    with tab1:
        st.header("Generated Lessons (V2)")
        lessons_df = parser.get_lessons()
        if lessons_df.empty:
            st.info("No lessons found. Make sure GEPA was run with a `lesson_lm`.")
        else:
            # Format display
            display_cols = ["iteration", "component_name", "accepted", "score_delta", "intent", "lesson", "categories_succeeded", "categories_failed"]
            # Add score delta if possible
            if "score_after" in lessons_df.columns and "score_before" in lessons_df.columns:
                lessons_df["score_delta"] = lessons_df["score_after"] - lessons_df["score_before"]
            
            existing_cols = [c for c in display_cols if c in lessons_df.columns]
            
            st.dataframe(
                lessons_df[existing_cols].sort_values("iteration", ascending=False),
                use_container_width=True,
                hide_index=True
            )
            
    with tab2:
        st.header("Memory Query Events")
        events_df = parser.get_memory_events()
        if events_df.empty:
            st.info("No memory events found. Make sure GEPA was run with `use_reflection_memory=True`.")
        else:
            queries = events_df[events_df["event"] == "query"].copy()
            if queries.empty:
                st.info("No query events found.")
            else:
                for _, row in queries.sort_values("iteration", ascending=False).iterrows():
                    with st.expander(f"Iter {row.get('iteration', '?')} - {row.get('component', '?')} ({row.get('query_n', 0)} entries)"):
                        st.markdown("**Injected Text:**")
                        st.code(row.get("formatted_text", ""))
                        st.markdown("**Selected Entries:**")
                        st.json(row.get("entries_returned", []))
