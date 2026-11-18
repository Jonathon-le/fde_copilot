import sys
import importlib.util
from app import config
print(sys.version,sys.version_info,sys.executable)
moudles=("fastapi","openai","chromadb","sentence_transformers","streamlit","jieba","rank_bm25","pypdf","httpx")
moudle_dict={}
for a_moudle in moudles :
    spec=importlib.util.find_spec(a_moudle)
    if not spec :
        moudle_dict[a_moudle]="[MISSING]"
    else :
        moudle_dict[a_moudle]="[OK]"
print(moudle_dict)
a_set=config.get_settings()
print(f"base URL:{a_set.llm_BASE_URL},model_name:{a_set.llm_MODEL}")
if(a_set.llm_API_KEY.strip()) :
    print(f"API_KEY:已配置")
else:
    print(f"API_KEY:未配置")
print(f"mock:{a_set.mock_mode}")
print(f"data_dir:{a_set.data_dir},db_path:{a_set.db_path},chroma_dir:{a_set.chroma_dir},knowledge_dir:{a_set.knowledge_dir}")
a_set.ensure_dirs()
for path in (a_set.data_dir, a_set.chroma_dir, a_set.knowledge_dir):
    print(f"{path}: {'存在' if path.exists() else '不存在'}")

for moudle_check in moudle_dict :
    if moudle_dict[moudle_check]=="[MISSING]" :
        sys.exit(1)
