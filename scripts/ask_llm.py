import argparse
import sys

from app.llm.client import LLMError, chat

DEFAULT_SYSTEM="你是一个简洁，准确的中文客服助手。"

def build_parser()->argparse.ArgumentParser:
    parser=argparse.ArgumentParser(
        prog="ask_llm",
        description="向大模型提一个问题并打印回答。"
        )
    #question必填，前面不加--，为位置参数默认必填，其余的可通过required来控制
    parser.add_argument("question",help="要问的问题")
    parser.add_argument("--system",default=DEFAULT_SYSTEM,help="system prompt" )
    parser.add_argument("--temperature",type=float,default=0.2,help="0.0~2.0,越大越发散")
    parser.add_argument("--max-tokens",type=int,default=None,help="输出上限,默认取配置LLM_MAX_TOKENS")
    #用于测试调用，加--mock不走真是chat接口，不加则调用llm大模型
    parser.add_argument("--mock",action="store_true",help="不联网，打印固定答案")
    parser.add_argument("--show-usage",action="store_true",help="把token用量打印到stderr")
    return parser


def main(argv:list[str]|None=None)->int :
    parser=build_parser()
    args=parser.parse_args(argv)

    if not args.question.strip() :
        parser.error("问题不能为空")
        
    if not 0.0<=args.temperature<=2.0 :
        parser.error(f"--temperature必须在0.0~2.0之间，当前{args.temperature}")
    
    if args.mock:
        print(f"[mock]收到问题：{args.question}")
        print("你好，这是mock回答")
        return 0
    messages=[    
        {"role": "system", "content": args.system},
        {"role": "user", "content": args.question}
        ]
    try :
        result=chat(message=messages,temperature=args.temperature,max_tokens=args.max_tokens)
    except LLMError as exc :
        print(f"错误：{exc}",file=sys.stderr)
        return 1
    print(result["content"])
    if args.show_usage and result.get("usage") :
        print(f"usage:{result['usage']}",file=sys.stderr)
    return 0
if __name__=="__main__" :
    sys.exit(main())

