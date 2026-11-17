# Streamlit Cloud의 sqlite 버전 문제 대비 (Colab에서는 영향 없음)
try:
    __import__("pysqlite3"); import sys; sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
except ImportError:
    pass

import os
import warnings
from operator import itemgetter

warnings.filterwarnings(action="ignore")

import pymupdf
pymupdf.set_messages(path=os.devnull)

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_pymupdf4llm import PyMuPDF4LLMLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma


# ════════════════════════════════════════════════════════
# ① 설정값 (조별 표준안이 정해지면 여기만 바꾸세요)
# ════════════════════════════════════════════════════════
CHUNK_SIZE      = 600        # 한 조각의 최대 글자 수
CHUNK_OVERLAP   = 100        # 앞뒤 조각이 겹치는 글자 수 (CHUNK_SIZE보다 작아야 함)
TOP_K           = 5          # 답변에 넘길 문단 수
FETCH_K         = 20         # MMR이 먼저 뽑는 후보 문단 수
EMBEDDING_MODEL = "text-embedding-3-small"
LLM_MODEL       = "gpt-4o-mini"
TEMPERATURE     = 0          # 0에 가까울수록 문서에 충실한 답변
NOT_FOUND_MSG   = "백서에서 해당 내용을 찾을 수 없습니다."


# ════════════════════════════════════════════════════════
# ④ 프롬프트 템플릿
# ════════════════════════════════════════════════════════
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


# RAG 파이프라인 함수
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

    # RAG 체인 구성 (입력·출력 형식은 기존과 동일)
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
