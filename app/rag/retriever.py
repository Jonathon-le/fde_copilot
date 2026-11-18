from app.rag import index, embedding
from app.rag.index import IndexEmptyError
def search_vector(query:str,*,top_k:int=4)->list[dict] :
    if top_k < 1:
        raise ValueError("top_k 必须 >= 1")

    collection=index.get_collection()
    if collection.count()==0 :
        raise IndexEmptyError("向量库为空,请先运行python -m scripts.build_index")
    vec=embedding.embed_query(query)
    result=collection.query(query_embeddings=[vec],n_results=top_k,include=["metadatas","documents","distances"])
    docs=result["documents"][0]
    metas=result["metadatas"][0]
    dists=result["distances"][0]
    hits = []
    for doc, meta, dist in zip(docs, metas, dists):
        hits.append({
            "text":         doc,
            "doc_id":       meta["doc_id"],
            "title":        meta["title"],
            "section":      meta["section"],
            "chunk_index":  meta["chunk_index"],
            "source_path":  meta["source_path"],
            "score":        1 - dist,        # ← 距离换成分数
        })

    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits
def search(query:str,*,top_k:int=4)->list[dict]:
    return search_vector(query,top_k=top_k)
