# ════════════════════════════════════════════════════════
# AIVLE School 학습 도우미 — 조별 통합본 [버전 B: 주제별 TOP3 추천 질문]
# 베이스: app_v7 (화면 배치 그대로: 사이드바 추천 질문 · 오른쪽 히스토리,
#         관리자 모드, 답변 근거 '📖 출처', chroma_db 오류 시 재시도)
# 반영:   [3차 수정] 관리자 모드 버튼을 누를 때마다 비밀번호 입력
#         [5차 수정] 답변 본문의 근거 표시는 지우고 '출'에 쪽 번호만 표시
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
# [7차 수정 1] [답변 형식]의 근거 줄을 바꿈: 쪽 번호는 [문서]를 근거로 답했을 때만 적게 함
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
- [문서]의 내용을 근거로 답했을 때만, 답변 끝에 근거가 된 쪽 번호를 (근거: p.3, p.5)처럼 적으세요.
- [문서]에 근거가 없는 답변(백서와 관련 없는 질문, 일반 지식으로 답한 경우)에는 근거를 적지 마세요.
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
#  - 돌려준 쪽 번호는 "출"에서만 보여 줌
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

        # [7차 수정 2] 출처는 '답변이 실제로 적은 쪽 번호'만 표시
        #  - 이전: 답변에 쪽 번호가 없으면 검색된 문단 3개의 쪽 번호를 대신 표시
        #          → 검색기는 관련 없는 질문에도 항상 문단을 돌려주므로, 무관한 질문에도 출처가 나왔음
        #  - 변경: 답변에 쪽 번호가 없으면 출처를 표시하지 않음
        #  - 추가: 실제로 검색된 문단에 없는 쪽 번호(모델이 잘못 적은 번호)는 걸러 냄
        retrieved_pages = {
            f"p.{doc.metadata['page'] + 1}"
            for doc in retrieved_docs
            if isinstance(doc.metadata.get("page"), int)
        }
        pages = [p for p in pages if p in retrieved_pages]

        if NOT_FOUND_MSG in answer:
            pages = []   # 백서에 없는 내용이면 근거를 표시하지 않음

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


def start_new_chat():
    """[새 대화 개선] '새 대화' 버튼을 누르면 실행: 검색어를 지우고 빈 대화를 새로 엶"""
    ss.search_kw = ""   # 검색창 비우기 → 검색 결과 화면에서 빠져나옴
    new_chat()


def clear_search():
    """[원래 대화로 돌아가기] 검색어만 지움 → 검색 전에 보던 대화가 그대로 다시 보임"""
    ss.search_kw = ""


def open_from_history(chat_id, q_id):
    """[원래 대화로 돌아가기] 히스토리에서 질문을 누르면: 검색어를 지우고 그 질문이 있는 대화를 엶"""
    ss.search_kw = ""
    ss.current_id = chat_id
    ss.selected_q = q_id


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
    with st.expander("📖 출처", expanded=False):
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

# ════════════════════════════════════════════════════════
# 🎨 [UI 디자인] 화면 배치와 스타일 (기능 코드는 위쪽 그대로, 여기서는 '보이는 모습'만 바꿈)
#   왼쪽 사이드바 : 로고 · 추천 질문 카드 · 적용된 문서 · 관리자 모드
#   오른쪽 큰 영역: 헤더(제목 · 백서 배지)
#                   ├ 가운데: 채팅 + 채팅 입력
#                   └ 오른쪽: 히스토리(제목 · 검색 · 새 대화 · 질문 목록)
# 주석의 🎨 [UI 디자인] 표시로 해당 부분을 찾을 수 있어요.
# ════════════════════════════════════════════════════════

