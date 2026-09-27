"""
Stage 7 — UI

Streamlit interface for the Marsh insurance pitch generator and auditor.

Important:
- Loads OPENROUTER_API_KEY (required) and TAVILY_API_KEY (live company research,
  optional) from the project-root .env via src/config.py.
- Official policy chunks are loaded into a session-local copy.
- Uploaded PDFs are added only to the current Streamlit session.
- The official baseline files are never overwritten.
"""

import io
import json
import os
import sys
import tempfile
from pathlib import Path


# ============================================================
# PROJECT ROOT
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Make sure the project root is available for imports
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# LOAD .ENV / API KEY  (single provider: OpenRouter — see src/config.py)
# ============================================================

from src.config import missing_keys, describe_routing, ENV_FILE_HINT  # noqa: E402  (loads .env)


# ============================================================
# STREAMLIT
# ============================================================

import streamlit as st
import pandas as pd


# ============================================================
# PROJECT IMPORTS
# ============================================================

from src.generation.company_profile import (
    generate_company_profile,
    CompanyProfileError,
)

from src.generation.pitch_generator import (
    retrieve_chunks_for_profile,
    generate_marketing_pitch,
    build_pptx,
    PitchGenerationError,
)

from src.retrieval.vector_store import PolicyVectorStore

from src.audit.verifier import (
    audit_pitch_content,
)

from src.audit.report_writer import (
    generate_audit_docx,
)

from src.audit.review import (
    apply_review,
    items_for_review,
    suggested_edit,
)

from src.audit.verifier import AuditReport
from src.generation.pitch_generator import PitchContent
from src.generation.company_profile import CompanyProfile

from src.ingestion.pdf_extractor import (
    extract_pdf,
)

from src.ingestion.chunker import (
    chunk_pages,
)


# ============================================================
# PATHS
# ============================================================

OFFICIAL_CHUNKS_PATH = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "chunks"
    / "all_chunks.json"
)

OUTPUT_DIR = (
    PROJECT_ROOT
    / "outputs"
    / "sample_decks"
)

AUDIT_OUTPUT_DIR = (
    PROJECT_ROOT
    / "outputs"
    / "audit_reports"
)


# ============================================================
# STREAMLIT CONFIG
# ============================================================

st.set_page_config(
    page_title="Marsh Pitch Assistant",
    layout="wide",
)

# Fail visibly (not with a raw traceback, and never silently) if any key
# the CURRENT stage routing needs is missing — a missing key must stop the
# app, not quietly skip a stage (e.g. the audit's semantic check).
_missing = missing_keys()
if _missing:
    st.error("Missing API key(s): " + ", ".join(_missing))
    st.markdown(
        f"Add to the `.env` file at `{ENV_FILE_HINT}`, then restart the app:\n\n```\n"
        + "\n".join(f"{k}=your_key_here   # used by: {', '.join(v)}" for k, v in _missing.items())
        + f"\n```\n\nCurrent model routing:\n```\n{describe_routing()}\n```"
    )
    st.stop()

from src.research.web_search import get_tavily_key, TAVILY_KEY_ENV  # noqa: E402

if not get_tavily_key():
    st.sidebar.warning(
        f"{TAVILY_KEY_ENV} not set — company profiles will use model knowledge only, "
        "and every company figure will be labelled as an assumption."
    )


# ============================================================
# SESSION STATE INITIALIZATION
# ============================================================

def init_session_state():

    if "messages" not in st.session_state:
        st.session_state.messages = []

    # --------------------------------------------------------
    # Load official policy documents
    # --------------------------------------------------------

    if "policy_docs" not in st.session_state:

        if not OFFICIAL_CHUNKS_PATH.exists():
            raise FileNotFoundError(
                "Official policy chunks file was not found:\n"
                f"{OFFICIAL_CHUNKS_PATH}"
            )

        # Load official baseline into a session-only copy.
        with open(
            OFFICIAL_CHUNKS_PATH,
            "r",
            encoding="utf-8"
        ) as f:

            st.session_state.policy_docs = json.load(f)

        st.session_state.official_doc_names = sorted(
            {
                c["source_document"]
                for c in st.session_state.policy_docs
            }
        )

        st.session_state.session_added_doc_names = []

    # --------------------------------------------------------
    # Build vector store
    # --------------------------------------------------------

    if "vector_store" not in st.session_state:

        with st.spinner(
            "Loading policy documents into the search index..."
        ):

            store = PolicyVectorStore()

            store.build(
                st.session_state.policy_docs
            )

            st.session_state.vector_store = store

    # --------------------------------------------------------
    # Uploaded files tracking
    # --------------------------------------------------------

    if "processed_upload_names" not in st.session_state:
        st.session_state.processed_upload_names = set()


