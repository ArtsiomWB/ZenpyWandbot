import abc
import json
import logging
import pathlib
from collections import defaultdict
from typing import Dict, List, Optional

import joblib
import scipy.sparse
import tiktoken
import wandb
from langchain.document_loaders import (
    TextLoader,
    UnstructuredMarkdownLoader,
    UnstructuredPowerPointLoader,
    UnstructuredWordDocumentLoader,
)
from langchain.embeddings import OpenAIEmbeddings
from langchain.embeddings.base import Embeddings
from langchain.schema import Document
from langchain.text_splitter import TextSplitter, TokenTextSplitter
from llama_index import Document as LlamaDocument
from sklearn.feature_extraction.text import TfidfVectorizer
from tqdm import tqdm
from wandbot.chat.langchain111 import (
    ChromaWithEmbeddingsAndScores,
    HybridRetriever,
    TFIDFRetrieverWithScore,
    WandbNotebookLoader,
)
from wandbot.ingestion.config import DataStoreConfig, VectorIndexConfig
from wandbot.ingestion.utils import add_metadata_to_documents, fetch_git_repo, md5_dir

logger = logging.getLogger(__name__)

from langchain.text_splitter import Language, RecursiveCharacterTextSplitter


class DocumentStore:
    docs = {}
    ref_doc_info = defaultdict(lambda: dict())

    def __init__(self, docs: dict, ref_doc_info: defaultdict[lambda: dict]):
        self.docs = docs
        self.ref_doc_info = ref_doc_info

    def to_dict(self):
        docs = {}
        for k, v in self.docs.items():
            docs[k] = v.to_dict()
        return {"docs": docs, "ref_doc_info": dict(self.ref_doc_info)}

    @classmethod
    def from_dict(cls, doc_store_dict):
        docs = {}
        for k, v in doc_store_dict["docs"].items():
            docs[k] = LlamaDocument.from_dict(v)
        return cls(docs=docs, ref_doc_info=doc_store_dict["ref_doc_info"])


class DataStore:
    document_store: DocumentStore = None

    def __init__(self, config: DataStoreConfig):
        self.config = config
        self.md_text_splitter: TextSplitter = (
            RecursiveCharacterTextSplitter.from_language(
                language=Language.MARKDOWN,
                chunk_size=self.config.chunk_size * 3,
                chunk_overlap=self.config.chunk_overlap,
            )
        )
        self.python_text_splitter: TextSplitter = (
            RecursiveCharacterTextSplitter.from_language(
                language=Language.PYTHON,
                chunk_size=self.config.chunk_size * 3,
                chunk_overlap=self.config.chunk_overlap,
            )
        )
        self.js_text_splitter: TextSplitter = (
            RecursiveCharacterTextSplitter.from_language(
                language=Language.JS,
                chunk_size=self.config.chunk_size * 3,
                chunk_overlap=self.config.chunk_overlap,
            )
        )

        self.go_text_splitter: TextSplitter = (
            RecursiveCharacterTextSplitter.from_language(
                language=Language.GO,
                chunk_size=self.config.chunk_size * 3,
                chunk_overlap=self.config.chunk_overlap,
            )
        )
        self.html_text_splitter: TextSplitter = (
            RecursiveCharacterTextSplitter.from_language(
                language=Language.HTML,
                chunk_size=self.config.chunk_size * 3,
                chunk_overlap=self.config.chunk_overlap,
            )
        )
        self.token_splitter: TextSplitter = TokenTextSplitter(
            encoding_name=self.config.encoding_name,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            allowed_special={"<|endoftext|>"},
        )

    def load_docstore_metadata(
        self,
    ):
        data_source = self.config.data_source
        if data_source.is_git_repo:
            doc_store_metadata = fetch_git_repo(data_source, data_source.git_id_file)
        else:
            doc_store_metadata = {
                "commit_hash": md5_dir(
                    data_source.local_path, file_pattern=data_source.file_pattern
                )
            }
        doc_store_metadata["source_name"] = self.config.name
        return doc_store_metadata

    def make_documents_tokenization_safe(self, documents):
        encoding = tiktoken.get_encoding(self.config.encoding_name)
        special_tokens_set = encoding.special_tokens_set

        def remove_special_tokens(text):
            for token in special_tokens_set:
                text = text.replace(token, "")
            return text

        cleaned_documents = []
        for document in documents:
            document = Document(
                page_content=remove_special_tokens(document.page_content),
                metadata=document.metadata,
            )
            cleaned_documents.append(document)
        return cleaned_documents

    def create_docstore_from_documents(
        self,
        documents: List[Document],
        source_map: Optional[Dict[str, str]] = None,
        metadata: Dict[str, str] = None,
    ) -> DocumentStore:
        documents = self.make_documents_tokenization_safe(documents)
        documents = self.token_splitter.split_documents(documents)
        documents = add_metadata_to_documents(documents, source_map)
        llama_documents: Dict[str, LlamaDocument] = {}
        for doc in documents:
            llama_documents[doc.metadata["doc_id"]] = LlamaDocument(
                text=doc.page_content,
                extra_info=doc.metadata,
                doc_id=doc.metadata["doc_id"],
            )
        if metadata is None:
            metadata = {}
        metadata["num_docs"] = len(llama_documents)
        return DocumentStore(docs=llama_documents, ref_doc_info={"metadata": metadata})

    @abc.abstractmethod
    def load_docstore(self) -> DocumentStore:
        raise NotImplementedError("Implement this in the subclass")

    def load(self) -> DocumentStore:
        docstore = self.load_docstore()
        return docstore


