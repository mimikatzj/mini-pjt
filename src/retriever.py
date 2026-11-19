"""체크리스트 검색 (RAG). data/checklist.csv를 원본으로 검색 인덱스를 만들고 관련 항목을 찾는다.

실행:
  python -m src.retriever --build            인덱스 생성 (체크리스트가 바뀌면 다시 실행)
  python -m src.retriever --search "검색어"   상위 항목 확인

검색 방식 (search의 mode):
  vector  — Chroma 벡터 검색 (의미가 비슷한 항목)
  bm25    — BM25 키워드 검색 (kiwipiepy 형태소 단위로 같은 단어가 겹치는 항목)
  hybrid  — 두 결과를 EnsembleRetriever로 가중 합산 (기본값, 실습 day2_practice/hybrid.py 패턴)
M2에서 추가 예정: 쿼리 확장 (2-3), 리랭킹 (2-4).
"""

import argparse
import csv
import re
from functools import lru_cache

from kiwipiepy import Kiwi
from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document

from src import config

# 명사, 수사, 대명사, 동사·형용사 어간, 외국어, 숫자만 남긴다 (실습 사전 검증본과 같은 품사 집합)
KEEP_TAGS = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN"}

COLLECTION = "checklist"


_ID_PATTERN = re.compile(r"\d+\.\d+\.\d+")


def normalize_id(raw: str) -> str:
    """LLM이 돌려준 항목 ID에서 "2.9.2" 형태만 뽑는다. 예: "[2.9.2] 백업 및 복구관리" → "2.9.2".

    모양만 정리할 뿐이다. 실제로 있는 ID인지는 호출한 쪽에서 checklist.csv로 다시 검증한다.
    """
    m = _ID_PATTERN.search(raw or "")
    return m.group() if m else (raw or "").strip()


def load_checklist() -> dict[str, dict[str, str]]:
    """checklist.csv를 {항목 ID: 행} 사전으로 읽는다. 판정 근거 ID 검증에도 쓴다."""
    with open(config.CHECKLIST_PATH, encoding="utf-8-sig", newline="") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


def to_document(row: dict[str, str]) -> Document:
    """체크리스트 한 항목을 검색용 청크 하나로 만든다 (항목 단위 청크)."""
    text = (
        f"[{row['id']}] {row['점검항목']} ({row['분야']})\n"
        f"점검내용: {row['점검내용']}\n"
        f"확인사항: {row['확인사항']}"
    )
    return Document(page_content=text, metadata={"id": row["id"], "분야": row["분야"], "점검항목": row["점검항목"]})


def _embeddings() -> BedrockEmbeddings:
    return BedrockEmbeddings(model_id=config.EMBEDDING_MODEL, region_name=config.AWS_REGION)


def build_index() -> int:
    """인덱스를 처음부터 다시 만든다. 기존 컬렉션을 지워 삭제된 항목이 남지 않게 한다."""
    rows = load_checklist()
    db = Chroma(collection_name=COLLECTION, embedding_function=_embeddings(), persist_directory=str(config.CHROMA_DIR))
    db.delete_collection()
    db = Chroma.from_documents(
        documents=[to_document(r) for r in rows.values()],
        embedding=_embeddings(),
        ids=list(rows),  # 항목 ID를 문서 ID로 써서 중복 적재를 막는다
        collection_name=COLLECTION,
        persist_directory=str(config.CHROMA_DIR),
    )
    get_vectorstore.cache_clear()
    return db._collection.count()


@lru_cache(maxsize=1)
def get_vectorstore() -> Chroma:
    """저장된 인덱스를 연다. 실행 중에는 한 번만 연다."""
    if not config.CHROMA_DIR.exists():
        raise FileNotFoundError("검색 인덱스가 없습니다. 먼저 `python -m src.retriever --build`를 실행하세요.")
    return Chroma(collection_name=COLLECTION, embedding_function=_embeddings(), persist_directory=str(config.CHROMA_DIR))