# ============================================================
# DOCUMENT SUMMARY
# ============================================================

def doc_summary(
    docs: list[dict]
) -> dict[str, int]:

    """
    Return:
        {
            document_name: page_count
        }
    """

    summary: dict[str, set] = {}

    for chunk in docs:

        summary.setdefault(
            chunk["source_document"],
            set()
        ).add(
            chunk["page_number"]
        )

    return {
        name: len(pages)
        for name, pages in summary.items()
    }


# ============================================================
# SIDEBAR
# ============================================================

def render_sidebar():
    with st.sidebar:
        st.header("Navigation")
        selection = st.radio(
            "Menu",
            ["Chat-bot", "Add Document"],
            label_visibility="collapsed"
        )
    return selection

def render_add_document_page():
    st.title("📄 Document Management")
    st.caption("Manage existing policy documents and upload new ones to the session's search index.")

    summary = doc_summary(st.session_state.policy_docs)

    st.subheader("Currently Uploaded Documents")

    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**Official (graded baseline)**")
        for name in st.session_state.official_doc_names:
            st.write(f"- 📄 {name} ({summary.get(name, 0)} pages)")

    with col2:
        st.markdown("**Added this session**")
        if st.session_state.session_added_doc_names:
            for name in list(st.session_state.session_added_doc_names):
                sub_col1, sub_col2 = st.columns([0.85, 0.15])
                with sub_col1:
                    st.write(f"📄 {name} ({summary.get(name, 0)} pages)")
                with sub_col2:
                    if st.button("🗑️", key=f"del_{name}", help="Delete Document"):
                        st.session_state.session_added_doc_names.remove(name)
                        st.session_state.processed_upload_names.discard(name)
                        st.session_state.policy_docs = [
                            chunk for chunk in st.session_state.policy_docs
                            if chunk["source_document"] != name
                        ]
                        store = PolicyVectorStore()
                        store.build(st.session_state.policy_docs)
                        st.session_state.vector_store = store
                        st.rerun()
        else:
            st.write("No documents added yet.")

    st.divider()

    st.subheader("➕ Add More Documents")
    st.caption("Adds a document to THIS SESSION's search index only. The official 4-document baseline used for grading is never modified.")
    
    uploaded_files = st.file_uploader(
        "Upload a policy PDF",
        type=["pdf"],
        accept_multiple_files=True,
    )

    new_files = [
        f for f in (uploaded_files or [])
        if f.name not in st.session_state.processed_upload_names
    ]

    if new_files:
        with st.status("Processing new document(s)...", expanded=True) as status:
            import tempfile, os
            for uploaded_file in new_files:
                st.write(f"Extracting text from {uploaded_file.name}...")
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp.write(uploaded_file.read())
                        tmp_path = tmp.name
                    pages = extract_pdf(tmp_path)
                    new_chunks = chunk_pages(pages, source_doc=uploaded_file.name)
                    st.session_state.policy_docs.extend(new_chunks)
                    st.session_state.session_added_doc_names.append(uploaded_file.name)
                    st.session_state.processed_upload_names.add(uploaded_file.name)
                    st.write(f"Added {len(new_chunks)} chunks from {uploaded_file.name}")
                finally:
                    if tmp_path:
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
            
            st.write("Rebuilding search index...")
            store = PolicyVectorStore()
            store.build(st.session_state.policy_docs)
            st.session_state.vector_store = store
            
            status.update(label="Document(s) added and indexed", state="complete", expanded=False)
        st.rerun()

    if st.button("🔄 Rebuild Index"):
        with st.status("Rebuilding index...", expanded=True) as status:
            store = PolicyVectorStore()
            store.build(st.session_state.policy_docs)
            st.session_state.vector_store = store
            status.update(label="Index rebuilt", state="complete")


# ============================================================
# COMPANY NAME EXTRACTION
# ============================================================

def extract_company_name(
    user_text: str
) -> str:

    """
    Remove a few common conversational prefixes.
    Otherwise treat the input directly as the company name.
    """

    text = user_text.strip()

    prefixes = [
        "generate a pitch for",
        "create a pitch for",
        "make a pitch for",
        "pitch for",
        "generate pitch for",
    ]

    lowered = text.lower()

    for prefix in prefixes:

        if lowered.startswith(prefix):

            return text[
                len(prefix):
            ].strip(" :.-")

    return text


# ============================================================
# PIPELINE
# ============================================================

