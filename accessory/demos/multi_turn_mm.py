import sys
import os
sys.path.append(os.path.abspath(__file__).rsplit('/', 3)[0])

import argparse
import multiprocessing as mp
import numpy as np
from typing import List, Optional

import torch
import torch.distributed as dist

from fairscale.nn.model_parallel import initialize as fs_init

import gradio as gr

from accessory.util.misc import setup_for_distributed
from accessory.util.tensor_parallel import load_tensor_parallel_model_list
from accessory.util.tensor_type import default_tensor_type
from accessory.model.meta import MetaModel
from accessory.data.conversation import default_conversation, ConversationGenerator
from accessory.data.transform import get_transform


class Ready: pass
class ModelFailure: pass

def model_worker(
    rank: int, args: argparse.Namespace, barrier: mp.Barrier,
    request_queue: mp.Queue, response_queue: Optional[mp.Queue] = None,
) -> None:
    """
    The worker function that manipulates the GPU to run the inference.
    Exact n_gpu workers are started, with each one operating on a separate GPU.

    Args:
        rank (int): Distributed rank of the worker.
        args (argparse.Namespace): All command line arguments.
        barrier (multiprocessing.Barrier): A barrier used to delay the start
            of Web UI to be after the start of the model.
    """

    world_size = len(args.gpu_ids)
    gpu_id = args.gpu_ids[rank]
    dist.init_process_group(
        backend="nccl", rank=rank, world_size=world_size,
        init_method=f"tcp://{args.master_addr}:{args.master_port}",
    )
    print(f"| distributed init on worker {rank}/{world_size}. "
          f"using gpu: {gpu_id}")
    fs_init.initialize_model_parallel(world_size)
    torch.cuda.set_device(gpu_id)

    torch.manual_seed(1)
    np.random.seed(1)

    # set the print behavior.
    setup_for_distributed(rank == 0)

    target_dtype = {
        "bf16": torch.bfloat16,
        "fp16": torch.float16
    }[args.dtype]
    model = MetaModel.from_pretrained(
        args.llama_type, args.llama_config, args.tokenizer_path,
        with_visual=True, max_seq_len=args.model_max_seq_len,
        dtype=target_dtype, device="cpu" if args.quant else "cuda"
    )
    print("Loading pretrained weights ...")
    load_result = load_tensor_parallel_model_list(model, args.pretrained_path)
    print("load result:\n", load_result)
    if args.quant:
        from accessory.util.quant import quantize
        print("Quantizing model to 4bit!")
        from transformers.utils.quantization_config import BitsAndBytesConfig
        quantization_config = BitsAndBytesConfig.from_dict(
            config_dict={
                "load_in_8bit": False,
                "load_in_4bit": True,
                "bnb_4bit_quant_type": "nf4",
            },
            return_unused_kwargs=False,
        )
        quantize(model, quantization_config)
        model.cuda()
    model.eval()
    print(f"Model = {str(model)}")
    conv_generator = ConversationGenerator(model.tokenizer, conv_template_func=default_conversation)
    conv_sep = conv_generator.response_end_signal

    barrier.wait()

    while True:
        if response_queue is not None:
            response_queue.put(Ready())
        try:
            image, chatbot, max_gen_len, temperature, top_p, img_transform = request_queue.get()
            if image is not None:
                image = image.convert("RGB")
                transform = get_transform(img_transform, getattr(model.llma, 'image_size', 224))
                image = transform(image).unsqueeze(0).cuda().to(target_dtype)
            else:
                image = None
            prompt = conv_generator.qas_to_prompt(chatbot)

            with torch.cuda.amp.autocast(dtype=target_dtype, enabled=not args.quant):
                print(prompt)
                for stream_response in model.stream_generate(
                    prompt, image,
                    max_gen_len, temperature, top_p
                ):
                    end_pos = stream_response["text"].find(conv_sep)
                    if end_pos != -1:
                        stream_response["text"] = (
                            stream_response['text'][:end_pos].rstrip() + "\n"
                        )
                        stream_response["end_of_content"] = True

                    # keep a few characters if not end_of_content to avoid sending
                    # part of conv_sep before all of it is generated.
                    if not stream_response["end_of_content"]:
                        if len(stream_response["text"]) < len(conv_sep):
                            continue
                        stream_response["text"] = (
                            stream_response["text"][:-len(conv_sep)]
                        )

                    if response_queue is not None:
                        response_queue.put(stream_response)

                    if stream_response["end_of_content"]:
                        break
        except Exception:
            response_queue.put(ModelFailure())

