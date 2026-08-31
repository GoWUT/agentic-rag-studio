import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool


load_dotenv()


@tool
def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


def main():
    """Run this network smoke test explicitly, not during test discovery."""
    llm = ChatOpenAI(
        model="deepseek-v4-flash",
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
        temperature=0,
    )
    llm_with_tools = llm.bind_tools([multiply])
    response = llm_with_tools.invoke("请使用工具计算 123 × 456")
    print(response)
    print("Tool Calls:")
    print(response.tool_calls)


if __name__ == "__main__":
    main()
