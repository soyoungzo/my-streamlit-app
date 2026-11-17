import os
import uuid
import hashlib
import tempfile

import streamlit as st

st.set_page_config(page_title="AIVLE School 학습 도우미", page_icon="🎓", layout="wide")

# API 키: Colab은 노트북에서 읽은 값, Cloud는 Secrets 사용
if not os.environ.get("OPENAI_API_KEY"):
    try:
        os.environ["OPENAI_API_KEY"] = st.secrets["OPENAI_API_KEY"]
    except Exception:
        st.error("OPENAI_API_KEY가 설정되지 않았어요.")
        st.stop()

from rag import rag_pipeline

LOGO_URL = "https://raw.githubusercontent.com/Jangrae/img/master/aivle.png"
MODEL_NAME = "gpt-4o-mini"   # rag.py에서 쓰는 모델 이름(하단 안내 문구용)
SUGGESTED = [
    "비전공자도 따라갈 수 있나요?",
    "교육비와 자기부담금은 얼마인가요?",
    "미니 프로젝트는 어떻게 진행되나요?",
]

# ── 세션 상태 ───────────────────────────────────────────
# Streamlit은 입력할 때마다 파일 전체를 다시 실행하므로
# 체인과 대화 기록을 session_state에 저장해 둡니다.
ss = st.session_state
ss.setdefault("rag_chain", None)
ss.setdefault("file_hash", None)
ss.setdefault("file_name", None)
ss.setdefault("chats", {})        # {대화ID: {"title": 제목, "messages": [...]}}
ss.setdefault("current_id", None)


def new_chat():
    chat_id = uuid.uuid4().hex[:8]
    ss.chats[chat_id] = {"title": "새 대화", "messages": []}
    ss.current_id = chat_id


def delete_chat(chat_id):
    ss.chats.pop(chat_id, None)
    if ss.current_id == chat_id:
        ss.current_id = next(iter(ss.chats), None)
    if not ss.chats:
        new_chat()


if ss.current_id is None:
    new_chat()


def build_chain(uploaded_file):
    data = uploaded_file.getvalue()
    file_hash = hashlib.md5(data).hexdigest()

    # 같은 파일이면 다시 벡터화하지 않음
    if file_hash == ss.file_hash:
        return

    # 업로드 객체 → 임시 .pdf 파일로 저장 → 경로를 rag_pipeline에 전달
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        with st.spinner("백서를 읽고 벡터 저장소를 만드는 중입니다..."):
            ss.rag_chain = rag_pipeline(tmp_path)
        ss.file_hash = file_hash
        ss.file_name = uploaded_file.name
    finally:
        os.remove(tmp_path)


def ask(question):
    try:
        return ss.rag_chain.invoke({"input": question})
    except Exception as e:
        return f"오류가 발생했어요: {e}"


# ── 스타일 ──────────────────────────────────────────────
st.markdown(f"""
<style>
.block-container {{ padding-top: 4rem; padding-bottom: 1rem; }}

/* 사이드바 메뉴(새 대화 · 추천 질문): 글자만 보이고 마우스를 올리면 배경 */
[class*="st-key-nav_"] button {{
    justify-content: flex-start; padding: 6px 10px; border-radius: 8px; color: inherit;
}}
[class*="st-key-nav_"] button > div {{ justify-content: flex-start; width: 100%; }}
[class*="st-key-nav_"] button p {{
    width: 100%; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
[class*="st-key-nav_"] button:hover {{ background-color: rgba(0,0,0,0.06); color: inherit; }}
[data-testid="stSidebar"] [class*="st-key-nav_q"] {{ margin-top: -0.7rem; }}
.st-key-nav_new button {{ background-color: rgba(0,0,0,0.06); font-weight: 600; }}

/* 사이드바 하단 문서 카드 */
.doc-card {{
    margin-top: 1.5rem; padding: 12px 14px; border: 1px solid rgba(0,0,0,0.1);
    border-radius: 12px; font-size: 0.85rem; line-height: 1.4;
}}
.doc-card .label {{ color: #888; font-size: 0.75rem; }}

/* 상단 바 */
.topbar {{
    display: flex; justify-content: space-between; align-items: center;
    flex-wrap: wrap; gap: 8px;
    padding: 4px 0 12px; margin-bottom: 8px; border-bottom: 1px solid rgba(0,0,0,0.08);
}}
.topbar .title {{ font-size: 1.35rem; font-weight: 700; line-height: 1.6; }}
.topbar .badge {{
    font-size: 0.8rem; padding: 6px 12px; border-radius: 8px;
    background: #111; color: #fff;
}}
.topbar .badge.off {{ background: #eee; color: #666; }}

/* 채팅 말풍선: 질문은 테두리 박스, 답변은 회색 카드 */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {{
    background: transparent;
}}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"])
  [data-testid="stChatMessageContent"] {{
    flex: 0 1 auto; margin-left: 0 !important; margin-right: auto !important;
    border: 1px solid rgba(0,0,0,0.12); border-radius: 12px; padding: 6px 14px;
}}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {{
    background: #f6f7f9; border-radius: 14px; padding: 14px 16px;
}}

/* 오른쪽 히스토리 카드 */
[class*="st-key-card_"] {{
    border: 1px solid rgba(0,0,0,0.08); border-radius: 12px; padding: 10px 12px 12px; gap: 0.2rem;
}}
[class*="st-key-card_"]:hover {{ background: rgba(0,0,0,0.03); }}
.st-key-card_{ss.current_id} {{ background: #f3f4f6; border-color: rgba(0,0,0,0.15); }}
[class*="st-key-open_"] button {{
    justify-content: flex-start; padding: 0; min-height: 0; color: inherit;
}}
[class*="st-key-open_"] button > div {{ justify-content: flex-start; width: 100%; }}
[class*="st-key-open_"] button p {{
    width: 100%; text-align: left; font-weight: 600; font-size: 0.88rem;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
[class*="st-key-open_"] button:hover {{ color: inherit; }}
[class*="st-key-card_"] [data-testid="stCaptionContainer"] {{ padding-bottom: 4px; }}
[class*="st-key-card_"] [data-testid="stCaptionContainer"] p {{
    font-size: 0.78rem; line-height: 1.5; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 0;
}}
[class*="st-key-del_"] button {{ color: #aaa; padding: 0; min-height: 0; }}
[class*="st-key-del_"] button:hover {{ color: #e5484d; }}

/* 하단 안내 문구 */
.disclaimer {{ text-align: center; font-size: 0.75rem; color: #999; margin-top: 4px; }}
</style>
""", unsafe_allow_html=True)