def run_pipeline(
    company_name: str,
    status_box
) -> dict:

    """
    Runs the complete pipeline:

    Stage 3 → Company Profile
    Stage 4 → Retrieval
    Stage 5 → Pitch Generation
    Stage 6 → Audit
    PowerPoint / Word → Rendering
    """

    result = {
        "steps": []
    }

    # --------------------------------------------------------
    # Progress helper
    # --------------------------------------------------------

    def log(message: str):

        status_box.write(
            message
        )

        result["steps"].append(
            message
        )

    # ========================================================
    # STAGE 3 — COMPANY PROFILE
    # ========================================================

    log(
        "🔍 Researching company profile..."
    )

    profile = generate_company_profile(
        company_name
    )

    if profile.data_source == "live_web":
        log(
            f"Live research: {len(profile.sources)} web source(s), "
            f"{len(profile.facts)} sourced fact(s)"
        )
    else:
        log(
            "⚠️ No live company data — profile uses clearly-labelled assumptions"
            + (f" ({'; '.join(profile.research_notes)})" if profile.research_notes else "")
        )

    log(
        f"Found: {profile.industry} — "
        f"{profile.size_estimate}"
    )

    result["profile"] = (
        profile.model_dump()
    )

    # ========================================================
    # STAGE 4 — RETRIEVAL
    # ========================================================

    log(
        "📄 Searching policy documents "
        "for relevant coverage..."
    )

    matched_chunks = (
        retrieve_chunks_for_profile(
            profile,
            st.session_state.vector_store
        )
    )

    log(
        f"Found {len(matched_chunks)} "
        f"relevant policy section(s)"
    )

    # ========================================================
    # STAGE 5 — PITCH GENERATION
    # ========================================================

    log(
        "✍️ Drafting the pitch..."
    )

    pitch = generate_marketing_pitch(
        profile,
        matched_chunks,
        company_name
    )

    log(
        "Recommended policy: "
        f"{pitch.recommended_policy.policy_name}"
    )

    result["pitch"] = (
        pitch.model_dump()
    )

    # ========================================================
    # STAGE 6 — AUDIT
    # ========================================================

    log(
        "🕵️ Auditing every claim "
        "against source documents..."
    )

    audit = audit_pitch_content(
        pitch,
        st.session_state.policy_docs,
        profile=profile,
    )

    log(
        f"Audit complete: "
        f"{audit.overall_status} "
        f"({audit.confidence_score}% confidence)"
    )

    result["audit"] = (
        audit.model_dump()
    )

    # The final deck and audit report are produced only AFTER the advisor's
    # human verification (render_review_panel -> finalise_after_review).
    log(
        "🧑‍⚖️ Ready for human verification — review the flagged items below."
    )

    return result


# ============================================================
# HUMAN VERIFICATION + FINALISATION
# ============================================================

STATUS_LABEL = {
    "VERIFIED": "✅ Verified",
    "VERIFIED_WITH_CONDITION": "🟢 Verified – condition to add",
    "FLAGGED": "🟠 Flagged",
    "REJECTED": "🔴 Rejected",
    "UNCHECKED": "⚪ Unchecked",
}
ACTIONS = ["Approve as written", "Edit wording", "Remove from pitch"]
DEFAULT_ACTION = {"VERIFIED_WITH_CONDITION": 1, "FLAGGED": 1, "REJECTED": 2, "UNCHECKED": 0}


def _safe_name(company_name: str) -> str:
    n = "".join(c if c.isalnum() or c in " _-" else "_" for c in company_name).strip()
    return n or "Company"


def _message_by_key(key: str):
    for m in st.session_state.messages:
        if m.get("key") == key:
            return m
    return None


def finalise_after_review(company_name: str, data: dict, claim_decisions: dict,
                          statement_decisions: dict, reviewer: str) -> dict:
    """Applies the advisor's decisions, re-checks edits, then builds the FINAL deck + audit report."""
    pitch = PitchContent.model_validate(data["pitch"])
    audit = AuditReport.model_validate(data["audit"])
    profile = CompanyProfile.model_validate(data["profile"]) if data.get("profile") else None

    final_pitch, final_profile, reviewed = apply_review(
        pitch, audit, claim_decisions, statement_decisions, reviewer,
        st.session_state.policy_docs, profile=profile,
    )

    name = _safe_name(company_name)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    deck_path = OUTPUT_DIR / f"{name}_Pitch.pptx"
    build_pptx(final_pitch, str(deck_path), profile=final_profile)
    report_path = AUDIT_OUTPUT_DIR / f"{name}_Audit_Report.docx"
    generate_audit_docx(company_name, final_pitch.model_dump(), reviewed.model_dump(), str(report_path),
                        deck_file=deck_path.name)

    return {
        "audit": reviewed.model_dump(),
        "pitch": final_pitch.model_dump(),
        "pptx_bytes": deck_path.read_bytes(),
        "audit_docx_bytes": report_path.read_bytes(),
    }