@lru_cache(maxsize=1)
def _kiwi() -> Kiwi:
    return Kiwi()


def korean_tokenizer(text: str) -> list[str]:
    """한국어 형태소 단위로 자른다. 예: "접근권한을 회수하지" → ["접근", "권한", "회수", "하"]"""
    return [t.form for t in _kiwi().tokenize(text) if t.tag in KEEP_TAGS]


@lru_cache(maxsize=1)
def _bm25_docs() -> tuple[Document, ...]:
    """BM25는 디스크 인덱스 없이 checklist.csv에서 바로 만든다 (65개라 실행마다 만들어도 빠르다)."""
    return tuple(to_document(r) for r in load_checklist().values())


@lru_cache(maxsize=1)
def _bm25_index() -> BM25Retriever:
    """BM25 인덱스는 실행 중 한 번만 만든다 (65개 항목 형태소 분석). 검색마다 만들면 느리다 (2-10 트레이스로 발견)."""
    return BM25Retriever.from_documents(list(_bm25_docs()), preprocess_func=korean_tokenizer)


def _bm25(k: int) -> BM25Retriever:
    return _bm25_index().model_copy(update={"k": k})  # 인덱스는 공유하고 결과 개수만 바꾼다


def search(query: str, k: int = config.RETRIEVE_K, mode: str = "hybrid") -> list[Document]:
    """검색어와 관련된 체크리스트 항목을 k개 돌려준다."""
    if mode == "vector":
        return get_vectorstore().similarity_search(query, k=k)
    if mode == "bm25":
        return _bm25(k).invoke(query)
    if mode == "hybrid":
        vector = get_vectorstore().as_retriever(search_kwargs={"k": k})
        ensemble = EnsembleRetriever(retrievers=[_bm25(k), vector], weights=list(config.HYBRID_WEIGHTS), id_key="id")
        return ensemble.invoke(query)[:k]  # 두 결과의 합집합을 가중 순위로 정렬하므로 k개로 자른다
    raise ValueError(f"알 수 없는 검색 방식: {mode}")


RRF_K = 60  # Reciprocal Rank Fusion 상수 (관례값)


def fused_search(queries: list[str], k: int = config.RETRIEVE_K, per_query: int = 5) -> list[Document]:
    """검색어 여러 개로 각각 하이브리드 검색한 뒤 순위를 합산(RRF)해 상위 k개를 돌려준다 (쿼리 확장용).

    여러 검색어에서 고르게 상위에 나온 항목이 위로 온다. 계산 규칙이므로 코드로 한다.
    """
    scores: dict[str, float] = {}
    docs: dict[str, Document] = {}
    for q in queries:
        for rank, doc in enumerate(search(q, k=per_query), 1):
            doc_id = doc.metadata["id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1 / (RRF_K + rank)
            docs[doc_id] = doc
    ranked = sorted(scores, key=scores.get, reverse=True)
    return [docs[i] for i in ranked[:k]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="체크리스트 검색 인덱스")
    parser.add_argument("--build", action="store_true", help="인덱스 생성")
    parser.add_argument("--search", metavar="검색어", help="검색 결과 확인 (벡터·BM25·하이브리드 비교)")
    args = parser.parse_args()

    if args.build:
        print(f"인덱스 생성 완료: {build_index()}개 항목 → {config.CHROMA_DIR}")
    if args.search:
        import sys

        sys.stdout.reconfigure(encoding="utf-8")
        print(f"검색어: {args.search}  (토큰: {korean_tokenizer(args.search)})")
        for mode in ("vector", "bm25", "hybrid"):
            hits = search(args.search, mode=mode)
            print(f"  {mode:7} " + "  ".join(f"{d.metadata['id']} {d.metadata['점검항목']}" for d in hits))
    if not (args.build or args.search):
        parser.print_help()
