import jieba
STOPWORDS=["的","了","是","在","和","有","就","也","都","要","会","吗","呢","啊","吧","我们","你们","他们","可以","一个","什么","怎么","多少"]
def tokenize(text:str) :
    tokens=[]
    for word in jieba.lcut(text.lower()) :
        if not word :#没有词跳过
            continue
        if word in STOPWORDS :#有停止词跳过
            continue
        if not any(ch.isalnum() for ch in word ): #全是标点跳过
            continue
        tokens.append(word)#其他加入检索词
    return tokens
def tokenize_many(texts:list[str]) ->list[list[str]]:
    return [tokenize(text) for text in texts]