def render_review_panel(company_name: str, data: dict, key_prefix: str):
    audit = data["audit"]
    items = items_for_review(audit)
    others = audit.get("other_statements") or []
    n_auto = sum(1 for r in audit.get("claim_results", []) if r.get("final_status") == "VERIFIED")

    st.markdown("#### 🧑‍⚖️ Human verification")
    st.caption(
        "Check the items the automated audit could not fully verify, then finalise. "
        "The deck and audit report are generated from your decisions; edited wording is "
        "automatically re-checked against the brochure."
    )

    with st.form(key=f"{key_prefix}_review_form"):
        reviewer = st.text_input("Advisor name", key=f"{key_prefix}_reviewer",
                                 placeholder="Your name (recorded in the audit report)")

        claim_decisions = {}
        if items:
            st.markdown(f"**Policy claims to review ({len(items)})**"
                        + (f" · {n_auto} other claim(s) verified automatically" if n_auto else ""))
        else:
            st.success(f"All {n_auto} policy claims were verified automatically — nothing to review.")

        for r in items:
            cid = r["claim_id"]
            with st.container(border=True):
                where = "Recommendation" if r.get("claim_type") == "talking_point" else f"Benefit for: {r.get('risk', '')}"
                st.markdown(f"**{cid} · {STATUS_LABEL.get(r['final_status'], r['final_status'])}** — _{where}_")
                st.write(r.get("matched_benefit", ""))
                if r.get("semantic_reason"):
                    st.caption(f"**Finding:** {r['semantic_reason']}")
                if r.get("missing_condition"):
                    st.caption(f"**Condition to add:** {r['missing_condition']}")
                src = r.get("matched_document") or r.get("source_policy", "")
                if r.get("evidence_excerpt"):
                    st.caption(f"**Source text** ({src}, p.{r.get('matched_page') or r.get('page_number')}): "
                               f"{r['evidence_excerpt']}")
                action = st.radio("Decision", ACTIONS, index=DEFAULT_ACTION.get(r["final_status"], 0),
                                  horizontal=True, key=f"{key_prefix}_{cid}_action")
                text = st.text_area("Wording to use if you choose 'Edit wording'", value=suggested_edit(r),
                                    height=80, key=f"{key_prefix}_{cid}_text")
                claim_decisions[cid] = {
                    "action": {0: "approve", 1: "edit", 2: "remove"}[ACTIONS.index(action)],
                    "text": text,
                }

        statement_decisions = {}
        if others:
            st.markdown("**Statements outside the policy documents** — untick anything that should not go to the client")
            for i, o in enumerate(others):
                unverified = "NOT verified" in o.get("status", "")
                label = f"[{o['category']}] {o['statement']}" + ("  ⚠️ not verified in its web source" if unverified else "")
                statement_decisions[i] = st.checkbox(label, value=not unverified, key=f"{key_prefix}_stmt_{i}")

        submitted = st.form_submit_button("✅ Finalise — generate deck & audit report", type="primary")

    if submitted:
        target = _message_by_key(key_prefix) or data
        with st.spinner("Applying your decisions, re-checking edits and generating the files..."):
            try:
                target["final"] = finalise_after_review(company_name, target, claim_decisions,
                                                        statement_decisions, reviewer)
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not finalise: {e}")
                return
        st.rerun()


# ============================================================
# RENDER RESULT
# ============================================================

