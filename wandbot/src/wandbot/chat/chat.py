import json
import logging
from typing import List, Optional, Tuple

import langchain
import pandas as pd
import tiktoken
import wandb
from langchain import LLMChain, OpenAI
from langchain.cache import SQLiteCache
from langchain.callbacks import get_openai_callback
from langchain.callbacks.tracers import WandbTracer
from langchain.callbacks.tracers.wandb import WandbRunArgs

from langchain.chains.conversational_retrieval.base import (
    BaseConversationalRetrievalChain,
)
from langchain.chains.qa_with_sources import load_qa_with_sources_chain
from langchain.chat_models import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import LLMChainExtractor
from langchain.schema import BaseRetriever
# from wandb.integration.langchain.wandb_tracer import WandbRunArgs
from wandbot.chat.config import ChatConfig
from wandbot.chat.langchain111 import ConversationalRetrievalQAWithSourcesandScoresChain
from wandbot.chat.prompts import load_chat_prompt, load_history_prompt
from wandbot.chat.schemas import ChatRepsonse, ChatRequest
from wandbot.database.schemas import QuestionAnswer
from wandbot.ingestion.datastore import VectorIndex
from wandbot.ingestion.utils import Timer

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


def get_chat_history(
    chat_history: List[QuestionAnswer] | None,
) -> List[tuple[str, str]]:
    if not chat_history:
        return []
    else:
        return [
            (question_answer.question, question_answer.answer)
            for question_answer in chat_history
        ]


