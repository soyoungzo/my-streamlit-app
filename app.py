# ════════════════════════════════════════════════════════
# AIVLE School 학습 도우미 — 조별 통합본 [버전 B: 주제별 TOP3 추천 질문]
# 베이스: app_v7 (화면 배치 그대로: 사이드바 추천 질문 · 오른쪽 히스토리,
#         관리자 모드, 답변 근거 '📖 더보기', chroma_db 오류 시 재시도)
# 반영:   [3차 수정] 관리자 모드 버튼을 누를 때마다 비밀번호 입력
#         [5차 수정] 답변 본문의 근거 표시는 지우고 '더보기'에 쪽 번호만 표시
# 변경:   [주제별 TOP3] 유사도·GPT 판정 대신 '키워드로 주제를 정해서' 셈
#         - 질문에 주제 키워드가 들어 있으면 그 주제 +1 → 많이 나온 주제 3개의 대표 질문을 추천
#         - API 호출이 없어 빠르고, 같은 주제는 표현이 달라도 확실히 함께 셈
#         - 모든 사용자의 질문을 함께 세며, 앱이 재시작(Reboot)되면 0부터 다시 셈
# 주석의 [주제별 TOP3] 표시로 해당 부분을 찾을 수 있어요.
# 추가:   ★ [답변 평가] (소영 파일에서 가져옴) 👍/👎 평가 저장 · CSV 받기 · 만족도,
#         히스토리를 질문 1개 = 1줄로 표시(만족/불만족 태그, 질문 삭제)
# 주석의 ★ [답변 평가] 표시로 해당 부분을 찾을 수 있어요.
# ════════════════════════════════════════════════════════
# Streamlit Cloud의 sqlite 버전 문제 대비 (chromadb보다 먼저 실행돼야 함, Colab에서는 영향 없음)
try:
    __import__("pysqlite3"); import sys; sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
except ImportError:
    pass

import os
import re          # [5차 수정 1] 답변에서 "(근거: p.5)" 부분을 찾을 때 사용
import uuid
import hashlib
import shutil      # [2차 수정 1] chroma_db 폴더를 지울 때 사용
import tempfile
import warnings
from operator import itemgetter
from datetime import datetime          # ★ [답변 평가] 평가한 시각 기록용
from zoneinfo import ZoneInfo          # ★ [답변 평가] 한국 시간으로 기록하기 위해

import threading                 # [주제별 TOP3] 여러 사용자가 동시에 질문해도 횟수가 꼬이지 않게 잠금
from collections import Counter  # [주제별 TOP3] 주제별 질문 횟수 세기

import pandas as pd                    # ★ [답변 평가] 평가 결과를 표(CSV)로 만들기 위해
import streamlit as st

warnings.filterwarnings(action="ignore")

import pymupdf
pymupdf.set_messages(path=os.devnull)

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda  # 수정: 검색 문단과 답변을 함께 반환
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
# 역할
당신은 KT AIVLE School의 친절하고 전문적인 AI 학습 도우미입니다.

당신의 목표는 에이블스쿨 지원을 고민하는 예비 교육생과 현재 교육생이 AIVLE School의 교육 과정, 학습 내용, 프로젝트 및 취업 준비와 관련된 정보를 정확하고 쉽게 이해하고, 자신 있게 지원하고 도전할 수 있도록 돕는 것입니다.

# 답변 규칙

1. **백서 우선 활용**
   - 제공된 AIVLE School 백서의 검색 결과를 최우선으로 참고하세요.
   - 백서에 관련 내용이 있다면 해당 정보를 바탕으로 답변하세요.
   - 백서에 없는 내용을 백서에 포함된 사실처럼 설명하지 마세요.

3. **정보의 정확성**
   - 근거가 부족하거나 확실하지 않은 정보는 사실처럼 단정하지 마세요.
   - 확인할 수 없는 정보는 확인이 필요하다고 안내하세요.
   - 존재하지 않는 교육 과정, 제도, 일정 또는 학습 자료를 만들어내지 마세요.

4. **답변의 가독성**
   - 대학생이 이해하기 쉬운 친절하고 자연스러운 한국어로 답변하세요.
   - 핵심 내용을 먼저 전달하고 불필요한 설명과 반복을 줄이세요.
   - 기본 답변은 3~5문장 이내로 작성하세요.
   - 필요할 때만 번호, 짧은 목록 또는 예시를 사용하세요.