class DocumentationDataStore(DataStore):
    def load_docstore(
        self,
    ) -> DocumentStore:
        metadata = self.load_docstore_metadata()

        local_paths = (
            self.config.data_source.local_path / self.config.data_source.base_path
        ).rglob(self.config.data_source.file_pattern)
        dir_name = self.config.data_source.local_path.stem

        path_parts = map(lambda x: x.parts, local_paths)
        path_parts = list(filter(lambda x: len(x) > 2, path_parts))
        doc_indices = list(map(lambda x: x.index(dir_name), path_parts))

        local_paths = map(lambda x: str(pathlib.Path(*x)), path_parts)

        link_paths = map(
            lambda x: str(pathlib.Path(*x[1][(x[0] + 2) :]))[:-3],
            zip(doc_indices, path_parts),
        )
        link_paths = map(lambda x: x.replace("/other", ""), link_paths)
        link_paths = map(lambda x: x.replace("/intro", ""), link_paths)
        link_paths = map(lambda x: x.replace("/README", ""), link_paths)

        link_paths = map(
            lambda x: f"{self.config.data_source.remote_path}{x}", link_paths
        )

        document_files = dict(zip(local_paths, link_paths))

        documents = []
        for f_name in tqdm(
            document_files,
            desc=f"Loading documentation from {self.config.data_source.local_path}",
        ):
            try:
                documents.extend(UnstructuredMarkdownLoader(f_name).load())
            except:
                logger.warning(f"Failed to load documentation {f_name}")
        document_sections = self.md_text_splitter.split_documents(documents)

        document_store = self.create_docstore_from_documents(
            document_sections, document_files, metadata
        )
        return document_store


