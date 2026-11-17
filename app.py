# Streamlit Cloud의 sqlite 버전 문제 대비 (chromadb보다 먼저 실행돼야 함, Colab에서는 영향 없음)
try:
    __import__("pysqlite3"); import sys; sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
except ImportError:
    pass

import os
import uuid
import hashlib
import html        # ★ [답변 평가] 질문 글자를 화면에 안전하게 넣기 위해
import shutil      # [2차 수정 1] chroma_db 폴더를 지울 때 사용
import tempfile
import warnings
from operator import itemgetter
from datetime import datetime          # ★ [답변 평가] 평가한 시각 기록용
from zoneinfo import ZoneInfo          # ★ [답변 평가] 한국 시간으로 기록하기 위해

import pandas as pd                    # ★ [답변 평가] 평가 결과를 표(CSV)로 만들기 위해
import streamlit as st

warnings.filterwarnings(action="ignore")

import pymupdf
pymupdf.set_messages(path=os.devnull)

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_pymupdf4llm import PyMuPDF4LLMLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma

st.set_page_config(page_title="AIVLE School 학습 도우미", page_icon="🎓", layout="wide")

# API 키: Colab은 노트북에서 읽은 값, Cloud는 Secrets 사용
if not os.environ.get("OPENAI_API_KEY"):
    try:
        os.environ["OPENAI_API_KEY"] = st.secrets["OPENAI_API_KEY"]
    except Exception:
        st.error("OPENAI_API_KEY가 설정되지 않았어요.")
        st.stop()


# ════════════════════════════════════════════════════════
# [RAG 영역] 답변을 만드는 부분 (구 rag.py)
# ════════════════════════════════════════════════════════

# ① 설정값 (조별 표준안이 정해지면 여기만 바꾸세요)
CHUNK_SIZE      = 600        # 한 조각의 최대 글자 수
CHUNK_OVERLAP   = 100        # 앞뒤 조각이 겹치는 글자 수 (CHUNK_SIZE보다 작아야 함)
TOP_K           = 5          # 답변에 넘길 문단 수
FETCH_K         = 20         # MMR이 먼저 뽑는 후보 문단 수
EMBEDDING_MODEL = "text-embedding-3-small"
LLM_MODEL       = "gpt-4o-mini"
TEMPERATURE     = 0          # 0에 가까울수록 문서에 충실한 답변
NOT_FOUND_MSG   = "백서에서 해당 내용을 찾을 수 없습니다."

# ④ 프롬프트 템플릿
SYSTEM_TEMPLATE = """
[역할]
당신은 KT 에이블스쿨 지원자와 교육생을 돕는 학습 도우미입니다.
아래 [문서]는 에이블스쿨 백서에서 질문과 관련된 부분만 뽑은 것입니다.

[답변 규칙]
1. 반드시 [문서]에 있는 내용만 근거로 답하세요. 일반 상식이나 추측을 더하지 마세요.
2. 숫자, 날짜, 금액, 비율, 기수는 [문서]에 적힌 그대로 옮기세요.
3. 기수나 시점에 따라 내용이 다르면 "10기 기준", "2024년 기준"처럼 기준을 함께 밝히세요.
4. [문서]에 일부만 있으면 확인되는 부분만 답하고, 나머지는 백서에 없다고 말하세요.
5. [문서]에 전혀 없으면 다른 말을 덧붙이지 말고 "{not_found}"라고만 답하세요.
6. 질문이 모호하면 가장 가능성 높은 뜻으로 답하고, 어떻게 이해했는지 한 줄로 밝히세요.

[답변 형식]
- 첫 문장에서 질문에 대한 핵심 답을 바로 말하세요.
- 항목이 3개 이상이면 글머리표로 정리하세요.
- 답변 끝에 근거가 된 쪽 번호를 (근거: p.3, p.5)처럼 적으세요.
- 친절하고 격려하는 말투로, 존댓말을 쓰세요.

[문서]
{context}
""".strip()

prompt = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_TEMPLATE),
    ("human", "{input}"),
]).partial(not_found=NOT_FOUND_MSG)


