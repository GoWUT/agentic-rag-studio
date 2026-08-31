from langchain_core.tools import tool
from langchain_community.utilities import GoogleSerperAPIWrapper
from langchain_community.retrievers import ArxivRetriever
import requests


def format_pdf_hits(docs):
    lines = ["PDF RAG Results:"]
    for i, d in enumerate(docs, 1):
        snippet = d.page_content.replace("\n", " ")[:400]
        lines.append(f"{i}. (page {d.metadata.get('page')}) {snippet}")
    return "\n".join(lines)


def make_pdf_tool(retriever):
    @tool("search_pdf")
    def search_pdf(query: str) -> str:
        """Search the uploaded PDF and return relevant chunks."""
        hits = retriever.invoke(query)
        return format_pdf_hits(hits)

    return search_pdf


def make_web_tool(serper_api_key: str):
    serper = GoogleSerperAPIWrapper(serper_api_key=serper_api_key)

    @tool("search_web")
    def search_web(query: str) -> str:
        """Search the web using Serper."""
        try:
            results = serper.results(query)
        except requests.HTTPError as error:
            status_code = (
                error.response.status_code
                if error.response is not None
                else "unknown"
            )
            return (
                "WEB_SEARCH_UNAVAILABLE: "
                f"Serper returned HTTP {status_code}. "
                "Check SERPER_API_KEY, account permissions, and quota. "
                "Do not fabricate web search results; tell the user that "
                "web search is currently unavailable."
            )
        except requests.RequestException:
            return (
                "WEB_SEARCH_UNAVAILABLE: Could not connect to Serper. "
                "Do not fabricate web search results; tell the user that "
                "web search is currently unavailable."
            )

        organic = results.get("organic", [])
        out = ["Web Search Results:"]
        for r in organic[:5]:
            out.append(f"- {r.get('title')}: {r.get('snippet')}")
        return "\n".join(out)

    return search_web


def make_arxiv_tool():
    arxiv = ArxivRetriever(max_results=3)

    @tool("search_arxiv")
    def search_arxiv(query: str) -> str:
        """Search arXiv for scientific papers."""
        papers = arxiv.invoke(query)
        out = ["arXiv Results:"]
        for p in papers:
            out.append(p.metadata.get("title", ""))
        return "\n".join(out)

    return search_arxiv


def build_tools(retriever, serper_api_key: str):
    tools = []

    if retriever:
        tools.append(make_pdf_tool(retriever))

    tools.append(make_web_tool(serper_api_key))
    tools.append(make_arxiv_tool())

    return tools
