from pathlib import Path
import sys
from typing import List,Dict
def load_document(root_dir:str) ->List[Dict] :
    root=Path(root_dir)#先把传入的根目录字符串转化成pathlib对象，便于后续调用方法
    docs=[]#用于装填所有文档字典
    #rglob会递归查找root文件夹以及所有嵌套的子文件夹，后面为匹配模式，返回为生成器需要用List（）转为列表
    file_list=sorted(list(root.rglob("*.md"))+list(root.rglob("*.txt")))
    for file in file_list : #遍历查找到的所有文件
        try :
             text=file.read_text(encoding="UTF-8")
             if text.strip()=="" : #说明为空文件
                 print(f"[SKIP]空文件:{file}",file=sys.stderr)
                 continue #遍历下一个文件
             else :
                 doc={#每个文档的一个“简历”
                     "doc_id":file.stem,#文件名去掉后缀
                     "title":file.name,#带后缀文件名
                     "path":str(file),#文件完整路径字符串
                     "text":text
                 }
                 docs.append(doc)#加入到dosc中保存                 
        except Exception as e :
            print(f"[Error]{file}{e}")
    return docs #返回加载完成后生成的内容



