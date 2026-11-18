import os
from pathlib import Path
PROJECT_ROOT=(Path(__file__).resolve().parent).parent
class Settings :
      
      def __init__(self):
            self.llm_BASE_URL=os.getenv("LLM_BASE_URL","https://api.deepseek.com/v1")
            self.llm_MODEL=os.getenv("LLM_MODEL","deepseek-chat")
            self.llm_API_KEY=os.getenv("LLM_API_KEY","")
            self.llm_timeout=float(os.getenv("LLM_TIMEOUT","30"))
            self.llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS","512"))
      @property
      def data_dir(self) :
            return PROJECT_ROOT/"data"
      @property
      def db_path(self) :
            return PROJECT_ROOT/"data"/"app.db"  
      @property
      def chroma_dir(self) :
            return PROJECT_ROOT/"data"/"chroma"
      @property
      def knowledge_dir (self):
            return PROJECT_ROOT/"data"/"knowledge"
      @property
      def mock_mode(self) :
            return  not self.llm_API_KEY.strip()
      def ensure_dirs(self) :
                for dirs in (self.data_dir,self.chroma_dir,self.knowledge_dir) :
                        dirs.mkdir(parents=True,exist_ok=True)
def get_settings() :
       return Settings()