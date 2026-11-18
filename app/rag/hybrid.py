RRF_K=60
CANDIDATE_FACTOR=3
MIN_CANDIDATES=20
from app.rag.retriever import search_vector 
from app.rag.bm25_index import search_bm25
def hit_key(hit)->str :
    return f"{hit['doc_id']}::{hit['chunk_index']}"


def rrf_fuse(ranked_lists:list[list[dict]],*,k=RRF_K,top_k=4)->list[dict] :
    fused={}
    for hits in ranked_lists :
        for rank,hit in enumerate(hits,start=1) :
            key=hit_key(hit)

            if key not in fused :
                fused[key]=dict(hit)
                fused[key]["rrf"]=0.0
            fused[key]["rrf"]+=1.0/(k+rank)
    ordered=sorted(fused.values(),key=lambda h:h["rrf"],reverse=True)[:top_k]
    for h in ordered :
        h["score"]=h.pop("rrf")
    return ordered
def search_hybrid(query,*,top_k=4,mode="hybrid")->list[dict] :
    if mode=="vector" :
        return search_vector(query,top_k=top_k)
    if mode=="bm25" :
        return search_bm25(query,top_k=top_k)
    if mode!="hybrid":
        raise ValueError(f"未知的mode:{mode!r}")
    candidate_k=max(top_k*CANDIDATE_FACTOR,MIN_CANDIDATES)
    vec_hits=search_vector(query,top_k=candidate_k)
    bm25_hits=search_bm25(query,top_k=candidate_k)
    return rrf_fuse([vec_hits,bm25_hits],top_k=top_k)