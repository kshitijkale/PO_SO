import streamlit as st
import plotly.express as px

def render_dashboard(parser):
    st.title("📊 Dashboard")
    
    summary_df = parser.get_summary()
    if summary_df.empty:
        st.warning("No summary.jsonl data found.")
        return
        
    # Key Metrics
    st.header("Key Metrics")
    col1, col2, col3, col4 = st.columns(4)
    
    last_row = summary_df.iloc[-1]
    
    with col1:
        st.metric("Total Iterations", int(last_row.get("iteration", 0)))
    with col2:
        st.metric("Best Score", f"{last_row.get('best_score', 0):.4f}")
    with col3:
        st.metric("Total Metric Calls", int(last_row.get("total_evals", 0)))
    with col4:
        st.metric("Acceptance Rate", f"{last_row.get('acceptance_rate', 0):.1%}")

    st.markdown("---")
    
    # Best Score Progress
    st.header("Score Progression")
    
    if "best_score" in summary_df.columns and "iteration" in summary_df.columns:
        fig = px.line(
            summary_df, 
            x="iteration", 
            y="best_score", 
            title="Best Score over Iterations",
            markers=True
        )
        st.plotly_chart(fig, use_container_width=True)
    
    # Raw Data
    with st.expander("View Raw Summary Data"):
        st.dataframe(summary_df)
