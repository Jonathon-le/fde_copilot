import argparse
import json
import sys
import unicodedata

from app.rag.hybrid import CANDIDATE_FACTOR,MIN_CANDIDATES,rrf_fuse,search_hybrid
from app.rag.index import IndexEmptyError

def _key(hit:dict)->str :
    return f"{hit['doc_id']}::{hit['chunk_index']}"

def _label(hit:dict) ->str :
    section=hit["section"].lstrip("#").strip()
    return f"{hit['title'].removesuffix('.md')} / {section}"
def _lable_idx(hit:dict)->str :
    return f"{_label(hit)}#{hit['chunk_index']}"
#计算text占多少格
def _dwidth(text:str)-> int :
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
#控制输出格式，补空格凑满一列
def _pad(text:str,width:int)->str :
    return text +" "*max(0,width-_dwidth(text))
def build_parser() ->argparse.ArgumentParser :
    parser=argparse.ArgumentParser(
        prog="retrieval_compare",
        description="对比向量检索，BM25检索，RRF融合三路结果。",
    )
    parser.add_argument("query",help="要检索的问题")
    parser.add_argument("--top-k",type=int,default=3,help="展示条数,默认3")
    parser.add_argument("--json",action="store_true",help="输出机器可读JSON")
    return parser
def main(argv:list[str]|None=None)->int :
    parser=build_parser()
    args=parser.parse_args(argv)
    if not args.query.strip() :
        parser.error("问题不能为空")
    if args.top_k<1 :
        parser.error(f"--top_k必须>=1，当前{args.top_k}")
    candidate_k=max(args.top_k*CANDIDATE_FACTOR,MIN_CANDIDATES)
    try :
        vec_hits=search_hybrid(args.query,top_k=candidate_k,mode="vector")
        bm_hits=search_hybrid(args.query,top_k=candidate_k,mode="bm25")
    except IndexEmptyError as exc :
        print(f"错误:{exc}",file=sys.stderr)
        return 1
    vec_rank={_key(h):i for i,h in enumerate(vec_hits,start=1)}
    bm_rank={_key(h):i for i,h in enumerate(bm_hits,start=1)}
    fused=rrf_fuse([vec_hits,bm_hits],top_k=args.top_k)

    if args.json :
        payload={
            "query":args.query,
            "top_k":args.top_k,
            "candidate_k":candidate_k,
            "fused":[
                {
                    "rank": i,
                    "chunk":_label(h),
                    "doc_id":h["doc_id"],
                    "chunk_index":h["chunk_index"],
                    "rrf":round(h["score"],6),
                    "vector_rank":vec_rank.get(_key(h)),
                    "bm25_rank":bm_rank.get(_key(h)),

                }
                for i,h in enumerate(fused,start=1)
            ],
            "only_vector":[_lable_idx(h) for h in vec_hits if _key(h) not in bm_rank],
            "only_bm25":[_lable_idx(h) for h in bm_hits if _key(h) not in vec_rank],
            }
        print(json.dumps(payload,ensure_ascii=False,indent=2))
        return 0
    print(f"query:{args.query}  top_k={args.top_k}   候选池={candidate_k}")
    print()
    print(_pad("#",3)+_pad("chunk",36)+"RRF".rjust(9)+"向量".rjust(8)+"BM25".rjust(8))
    for i,hit in enumerate(fused,start=1) :
        key=_key(hit)
        print(
            _pad(str(i),3)
            +_pad(_label(hit),36)
            +f"{hit['score']:>9.5f}"
            +f"{vec_rank.get(key,'——'):>8}"
            +f"{bm_rank.get(key,'——'):>8}"
        )
    print()
    only_vec=[h for h in vec_hits if _key(h) not in bm_rank]
    only_bm=[h for h in bm_hits if _key(h) not in vec_rank]
    print("只被向量命中："+(";".join(_lable_idx(h) for h in only_vec)or "--"))
    print("只被BM25命中:"+(";".join(_lable_idx(h) for h in only_bm)or "--"))
    return 0

if __name__=="__main__" :
    sys.exit(main())