def render_assistant_result(
    company_name: str,
    data: dict,
    key_prefix: str
):
    pitch = data["pitch"]
    audit = data["audit"]

    status_color = {
        "PASS": "green",
        "PASS WITH CONDITIONS": "green",
        "NEEDS HUMAN REVIEW": "orange",
        "REJECT — REGENERATE": "red",
        "AUDIT INCOMPLETE": "gray",
        "NO CLAIMS TO AUDIT": "gray",
    }
    color = status_color.get(audit["overall_status"], "gray")

    st.markdown(f"### Pitch for {company_name}")
    st.write(f"**Industry:** {pitch['industry']}  |  **Size:** {pitch['size_estimate']}")
    st.write(f"**Recommended policy:** {pitch['recommended_policy']['policy_name']}")

    unchecked = audit.get("unchecked_count", 0)
    st.markdown(
        f":{color}[**Automated audit: {audit['overall_status']}** — "
        f"{audit['confidence_score']}% confidence "
        f"({audit['verified_count']} verified, "
        f"{audit.get('conditional_count', 0)} verified with condition, "
        f"{audit['flagged_count']} flagged, "
        f"{audit['rejected_count']} rejected"
        + (f", {unchecked} unchecked" if unchecked else "")
        + ")]"
    )
    for note in audit.get("notes") or []:
        st.warning(note)

    final = data.get("final")
    if not final:
        render_review_panel(company_name, data, key_prefix)
        return

    fa = final["audit"]
    fcolor = {"APPROVED FOR CLIENT": "green", "APPROVED WITH ADVISOR OVERRIDES": "orange"}.get(fa.get("final_status"), "red")
    dec = [r.get("advisor_decision") for r in fa.get("claim_results", [])]
    st.markdown(
        f":{fcolor}[**After advisor review: {fa.get('final_status')}**] — reviewed by {fa.get('reviewed_by')} "
        f"on {fa.get('reviewed_at')} · {dec.count('auto-verified')} auto-verified, {dec.count('approved')} approved, "
        f"{dec.count('edited')} edited, {dec.count('removed')} removed"
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button("⬇ Pitch Deck (.pptx)", data=final["pptx_bytes"],
                           file_name=f"{_safe_name(company_name)}_Pitch.pptx", key=f"{key_prefix}_pptx")
    with col2:
        st.download_button("⬇ Audit Report (.docx)", data=final["audit_docx_bytes"],
                           file_name=f"{_safe_name(company_name)}_Audit_Report.docx", key=f"{key_prefix}_audit_docx")
    with col3:
        st.download_button("⬇ Audit Data (.json)", data=json.dumps(fa, indent=2),
                           file_name=f"{_safe_name(company_name)}_Audit.json", key=f"{key_prefix}_audit")

    with st.expander("View claim-by-claim audit with advisor decisions"):
        rows = [{
            "ID": r.get("claim_id", ""),
            "Automated result": r["final_status"],
            "Advisor decision": r.get("advisor_decision", ""),
            "Claim": r.get("advisor_text") or r["matched_benefit"],
            "Re-check (edits)": r.get("recheck_status", ""),
            "Source": f"{r['source_policy']} p.{r['page_number']}",
        } for r in fa.get("claim_results", [])]
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
        else:
            st.write("No claims were generated to audit.")


# ============================================================
# CHATBOT PAGE
# ============================================================

def render_chatbot_page():
    st.title("💼 Marsh Insurance Pitch Assistant")

    st.caption(
        "Type a company name to generate a "
        "policy-grounded marketing pitch, "
        "audited against the source policy documents."
    )

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.write(msg["content"])
            else:
                if msg.get("error"):
                    st.error(msg["error"])
                else:
                    render_assistant_result(
                        msg["company_name"],
                        msg,
                        key_prefix=msg["key"]
                    )

    user_input = st.chat_input("Enter a company name, e.g. 'TCS'")

    if user_input:
        company_name = extract_company_name(user_input)

        st.session_state.messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.write(user_input)

        with st.chat_message("assistant"):
            if not company_name:
                error_msg = "Please enter a company name — the input was empty."
                st.error(error_msg)
                st.session_state.messages.append({"role": "assistant", "error": error_msg})
            else:
                with st.status("Thinking...", expanded=True) as status:
                    try:
                        result = run_pipeline(company_name, status)
                        status.update(label="Done", state="complete", expanded=False)
                    except (CompanyProfileError, PitchGenerationError) as e:
                        status.update(label="Failed", state="error", expanded=True)
                        error_msg = f"Couldn't generate a pitch for {company_name}: {e}"
                        st.error(error_msg)
                        st.session_state.messages.append({"role": "assistant", "error": error_msg})
                        result = None
                    except Exception as e:
                        status.update(label="Failed", state="error", expanded=True)
                        error_msg = f"Unexpected error while generating the pitch for {company_name}: {e}"
                        st.error(error_msg)
                        st.session_state.messages.append({"role": "assistant", "error": error_msg})
                        result = None

                if result is not None:
                    key_prefix = f"msg{len(st.session_state.messages)}"
                    render_assistant_result(company_name, result, key_prefix=key_prefix)
                    st.session_state.messages.append({
                        "role": "assistant",
                        "company_name": company_name,
                        "key": key_prefix,
                        "error": None,
                        **result
                    })

# ============================================================
# INITIALIZE APP
# ============================================================

try:
    init_session_state()
    selection = render_sidebar()
    if selection == "Chat-bot":
        render_chatbot_page()
    elif selection == "Add Document":
        render_add_document_page()
except Exception as e:
    st.error("The application could not start.")
    st.exception(e)
    st.stop()
