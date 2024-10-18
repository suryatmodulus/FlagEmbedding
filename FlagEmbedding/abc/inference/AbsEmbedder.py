from abc import ABC, abstractmethod
import torch
import numpy as np
from multiprocessing import Queue
from typing import List, Union, Literal, Any, Dict
import multiprocessing as mp
import math
from tqdm import tqdm, trange
from transformers import is_torch_npu_available


class AbsEmbedder(ABC):
    """
    Base class for embedder.
    Extend this class and implement `encode_queries`, `encode_passages`, `encode` for custom embedders.
    """

    def __init__(
            self,
            model_name_or_path: str,
            normalize_embeddings: bool = False,
            use_fp16: bool = False,
    ):
        self.model_name_or_path = model_name_or_path
        self.normalize_embeddings = normalize_embeddings
        self.use_fp16 = use_fp16

    def encode_queries(
            self,
            queries: Union[List[str], str],
            multi_gpus: bool = False,
            **kwargs
    ):
        if not multi_gpus:
            return self.encode_queries_single_gpu(
                queries,
                **kwargs
            )

        pool = self.start_multi_process_pool(s_type='query')
        scores = self.encode_multi_process(queries,
                                           pool,
                                           **kwargs)
        self.stop_multi_process_pool(pool)
        return scores

    def encode_corpus(
            self,
            queries: Union[List[str], str],
            multi_gpus: bool = False,
            **kwargs
    ):
        if not multi_gpus:
            return self.encode_corpus_single_gpu(
                queries,
                **kwargs
            )

        pool = self.start_multi_process_pool(s_type='corpus')
        scores = self.encode_multi_process(queries,
                                           pool,
                                           **kwargs)
        self.stop_multi_process_pool(pool)
        return scores

    @abstractmethod
    def encode_queries_single_gpu(
            self,
            queries: Union[List[str], str],
            batch_size: int = 256,
            max_length: int = 512,
            **kwargs: Any,
    ):
        """
        This method should encode queries and return embeddings.
        """
        pass

    @abstractmethod
    def encode_corpus_single_gpu(
            self,
            corpus: Union[List[str], str],
            batch_size: int = 256,
            max_length: int = 512,
            **kwargs: Any,
    ):
        """
        This method should encode corpus and return embeddings.
        """
        pass

    @abstractmethod
    def encode(
            self,
            sentences: Union[List[str], str],
            batch_size: int = 256,
            max_length: int = 512,
            **kwargs: Any,
    ):
        """
        This method should encode sentences and return embeddings.
        """
        pass

    def start_multi_process_pool(
            self, target_devices: List[str] = None, s_type: str = 'query'
    ) -> Dict[Literal["input", "output", "processes"], Any]:
        """
        Starts a multi-process pool to process the encoding with several independent processes
        via :meth:`SentenceTransformer.encode_multi_process <sentence_transformers.SentenceTransformer.encode_multi_process>`.

        This method is recommended if you want to encode on multiple GPUs or CPUs. It is advised
        to start only one process per GPU. This method works together with encode_multi_process
        and stop_multi_process_pool.

        Args:
            target_devices (List[str], optional): PyTorch target devices, e.g. ["cuda:0", "cuda:1", ...],
                ["npu:0", "npu:1", ...], or ["cpu", "cpu", "cpu", "cpu"]. If target_devices is None and CUDA/NPU
                is available, then all available CUDA/NPU devices will be used. If target_devices is None and
                CUDA/NPU is not available, then 4 CPU devices will be used.
            s_type: The type of sentence.
        Returns:
            Dict[str, Any]: A dictionary with the target processes, an input queue, and an output queue.
        """
        if target_devices is None:
            if torch.cuda.is_available():
                target_devices = [f"cuda:{i}" for i in range(torch.cuda.device_count())]
            elif is_torch_npu_available():
                target_devices = [f"npu:{i}" for i in range(torch.npu.device_count())]
            else:
                print("CUDA/NPU is not available. Starting 4 CPU workers")
                target_devices = ["cpu"] * 4

        print("Start multi-process pool on devices: {}".format(", ".join(map(str, target_devices))))

        self.model.to("cpu")
        self.model.share_memory()
        ctx = mp.get_context("spawn")
        input_queue = ctx.Queue()
        output_queue = ctx.Queue()
        processes = []

        for device_id in tqdm(target_devices, desc='initial target device'):
            p = ctx.Process(
                target=AbsEmbedder._encode_query_multi_process_worker if s_type == 'query' else AbsEmbedder._encode_corpus_multi_process_worker,
                args=(device_id, self, input_queue, output_queue),
                daemon=True,
            )
            p.start()
            processes.append(p)

        return {"input": input_queue, "output": output_queue, "processes": processes}

    def encode_multi_process(
        self,
        sentences: List[str],
        pool: Dict[Literal["input", "output", "processes"], Any],
        chunk_size: int = None,
        show_progress_bar: bool = True,
        **kwargs
    ) -> np.ndarray:
        """
        Encodes a list of sentences using multiple processes and GPUs via
        :meth:`SentenceTransformer.encode <sentence_transformers.SentenceTransformer.encode>`.
        The sentences are chunked into smaller packages and sent to individual processes, which encode them on different
        GPUs or CPUs. This method is only suitable for encoding large sets of sentences.

        Args:
            sentences (List[str]): List of sentences to encode.
            pool (Dict[Literal["input", "output", "processes"], Any]): A pool of workers started with
                :meth:`SentenceTransformer.start_multi_process_pool <sentence_transformers.SentenceTransformer.start_multi_process_pool>`.
            chunk_size (int): Sentences are chunked and sent to the individual processes. If None, it determines a
                sensible size. Defaults to None.
            show_progress_bar (bool, optional): Whether to output a progress bar when encode sentences. Defaults to None.

        Returns:
            np.ndarray: A 2D numpy array with shape [num_inputs, output_dimension].

        Example:
            ::

                from sentence_transformers import SentenceTransformer

                def main():
                    model = SentenceTransformer("all-mpnet-base-v2")
                    sentences = ["The weather is so nice!", "It's so sunny outside.", "He's driving to the movie theater.", "She's going to the cinema."] * 1000

                    pool = model.start_multi_process_pool()
                    embeddings = model.encode_multi_process(sentences, pool)
                    model.stop_multi_process_pool(pool)

                    print(embeddings.shape)
                    # => (4000, 768)

                if __name__ == "__main__":
                    main()
        """

        if chunk_size is None:
            # chunk_size = min(math.ceil(len(sentences) / len(pool["processes"]) / 10), 5000)
            chunk_size = math.ceil(len(sentences) / len(pool["processes"]))

        # if show_progress_bar is None:
        #     show_progress_bar = logger.getEffectiveLevel() in (logging.INFO, logging.DEBUG)

        print(f"Chunk data into {math.ceil(len(sentences) / chunk_size)} packages of size {chunk_size}")

        input_queue = pool["input"]
        last_chunk_id = 0
        chunk = []

        for sentence in sentences:
            chunk.append(sentence)
            if len(chunk) >= chunk_size:
                input_queue.put(
                    [last_chunk_id, chunk, kwargs]
                )
                last_chunk_id += 1
                chunk = []

        if len(chunk) > 0:
            input_queue.put([last_chunk_id, chunk, kwargs])
            last_chunk_id += 1

        output_queue = pool["output"]
        results_list = sorted(
            [output_queue.get() for _ in trange(last_chunk_id, desc="Chunks", disable=not show_progress_bar)],
            key=lambda x: x[0],
        )
        embeddings = np.concatenate([result[1] for result in results_list])
        return embeddings

    @staticmethod
    def _encode_query_multi_process_worker(
            target_device: str, model: ABC, input_queue: Queue, results_queue: Queue
    ) -> None:
        """
        Internal working process to encode sentences in multi-process setup
        """
        while True:
            try:
                chunk_id, sentences, kwargs = (
                    input_queue.get()
                )
                # print(chunk_id, sentences, kwargs)
                # print('====', target_device)
                embeddings = model.encode_corpus_single_gpu(
                    sentences,
                    device=target_device,
                    **kwargs
                )

                results_queue.put([chunk_id, embeddings])
            except:
                break

    @staticmethod
    def _encode_corpus_multi_process_worker(
            target_device: str, model: ABC, input_queue: Queue, results_queue: Queue
    ) -> None:
        """
        Internal working process to encode sentences in multi-process setup
        """
        while True:
            try:
                chunk_id, sentences, kwargs = (
                    input_queue.get()
                )
                # print(chunk_id, sentences, kwargs)
                # print('====', target_device)
                embeddings = model.encode_corpus_single_gpu(
                    sentences,
                    device=target_device,
                    **kwargs
                )

                results_queue.put([chunk_id, embeddings])
            except:
                break

    @staticmethod
    def stop_multi_process_pool(pool: Dict[Literal["input", "output", "processes"], Any]) -> None:
        """
        Stops all processes started with start_multi_process_pool.

        Args:
            pool (Dict[str, object]): A dictionary containing the input queue, output queue, and process list.

        Returns:
            None
        """
        for p in pool["processes"]:
            p.terminate()

        for p in pool["processes"]:
            p.join()
            p.close()

        pool["input"].close()