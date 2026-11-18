import functools
import chromadb
from app.config import get_settings
from app.rag import embedding
class IndexEmptyError(RuntimeError):
    """向量库为空，需要先运行build_index""" 
COLLECTION_NAME="knowledge"
BATCH_SIZE=64
@functools.lru_cache(maxsize=1)
def get_client() :
    client=chromadb.PersistentClient(path=str(get_settings().chroma_dir))
    return client

def get_collection() :
    collection=get_client().get_or_create_collection(
        name=COLLECTION_NAME,
       #告诉chroma用余弦距离计算相似度
        metadata={"hnsw:space":"cosine"},
    )
    return collection


def build_index(chunks:list[dict],*,reset:bool=False)->int :
    if not chunks :
        return 0
   
    if reset :
        try :
            get_client().delete_collection(COLLECTION_NAME)
        except chromadb.errors.NotFoundError :
            pass

    collection=get_collection()
    total=0
    for i in range(0,len(chunks),BATCH_SIZE) :
        batch=chunks[i:i+BATCH_SIZE]
        ids=[f"{c['doc_id']}::{c['chunk_index']}" for c in batch]
        documents=[c["text"] for c in batch]
        metadatas=[
            {
                "doc_id":c["doc_id"],
                "title":c["title"],
                "section":c["section"],
                "chunk_index":c["chunk_index"],
                "source_path":c["source_path"]
            }
            for c in batch
        ]
        embeddings=embedding.embed_texts(documents)
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        total+=len(batch)
    return total

            
            
