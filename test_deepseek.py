import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


load_dotenv()


def main():
    """Run this network smoke test explicitly, not during test discovery."""
    llm = ChatOpenAI(
        model="deepseek-v4-flash",
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )
    response = llm.invoke("你好，请回复：DeepSeek API 测试成功")
    print(response.content)


if __name__ == "__main__":
    main()