class CodeDataStore(DataStore):
    def load_docstore(self) -> DocumentStore:
        metadata = self.load_docstore_metadata()

        local_paths = (
            self.config.data_source.local_path / self.config.data_source.base_path
        ).rglob(self.config.data_source.file_pattern)

        paths = list(local_paths)
        local_paths = list(map(lambda x: str(x), paths))
        local_path_parts = list(map(lambda x: x.parts, paths))
        examples_idx = list(
            map(
                lambda x: x.index(self.config.data_source.local_path.stem),
                local_path_parts,
            )
        )
        remote_paths = list(
            map(
                lambda x: "/".join(x[1][x[0] + 1 :]),
                zip(examples_idx, local_path_parts),
            )
        )
        remote_paths = list(
            map(
                lambda x: f"{self.config.data_source.remote_path}{x}",
                remote_paths,
            )
        )
        document_files = dict(zip(local_paths, remote_paths))

        documents = []
        for f_name in tqdm(
            document_files,
            desc=f"Loading code from {self.config.data_source.local_path}",
        ):
            try:
                if self.config.data_source.file_pattern == "*.ipynb":
                    document = WandbNotebookLoader(
                        f_name,
                        include_outputs=False,
                        max_output_length=0,
                        remove_newline=True,
                    ).load()
                else:
                    document = TextLoader(f_name).load()
                if f_name.endswith(".py") or f_name.endswith(".ipynb"):
                    document = self.python_text_splitter.split_documents(document)
                elif f_name.endswith(".md"):
                    document = self.md_text_splitter.split_documents(document)
                elif (
                    f_name.endswith(".js")
                    or f_name.endswith(".cjs")
                    or f_name.endswith(".ts")
                    or f_name.endswith(".tsx")
                ):
                    document = self.js_text_splitter.split_documents(document)
                elif f_name.endswith(".go"):
                    document = self.go_text_splitter.split_documents(document)
                elif f_name.endswith(".html"):
                    document = self.html_text_splitter.split_documents(document)
                elif f_name.endswith(".sh"):
                    document = self.token_splitter.split_documents(document)

                else:
                    raise ValueError(f"Unknown file extension")
                documents.extend(document)
            except Exception as e:
                logger.warning(f"Failed to load code in {f_name} with error {e}")
        document_sections = self.token_splitter.split_documents(documents)
        document_store = self.create_docstore_from_documents(
            document_sections, document_files, metadata
        )
        return document_store


class ExtraDataStore(DataStore):
    def load_docstore(self) -> DocumentStore:
        metadata = self.load_docstore_metadata()

        jsonl_paths = (
            self.config.data_source.local_path / self.config.data_source.base_path
        ).rglob(self.config.data_source.file_pattern)

        all_documents = []
        for path in tqdm(
            jsonl_paths,
            desc=f"Loading extra data from {self.config.data_source.local_path}",
        ):
            for line in path.open("r"):
                doc = json.loads(line)
                document = Document(
                    page_content=doc["document"], metadata={"source": doc["source"]}
                )
                all_documents.append(document)
        document_sections = self.md_text_splitter.split_documents(all_documents)
        document_store = self.create_docstore_from_documents(
            document_sections, None, metadata
        )
        return document_store


class GTMDataStore(DataStore):
    def load_docstore(
        self,
    ):
        metadata = self.load_docstore_metadata()

        document_paths = (
            self.config.data_source.local_path / self.config.data_source.base_path
        ).rglob(self.config.data_source.file_pattern)
        document_paths = list(document_paths)
        document_paths = list(map(lambda x: str(x), document_paths))
        all_documents = []
        for path in document_paths:
            if path.endswith(".docx"):
                document = UnstructuredWordDocumentLoader(path).load()[0]
            elif path.endswith(".pptx"):
                document = UnstructuredPowerPointLoader(path).load()[0]
            else:
                raise ValueError(f"Unsupported file type {path}")
            all_documents.append(document)
        document_sections = self.token_splitter.split_documents(all_documents)
        document_store = self.create_docstore_from_documents(
            document_sections, None, metadata
        )
        return document_store


