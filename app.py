"""Streamlit front-end for the Financial Red-Flag Extractor."""

from __future__ import annotations

import hashlib
import io
import tempfile
from io import BytesIO
from pathlib import Path

import fitz
import pandas as pd
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core_extractor import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ExtractionError,
    LocalAPIError,
    ParseError,
    RedFlagReport,
    analyze_pdf_text,
)

PAGE_TITLE = "Financial Statement Auditor"

# High-contrast dark theme badge & card colors
BADGE_COLORS = {
    "High": ("#ff4d4f", "#2a1215"),  # Red badge, dark red container background
    "Medium": ("#faad14", "#2b2111"),  # Amber badge, dark amber container background
    "Low": ("#52c41a", "#132313"),  # Green badge, dark green container background
}


def _init_session_state() -> None:
    defaults = {
        "last_file_hash": None,
        "filename": None,
        "page_count": 0,
        "report": None,
        "error": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _file_hash(file_bytes: bytes) -> str:
    return hashlib.md5(file_bytes, usedforsecurity=False).hexdigest()


def _count_pdf_pages(pdf_path: Path) -> int:
    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def _process_upload(
    file_bytes: bytes,
    filename: str,
    base_url: str,
    api_key: str,
    model: str,
) -> None:
    """Extract and analyze a PDF in chunks, storing results in session state."""
    st.session_state.error = None
    st.session_state.report = None
    st.session_state.filename = filename

    suffix = Path(filename).suffix or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        # Set page count upfront so metric updates immediately
        total_pages = _count_pdf_pages(tmp_path)
        st.session_state.page_count = total_pages

        # Create live status box
        status_box = st.empty()

        def update_progress(current_chunk: int, total_chunks: int):
            status_box.info(
                f"Analyzing page chunk **{current_chunk} of {total_chunks}** "
                f"using {model}…"
            )

        st.session_state.report = analyze_pdf_text(
            tmp_path,
            base_url=base_url,
            api_key=api_key,
            model=model,
            progress_callback=update_progress,
        )
        status_box.empty()  # Clear status indicator on completion

    except LocalAPIError as exc:
        st.session_state.error = str(exc)
    except ParseError as exc:
        st.session_state.error = f"Failed to parse model output: {exc}"
    except ExtractionError as exc:
        st.session_state.error = str(exc)
    # PyMuPDF raises RuntimeError subclasses (e.g. FileDataError,
    # FileNotFoundError) for corrupt/unreadable PDFs; OSError covers
    # temp-file I/O failures and ValueError guards malformed inputs.
    # Catching these specific types avoids a blind `except Exception` (Ruff BLE001).
    except (OSError, ValueError, RuntimeError) as exc:
        st.session_state.error = f"Unexpected error during analysis: {exc}"
    finally:
        tmp_path.unlink(missing_ok=True)


def _report_to_dataframe(report: RedFlagReport) -> pd.DataFrame:
    rows = [
        {
            "Category": item.category,
            "Flag Title": item.flag_title,
            "Risk Level": item.risk_level,
            "Page Number": item.page_number,
            "Excerpt": item.excerpt,
            "Analysis": item.analysis,
        }
        for item in report.risk_items
    ]
    return pd.DataFrame(rows)


def _risk_counts(report: RedFlagReport | None) -> tuple[int, int, int]:
    if report is None:
        return 0, 0, 0
    high = sum(1 for item in report.risk_items if item.risk_level == "High")
    medium = sum(1 for item in report.risk_items if item.risk_level == "Medium")
    low = sum(1 for item in report.risk_items if item.risk_level == "Low")
    return high, medium, low


def _generate_pdf_report(risk_items: list) -> io.BytesIO:
    """Generates an institutional PDF summary of findings using ReportLab."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30,
    )
    story = []
    styles = getSampleStyleSheet()

    story.append(Paragraph("<b>Financial Statement Audit Report</b>", styles["Title"]))
    story.append(Spacer(1, 12))

    table_data = [["Page", "Level", "Category", "Finding Title"]]
    for item in risk_items:
        r_level = (
            item.risk_level.value
            if hasattr(item.risk_level, "value")
            else str(item.risk_level)
        )
        table_data.append(
            [
                str(item.page_number),
                r_level,
                item.category,
                item.flag_title,
            ]
        )

    t = Table(table_data, colWidths=[40, 60, 150, 300])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ]
        )
    )
    story.append(t)
    doc.build(story)
    buffer.seek(0)
    return buffer


def _sort_risk_items(items: list, sort_option: str) -> list:
    """Sorts risk items dynamically based on user selection."""
    level_weights = {"High": 0, "Medium": 1, "Low": 2}

    def get_level_str(x):
        return (
            x.risk_level.value if hasattr(x.risk_level, "value") else str(x.risk_level)
        )

    if sort_option == "Risk (Descending: High → Low)":
        return sorted(
            items, key=lambda x: (level_weights.get(get_level_str(x), 9), x.page_number)
        )
    elif sort_option == "Risk (Ascending: Low → High)":
        return sorted(
            items,
            key=lambda x: (-level_weights.get(get_level_str(x), 0), x.page_number),
        )
    elif sort_option == "Category → Risk → Page":
        return sorted(
            items,
            key=lambda x: (
                x.category,
                level_weights.get(get_level_str(x), 9),
                x.page_number,
            ),
        )
    elif sort_option == "Page Number":
        return sorted(items, key=lambda x: x.page_number)
    return items


def _render_risk_card(item) -> None:
    """Renders a high-contrast dark card with bright readable title text."""
    r_level = (
        item.risk_level.value
        if hasattr(item.risk_level, "value")
        else str(item.risk_level)
    )
    fg_color, bg_color = BADGE_COLORS.get(r_level, ("#ffffff", "#222222"))

    st.markdown(
        f"""
        <div style="
            background-color: {bg_color}; 
            border-left: 5px solid {fg_color}; 
            padding: 16px; 
            border-radius: 8px; 
            margin-bottom: 16px;
        ">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <h3 style="margin: 0; color: #ffffff; font-size: 1.15rem; font-weight: 600;">{item.flag_title}</h3>
                <span style="
                    background-color: {fg_color}; 
                    color: #000000; 
                    font-weight: bold; 
                    padding: 3px 10px; 
                    border-radius: 4px; 
                    font-size: 0.8rem;
                ">{r_level.upper()}</span>
            </div>
            <div style="color: #a0a0a0; font-size: 0.85rem; margin-bottom: 12px;">
                <strong>Category:</strong> {item.category} &nbsp;|&nbsp; <strong>Page:</strong> {item.page_number}
            </div>
            <blockquote style="
                border-left: 3px solid #555555; 
                margin: 8px 0; 
                padding-left: 12px; 
                color: #d0d0d0; 
                font-style: italic; 
                background: rgba(255,255,255,0.03); 
                padding: 8px; 
                border-radius: 4px;
            ">
                "{item.excerpt}"
            </blockquote>
            <p style="margin: 8px 0 0 0; color: #e0e0e0; font-size: 0.95rem;">{item.analysis}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, layout="wide")
    _init_session_state()

    st.title(PAGE_TITLE)
    st.caption(
        "Upload a financial PDF to extract red flags and footnote risks "
        "using your local or cloud LLM."
    )

    with st.sidebar:
        st.header("Configuration")
        uploaded_file = st.file_uploader(
            "Upload PDF document",
            type=["pdf"],
            help="Financial statements, 10-K filings, audit reports, etc.",
        )

        provider = st.selectbox(
            "Provider Preset",
            options=["Local (Ollama)", "OpenAI", "Groq", "DeepSeek", "Custom Endpoint"],
            index=0,
        )

        # Set preset defaults
        if provider == "OpenAI":
            default_url = "https://api.openai.com/v1"
            default_model = "gpt-4o-mini"
        elif provider == "Groq":
            default_url = "https://api.groq.com/openai/v1"
            default_model = "llama-3.3-70b-versatile"
        elif provider == "DeepSeek":
            default_url = "https://api.deepseek.com"
            default_model = "deepseek-chat"
        else:
            default_url = DEFAULT_BASE_URL
            default_model = DEFAULT_MODEL

        base_url = st.text_input(
            "API Base URL",
            value=default_url,
            help="OpenAI-compatible endpoint base URL.",
        )
        api_key = st.text_input(
            "API Key",
            type="password",
            value="",
            help="Enter API key for cloud providers. Leave empty for local Ollama.",
        )
        model = st.text_input(
            "Model Name",
            value=default_model,
            help="Specify exact model identifier.",
        )
        rerun = st.button("Re-run Analysis", width="stretch")

    if uploaded_file is not None:
        file_bytes = uploaded_file.getvalue()
        current_hash = _file_hash(file_bytes)
        is_new_file = current_hash != st.session_state.last_file_hash

        if is_new_file or rerun:
            with st.spinner("Extracting text and analyzing red flags…"):
                _process_upload(
                    file_bytes, uploaded_file.name, base_url, api_key, model
                )
            st.session_state.last_file_hash = current_hash

    report: RedFlagReport | None = st.session_state.report
    high_count, medium_count, low_count = _risk_counts(report)

    metric_cols = st.columns(3)
    metric_cols[0].metric("Total Pages Processed", st.session_state.page_count)
    metric_cols[1].metric("High-Risk Flags", high_count)
    metric_cols[2].metric("Medium-Risk Flags", medium_count)

    if st.session_state.error:
        st.error(st.session_state.error)
    elif uploaded_file is None and report is None:
        st.info("Upload a PDF in the sidebar to begin analysis.")
    elif report is not None:
        st.success(
            f"Analysis complete for **{st.session_state.filename}** — "
            f"{len(report.risk_items)} flag(s) identified "
            f"({low_count} low-risk)."
        )

    summary_tab, logs_tab = st.tabs(
        ["Visual Executive Summary", "Raw Audit Logs (Dataframe)"]
    )

    with summary_tab:
        if report is None or not report.risk_items:
            st.write("No risk items to display yet.")
        else:
            # Controls Bar: Search, Sorting, CSV/PDF Export
            c1, c2, c3, c4 = st.columns([2, 2, 1, 1])

            with c1:
                search_query = st.text_input(
                    "🔍 Search Findings",
                    placeholder="e.g. SBC, foreign currency, covenant...",
                )
            with c2:
                sort_choice = st.selectbox(
                    "🔀 Sort Flags By",
                    options=[
                        "Risk (Descending: High → Low)",
                        "Risk (Ascending: Low → High)",
                        "Category → Risk → Page",
                        "Page Number",
                    ],
                )

            df_summary = _report_to_dataframe(report)
            export_stem = Path(st.session_state.filename or "audit").stem

            with c3:
                st.download_button(
                    label="📥 CSV",
                    data=df_summary.to_csv(index=False).encode("utf-8"),
                    file_name=f"{export_stem}_red_flags.csv",
                    mime="text/csv",
                    width="stretch",
                )

            with c4:
                pdf_buffer = _generate_pdf_report(report.risk_items)
                st.download_button(
                    label="📄 PDF",
                    data=pdf_buffer,
                    file_name=f"{export_stem}_red_flags.pdf",
                    mime="application/pdf",
                    width="stretch",
                )

            # Filtering logic
            filtered_items = report.risk_items
            if search_query:
                q = search_query.lower()
                filtered_items = [
                    item
                    for item in filtered_items
                    if q in item.flag_title.lower()
                    or q in item.analysis.lower()
                    or q in item.excerpt.lower()
                    or q in item.category.lower()
                ]

            # Sorting logic
            sorted_items = _sort_risk_items(filtered_items, sort_choice)

            st.caption(
                f"Showing **{len(sorted_items)}** of **{len(report.risk_items)}** flags"
            )

            # Render Cards
            for item in sorted_items:
                with st.container():
                    _render_risk_card(item)

    with logs_tab:
        if report is None or not report.risk_items:
            st.write("No audit log data available.")
        else:
            df = _report_to_dataframe(report)
            st.dataframe(df, width="stretch", hide_index=True)

            excel_buffer = BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Red Flags")
            excel_buffer.seek(0)

            export_name = (
                Path(st.session_state.filename or "audit").stem + "_red_flags.xlsx"
            )
            st.download_button(
                label="Export to Excel",
                data=excel_buffer,
                file_name=export_name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )


if __name__ == "__main__":
    main()
