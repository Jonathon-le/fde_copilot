
import re
#先用正则匹配，把text按标题分为多个section（匹配#开头的标题，捕获标题文字）
def split_markdown_sections(text:str)->list[dict] :
        pattern = re.compile(r'(^#{1,3}\s+.+)', re.MULTILINE)#用compile编写的正则对象
        sections_list:list[str]=pattern.split(text)
        sections:list[dict]=[] #用于存储每个section的{section_title,section_text}
        pre_content=sections_list[0] #先处理前置内容
        if pre_content.split() :
                sections.append({"section_title":"前置内容","section_text":pre_content})
        #将后续的内容整理成字典{章节标题：章节内容}，存放在sections里
        for sec_title,sec_body in zip(sections_list[1::2],sections_list[2::2]) :
                sections.append({"section_title":sec_title,"section_text":sec_body})    
        return sections
def chunk_section(section_text:str,chunk_size=350,overlap=60) : #按长度对section切块
        start=0
        chunks:list[str]=[]#存放切块内容
        while start<len(section_text) :
                end=start+chunk_size
                if end >=len(section_text) :
                        chunks.append(section_text[start::])
                        break
                else : 
                      
                      for i in range(end,min(end+80,len(section_text))) :#索引保护
                              if section_text[i] in "！。？!\n" : #寻找距离最近的句子结束符
                                    end=i+1
                                    break
                      chunks.append(section_text[start:end:])            
                              
                start=end-overlap#每个chunk保留重叠
        return chunks
def chunk_document(doc:dict,chunk_size=350,overlap=60) :
        chunk_list=[]
        sections:list[dict]=split_markdown_sections(doc["text"])
        chunk_index=0
        for section in sections :
                chunks=chunk_section(section["section_text"],chunk_size,overlap)
                for chunk in chunks :
                    if chunk.strip() :    
                        chunk_list.append({"doc_id":doc["doc_id"],"title":doc["title"],"section":section["section_title"],"chunk_index":chunk_index,"text":chunk.strip(),"source_path":doc["path"]})
                        chunk_index+=1
        return chunk_list

                
            

        