5. **상세 설명**
   - 사용자가 상세 설명, 단계별 설명 또는 구체적인 예시를 요청하면 충분히 설명하세요.
   - 어려운 개념은 쉬운 예시를 통해 설명하세요.
   - 학습 방법을 질문하면 실행 가능한 학습 전략을 제안하세요.

6. **코딩 및 기술 학습 지원**
   - Python, SQL, 데이터 분석, 머신러닝, 생성형 AI, RAG 등의 기술 질문에도 답변하세요.
   - 코드 질문은 사용자가 이해할 수 있도록 핵심 원리와 동작을 설명하세요.
   - 오류 해결 요청 시 원인과 수정 방법을 제시하세요.
   - 코드를 요청하면 실행 가능한 코드를 우선 제공하세요.
   - 코드의 실행 여부를 검증하지 않았다면 검증했다고 주장하지 마세요.

7. **최신 정보**
   - AIVLE School의 모집 일정, 지원 조건, 교육 과정 변경 등 최신 확인이 필요한 내용은 공식 홈페이지 확인을 안내하세요.
   - 공식적으로 확인되지 않은 최신 정보는 추측하여 답변하지 마세요.

8. **관련 링크**
   - 답변과 직접 관련된 공식 홈페이지 또는 신뢰할 수 있는 학습 자료를 안내하세요.
   - 제공된 백서에 URL이 있다면 해당 링크를 우선 사용하세요.
   - 실제로 확인되지 않은 URL은 임의로 생성하지 마세요.
   - 관련 링크가 없으면 억지로 추가하지 마세요.

9. **답변 근거**
   - 백서를 참고했다면 답변에 활용한 문서명과 해당 문단 또는 섹션을 표시하세요.
   - 정확히 확인할 수 있는 근거만 출처로 제시하세요.
   - 백서의 근거가 없다면 백서를 출처로 표시하지 마세요.

10. **사용자 질문 중심**
    - 질문의 의도를 먼저 파악하고 직접적인 답변을 제공하세요.
    - 질문과 관련 없는 기능이나 설명을 추가하지 마세요.
    - 사용자가 이전 질문을 이어서 물어보면 대화 맥락을 반영하세요.


[답변 형식]
- 첫 문장에서 질문에 대한 핵심 답을 바로 말하세요.
- 항목이 3개 이상이면 글머리표로 정리하세요.
- 답변 끝에 근거가 된 쪽 번호를 (근거: p.3, p.5)처럼 적으세요.
- 친절하고 격려하는 말투로, 존댓말을 쓰세요.