# 문서 포맷터: 각 문단 앞에 쪽 번호를 붙여 근거를 표시할 수 있게 함
def format_docs(docs):
    blocks = []
    for d in docs:
        page = d.metadata.get("page")
        label = f"[p.{page + 1}]" if isinstance(page, int) else "[p.?]"
        blocks.append(f"{label}\n{d.page_content}")
    return "\n\n---\n\n".join(blocks)


# RAG 파이프라인 함수 (01번 노트북의 rag_pipeline 그대로)
def rag_pipeline(filepath):

    # PDF 파일 읽기 (PyMuPDF4LLM은 제목을 마크다운 #으로 바꿔 줌)
    loader = PyMuPDF4LLMLoader(filepath)
    docs = loader.load()

    # ② 텍스트 분할: 장·절 제목 → 문단 → 줄 → 문장 순서로 자르기
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n## ", "\n### ", "\n#### ", "\n\n", "\n", ". ", " ", ""],
    )
    splits = text_splitter.split_documents(docs)

    # Embedding 모델
    embeddings = OpenAIEmbeddings(model=EMBEDDING_MODEL)

    # 벡터저장소 만들기
    vectorstore = Chroma(
        collection_name="aivle_docs",
        embedding_function=embeddings,
        persist_directory="chroma_db",
    )

    # 기존 데이터 비우기
    vectorstore.reset_collection()

    # 문서 추가
    vectorstore.add_documents(splits)

    # ③ 검색기 구성: MMR로 관련 있으면서 서로 겹치지 않는 문단 선택
    retriever = vectorstore.as_retriever(
        search_type="mmr",
        search_kwargs={"k": TOP_K, "fetch_k": FETCH_K},
    )

    # LLM 선언
    llm = ChatOpenAI(model=LLM_MODEL, temperature=TEMPERATURE)

    # RAG 체인 구성
    rag_chain = (
        {
            "context": itemgetter("input") | retriever | format_docs,
            "input": itemgetter("input"),
        }
        | prompt
        | llm
        | StrOutputParser()
    )

    return rag_chain


# ════════════════════════════════════════════════════════
# [화면 영역] 사용자가 보는 부분 (구 app.py)
# ════════════════════════════════════════════════════════

LOGO_URL = "https://raw.githubusercontent.com/Jangrae/img/master/aivle.png"
MODEL_NAME = LLM_MODEL       # 하단 안내 문구용 (위 설정값을 그대로 사용)
ADMIN_PASSWORD = "0207"      # [수정 1] 관리자 모드 비밀번호 (바꾸려면 여기만 수정)
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
ss.setdefault("is_admin", False)  # [수정 2] 관리자 인증 여부 (비밀번호를 맞히면 True)
ss.setdefault("feedback", {})     # ★ [답변 평가] {"대화ID_번호": {평가 한 줄}} — CSV로 저장될 내용


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


# [2차 수정 2] chroma_db 폴더를 통째로 지우는 함수 (새로 추가)
# 폴더 안의 DB 파일이 깨졌거나 다른 버전의 chromadb로 만들어져 오류가 날 때 사용
def reset_chroma_dir():
    try:
        # chromadb가 기억하고 있는 이전 연결을 끊어야 폴더를 지운 뒤 다시 쓸 수 있음
        from chromadb.api.shared_system_client import SharedSystemClient
        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    shutil.rmtree("chroma_db", ignore_errors=True)


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
            # [2차 수정 3] rag_pipeline은 그대로 두고(chroma_db + reset_collection 사용),
            # 오류가 나면 chroma_db 폴더를 지운 뒤 한 번 더 실행
            try:
                ss.rag_chain = rag_pipeline(tmp_path)
            except Exception:
                reset_chroma_dir()
                ss.rag_chain = rag_pipeline(tmp_path)
        ss.file_hash = file_hash
        ss.file_name = uploaded_file.name
    finally:
        os.remove(tmp_path)