# 🎨 [UI 디자인] 색상 (여기만 바꾸면 전체 색이 바뀜)
PRIMARY      = "#10B3A6"   # 메인 민트 (버튼·강조)
PRIMARY_DARK = "#0C8F85"   # 진한 민트 (글자·마우스 올렸을 때)
PRIMARY_SOFT = "#DFEEEE"   # 연한 민트 (히스토리에서 지금 열린 질문 · 질문 말풍선)
BG_SOFT      = "#F3FAF9"   # 아주 연한 민트 (히스토리 영역 · 추천 질문 카드 · 적용된 문서)
BG_PAGE      = "#EDEDED"   # 학습 도우미 영역 뒤 배경 (연한 회색)
SOURCE_BG    = "#F8F8F8"   # 📖 출처 배경
LINE         = "#E3EDEC"   # 구분선
TEXT         = "#1F2D2C"   # 기본 글자
TEXT_SUB     = "#7A8B8A"   # 보조 글자

# 🎨 [UI 디자인] 로고: 저장소에 aivle.png 파일이 있으면 그 파일을, 없으면 기존 주소를 사용
LOGO_FILE = "aivle.png"
logo_src = LOGO_FILE if os.path.exists(LOGO_FILE) else LOGO_URL

# 🎨 [UI 디자인] 히스토리에서 색을 입힐 질문 = 위쪽 기능 코드의 active_q (질문 하나)
#   - 지금 열린 대화에서 마지막으로 질문했거나, 히스토리에서 눌러서 연 질문
#   - 그 질문이 지금 열린 대화에 없으면 "none" → 아무 줄에도 색이 들어가지 않음

# 🎨 [UI 디자인] 삭제 아이콘: 가는 원 안에 끝이 둥근 ✕ (평소 회색 / 마우스를 올리면 빨간색)
def close_icon(color, fill="none"):
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
           f"stroke='{color}' stroke-width='1.5' stroke-linecap='round'>"
           f"<circle cx='12' cy='12' r='10.75' fill='{fill}'/>"
           f"<path d='M8.5 8.5l7 7M15.5 8.5l-7 7'/></svg>")
    return 'url("data:image/svg+xml,' + svg.replace("#", "%23") + '")'

CLOSE_ICON       = close_icon("#9AA9A8")
CLOSE_ICON_HOVER = close_icon("#E5484D", fill="#FFF5F5")

