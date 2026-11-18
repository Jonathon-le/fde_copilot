from pathlib import Path
import functools
from sentence_transformers import SentenceTransformer
MODEL_NAME="BAAI/bge-small-zh-v1.5"
PROJECT_ROOT=Path(__file__).resolve().parents[2]
MODEL_DIR=PROJECT_ROOT/"models"/"bge-small-zh-v1.5"
QUERY_PREFIX="为这个句子生成表示以用于检索相关文章："
#functools的lru_cache(maxsize=1)方法限制模型只加载一次
@functools.lru_cache(maxsize=1)
def get_model() ->SentenceTransformer :
    if not MODEL_DIR.exists() :
        raise FileNotFoundError(f"模型目录不存在：{MODEL_DIR}")
    return SentenceTransformer(str(MODEL_DIR))


def get_model_name():
    return MODEL_NAME


#批量把文本转为向量
def embed_texts(texts:list[str])->list[list[float]] :
    if not texts :
        return []
    vecs=get_model().encode(
        texts,
        normalize_embeddings=True,#归一化
        show_progress_bar=False,
    )
    #model.encode返回numpy数组，jason序列化会报ndarray is not json serializable
    return vecs.tolist()


#把查询转为向量
def embed_query(text:str)->list[float]:
    vec=get_model().encode(
        QUERY_PREFIX+text,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return vec.tolist()