# ── 왼쪽 사이드바: 로고 · 검색 · 메뉴 · 문서 상태 ────────
with st.sidebar:
    st.image(LOGO_URL, width=150)
    keyword = st.text_input("검색", placeholder="🔍 대화 검색", label_visibility="collapsed")

    if st.button("💬  새 대화", key="nav_new", width="stretch", type="tertiary"):
        new_chat()
        st.rerun()

    st.caption("추천 질문")
    for i, q in enumerate(SUGGESTED):
        if st.button(f"💡  {q}", key=f"nav_q{i}", width="stretch", type="tertiary",
                     disabled=ss.rag_chain is None):
            ss.pending_question = q

    doc = ss.file_name or "아직 문서가 없어요"
    st.markdown(
        f'<div class="doc-card"><div class="label">적용된 문서</div>📄 {doc}</div>',
        unsafe_allow_html=True,
    )

current = ss.chats[ss.current_id]

# ── 상단 바 ─────────────────────────────────────────────
badge = '<span class="badge">✔ 백서 적용됨</span>' if ss.rag_chain else '<span class="badge off">문서 미적용</span>'
st.markdown(
    f'<div class="topbar"><span class="title">AIVLE School 학습 도우미</span>{badge}</div>',
    unsafe_allow_html=True,
)

# ── 본문: 채팅(가운데) · 히스토리(오른쪽) ───────────────
chat_col, hist_col = st.columns([3, 1], gap="large")

# 오른쪽: 히스토리 카드
with hist_col:
    st.markdown(f"**히스토리** <span style='color:#999'>({len(ss.chats)})</span>",
                unsafe_allow_html=True)
    for chat_id, chat in reversed(list(ss.chats.items())):
        text = chat["title"] + " ".join(m["content"] for m in chat["messages"])
        if keyword and keyword not in text:
            continue

        answers = [m["content"] for m in chat["messages"] if m["role"] == "assistant"]
        preview = answers[-1] if answers else "아직 대화가 없어요"
        preview = " ".join(preview.split())   # 줄바꿈 없이 한 줄로

        with st.container(key=f"card_{chat_id}"):
            c1, c2 = st.columns([6, 1], gap="small", vertical_alignment="center")
            if c1.button(chat["title"], key=f"open_{chat_id}", width="stretch", type="tertiary"):
                ss.current_id = chat_id
                st.rerun()
            if c2.button("✕", key=f"del_{chat_id}", help="대화 삭제", type="tertiary"):
                delete_chat(chat_id)
                st.rerun()
            st.caption(preview)

# 가운데: 대화 영역 + 다시 생성 + 입력창 + 첨부
with chat_col:
    chat_box = st.container(height=470, border=False)

    # 다시 생성: 마지막 질문으로 답변을 새로 받기
    regenerate = False
    if current["messages"] and ss.rag_chain is not None:
        _, mid, _ = st.columns([2, 1, 2])
        regenerate = mid.button("↻ 다시 생성", width="stretch")

    with st.container(border=True):
        user_input = st.chat_input("메시지를 입력하세요", max_chars=3000,
                                   disabled=ss.rag_chain is None)
        a1, a2 = st.columns([1, 3], vertical_alignment="center")
        with a1:
            with st.popover("📎 파일 업로드", width="stretch"):
                uploaded = st.file_uploader("AIVLE School 백서(PDF)", type="pdf")
                if uploaded is not None:
                    build_chain(uploaded)
                    st.success("적용 완료")
        a2.caption("백서 PDF를 올리면 그 내용을 바탕으로 답해요.")

    st.markdown(
        f'<div class="disclaimer">답변은 업로드한 백서를 바탕으로 하며 부정확할 수 있어요. '
        f'Model: {MODEL_NAME}</div>',
        unsafe_allow_html=True,
    )

    question = user_input or ss.pop("pending_question", None)

    with chat_box:
        for idx, msg in enumerate(current["messages"]):
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg["role"] == "assistant":
                    st.feedback("thumbs", key=f"fb_{ss.current_id}_{idx}")

        if regenerate:
            last_q = next(m["content"] for m in reversed(current["messages"]) if m["role"] == "user")
            with st.spinner("답변을 다시 만드는 중..."):
                new_answer = ask(last_q)
            if current["messages"][-1]["role"] == "assistant":
                current["messages"][-1]["content"] = new_answer
            st.rerun()

        if question:
            # 첫 질문을 히스토리 제목으로 사용
            if not current["messages"]:
                current["title"] = question[:20]

            current["messages"].append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)

            with st.chat_message("assistant"):
                with st.spinner("답변을 찾는 중..."):
                    answer = ask(question)
                st.markdown(answer)

            current["messages"].append({"role": "assistant", "content": answer})
            st.rerun()   # 히스토리 제목·미리보기를 바로 갱신