# ── 스타일 ──────────────────────────────────────────────
st.markdown(f"""
<style>
/* ── 전체 배경: 연한 회색 단색 ── */
.stApp {{ background: {BG_PAGE}; color: {TEXT}; }}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding: 2.2rem 2rem 1rem; max-width: 100%; }}

/* ── 왼쪽 사이드바 ── */
[data-testid="stSidebar"] {{ background: #fff; border-right: 1px solid #E2E2E2; }}
[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{ padding-top: 1.6rem; padding-bottom: 1.2rem; }}
/* 사이드바 내용을 화면 높이에 딱 맞춤 → 히스토리 목록이 남는 공간을 채우고, 관리자 모드는 항상 맨 아래 */
[data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] {{ height: calc(100vh - 124px); }}

/* 가로 줄 안의 글씨가 버튼과 같은 높이에 오도록, 글씨 요소의 기본 여백을 없앰 */
.st-key-hist_head [data-testid="stMarkdownContainer"],
.st-key-header [data-testid="stMarkdownContainer"],
.st-key-doc_card [data-testid="stMarkdownContainer"],
[class*="st-key-hrow_"] [data-testid="stMarkdownContainer"] {{ margin-bottom: 0 !important; }}
.st-key-header [data-testid="stMarkdownContainer"] p,
.st-key-doc_card [data-testid="stMarkdownContainer"] p,
[class*="st-key-hrow_"] [data-testid="stMarkdownContainer"] p {{ margin: 0; }}

/* 히스토리 제목: "히스토리 (3)" */
.hist-title {{ font-weight: 700; font-size: 1.02rem; line-height: 1.4; }}
.hist-title .count {{
    margin-left: 6px; font-size: 0.72rem; font-weight: 600; color: {PRIMARY_DARK};
    background: {PRIMARY_SOFT}; padding: 1px 8px; border-radius: 999px;
}}

/* 대화 검색창 */
.st-key-search_kw [data-testid="stTextInputRootElement"] {{
    border: 1px solid #DADADA; border-radius: 10px; background: #fff;
}}
.st-key-search_kw [data-testid="stTextInputRootElement"]:focus-within {{ border-color: {PRIMARY}; }}
.st-key-search_kw input {{ font-size: 0.85rem; background: #fff; }}

/* 히스토리 영역 안의 검색 · 새 대화 묶음 (아래 질문 목록과 간격) */
.st-key-rec_tools {{ gap: 8px; margin: 4px 0 6px; }}
.st-key-rec_tools [data-testid="stCaptionContainer"],
.st-key-rec_tools [data-testid="stMarkdownContainer"] {{ margin-bottom: 0 !important; }}
.st-key-rec_tools [data-testid="stCaptionContainer"] p {{ font-size: 0.75rem; line-height: 1.45; margin: 0; }}

/* 새 대화 시작하기 / 검색 마치고 원래 대화로: 메인 민트 단색 버튼 */
.st-key-nav_new button {{
    background: {PRIMARY} !important; color: #fff !important; border-radius: 10px;
    font-weight: 600; padding: 8px 10px; justify-content: center;
}}
.st-key-nav_new button:hover {{ background: {PRIMARY_DARK} !important; color: #fff !important; }}

/* 히스토리 목록(오른쪽 영역): 화면 높이에 맞춰 늘어나고, 넘치면 스크롤 */
[data-testid="stLayoutWrapper"]:has(> .st-key-hist_list) {{ height: auto !important; }}
.st-key-hist_list {{ height: calc(100vh - 330px) !important; min-height: 160px; overflow-y: auto; }}

/* ★ [답변 평가] 히스토리 한 줄 = 질문 1개 (질문 · 태그 · ✕), 줄마다 아래 구분선 */
[class*="st-key-hrow_"] {{
    padding: 2px 4px 2px 10px; gap: 6px; flex-wrap: nowrap;
    border-radius: 10px; border-bottom: 1px solid #EEEEEE;
}}
[class*="st-key-hrow_"]:hover {{ background: #E9F4F3; }}
/* 지금 열려 있는 질문 하나만: 연한 민트(#DFEEEE) 배경 */
.st-key-hrow_{active_q}, .st-key-hrow_{active_q}:hover {{ background: {PRIMARY_SOFT}; border-bottom-color: transparent; }}
[class*="st-key-hopen_"] {{ flex: 1 1 0; min-width: 0; }}
[class*="st-key-hopen_"] button {{
    justify-content: flex-start; padding: 9px 0; min-height: 0; color: {TEXT}; width: 100%;
}}
[class*="st-key-hopen_"] button > div {{ justify-content: flex-start; width: 100%; min-width: 0; }}
[class*="st-key-hopen_"] button p {{
    width: 100%; text-align: left; font-size: 0.86rem;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
[class*="st-key-hopen_"] button:hover {{ color: {PRIMARY_DARK}; }}
/* 삭제 버튼: 가는 원 안의 ✕ 아이콘 (크기 20×20 그대로, 버튼 글자는 숨기고 그림으로 표시) */
[class*="st-key-hdel_"] button {{
    width: 20px; height: 20px; min-height: 0; padding: 0; border: none; border-radius: 50%;
    background: {CLOSE_ICON} center / 100% 100% no-repeat;
}}
[class*="st-key-hdel_"] button p {{ font-size: 0; color: transparent; }}
[class*="st-key-hdel_"] button:hover {{ background: {CLOSE_ICON_HOVER} center / 100% 100% no-repeat; }}

/* ★ [답변 평가] 만족/불만족 태그 */
.rate-tag {{
    display: inline-block; font-size: 0.7rem; font-weight: 700;
    padding: 1px 8px; border-radius: 999px; line-height: 1.6; white-space: nowrap;
}}
.rate-tag.good {{ background: {PRIMARY_SOFT}; color: {PRIMARY_DARK}; }}
.rate-tag.bad  {{ background: #FDECEA; color: #C0392B; }}

/* 적용된 문서 카드: 연한 민트 바탕, 안쪽 여백 위·아래·양옆 모두 16px */
/* 카드를 감싼 칸이 위쪽 빈 공간을 채워서, 문서 카드 · 관리자 모드가 사이드바 맨 아래로 내려감 */
[data-testid="stLayoutWrapper"]:has(> .st-key-doc_card) {{ margin-top: auto; }}
.st-key-doc_card {{
    padding: 16px; border-radius: 14px;
    background: {BG_SOFT}; border: 1px solid {LINE}; gap: 0;
}}
.st-key-doc_card [data-testid="stHorizontalBlock"] {{ gap: 8px; }}
.doc-card {{ font-size: 0.85rem; line-height: 1.45; word-break: break-all; color: {TEXT}; }}
.doc-card .label {{ color: {TEXT_SUB}; font-size: 0.72rem; margin-bottom: 2px; }}
.st-key-doc_download {{ display: flex; justify-content: flex-end; }}
.st-key-doc_download button {{
    padding: 2px 8px; min-height: 0; font-size: 1rem; color: {PRIMARY_DARK};
    background: #fff; border: 1px solid {LINE}; border-radius: 8px;
}}
.st-key-doc_download button:hover {{ color: #fff; background: {PRIMARY}; border-color: {PRIMARY}; }}

/* 관리자 모드 버튼: 사이드바 맨 아래, 글자는 왼쪽 · 화살표는 오른쪽에 크게 */
.st-key-nav_admin {{ margin-top: 0.4rem; padding-top: 0.6rem; border-top: 1px solid #EEEEEE; }}
.st-key-nav_admin button {{
    width: 100%; padding: 8px 4px; min-height: 0; color: {TEXT_SUB}; font-weight: 600;
    justify-content: space-between;
}}
.st-key-nav_admin button > div,
.st-key-nav_admin button > div > span {{ width: 100%; justify-content: space-between; }}
.st-key-nav_admin button [data-testid="stIconMaterial"] {{
    font-size: 1.6rem !important; width: 1.6rem !important; height: 1.6rem !important;
}}
.st-key-nav_admin button:hover {{ color: {PRIMARY_DARK}; }}

/* ── 오른쪽 큰 영역(메인 패널): 흰색 둥근 카드 ── */
.st-key-panel {{
    background: #fff; border-radius: 22px; border: 1px solid #E2E2E2;
    padding: 0; gap: 0; overflow: hidden;   /* 🎨 그림자 효과 삭제 */
}}

/* 헤더: 제목 · 백서 배지 */
.st-key-header {{ padding: 16px 24px; border-bottom: 1px solid {LINE}; gap: 12px; }}
.header-title {{ font-size: 1.15rem; font-weight: 700; color: {TEXT}; }}
.badge {{
    font-size: 0.76rem; font-weight: 600; padding: 6px 12px; border-radius: 999px;
    background: {PRIMARY_SOFT}; color: {PRIMARY_DARK}; white-space: nowrap;
}}
.badge.off {{ background: #EEF1F1; color: {TEXT_SUB}; }}

/* 헤더 아래: 채팅(왼쪽) · 추천 질문(오른쪽) */
.st-key-body {{ gap: 0; }}
.st-key-chat_area {{ padding: 6px 22px 18px; }}

/* 채팅 목록: 화면 높이에 맞춰 늘어나고, 넘치면 스크롤 */
.st-key-chat_box {{ height: calc(100vh - 390px) !important; min-height: 280px; }}

/* 첫 화면 안내 */
.welcome {{ text-align: center; margin-top: 12vh; color: {TEXT_SUB}; }}
.welcome .icon {{
    width: 56px; height: 56px; margin: 0 auto 14px; border-radius: 16px; font-size: 1.6rem;
    display: flex; align-items: center; justify-content: center;
    background: {PRIMARY}; color: #fff;
}}
.welcome .title {{ font-size: 1.15rem; font-weight: 700; color: {TEXT}; margin-bottom: 4px; }}
.welcome .desc {{ font-size: 0.88rem; }}

/* 채팅 말풍선: 질문 = 연한 민트(오른쪽), 답변 = 흰 카드 + 테두리 */
[data-testid="stChatMessage"] {{ padding: 0; background: transparent; gap: 10px; }}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {{
    flex-direction: row-reverse;
}}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"])
  [data-testid="stChatMessageContent"] {{
    flex: 0 1 auto; margin-left: auto !important; margin-right: 0 !important;
    background: {PRIMARY_SOFT}; border-radius: 16px 4px 16px 16px; padding: 8px 16px;
    color: {TEXT};
}}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"])
  [data-testid="stChatMessageContent"] {{
    background: #fff; border: 1px solid {LINE}; border-radius: 4px 16px 16px 16px;
    padding: 14px 18px; box-shadow: 0 4px 14px rgba(0,0,0,0.04);
}}
[data-testid="stChatMessageAvatarUser"] {{ background: {TEXT}; }}
[data-testid="stChatMessageAvatarAssistant"] {{ background: {PRIMARY}; }}

/* 📖 출처 접이식: 연한 회색 단색 */
[data-testid="stChatMessage"] [data-testid="stExpander"] details {{
    border: 1px solid {SOURCE_BG}; border-radius: 10px; background: {SOURCE_BG};
}}

/* ★ [답변 평가] 👍/👎 버튼: 흰 네모 버튼, 눌린 버튼은 색을 꽉 채움 */
[data-testid="stFeedback"] button {{
    border: 1px solid {LINE}; border-radius: 8px;
    padding: 4px 10px; background: #fff; color: {TEXT_SUB};
}}
[data-testid="stFeedback"] button:hover {{ border-color: {PRIMARY}; color: {PRIMARY_DARK}; }}
[data-testid="stFeedback"] button[aria-label="Thumbs up"][aria-checked="true"] {{
    background: {PRIMARY} !important; border-color: {PRIMARY} !important; color: #fff !important;
}}
[data-testid="stFeedback"] button[aria-label="Thumbs down"][aria-checked="true"] {{
    background: #E05A4F !important; border-color: #E05A4F !important; color: #fff !important;
}}
[data-testid="stFeedback"] button[aria-checked="true"] [data-testid="stIconMaterial"] {{
    font-variation-settings: "FILL" 1; color: #fff !important;
}}
[data-testid="stFeedback"] [role="radiogroup"] {{ gap: 6px; }}

/* 다시 생성: 흰 알약 모양 버튼 */
.st-key-regen button {{
    border-radius: 999px; border: 1px solid {LINE}; background: #fff; color: {TEXT};
    box-shadow: 0 4px 12px rgba(0,0,0,0.06); padding: 4px 18px; font-size: 0.85rem;
}}
.st-key-regen button:hover {{ border-color: {PRIMARY}; color: {PRIMARY_DARK}; }}

/* 채팅 입력: 최대 두 줄까지만 보이고, 넘치면 입력창 안에서 스크롤 */
.st-key-input_box {{ margin-top: 8px; }}
[data-testid="stChatInput"] {{ background: transparent; border: none; }}
[data-testid="stChatInput"] > div {{
    background: {BG_SOFT}; border: 1px solid {LINE} !important; border-radius: 14px;
}}
[data-testid="stChatInput"] > div:focus-within {{
    border-color: {PRIMARY} !important; box-shadow: 0 0 0 3px rgba(16,179,166,0.12);
}}
[data-testid="stChatInputTextArea"] {{
    max-height: 3.2em !important; line-height: 1.6em; overflow-y: auto !important;
}}
[data-testid="stChatInputSubmitButton"] {{
    background: {PRIMARY} !important; color: #fff !important; border-radius: 10px;
}}
[data-testid="stChatInputSubmitButton"]:disabled {{ background: #BFD9D6 !important; }}
.input-hint {{ font-size: 0.75rem; color: {TEXT_SUB}; margin: 4px 4px 0; }}

/* 하단 안내 문구 */
.disclaimer {{ text-align: center; font-size: 0.72rem; color: #A3B1B0; margin-top: 2px; }}

/* 관리자 팝업: '확인' 같은 주요 버튼과 파일 업로드 영역도 민트색으로 */
[data-testid="stBaseButton-primary"] {{ background: {PRIMARY}; border-color: {PRIMARY}; color: #fff; }}
[data-testid="stBaseButton-primary"]:hover {{ background: {PRIMARY_DARK}; border-color: {PRIMARY_DARK}; color: #fff; }}
[data-testid="stFileUploaderDropzone"] {{ background: {BG_SOFT}; border: 1px dashed #A9D6D1; border-radius: 12px; }}
[role="dialog"] {{ border-radius: 18px; }}

/* ── 오른쪽 영역(히스토리): 아주 연한 민트 단색 + 왼쪽 구분선 ── */
.st-key-right_area {{
    background: {BG_SOFT}; border-left: 1px solid {LINE};
    padding: 22px 16px; min-height: calc(100vh - 150px); gap: 12px;
}}
.rec-title {{ font-weight: 700; font-size: 1.02rem; line-height: 1.4; margin-top: 0.6rem; }}
.rec-sub {{ font-size: 0.78rem; color: {TEXT_SUB}; line-height: 1.4; margin-top: 4px; margin-bottom: 10px; }}
/* 추천 질문 카드: 아주 연한 민트 카드, 첫 줄 🔥 · 둘째 줄 "Q. 질문" */
[class*="st-key-rec_q"] button {{
    width: 100%; min-height: 100px; padding: 14px 16px;
    align-items: flex-start; justify-content: flex-start;
    background: {BG_SOFT}; border: 1px solid {BG_SOFT}; border-radius: 14px;
    color: {TEXT}; transition: all .15s ease;
}}
[class*="st-key-rec_q"] button > div {{ justify-content: flex-start; align-items: flex-start; width: 100%; }}
[class*="st-key-rec_q"] button p {{
    text-align: left; font-size: 0.98rem; line-height: 1.55; white-space: normal;
}}
[class*="st-key-rec_q"] button p strong {{ color: {PRIMARY}; font-size: 1.02rem; }}
[class*="st-key-rec_q"] button:hover {{
    border-color: {PRIMARY}; color: {TEXT}; background: #fff;
    transform: translateY(-2px); box-shadow: 0 8px 18px rgba(16,179,166,0.15);
}}
[class*="st-key-rec_q"] button:disabled {{ opacity: 0.55; transform: none; }}
</style>
""", unsafe_allow_html=True)