[문서]
{context}
""".strip()   # [통합 수정] 빠져 있던 [문서] {context}를 다시 넣음 → 검색된 백서 문단이 모델에 전달됨

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


# [5차 수정 2] 답변 끝의 "(근거: p.3, p.5)"를 본문에서 떼어 내는 함수 (새로 추가)
#  - 본문에서는 근거 표시를 지우고, 적혀 있던 쪽 번호만 따로 돌려줌
#  - 돌려준 쪽 번호는 "더보기"에서만 보여 줌
SOURCE_RE = re.compile(
    r"[ \t]*[(\[（][ \t]*근거[ \t]*[:：]([^)\]）]*)[)\]）]"    # (근거: p.3, p.5)
    r"|^[ \t]*근거[ \t]*[:：](.*)$",                             # 괄호 없이 줄 전체가 "근거: p.3"
    re.MULTILINE,
)


def split_answer_and_pages(answer):
    pages = []
    for m in SOURCE_RE.finditer(answer):
        for num in re.findall(r"p\.?\s*(\d+)", m.group(1) or m.group(2) or ""):
            label = f"p.{num}"
            if label not in pages:
                pages.append(label)
    body = SOURCE_RE.sub("", answer).strip()
    return body, pages


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

    # 수정: 검색 문서를 한 번만 가져와 답변과 근거 문단을 함께 반환
    answer_chain = prompt | llm | StrOutputParser()

    def answer_with_sources(payload):
        question = payload["input"]
        retrieved_docs = retriever.invoke(question)
        answer = answer_chain.invoke({
            "input": question,
            "context": format_docs(retrieved_docs),
        })
        # [5차 수정 3] 답변 본문에서 "(근거: ...)"를 지우고, 그 쪽 번호를 근거로 사용
        answer, pages = split_answer_and_pages(answer)

        if NOT_FOUND_MSG in answer:
            pages = []   # 백서에 없는 내용이면 근거를 표시하지 않음
        elif not pages:
            # 답변에 쪽 번호가 없으면 기존처럼 검색된 문단(최대 3개)의 쪽 번호를 사용
            for doc in retrieved_docs[:3]:
                page = doc.metadata.get("page")
                label = f"p.{page + 1}" if isinstance(page, int) else "페이지 정보 없음"
                if label not in pages:
                    pages.append(label)

        sources = [{"page": p} for p in pages]
        return {"answer": answer, "sources": sources}

    rag_chain = RunnableLambda(answer_with_sources)  # 수정: 기존 변수명 유지

    return rag_chain


# ════════════════════════════════════════════════════════
# [화면 영역] 사용자가 보는 부분 (구 app.py)
# ════════════════════════════════════════════════════════

LOGO_URL = "https://raw.githubusercontent.com/Jangrae/img/master/aivle.png"
MODEL_NAME = LLM_MODEL       # 하단 안내 문구용 (위 설정값을 그대로 사용)
ADMIN_PASSWORD = "0207"      # [수정 1] 관리자 모드 비밀번호 (바꾸려면 여기만 수정)

# ════════════════════════════════════════════════════════
# [주제별 TOP3] 추천 질문(많이 묻는 주제 TOP3) 기능
# ════════════════════════════════════════════════════════
# 동작 순서
#   ① 질문에서 띄어쓰기를 지우고, 아래 TOPICS의 키워드가 들어 있는지 확인
#   ② 키워드가 들어 있는 주제마다 횟수 +1 (한 질문이 여러 주제에 해당하면 모두 +1)
#   ③ 횟수가 많은 주제 3개의 '대표 질문'을 사이드바 추천 질문에 표시
#      (기록이 부족하면 TOPICS 맨 앞 주제부터 채움 → 처음 화면은 기존 추천 질문 3개와 같음)
# - 백서에 없는 질문(NOT_FOUND_MSG)이나 오류 답변은 세지 않음
# - 어느 주제에도 해당하지 않는 질문은 세지 않음 → 필요하면 TOPICS에 키워드를 추가하세요

TOP_N = 3   # [주제별 TOP3] 추천 질문 개수

# [주제별 TOP3] 주제 목록: 이름 / 추천 질문으로 보여 줄 대표 질문 / 이 주제로 볼 키워드
# - 키워드는 띄어쓰기 없이 비교하므로 "자기 부담금", "자기부담금" 모두 잡힘
# - 맨 앞 3개가 기록이 없을 때 기본으로 보이는 추천 질문
# - 너무 짧거나 흔한 키워드(예: "자격")는 다른 주제와 겹칠 수 있으니 피하세요
#   (예: "자격"을 넣으면 "AICE 자격증" 질문도 지원 자격으로 잘못 셈)
TOPICS = [
    {"name": "비전공자·난이도", "question": "비전공자도 따라갈 수 있나요?",
     "keywords": ["비전공", "전공자", "문과", "코딩경험", "초보", "난이도", "따라갈", "따라가"]},
    {"name": "교육비", "question": "교육비와 자기부담금은 얼마인가요?",
     "keywords": ["교육비", "비용", "자기부담금", "부담금", "수강료", "학비", "무료", "유료", "돈"]},
    {"name": "프로젝트", "question": "미니 프로젝트는 어떻게 진행되나요?",
     "keywords": ["프로젝트", "미프", "빅프", "해커톤", "팀플"]},
    {"name": "트랙", "question": "어떤 트랙이 있나요?",
     "keywords": ["트랙", "ai개발자", "dx컨설턴트", "컨설턴트"]},
    {"name": "훈련장려금", "question": "훈련장려금은 얼마나 받을 수 있나요?",
     "keywords": ["장려금", "훈련수당", "수당", "지원금"]},
    {"name": "지원 자격·선발", "question": "지원 자격과 선발 과정은 어떻게 되나요?",
     "keywords": ["지원자격", "자격요건", "자격조건", "지원조건", "지원할수", "지원방법", "지원하려면",
                  "선발", "서류", "면접", "코딩테스트", "합격", "전형", "나이", "졸업"]},
    {"name": "기간·장소", "question": "교육 기간과 장소는 어떻게 되나요?",
     "keywords": ["기간", "일정", "몇개월", "몇달", "시간표", "수업시간", "출석", "교육장", "장소",
                  "지역", "지방", "온라인", "오프라인", "대면", "수도권", "제주", "부산", "대구", "광주", "대전"]},
    {"name": "취업", "question": "수료 후 취업은 어떻게 연결되나요?",
     "keywords": ["취업", "채용", "우대", "입사", "잡페어", "수료후", "진로"]},
    {"name": "AICE 자격증", "question": "AICE 자격증은 무엇인가요?",
     "keywords": ["aice", "에이스", "자격증"]},
    {"name": "학습 방법·후기", "question": "선배들이 추천하는 공부 방법이 있나요?",
     "keywords": ["공부", "후기", "조언", "꿀팁", "선배", "복습", "예습", "학습방법", "학습전략"]},
]


# [주제별 TOP3] 주제별 횟수 저장소
# st.session_state는 사용자마다 따로지만, st.cache_resource는 '모든 사용자'가 함께 씀
@st.cache_resource
def get_topic_stats():
    return {
        "counts": Counter(),        # {주제 이름: 횟수}
        "lock": threading.Lock(),
    }


def find_topics(question):
    """[주제별 TOP3] 질문에 들어 있는 키워드로 해당 주제 이름 목록을 찾음"""
    text = question.replace(" ", "").lower()   # 띄어쓰기·대소문자 차이 없애기
    return [t["name"] for t in TOPICS
            if any(k.replace(" ", "").lower() in text for k in t["keywords"])]


def record_question(question, answer):
    """[주제별 TOP3] 답변을 받은 질문을 주제별 횟수에 더하기
    - 백서에 없는 질문이나 오류 답변은 추천하면 안 되므로 세지 않음
    - 판단 결과는 관리자 화면(🔧)에 보여 주려고 top3_debug에 기록
    """
    if NOT_FOUND_MSG in answer or answer.startswith("오류가 발생했어요"):
        ss["top3_debug"] = f"[{question}] → 백서에 없는 질문/오류라서 세지 않음"
        return
    names = find_topics(question)
    if not names:
        ss["top3_debug"] = f"[{question}] → 해당 주제 없음 (세지 않음, 필요하면 TOPICS에 키워드 추가)"
        return
    stats = get_topic_stats()
    with stats["lock"]:
        for name in names:
            stats["counts"][name] += 1
    ss["top3_debug"] = f"[{question}] → 주제: {', '.join(names)} (+1)"


def get_top_questions(n=TOP_N):
    """[주제별 TOP3] 많이 나온 주제 순서로 n개 반환: [(대표 질문, 횟수), ...]
    - 횟수가 같으면 TOPICS에 적힌 순서대로
    - 기록이 부족하면 TOPICS 맨 앞 주제부터 채움 (이때 횟수는 None)
    """
    stats = get_topic_stats()
    with stats["lock"]:
        counts = dict(stats["counts"])
    asked = [t for t in TOPICS if counts.get(t["name"])]
    asked.sort(key=lambda t: -counts[t["name"]])          # 많이 나온 순 (같으면 TOPICS 순서 유지)
    top = [(t["question"], counts[t["name"]]) for t in asked[:n]]
    for t in TOPICS:                                      # 빈자리 채우기
        if len(top) >= n:
            break
        if t["question"] not in [q for q, _ in top]:
            top.append((t["question"], None))
    return top


# ── 세션 상태 ───────────────────────────────────────────
# Streamlit은 입력할 때마다 파일 전체를 다시 실행하므로
# 체인과 대화 기록을 session_state에 저장해 둡니다.
ss = st.session_state
ss.setdefault("rag_chain", None)
ss.setdefault("file_hash", None)
ss.setdefault("file_name", None)
ss.setdefault("file_data", None)
ss.setdefault("chats", {})        # {대화ID: {"title": 제목, "messages": [...]}}
ss.setdefault("current_id", None)
ss.setdefault("is_admin", False)  # [수정 2] 관리자 인증 여부 (비밀번호를 맞히면 True)
ss.setdefault("feedback", {})     # ★ [답변 평가] {"답변ID": {평가 한 줄}} — CSV로 저장될 내용


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
        ss.file_data = data
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
        # 수정: 오류 상황에도 답변과 근거의 반환 구조를 일정하게 유지
        return {"answer": f"오류가 발생했어요: {e}", "sources": []}


# 수정: AI 답변 아래 원본 검색 문단을 접이식으로 표시
# 실제로 검색된 문단이며, AI가 모든 문단을 사용했다는 의미는 아님
def show_sources(sources):
    if not sources:
        return

    # [5차 수정 4] 소제목(위치 안내) 없이 쪽 번호만 한 줄로 출력
    with st.expander("📖 더보기", expanded=False):
        st.caption("답변 생성 시 제공된 백서 참고 위치")

        pages = []
        for source in sources:
            page = source.get("page", "쪽 번호 없음")
            if page not in pages:
                pages.append(page)

        st.markdown(", ".join(f"**{p}**" for p in pages))


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
.st-key-doc_download {{ position: relative; top: 14px; }}
.st-key-doc_card {{
    margin-top: 1.5rem; padding: 12px 14px 28px; border: 1px solid rgba(0,0,0,0.1);
    border-radius: 12px;
}}
.doc-card {{ font-size: 0.85rem; line-height: 1.4; word-break: break-all; }}
.doc-card .label {{ color: #888; font-size: 0.75rem; }}
.st-key-doc_download button {{ padding: 2px 8px; min-height: 0; font-size: 1.1rem; color: #555; }}

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

    # [주제별 TOP3] 추천 질문 = 많이 묻는 주제 TOP3의 대표 질문 (원본 위치·모양 그대로)
    # - 실제로 질문이 들어온 주제: 🔥 아이콘 (마우스를 올리면 그 주제 질문 횟수 표시)
    # - 기록이 부족해 채운 기본 주제: 💡 아이콘
    # - 누르면 대표 질문이 바로 채팅으로 들어감 (pending_question)
    st.caption("추천 질문 · 많이 묻는 주제 TOP3")
    for i, (q, cnt) in enumerate(get_top_questions()):
        icon = "🔥" if cnt else "💡"
        tip = f"이 주제 질문 {cnt}회" if cnt else "기본 추천 질문"
        if st.button(f"{icon}  {q}", key=f"nav_q{i}", width="stretch", type="tertiary",
                     help=tip, disabled=ss.rag_chain is None):
            ss.pending_question = q

    # [주제별 TOP3] 관리자 확인용: 마지막 질문이 어느 주제로 세졌는지 표시
    # (관리자 모드로 로그인한 사람에게만 보임 → 시연 화면에는 안 나옴)
    if ss.is_admin and ss.get("top3_debug"):
        st.caption(f"🔧 {ss.top3_debug}")

    doc = ss.file_name or "아직 문서가 없어요"
    with st.container(key="doc_card"):
        d1, d2 = st.columns([5, 1], gap="small", vertical_alignment="center")
        d1.markdown(
            f'<div class="doc-card"><div class="label">적용된 문서</div>📄 {doc}</div>',
            unsafe_allow_html=True,
        )
        if ss.file_data is not None:
            d2.download_button(
                "⬇", data=ss.file_data, file_name=ss.file_name, mime="application/pdf",
                key="doc_download", help="백서 PDF 다운로드", type="tertiary",
                on_click="ignore",   # 눌러도 화면을 다시 그리지 않고 파일만 내려받음
            )

    # [수정 5] 관리자 모드 버튼 (누르면 비밀번호 팝업이 열림)
    if st.button("관리자 모드  →", key="nav_admin", type="tertiary"):
        # [3차 수정 1] 버튼을 누를 때마다 인증을 초기화 → 매번 비밀번호를 다시 입력해야 함
        ss.is_admin = False
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
    chat_box = st.container(height=730, border=False)

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
                    show_sources(msg.get("sources", []))  # 수정: 이전 답변의 근거 유지
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
                # 수정: 다시 생성할 때 답변과 근거 모두 갱신
                current["messages"][-1]["content"] = new_answer["answer"]
                current["messages"][-1]["sources"] = new_answer["sources"]
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
                    result = ask(question)  # 수정: 답변 + 근거 반환
                st.markdown(result["answer"])  # 수정: 답변 출력
                show_sources(result["sources"])  # 수정: 접이식 근거 표시

            # 수정: 히스토리에 답변과 원본 검색 문단 함께 저장
            current["messages"].append({
                "role": "assistant",
                "content": result["answer"],
                "sources": result["sources"],
                "rating": None,   # ★ [답변 평가] 아직 평가 안 함
            })
            # [주제별 TOP3] 이 질문을 주제별 횟수에 반영
            # (ask()가 {"answer", "sources"}를 돌려주므로 답변 글자인 result["answer"]를 넘김)
            record_question(question, result["answer"])
            st.rerun()   # 히스토리 제목·미리보기·추천 질문 TOP3를 바로 갱신