def gradio_worker(
    request_queues: List[mp.Queue], response_queue: mp.Queue,
    args: argparse.Namespace, barrier: mp.Barrier,
) -> None:
    """
    The gradio worker is responsible for displaying the WebUI and relay the
    requests to model workers. It should be launched only once.

    Args:
        request_queues (List[mp.Queue]): A list of request queues (one for
            each model worker).
        args (argparse.Namespace): All command line arguments.
        barrier (multiprocessing.Barrier): A barrier used to delay the start
            of Web UI to be after the start of the model.
    """

    def show_user_input(msg, chatbot):
        return "", chatbot + [[msg, None]]

    def stream_model_output(img, chatbot, max_gen_len, gen_t, top_p, img_transform):
        while True:
            content_piece = response_queue.get()
            if isinstance(content_piece, Ready):
                break
        for queue in request_queues:
            queue.put((img, chatbot, max_gen_len, gen_t, top_p, img_transform))
        while True:
            content_piece = response_queue.get()
            if isinstance(content_piece, ModelFailure):
                raise RuntimeError
            chatbot[-1][1] = content_piece["text"]
            yield chatbot
            if content_piece["end_of_content"]:
                break

    def undo(chatbot):
        if len(chatbot) > 0:
            chatbot = chatbot[:-1]
        return chatbot

    def clear():
        chatbot = []
        msg = ""
        return chatbot, msg

    with gr.Blocks(css="#image_input {height: 100% !important}") as demo:
        gr.Markdown("# LLaMA2-Accessory Multi-turn Mutli-modal Demo")
        with gr.Row() as r:
            with gr.Column(scale=1):
                img_input = gr.Image(label='Image Input', type='pil', elem_id="image_input")
            with gr.Column(scale=2):
                chatbot = gr.Chatbot()
                msg = gr.Textbox()
        with gr.Row():
            submit_button = gr.Button("Submit", variant="primary")
            undo_button = gr.Button("Undo")
            clear_button = gr.ClearButton([chatbot, msg, img_input])
        with gr.Row():
            max_gen_len = gr.Slider(
                minimum=1, maximum=args.model_max_seq_len // 2,
                value=args.model_max_seq_len // 2, interactive=True,
                label="Single-turn max response length",
            )
            gen_t = gr.Slider(
                minimum=0, maximum=1, value=0.1, interactive=True,
                label="Temperature",
            )
            top_p = gr.Slider(
                minimum=0, maximum=1, value=0.75, interactive=True,
                label="Top-p",
            )
            img_transform = gr.Dropdown(choices=["padded_resize", "resized_center_crop"],
                                          value="padded_resize", label="Image Transform")
        msg.submit(
            show_user_input, [msg, chatbot], [msg, chatbot],
        ).then(
            stream_model_output, [img_input, chatbot, max_gen_len, gen_t, top_p, img_transform], chatbot,
        )
        submit_button.click(
            show_user_input, [msg, chatbot], [msg, chatbot],
        ).then(
            stream_model_output, [img_input, chatbot, max_gen_len, gen_t, top_p, img_transform], chatbot,
        )
        undo_button.click(undo, chatbot, chatbot)
        img_input.change(clear, [], [chatbot, msg])
    barrier.wait()
    demo.queue(api_open=True, concurrency_count=1).launch(share=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser("LLaMA2-Accessory Chat Demo")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--gpu_ids", type=int, nargs="+",
        help="A list of space-separated gpu ids to run the model on. "
             "The model will span across GPUs in tensor-parallel mode."
    )
    group.add_argument(
        "--n_gpus", type=int, default=1,
        help="Number of GPUs to run the model on. Equivalent to "
             "--gpu_ids 0 1 2 ... n-1"
    )
    parser.add_argument(
        "--tokenizer_path", type=str, default=None,
        help="Path to the tokenizer.model file provided along with the LLaMA "
             "model."
    )
    parser.add_argument(
        "--llama_type", default=None, type=str, metavar="MODEL",
        help="LLaMA model type."
    )
    parser.add_argument(
        "--llama_config", type=str, default=None, nargs="*",
        help="Path to the llama model config json."
    )
    parser.add_argument(
        "--model_max_seq_len", type=int, default=2048,
        help="Max sequence length accepted by the pretrained model."
    )
    parser.add_argument(
        "--pretrained_path", type=str, required=True, nargs="+",
        help="Path to the llama model checkpoints. A list of checkpoints is "
             "supported and will be merged from left to right.")
    parser.add_argument(
        "--master_port", type=int, default=23560,
        help="A port used by the PyTorch distributed module to initialize."
    )
    parser.add_argument(
        "--master_addr", type=str, default="127.0.0.1",
        help="An address used by the PyTorch distributed module to initialize."
    )
    parser.add_argument(
        "--dtype", type=str, choices=["fp16", "bf16"], default="fp16",
        help="The dtype used for model weights and inference."
    )
    parser.add_argument(
        "--quant", action="store_true", default=False,
        help="enable quantization"
    )
    args = parser.parse_args()

    # check and setup gpu_ids to use
    if args.gpu_ids is None:
        if args.n_gpus is None:
            args.n_gpus = 1
        assert args.n_gpus > 0, (
            "The demo currently must run on a positive number of GPUs."
        )
        args.gpu_ids = list(range(args.n_gpus))

    # using the default "fork" method messes up some imported libs (e.g.,
    # pandas)
    mp.set_start_method("spawn")

    # setup the queues and start the model workers
    request_queues = []
    response_queue = mp.Queue()
    worker_processes = []
    barrier = mp.Barrier(len(args.gpu_ids) + 1)
    for rank, gpu_id in enumerate(args.gpu_ids):
        request_queue = mp.Queue()
        rank_response_queue = response_queue if rank == 0 else None
        process = mp.Process(
            target=model_worker,
            args=(rank, args, barrier, request_queue, rank_response_queue),
        )
        process.start()
        worker_processes.append(process)
        request_queues.append(request_queue)

    gradio_worker(request_queues, response_queue, args, barrier)