current = ss.chats[ss.current_id]

# ── 🎨 오른쪽 큰 영역(메인 패널) ───────────────────────────
with st.container(key="panel"):

    # ── 헤더: 제목 · 백서 배지 (원래 '상단 바') ──
    with st.container(key="header", horizontal=True, vertical_alignment="center"):
        st.markdown('<div class="header-title">AIVLE School 학습 도우미</div>',
                    unsafe_allow_html=True, width="stretch")
        badge = ('<span class="badge">✔ 백서 적용됨</span>' if ss.rag_chain
                 else '<span class="badge off">문서 미적용</span>')
        st.markdown(badge, unsafe_allow_html=True, width="content")

    # ── 헤더 아래: 채팅(가운데) · 추천 질문(오른쪽) ──
    with st.container(key="body"):
        chat_col, rec_col = st.columns([3.2, 1], gap=None)

# ── 히스토리 (오른쪽) ────────────────────────────────────
# 🎨 [위치 이동] 원래 왼쪽 사이드바에 있던 히스토리 목록을 오른쪽 영역으로 옮김
with rec_col:
    with st.container(key="right_area"):
        # 히스토리 목록 (★ [답변 평가] 질문 1개 = 1줄)
        title_box = st.container(key="hist_head")   # 히스토리 제목 자리를 먼저 잡아 둠 (개수는 검색 후에 채움)

        # 🎨 [위치 이동] 대화 검색 + 새 대화 버튼: 히스토리 제목과 질문 목록 사이
        with st.container(key="rec_tools"):
            keyword = st.text_input("검색", placeholder="🔍 대화 검색", label_visibility="collapsed",
                                    key="search_kw")
            if keyword:
                st.caption("검색을 마쳤다면 아래 버튼을 눌러 원래 대화로 돌아가세요.")
                st.button("↩  검색 마치고 원래 대화로", key="nav_new", width="stretch", type="tertiary",
                          help="검색어를 지우고 검색 전에 보던 대화로 돌아가요", on_click=clear_search)
            else:
                st.button("✏️  새 대화 시작하기", key="nav_new", width="stretch", type="tertiary",
                          help="빈 대화를 새로 열어 질문해요", on_click=start_new_chat)

        items = history_items()
        if keyword:   # 대화 검색: 질문이나 답변에 검색어가 있는 줄만 (띄어쓰기·대소문자 무시)
            norm = lambda s: s.replace(" ", "").lower()
            kw = norm(keyword)
            items = [it for it in items
                     if kw in norm(it["question"]["content"])
                     or (it["answer"] and kw in norm(it["answer"]["content"]))]

        # [위치 이동] 위에서 잡아 둔 자리에 제목 표시
        title_box.markdown(f'<div class="hist-title">히스토리<span class="count">{len(items)}</span></div>',
                           unsafe_allow_html=True)

        with st.container(key="hist_list", height=400, border=False):
            if not items:
                st.caption("검색 결과가 없어요" if keyword else "아직 질문이 없어요")

            for it in items:
                q = it["question"]
                q_id = msg_id(q)
                q_text = " ".join(q["content"].split())    # 줄바꿈 없이 한 줄로

                # 한 줄 안에 [질문 ......] [태그] [✕] 를 가로로 배치
                with st.container(key=f"hrow_{q_id}", horizontal=True, vertical_alignment="center"):
                    # [원래 대화로 돌아가기] 질문을 누르면 검색어를 지우고 그 질문이 있는 대화를 엶
                    st.button(q_text, key=f"hopen_{q_id}", type="tertiary", width="stretch",
                              on_click=open_from_history, args=(it["chat_id"], q_id))
                    tag = rating_tag(it["rating"])
                    if tag:
                        st.markdown(tag, unsafe_allow_html=True, width="content")
                    if st.button("✕", key=f"hdel_{q_id}", help="질문 삭제", type="tertiary"):
                        delete_question(it["chat_id"], q_id)
                        st.rerun()