# ── [수정 3] 관리자 모드 팝업 (새로 추가) ────────────────
# 1단계: 비밀번호 입력 → 2단계: 맞으면 같은 팝업이 문서 업로드 화면으로 바뀜
@st.dialog("관리자 모드")
def admin_dialog():
    # 1단계: 아직 인증 전이면 비밀번호 입력 폼을 보여 줌
    if not ss.is_admin:
        slot = st.empty()   # 인증에 성공하면 이 자리(입력 폼)를 지움
        with slot.form("admin_login", border=False):
            pw = st.text_input("비밀번호를 입력하세요", type="password")
            submitted = st.form_submit_button("확인", width="stretch")
        if submitted:
            if pw == ADMIN_PASSWORD:
                ss.is_admin = True
                slot.empty()
            else:
                st.error("비밀번호가 올바르지 않아요.")

    # 2단계: 인증된 관리자에게만 업로드 화면을 보여 줌 (기존 📎 파일 업로드를 여기로 옮김)
    if ss.is_admin:
        st.caption("백서 PDF를 올리면 그 내용을 바탕으로 답해요.")
        uploaded = st.file_uploader("AIVLE School 백서(PDF)", type="pdf")

        # [2차 수정 4] 확인 버튼 추가: 파일을 고른 뒤 버튼을 눌러야 업로드가 진행됨
        #  (파일을 고르기 전에는 버튼이 비활성화)
        if st.button("확인", type="primary", width="stretch", disabled=uploaded is None):
            # [2차 수정 5] 그래도 오류가 나면 앱이 멈추지 않고 팝업 안에 원인을 표시
            try:
                build_chain(uploaded)
            except Exception as e:
                st.error(f"문서를 적용하지 못했어요: {type(e).__name__}: {e}")
            else:
                st.rerun()   # 팝업을 닫고 화면 전체를 새로 그림 (배지·입력창 활성화)

        # ★ [답변 평가] 관리자만 보는 만족도 + CSV 다운로드
        st.divider()
        df = feedback_df()
        total = len(df)                              # 평가한 답변 수
        good = int((df["점수"] == 1).sum())          # 👍 받은 답변 수
        rate = round(good / total * 100) if total > 0 else 0   # 만족도(%)

        st.markdown(f"**답변 만족도**  {rate}% ({good}/{total})")
        st.download_button(
            "⬇️  평가 결과 CSV 받기",
            data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name="feedback.csv",
            mime="text/csv",
            width="stretch",
            disabled=total == 0,                     # 평가가 하나도 없으면 비활성화
        )


def ask(question):
    try:
        return ss.rag_chain.invoke({"input": question})
    except Exception as e:
        return f"오류가 발생했어요: {e}"


# ════════════════════════════════════════════════════════
# ★ [답변 평가] 👍/👎 평가를 저장하고 CSV로 만드는 부분
# ════════════════════════════════════════════════════════

FEEDBACK_FILE = "feedback.csv"                                   # 저장할 파일 이름
FEEDBACK_COLUMNS = ["평가시각", "대화제목", "질문", "답변", "평가", "점수"]   # CSV 열 순서


def get_feedback():
    """평가 장부를 꺼내 줍니다. 장부가 없으면(앱 재시작·재배포 직후 등) 빈 장부를 새로 만들어 줍니다.

    ss.feedback 처럼 바로 꺼내면 장부가 없을 때 AttributeError가 나기 때문에,
    평가 장부는 항상 이 함수로만 꺼내 씁니다.
    """
    return ss.setdefault("feedback", {})


def feedback_df():
    """지금까지 받은 평가를 표(DataFrame)로 만들어 돌려줍니다."""
    rows = list(get_feedback().values())
    return pd.DataFrame(rows, columns=FEEDBACK_COLUMNS)


def save_feedback_csv():
    """평가 표를 feedback.csv 파일로 저장합니다. (엑셀에서 한글이 안 깨지도록 utf-8-sig)"""
    feedback_df().to_csv(FEEDBACK_FILE, index=False, encoding="utf-8-sig")


def msg_id(m):
    """메시지의 고유 ID를 돌려줍니다. 없으면 새로 만들어 붙입니다.

    질문을 삭제하면 메시지 순서(0, 1, 2...)가 당겨지기 때문에,
    순서 번호 대신 절대 바뀌지 않는 ID로 메시지를 구분합니다.
    """
    return m.setdefault("id", uuid.uuid4().hex[:8])


def find_index(messages, target_id):
    """메시지 목록에서 ID가 target_id인 메시지가 몇 번째인지 찾습니다. 없으면 None."""
    for i, m in enumerate(messages):
        if m.get("id") == target_id:
            return i
    return None