class Chat:
    config: ChatConfig

    def __init__(self, config: Optional[ChatConfig] = None):
        if config is not None:
            self.config: ChatConfig = config
        langchain.llm_cache = SQLiteCache(database_path=str(self.config.llm_cache_path))
        self.vector_index: VectorIndex = VectorIndex(
            config=self.config.vectorindex_config
        )
        self.chat_prompt: ChatPromptTemplate = load_chat_prompt(self.config.chat_prompt)
        self._retriever: BaseRetriever = self._load_retriever()
        self._chain: BaseConversationalRetrievalChain = self._load_chain(
            self.config.model_name, self.config.max_retries
        )
        self._fallback_chain: BaseConversationalRetrievalChain = self._load_chain(
            self.config.fallback_model_name, self.config.max_fallback_retries
        )
        self.tokenizer = tiktoken.get_encoding("cl100k_base")
        WandbTracer(
            run_args=WandbRunArgs(
                project=self.config.wandb_project,
                entity=self.config.wandb_entity,
                config=self.config.dict(),
                job_type=self.config.wandb_job_type,
            )
        )

    def _load_retriever(self) -> BaseRetriever:
        self.vector_index = self.vector_index.load_from_artifact(
            self.config.vectorindex_artifact
        )
        llm = OpenAI(
            temperature=0, batch_size=10, max_retries=self.config.max_fallback_retries
        )
        compressor = LLMChainExtractor.from_llm(llm)
        compression_retriever = ContextualCompressionRetriever(
            base_compressor=compressor, base_retriever=self.vector_index.retriever
        )
        return compression_retriever

    @property
    def retriever(self):
        if self._retriever is None:
            self._retriever = self._load_retriever()
        return self._retriever

    def _load_chain(
        self, model_name: str = None, max_retries: int = 1
    ) -> BaseConversationalRetrievalChain:
        map_llm = OpenAI(
            batch_size=10,
            temperature=0.0,
            max_retries=self.config.max_fallback_retries,
        )
        reduce_llm = ChatOpenAI(
            model_name=model_name,
            temperature=self.config.chat_temperature,
            max_retries=max_retries,
        )
        question_generator = LLMChain(
            llm=ChatOpenAI(
                model_name=self.config.fallback_model_name,
                temperature=self.config.chat_temperature,
                max_retries=self.config.max_fallback_retries,
            ),
            prompt=load_history_prompt(self.config.history_prompt),
            verbose=self.config.verbose,
        )
        if self.config.chain_type == "map_reduce":
            doc_chain = load_qa_with_sources_chain(
                map_llm,
                chain_type=self.config.chain_type,
                combine_prompt=self.chat_prompt,
                verbose=self.config.verbose,
                reduce_llm=reduce_llm,
            )

        else:
            doc_chain = load_qa_with_sources_chain(
                map_llm,
                chain_type=self.config.chain_type,
                prompt=self.chat_prompt,
                verbose=self.config.verbose,
            )
        chain = ConversationalRetrievalQAWithSourcesandScoresChain(
            retriever=self.retriever,
            question_generator=question_generator,
            combine_docs_chain=doc_chain,
            return_source_documents=True,
        )

        return chain

    @property
    def chain(self):
        if self._chain is None:
            self._chain = self._load_chain(
                model_name=self.config.model_name,
                max_retries=self.config.max_retries,
            )
        return self._chain

    @property
    def fallback_chain(self):
        if self._fallback_chain is None:
            self._fallback_chain = self._load_chain(
                model_name=self.config.fallback_model_name,
                max_retries=self.config.max_fallback_retries,
            )
        return self._fallback_chain

    def validate_and_format_question(self, question: str) -> str:
        question = " ".join(question.strip().split())

        if len(self.tokenizer.encode(question)) > 1024:
            raise ValueError(
                f"Question is too long. Please rephrase your question to be shorter than {1024 * 3 // 4} words."
            )
        return question

    def format_response(self, result):
        response = {}
        if result["source_documents"]:
            source_documents = [
                {"source": doc.metadata["source"], "score": doc.metadata["score"]}
                for doc in result["source_documents"]
                # if doc.metadata["score"] <= self.config.source_score_threshold
            ]
        else:
            source_documents = []
        response["answer"] = result["answer"]
        response["model"] = result["model"]

        if len(source_documents) and self.config.include_sources:
            response["source_documents"] = json.dumps(source_documents)
            response["sources"] = result["sources"]
        else:
            response["source_documents"] = ""
            response["sources"] = ""

        return response

    def get_answer(
        self, query: str, chat_history: Optional[List[Tuple[str, str]]] = None
    ):
        if chat_history is None:
            chat_history = []
        try:
            result = self.chain(
                {
                    "question": query,
                    "chat_history": chat_history,
                },
                return_only_outputs=True,
            )
            result["model"] = self.config.model_name
        except Exception as e:
            logger.warning(f"{self.config.model_name} failed with {e}")
            logger.warning(f"Falling back to {self.config.fallback_model_name} model")
            try:
                result = self.fallback_chain(
                    {
                        "question": query,
                        "chat_history": chat_history,
                    },
                    return_only_outputs=True,
                )
                result["model"] = self.config.fallback_model_name
            except Exception as e:
                logger.warning(f"{self.config.fallback_model_name} failed with {e}")
                result = {
                    "answer": "\uE058"
                    + " Sorry, there seems to be an issue with our LLM service. Please try again in some time.",
                    "sources": "",
                    "source_documents": None,
                    "model": "None",
                }
        return self.format_response(result)

    def __call__(self, chat_request: ChatRequest) -> ChatRepsonse:
        with Timer() as timer:
            try:
                query = self.validate_and_format_question(chat_request.question)
            except ValueError as e:
                result = {
                    "answer": str(e),
                    "sources": "",
                }
            else:
                with get_openai_callback() as callback:
                    result = self.get_answer(
                        query, chat_history=get_chat_history(chat_request.chat_history)
                    )
                    usage_stats = {
                        "total_tokens": callback.total_tokens,
                        "prompt_tokens": callback.prompt_tokens,
                        "completion_tokens": callback.completion_tokens,
                        "successful_requests": callback.successful_requests,
                        "total_cost": callback.total_cost,
                    }

        result.update(
            dict(
                **{
                    "question": chat_request.question,
                    "time_taken": timer.elapsed,
                    "start_time": timer.start,
                    "end_time": timer.stop,
                },
                **usage_stats,
            )
        )
        wandb.run.log(
            {
                "chat": wandb.Table(
                    dataframe=pd.DataFrame({k: [v] for k, v in result.items()})
                )
            }
        )
        wandb.run.log(usage_stats)
        return ChatRepsonse(**result)


def main():
    config = ChatConfig()
    chat = Chat(config=config)
    chat_history = []
    while True:
        question = input("You: ")
        if question.lower() == "quit":
            break
        else:
            response = chat(ChatRequest(question=question, chat_history=chat_history))
            chat_history.append((question, response.answer))
            print(f"WandBot: {response.answer}")
            print(f"Time taken: {response.time_taken}")

if __name__ == "__main__":
    main()