class VectorIndex:
    def __init__(self, config: VectorIndexConfig):
        self.config = config

        self.embedding_fn: Embeddings = OpenAIEmbeddings()
        self.datastore: DocumentStore | None = None
        self.retriever: HybridRetriever | None = None
        self.wandb_run: wandb.sdk.wandb_run.Run | None = None
        self.saved_artifact: wandb.Artifact | None = None

    def load_datastore(self, sources: List[DataStore]):
        datastore = {"metadata": {}, "docs": {}}
        for source in sources:
            data_dict = source.load()
            datastore["docs"] = dict(**datastore.get("docs", {}), **data_dict.docs)
            datastore["metadata"][
                data_dict.ref_doc_info["metadata"]["source_name"]
            ] = dict(
                **data_dict.ref_doc_info["metadata"],
                **{
                    "config": json.loads(
                        source.config.json(exclude={"data_source": {"git_id_file"}})
                    )
                },
            )
        datastore = DocumentStore(
            docs=datastore["docs"], ref_doc_info={"metadata": datastore["metadata"]}
        )
        return datastore

    def get_docs_list(
        self,
    ):
        assert self.datastore is not None
        docs_list = []
        for doc_id, document in sorted(self.datastore.docs.items(), key=lambda x: x[0]):
            docs_list.append(document.to_langchain_format())
        return docs_list

    def create_dense_retriever(self, datastore: DocumentStore):
        if self.config.vectorindex_dir.is_dir():
            logger.debug(
                f"{self.config.vectorindex_dir} was found, loading existing vector store"
            )
            vectorstore = ChromaWithEmbeddingsAndScores(
                persist_directory=str(self.config.vectorindex_dir / "dense_retriever"),
                embedding_function=self.embedding_fn,
                collection_name=self.config.name,
                collection_metadata=datastore.ref_doc_info["metadata"],
            )
            logger.debug("Validating the vector store")
            collection_ids = vectorstore._collection.get(include=[])["ids"]

            if not sorted(collection_ids) == sorted(datastore.docs.keys()):
                logger.warning(
                    "The document ids in the vector store do not match the document ids loaded from files"
                )
                collection_docs_to_add = set(datastore.docs.keys()) - set(
                    collection_ids
                )
                collection_docs_to_delete = set(collection_ids) - set(
                    datastore.docs.keys()
                )

                if collection_docs_to_add:
                    logger.warning(
                        f"Adding {len(collection_docs_to_add)} documents to the vector store"
                    )
                    vectorstore.add_documents(
                        [
                            datastore.docs[doc_id].to_langchain_format()
                            for doc_id in collection_docs_to_add
                        ],
                        ids=list(collection_docs_to_add),
                    )
                if collection_docs_to_delete:
                    logger.warning(
                        f"Deleting {len(collection_docs_to_delete)} documents from the vector store"
                    )
                    vectorstore._collection.delete(ids=list(collection_docs_to_delete))

        else:
            logger.debug(
                f"{self.config.vectorindex_dir} was not found, creating a fresh vector store"
            )
            docs_list = self.get_docs_list()
            vectorstore = ChromaWithEmbeddingsAndScores(
                collection_name=self.config.name,
                persist_directory=str(self.config.vectorindex_dir / "dense_retriever"),
                embedding_function=self.embedding_fn,
                collection_metadata=datastore.ref_doc_info["metadata"],
            )
            vectorstore.add_texts(
                texts=[doc.page_content for doc in docs_list],
                metadatas=[doc.metadata for doc in docs_list],
                ids=[doc.metadata["doc_id"] for doc in docs_list],
            )
        return vectorstore.as_retriever()

    def create_retriever(self, datastore: DocumentStore):
        docs_list = self.get_docs_list()
        sparse_vectorizer = TfidfVectorizer(**self.config.sparse_vectorizer_kwargs)
        sparse_vectors = sparse_vectorizer.fit_transform(
            [doc.page_content for doc in docs_list]
        )
        sparse_retriever = TFIDFRetrieverWithScore(
            vectorizer=sparse_vectorizer,
            docs=docs_list,
            tfidf_array=sparse_vectors,
            k=3,
        )

        dense_retriever = self.create_dense_retriever(datastore)
        return HybridRetriever(
            dense=dense_retriever, sparse=sparse_retriever, k=self.config.retrieval_size
        )

    def load(self, data_sources: List[DataStore]) -> "VectorIndex":
        self.datastore = self.load_datastore(data_sources)
        self.retriever = self.create_retriever(self.datastore)
        return self

    def save(self):
        self.config.vectorindex_dir.mkdir(parents=True, exist_ok=True)
        # save the datastore
        datastore_dict = self.datastore.to_dict()
        with open(self.config.vectorindex_dir / "datastore.json", "w") as f:
            json.dump(datastore_dict, f)
        with open(self.config.vectorindex_dir / "metadata.json", "w") as f:
            json.dump(self.datastore.ref_doc_info["metadata"], f)

        # save the sparse retriever
        sparse_retriever_dir = self.config.vectorindex_dir / "sparse_retriever"
        sparse_retriever_dir.mkdir(parents=True, exist_ok=True)
        with open(sparse_retriever_dir / "sparse_vectorizer.pkl", "wb") as f:
            joblib.dump(self.retriever.sparse.vectorizer, f, compress=3)
        scipy.sparse.save_npz(
            str(sparse_retriever_dir / "tfidf_array.npz"),
            self.retriever.sparse.tfidf_array,
        )

        # save the dense retriever
        self.retriever.dense.vectorstore.persist()

        if self.wandb_run is None:
            self.wandb_run = wandb.init(
                project=self.config.wandb_project,
                entity=self.config.wandb_entity,
                config=self.config.dict(),
            )
        # save the config
        with open(self.config.vectorindex_dir / "config.json", "w") as f:
            f.write(self.config.json())
        artifact_metadata = dict(
            **self.config.dict(),
            **{"docs_metadata": self.datastore.ref_doc_info["metadata"]},
        )
        artifact = wandb.Artifact(
            name=self.config.name, type="vectorindex", metadata=artifact_metadata
        )

        artifact.add_dir(str(self.config.vectorindex_dir))
        self.saved_artifact = self.wandb_run.log_artifact(artifact)

        return self

    def load_from_artifact(
        self, artifact_path: Optional[str] = None, version: str = "latest"
    ):
        if self.wandb_run is None:
            self.wandb_run = wandb.init(
                project=self.config.wandb_project,
                entity="artsiom", #BUG: This is not being read and causing issue with loading data store
                config=self.config.dict(),
            )
        if artifact_path is None:
            artifact_path = f"{self.wandb_run.entity}/{self.wandb_run.project}/{self.config.name}:{version}"
        artifact = self.wandb_run.use_artifact(artifact_path)
        artifact_dir = pathlib.Path(artifact.download())

        # load the config
        with open(artifact_dir / "config.json", "r") as f:
            config_dict = json.load(f)
        self.config = VectorIndexConfig(**config_dict)

        # load the datastore
        with open(artifact_dir / "datastore.json", "r") as f:
            datastore_dict = json.load(f)
        self.datastore = DocumentStore.from_dict(datastore_dict)

        # load the metadata
        with open(artifact_dir / "metadata.json", "r") as f:
            metadata = json.load(f)

        # load the sparse retriever
        sparse_retriever_dir = artifact_dir / "sparse_retriever"
        with open(sparse_retriever_dir / "sparse_vectorizer.pkl", "rb") as f:
            sparse_vectorizer = joblib.load(f)
        tfidf_array = scipy.sparse.load_npz(
            str(sparse_retriever_dir / "tfidf_array.npz"),
        )
        docs_list = []
        for doc_id, document in sorted(self.datastore.docs.items(), key=lambda x: x[0]):
            docs_list.append(document.to_langchain_format())
        sparse_retriever = TFIDFRetrieverWithScore(
            vectorizer=sparse_vectorizer,
            docs=docs_list,
            tfidf_array=tfidf_array,
        )

        # load the dense retriever
        dense_retriever_dir = str(artifact_dir / "dense_retriever")
        dense_vectorstore = ChromaWithEmbeddingsAndScores(
            persist_directory=dense_retriever_dir,
            embedding_function=self.embedding_fn,
            collection_name=self.config.name,
            collection_metadata=metadata,
        )
        self.retriever = HybridRetriever(
            sparse=sparse_retriever,
            dense=dense_vectorstore.as_retriever(),
            k=self.config.retrieval_size,
        )
        return self
