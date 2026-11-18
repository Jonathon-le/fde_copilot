"""把检索结果和问题拼成 chat 接口要的 messages。

这一层只做字符串组装：不读文件、不查向量库、不调大模型。
"""

DEFAULT_SYSTEM = """你是企业客服知识库助手，只能依据【资料】回答。

规则：
1. 只使用【资料】中的内容回答，不补充资料之外的知识、常识或推测。
2. 如果【资料】中没有相关信息，只回复「资料中没有提到这个问题。」，不要猜测。
3. 每个结论后标注来源，格式为 [编号]，编号对应【资料】中的片段序号。
4. 资料之间互相矛盾时，指出矛盾并分别引用，不要擅自选择其中一个。
5. 使用中文，先给结论再给依据，不超过 200 字。"""

EMPTY_HINT = "（本次没有检索到任何资料）"


def format_user(question: str, hits: list[dict]) -> str:
    """把资料片段和问题拼成 user 的内容。

    hits 为空时也要返回合法文本，让上层自己决定是调模型还是直接拒答。
    """
    if not hits:
        return f"资料：\n{EMPTY_HINT}\n\n问题：{question}"

    blocks = []
    for i, hit in enumerate(hits, start=1):
        # 编号从 1 开始，和 system 里要求的「引用 [编号]」完全一致
        blocks.append(f"[{i}] （{hit['title']} / {hit['section']}）\n{hit['text'].strip()}")

    # 块之间空一行：长文本里空行是模型唯一的分段信号
    body = "\n\n".join(blocks)
    return f"资料：\n{body}\n\n问题：{question}"


def build_messages(
    question: str,
    hits: list[dict],
    *,
    system: str | None = None,
) -> list[dict]:
    """返回 [{"role": "system", ...}, {"role": "user", ...}]。"""
    # system 是「补充要求」，不是「整体替换」：
    # 一旦整体替换，上面的硬约束会被静默删掉，回答开始编也看不出来。
    if system is None:
        sys_prompt = DEFAULT_SYSTEM
    else:
        sys_prompt = f"{DEFAULT_SYSTEM}\n\n补充要求：{system}"

    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": format_user(question, hits)},
    ]