def save_rating(chat_id, answer_id, value):
    """👍/👎 버튼의 값이 바뀌었을 때 평가를 저장하는 함수입니다.

    chat_id   : 어떤 대화인지
    answer_id : 평가한 답변의 고유 ID
    value     : 버튼 값 (1 = 👍, 0 = 👎, None = 평가 취소)
    """

    chat = ss.chats.get(chat_id)
    if chat is None:
        return
    messages = chat["messages"]
    idx = find_index(messages, answer_id)
    if idx is None:
        return
    messages[idx]["rating"] = value    # 히스토리에 표시하려고 답변 메시지 안에도 적어 둠

    # 답변 ID가 장부의 이름표 → 평가를 바꾸면 덮어쓰기(중복 X)
    if value is None:
        get_feedback().pop(answer_id, None)
    else:
        get_feedback()[answer_id] = {
            "평가시각": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S"),
            "대화제목": chat["title"],
            "질문": messages[idx - 1]["content"],   # 답변 바로 앞 메시지가 질문
            "답변": messages[idx]["content"],
            "평가": "만족" if value == 1 else "불만족",
            "점수": value,                            # 만족도 계산용 숫자 (1 또는 0)
        }

    save_feedback_csv()


def rating_tag(rating):
    """평가 값(1, 0, None)을 히스토리용 태그로 바꿔 줍니다. 평가 안 했으면 빈 문자열."""
    if rating == 1:
        return '<span class="rate-tag good">만족</span>'
    if rating == 0:
        return '<span class="rate-tag bad">불만족</span>'
    return ""


def history_items():
    """모든 대화의 질문을 하나씩 꺼내 '히스토리 목록'을 만듭니다. (최신 질문이 맨 위)

    목록 한 줄 = 질문 1개이고, 그 질문 바로 다음 답변의 평가를 함께 담습니다.
    """
    items = []
    for chat_id, chat in ss.chats.items():
        messages = chat["messages"]
        for i, m in enumerate(messages):
            if m["role"] != "user":
                continue
            has_answer = i + 1 < len(messages) and messages[i + 1]["role"] == "assistant"
            answer = messages[i + 1] if has_answer else None
            items.append({
                "chat_id": chat_id,
                "question": m,
                "answer": answer,
                "rating": answer.get("rating") if answer else None,
            })
    # seq = 질문한 순서 번호 → 큰 번호(최신)가 위로
    items.sort(key=lambda it: it["question"].get("seq", 0), reverse=True)
    return items


def delete_question(chat_id, question_id):
    """히스토리 목록의 ✕ 버튼: 질문 1개와 그 답변을 대화에서 지웁니다.

    평가 장부(CSV)의 기록은 결과 보고서용이므로 지우지 않고 남겨 둡니다.
    """
    chat = ss.chats.get(chat_id)
    if chat is None:
        return
    messages = chat["messages"]
    i = find_index(messages, question_id)
    if i is None:
        return

    # 지울 범위: 질문 + (있으면) 바로 다음 답변
    end = i + 2 if i + 1 < len(messages) and messages[i + 1]["role"] == "assistant" else i + 1
    for m in messages[i:end]:
        ss.pop(f"fb_{m.get('id')}", None)        # 지운 답변의 버튼 상태도 정리
    del messages[i:end]

    if ss.get("selected_q") == question_id:
        ss.selected_q = None

    remaining = [m for m in messages if m["role"] == "user"]
    if remaining:
        chat["title"] = remaining[0]["content"][:20]   # 첫 질문이 지워졌으면 제목도 다음 질문으로
    elif chat_id == ss.current_id:
        chat["title"] = "새 대화"                       # 보고 있던 대화는 빈 대화로 남김
    else:
        delete_chat(chat_id)                            # 질문이 하나도 안 남은 다른 대화는 정리


# ★ [답변 평가] 히스토리 목록에서 강조할 질문 (지금 보고 있는 대화 안에 있을 때만)
selected = ss.get("selected_q")
active_q = selected if find_index(ss.chats[ss.current_id]["messages"], selected) is not None else "none"

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

/* [수정 4] 관리자 모드 버튼: 사이드바 맨 아래에 고정 */
[data-testid="stSidebar"] .st-key-nav_admin {{ position: fixed; bottom: 1.2rem; }}