# ── 왼쪽 사이드바: 로고 · 추천 질문 · 적용된 문서 · 관리자 모드 ──
# (채팅보다 먼저 그려야, 추천 질문을 누른 그 순간 바로 채팅으로 들어감)
with st.sidebar:
    st.image(logo_src, width=150)

    # 🎨 [위치 이동] 원래 오른쪽 영역에 있던 추천 질문을 사이드바로 옮김
    st.markdown('<div class="rec-title">추천 질문</div>'
                '<div class="rec-sub">많이 묻는 주제 TOP3</div>', unsafe_allow_html=True)
    # [주제별 TOP3] 추천 질문 = 많이 묻는 주제 TOP3의 대표 질문
    # - 실제로 질문이 들어온 주제: 첫 줄에 🔥 (마우스를 올리면 그 주제 질문 횟수 표시)
    # - 누르면 대표 질문이 바로 채팅으로 들어감 (pending_question)
    for i, (q, cnt) in enumerate(get_top_questions()):
        tip = f"이 주제 질문 {cnt}회" if cnt else "기본 추천 질문"
        # 🎨 첫 줄: 🔥 (실제로 많이 물어본 주제일 때만) / 둘째 줄: Q. 질문
        label = f"🔥  \n**Q.** {q}" if cnt else f"**Q.** {q}"
        if st.button(label, key=f"rec_q{i}", width="stretch",
                     help=tip, disabled=ss.rag_chain is None):
            ss.pending_question = q

    # [주제별 TOP3] 관리자 확인용: 마지막 질문이 어느 주제로 세졌는지 표시
    # (관리자 모드로 로그인한 사람에게만 보임 → 시연 화면에는 안 나옴)

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
    # 🎨 글자는 왼쪽, 화살표 아이콘은 오른쪽에 크게
    if st.button("관리자 모드", key="nav_admin", type="tertiary", width="stretch",
                 icon=":material/arrow_forward:", icon_position="right"):
        # [3차 수정 1] 버튼을 누를 때마다 인증을 초기화 → 매번 비밀번호를 다시 입력해야 함
        ss.is_admin = False
        admin_dialog()

