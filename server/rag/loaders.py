from langchain_community.document_loaders import PyPDFLoader

def load_pdf(pdf_path: str):
    loader = PyPDFLoader(pdf_path)
    docs = loader.load()

    if not docs:
        raise ValueError("PDF contains no extractable text")

    return docs