/* 사이드바 하단 문서 카드 */
.doc-card {{
    margin-top: 1.5rem; padding: 12px 14px; border: 1px solid rgba(0,0,0,0.1);
    border-radius: 12px; font-size: 0.85rem; line-height: 1.4;
}}
.doc-card .label {{ color: #888; font-size: 0.75rem; }}

/* ★ [답변 평가] 히스토리 카드의 만족/불만족 태그 */
.rate-tag {{
    display: inline-block; font-size: 0.72rem; font-weight: 600;
    padding: 1px 8px; border-radius: 999px; line-height: 1.6;
}}
.rate-tag.good {{ background: #e6f4ea; color: #1e7b34; }}
.rate-tag.bad  {{ background: #fdecea; color: #b42318; }}

/* ★ [답변 평가] 히스토리 목록: 한 줄 = 질문 1개 (질문 · 태그 · ✕) */
[class*="st-key-hrow_"] {{
    padding: 0 6px 0 12px; border-radius: 10px; gap: 6px; flex-wrap: nowrap;
}}
[class*="st-key-hrow_"]:hover {{ background: rgba(0,0,0,0.04); }}
.st-key-hrow_{active_q} {{ background: #f1f2f4; }}
.st-key-hrow_{active_q} [class*="st-key-hopen_"] button p {{ font-weight: 600; }}
[class*="st-key-hopen_"] {{ flex: 1 1 0; min-width: 0; }}
[class*="st-key-hopen_"] button {{
    justify-content: flex-start; padding: 8px 0; min-height: 0; color: inherit; width: 100%;
}}
[class*="st-key-hopen_"] button > div {{ justify-content: flex-start; width: 100%; min-width: 0; }}
[class*="st-key-hopen_"] button p {{
    width: 100%; text-align: left; font-size: 0.88rem;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
[class*="st-key-hopen_"] button:hover {{ color: inherit; }}
[class*="st-key-hdel_"] button {{ color: #aaa; padding: 0 2px; min-height: 0; }}
[class*="st-key-hdel_"] button:hover {{ color: #e5484d; }}

/* ★ [답변 평가] 채팅 답변 아래 👍/👎 버튼: 테두리 버튼으로 만들고, 눌린 버튼은 색을 꽉 채움 */
[data-testid="stFeedback"] button {{
    border: 1px solid rgba(0,0,0,0.15); border-radius: 8px;
    padding: 4px 10px; background: #fff; color: #666;
}}
[data-testid="stFeedback"] button:hover {{ border-color: rgba(0,0,0,0.35); color: #333; }}
[data-testid="stFeedback"] button[aria-label="Thumbs up"][aria-checked="true"] {{
    background: #1e7b34 !important; border-color: #1e7b34 !important; color: #fff !important;
}}
[data-testid="stFeedback"] button[aria-label="Thumbs down"][aria-checked="true"] {{
    background: #b42318 !important; border-color: #b42318 !important; color: #fff !important;
}}
/* 눌린 버튼의 아이콘은 속이 꽉 찬 모양으로 */
[data-testid="stFeedback"] button[aria-checked="true"] [data-testid="stIconMaterial"] {{
    font-variation-settings: "FILL" 1; color: #fff !important;
}}
[data-testid="stFeedback"] [role="radiogroup"] {{ gap: 6px; }}

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

    # [수정 5] 관리자 모드 버튼 (누르면 비밀번호 팝업이 열림)
    if st.button("관리자 모드  →", key="nav_admin", type="tertiary"):
        admin_dialog()

current = ss.chats[ss.current_id]

# ── 상단 바 ─────────────────────────────────────────────
badge = '<span class="badge">✔ 백서 적용됨</span>' if ss.rag_chain else '<span class="badge off">문서 미적용</span>'
st.markdown(
    f'<div class="topbar"><span class="title">AIVLE School 학습 도우미</span>{badge}</div>',
    unsafe_allow_html=True,
)

# ── 본문: 채팅(가운데) · 히스토리(오른쪽) ───────────────
chat_col, hist_col = st.columns([3, 1], gap="large")

# 오른쪽: 히스토리 목록 (★ [답변 평가] 대화 카드 대신 질문 1개 = 1줄)
with hist_col:
    items = history_items()
    if keyword:   # 대화 검색: 질문이나 답변에 검색어가 있는 줄만
        items = [it for it in items
                 if keyword in it["question"]["content"]
                 or (it["answer"] and keyword in it["answer"]["content"])]

    st.markdown(f"**히스토리** <span style='color:#999'>({len(items)})</span>",
                unsafe_allow_html=True)
    if not items:
        st.caption("아직 질문이 없어요")

    for it in items:
        q = it["question"]
        q_id = msg_id(q)
        q_text = " ".join(q["content"].split())    # 줄바꿈 없이 한 줄로

        # 한 줄 안에 [질문 ......] [태그] [✕] 를 가로로 배치
        with st.container(key=f"hrow_{q_id}", horizontal=True, vertical_alignment="center"):
            if st.button(q_text, key=f"hopen_{q_id}", type="tertiary", width="stretch"):
                ss.current_id = it["chat_id"]      # 이 질문이 있는 대화를 열고
                ss.selected_q = q_id               # 목록에서 이 줄을 강조
                st.rerun()
            tag = rating_tag(it["rating"])
            if tag:
                st.markdown(tag, unsafe_allow_html=True, width="content")
            if st.button("✕", key=f"hdel_{q_id}", help="질문 삭제", type="tertiary"):
                delete_question(it["chat_id"], q_id)
                st.rerun()

# 가운데: 대화 영역 + 다시 생성 + 입력창
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
        # [수정 6] 여기 있던 "📎 파일 업로드" 버튼을 삭제 (업로드는 관리자 모드 팝업에서만 가능)
        st.caption("관리자가 적용한 백서 내용을 바탕으로 답해요.")

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
                    # ★ [답변 평가] 답변마다 👍/👎 버튼 (답변 고유 ID로 구분)
                    a_id = msg_id(msg)
                    fb_key = f"fb_{a_id}"
                    # 다른 대화를 보다가 돌아오면 버튼 상태가 지워지므로, 저장해 둔 평가로 되살림
                    if fb_key not in ss and msg.get("rating") is not None:
                        ss[fb_key] = msg["rating"]
                    # 버튼이 돌려주는 값(1, 0, None)을 바로 받아서 사용
                    new_value = st.feedback("thumbs", key=fb_key)
                    # 저장된 평가와 다르면 = 방금 버튼을 눌렀다는 뜻 → 저장하고 화면 새로 그리기
                    if new_value != msg.get("rating"):
                        save_rating(ss.current_id, a_id, new_value)
                        st.rerun()   # 히스토리 태그를 바로 갱신

        if regenerate:
            last_q = next(m["content"] for m in reversed(current["messages"]) if m["role"] == "user")
            with st.spinner("답변을 다시 만드는 중..."):
                new_answer = ask(last_q)
            if current["messages"][-1]["role"] == "assistant":
                current["messages"][-1]["content"] = new_answer
                # ★ [답변 평가] 답변이 바뀌었으니 예전 평가는 지움
                last_id = msg_id(current["messages"][-1])
                current["messages"][-1]["rating"] = None
                ss.pop(f"fb_{last_id}", None)
                get_feedback().pop(last_id, None)
                save_feedback_csv()
            st.rerun()

        if question:
            # 첫 질문을 히스토리 제목으로 사용
            if not current["messages"]:
                current["title"] = question[:20]

            # ★ [답변 평가] 질문마다 고유 ID와 질문 순서(seq)를 붙임 → 히스토리 정렬·강조에 사용
            ss.q_seq = ss.get("q_seq", 0) + 1
            q_msg = {"role": "user", "content": question, "seq": ss.q_seq}
            current["messages"].append(q_msg)
            ss.selected_q = msg_id(q_msg)
            with st.chat_message("user"):
                st.markdown(question)

            with st.chat_message("assistant"):
                with st.spinner("답변을 찾는 중..."):
                    answer = ask(question)
                st.markdown(answer)

            # ★ [답변 평가] "rating": None → 아직 평가 안 함
            current["messages"].append({"role": "assistant", "content": answer, "rating": None})
            st.rerun()   # 히스토리 제목·미리보기를 바로 갱신
