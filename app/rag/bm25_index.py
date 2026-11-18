import functools
import numpy as np
from rank_bm25 import BM25Okapi
from app.rag.index import IndexEmptyError,get_collection
from app.rag.tokenizer import tokenize,tokenize_many
@functools.lru_cache(maxsize=1)
def _load() :
    collection=get_collection()
    if collection.count()==0 :
        raise IndexEmptyError("向量库为空，请先运行 python -m scripts.build_index")
    datas=collection.get(include=["documents","metadatas"])
    corpus=[f"{m['section']}\n{t}"for m,t in zip(datas["metadatas"],datas["documents"])]
    bm25=BM25Okapi(tokenize_many(corpus))
    chunks=[]
    for text,meta in zip(datas["documents"],datas["metadatas"]) :
            chunks.append(
                 {"text" :text,
                  "doc_id":meta["doc_id"],
                  "title" : meta["title"],
                  "section" : meta["section"],
                  "chunk_index" :meta["chunk_index"],
                  "source_path" :meta["source_path"],
                  "score":0.0
                                                    }
            )
    return bm25,chunks
def search_bm25(query:str,*,top_k:int=4)->list[dict] :
    bm25,chunks=_load()
    tokenize_query=tokenize(query)
    scores=bm25.get_scores(tokenize_query)
    hits=[]
    for i in np.argsort(-scores):
        if scores[i]<=0 :
            break
        hit=dict(chunks[int(i)])
        hit["score"]=float(scores[i])
        hits.append(hit)
        if len(hits)>=top_k :
            break
    return hits

