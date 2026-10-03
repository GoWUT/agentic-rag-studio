from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_community.utilities import GoogleSerperAPIWrapper
from langchain_community.retrievers import ArxivRetriever
import requests
from server.agent.evidence import evidence_from_hit, external_evidence


def format_pdf_hits(docs):
    lines = ["PDF RAG Results:"]
    for i, d in enumerate(docs, 1):
        snippet = d.page_content.replace("\n", " ")[:400]
        lines.append(f"{i}. (page {d.metadata.get('page')}) {snippet}")
    return "\n".join(lines)


def make_pdf_tool(retriever, *, structured: bool = False):
    @tool("search_pdf")
    def search_pdf(query: str):
        """Search the uploaded PDF and return relevant chunks."""
        hits = retriever.invoke(query)
        if structured:
            return {"evidence": [evidence_from_hit(hit).model_dump() for hit in hits]}
        return format_pdf_hits(hits)

    return search_pdf


def make_web_tool(serper_api_key: str, *, structured: bool = False):
    serper = GoogleSerperAPIWrapper(serper_api_key=serper_api_key)

    @tool("search_web")
    def search_web(query: str):
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
        if structured:
            return {"evidence": [external_evidence("web", r.get("title", ""), r.get("snippet", ""), r.get("link")) for r in organic[:5]]}
        out = ["Web Search Results:"]
        for r in organic[:5]:
            out.append(f"- {r.get('title')}: {r.get('snippet')}")
        return "\n".join(out)

    return search_web


def make_arxiv_tool(*, structured: bool = False):
    arxiv = ArxivRetriever(max_results=3)

    @tool("search_arxiv")
    def search_arxiv(query: str):
        """Search arXiv for scientific papers."""
        papers = arxiv.invoke(query)
        if structured:
            return {"evidence": [external_evidence("arxiv", p.metadata.get("Title", p.metadata.get("title", "")), p.page_content, p.metadata.get("entry_id", p.metadata.get("Entry ID"))) for p in papers]}
        out = ["arXiv Results:"]
        for p in papers:
            out.append(p.metadata.get("title", ""))
        return "\n".join(out)

    return search_arxiv


def make_workspace_tool(retriever):
    @tool("search_workspace")
    def search_workspace(query: str, config: RunnableConfig, document_ids: list[str] | None = None) -> dict:
        """Search all documents in the current workspace for supporting evidence."""
        coverage_query = config.get("configurable", {}).get("research_query", query)
        options = {'document_ids':document_ids} if document_ids is not None else {}
        return {"evidence": [evidence_from_hit(hit, "workspace").model_dump() for hit in retriever.invoke(query, coverage_query=coverage_query, **options)]}
    return search_workspace


def build_tools(retriever, serper_api_key: str, *, workspace_retriever=None, structured: bool = False):
    tools = []

    if retriever:
        tools.append(make_pdf_tool(retriever, structured=structured))

    if workspace_retriever is not None:
        tools.append(make_workspace_tool(workspace_retriever))

    tools.append(make_web_tool(serper_api_key, structured=structured))
    tools.append(make_arxiv_tool(structured=structured))

    return tools
