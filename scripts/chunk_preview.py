import argparse
import sys
from pathlib import Path
from app.rag.loaders import load_document
from app.rag.chunking import split_markdown_sections,chunk_section,chunk_document
def main() :
    parser=argparse.ArgumentParser(description="文档分块预览CLI工具")
    parser.add_argument("dir",nargs="?",default=Path(__file__).resolve().parents[1]/"data"/"knowledge",help="知识库目录")
    parser.add_argument("--json",action="store_true",help="输出JSONL格式")
    args=parser.parse_args()
    root=Path(args.dir)
    if not root.exists():
        print(f"目录不存在：{root}", file=sys.stderr)
        sys.exit(1)
    if not root.is_dir():
        print(f"不是目录：{root}", file=sys.stderr)
        sys.exit(1)
    docs=load_document(str(root))
    if not docs:
        print(f"目录中没有可加载的 .md / .txt 文档：{root}", file=sys.stderr)
        sys.exit(1)
    all_chunks=[]
    if args.json :        
        import json
        for doc in docs :
                 part_chunk=chunk_document(doc,350,60)
                 all_chunks.extend(part_chunk)
        if not all_chunks:
                 print("文档已加载，但没有切出任何 chunk", file=sys.stderr)
                 sys.exit(1)
        print(f"文档数量：{len(docs)}", file=sys.stderr)
        print(f"总chunk数:{len(all_chunks)}", file=sys.stderr)

        for doc in docs :
                chunks=[]
                sections=split_markdown_sections(doc["text"])
                for section in sections :
                        section_chunk=chunk_section(section["section_text"],chunk_size=350,overlap=60)
                        for chunk in section_chunk :
                            if chunk.strip() :
                                    chunks.append(chunk.strip())
                print(f"{doc['doc_id']}的chunk数目:{len(chunks)}", file=sys.stderr)

        for item in all_chunks :
            print(json.dumps(item,ensure_ascii=False))
    else :
            for doc in docs :
                    part_chunk=chunk_document(doc,350,60)
                    all_chunks.extend(part_chunk)  
            if not all_chunks:
                print("文档已加载，但没有切出任何 chunk")
                sys.exit(1)
            print(f"文档数量：{len(docs)}")
            print(f"总chunk数:{len(all_chunks)}")
            for doc in docs :
                    chunks=[]
                    sections=split_markdown_sections(doc["text"])
                    for section in sections :
                        section_chunk=chunk_section(section["section_text"],chunk_size=350,overlap=60)
                        for chunk in section_chunk :
                            if chunk.strip() :
                                chunks.append(chunk)
                    print(f"{doc["doc_id"]}的chunk数目:{len(chunks)}")
            preview_num=min(3,len(all_chunks))
            for i in range(preview_num) :
                print(f"section:{all_chunks[i]["section"]} chunk_index:{all_chunks[i]["chunk_index"]}\n===chunk:{all_chunks[i]["doc_id"]} preview ===\n{all_chunks[i]["text"][:100:]}")
if __name__=="__main__" :
     main()
    