# ── 채팅 (가운데): 대화 영역 + 다시 생성 + 입력창 ──────────
with chat_col:
    with st.container(key="chat_area"):
        chat_box = st.container(height=560, border=False, key="chat_box")

        # 다시 생성: 마지막 질문으로 답변을 새로 받기
        regenerate = False
        if current["messages"] and ss.rag_chain is not None:
            with st.container(key="regen", horizontal=True, horizontal_alignment="center"):
                regenerate = st.button("↻  다시 생성")

        with st.container(key="input_box"):
            user_input = st.chat_input("메시지를 입력하세요", max_chars=3000,
                                       disabled=ss.rag_chain is None)
            # [수정 6] 업로드는 관리자 모드 팝업에서만 가능
            st.markdown('<div class="input-hint">관리자가 적용한 백서 내용을 바탕으로 답해요.</div>',
                        unsafe_allow_html=True)

        st.markdown(
            f'<div class="disclaimer">답변은 업로드한 백서를 바탕으로 하며 부정확할 수 있어요. '
            f'Model: {MODEL_NAME}</div>',
            unsafe_allow_html=True,
        )

    question = user_input or ss.pop("pending_question", None)

    with chat_box:
        # ★ [검색 결과 보기] 검색어가 있으면 메인 채팅창에 '검색된 질문과 답변'을 보여 줌
        # - 오른쪽 히스토리에 걸러진 질문(items)과 그 답변을 순서대로 표시 (읽기 전용, 👍/👎 버튼 없음)
        # - 검색창을 비우면 원래 보던 대화로 돌아감
        # - 새 질문을 보내거나 '다시 생성'을 누른 순간에는 원래 대화 화면으로 처리
        search_mode = bool(keyword) and not question and not regenerate
        if search_mode:
            st.caption(f"🔍 '{keyword}' 검색 결과 {len(items)}개 · 검색어를 지우면 원래 대화로 돌아가요")
            for it in items:
                with st.chat_message("user"):
                    st.markdown(it["question"]["content"])
                if it["answer"]:
                    with st.chat_message("assistant"):
                        st.markdown(it["answer"]["content"])
                        show_sources(it["answer"].get("sources", []))

        # 🎨 [UI 디자인] 아직 대화가 없을 때 보여 줄 첫 화면 안내
        if not search_mode and not current["messages"] and not question:
            st.markdown(
                '<div class="welcome"><div class="icon">🎓</div>'
                '<div class="title">무엇이든 물어보세요!</div>'
                '<div class="desc">에이블스쿨 백서를 바탕으로 교육 과정·지원·취업까지 답해 드려요.<br>'
                '왼쪽 추천 질문으로 바로 시작할 수도 있어요.</div></div>',
                unsafe_allow_html=True,
            )

        # 원래 대화 표시 (★ [검색 결과 보기] 검색 중에는 건너뜀)
        for idx, msg in enumerate([] if search_mode else current["messages"]):
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
