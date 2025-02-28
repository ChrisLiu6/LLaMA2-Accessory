import warnings

import torch
import torch.nn as nn
import json
from typing import List, Optional, Iterable
from pathlib import Path

from fairscale.nn.model_parallel import initialize as fs_init

from .tokenizer import Tokenizer
from . import LLM
from accessory.util import misc, tensor_parallel
from accessory.util.tensor_type import default_tensor_type

import torch.distributed as dist


class MetaModel(nn.Module):
    """ Masked Autoencoder with VisionTransformer backbone
    """
    def __init__(
        self, llama_type: str, llama_config: List[str], tokenizer_path: str,
        with_visual: bool = False, max_seq_len: int = 4096
    ) -> None:
        super().__init__()

        self.llama_type = llama_type
        self.with_visual = with_visual

        ModelArgs = LLM.__dict__[llama_type].ModelArgs
        Transformer = LLM.__dict__[llama_type].Transformer

        llama_args = {}
        for _ in llama_config:
            with open(_, "r") as f:
                llama_args.update(json.loads(f.read()))
        llama_args['max_seq_len'] = max_seq_len
        llama_args['max_batch_size'] = 32

        self.tokenizer = Tokenizer(model_path=tokenizer_path)
        llama_args['vocab_size'] = self.tokenizer.n_words

        llama_args: ModelArgs = ModelArgs(**llama_args)
        print("Model Args:\n", llama_args)

        model = Transformer(llama_args, with_visual=with_visual)
        self.llma = model

        self.criterion = torch.nn.CrossEntropyLoss(ignore_index=0)

        self._set_default_trainability()

        self.is_peft = getattr(model, "is_peft", False)
        print(f"Model is Peft: {self.is_peft}")

        misc.mark_mp_params(self)

        param_count_local, param_count_all = 0, 0
        for name, param in self.named_parameters():
            is_model_parallel = getattr(param, "is_model_parallel", False)
            if param.requires_grad:
                if is_model_parallel:
                    param_count_all += param.numel() * fs_init.get_model_parallel_world_size()
                else:
                    param_count_all += param.numel()
                param_count_local += param.numel()
        print(f"Trainable parameter count : {param_count_local} (local rank), {param_count_all} (all).")

    @classmethod
    def from_pretrained(cls, pretrained_path: str|List[str],
                        llama_type: Optional[str] = None,
                        llama_config: Optional[str|List[str]] = None,
                        tokenizer_path: Optional[str] = None,
                        with_visual: bool = False, max_seq_len: int = 4096,
                        mp_group: Optional[dist.ProcessGroup] = None,
                        dtype=torch.bfloat16, device="cuda"):
        """
        Besides loading the `consolidated.*.pth` model weights, this function also tries to find tokenizer,
        'meta.json', and 'config.json' under `pretrained_path` to configure the `tokenizer_path`,
        `llama_type`, and `llama_config` of the model. The automatically determined values will be
        overridden by user's exploit specification of the arguments.
        :param pretrained_path: Paths to directories containing `consolidated.*.pth` weight files. If multiple paths
                are given, weights will be loaded sequentially.
        :param llama_type: Type of the inner LLM. The corresponding model class definition should be found in
                accessory/model/LLM/llama_type.py. If not specified, this function will probe the `meta.json`
                file under `pretrained_path` to try to determine the value.
        :param llama_config: Inner LLM configurations. Can be one or a list of strings, each of which is the path
                to a `*.json` configuration file. If not specified, this function will probe the `config.json`
                file under `pretrained_path` to try to determine the value.
        :param tokenizer_path: LLaMA2-Accessory supports both spm tokenizers (provided by Meta, generally named
                tokenizer.model) and huggingface tokenizers (composed of tokenizer.json and tokenizer_config.json).
                When using spm tokenizers, tokenizer_path should point to the `tokenizer.model` file;
                when using huggingface tokenizers, tokenizer_path should point to the directory containing
                tokenizer.json and tokenizer_config.json. If not specified, this function will probe the
                `pretrained_path` directory for tokenizer in either format.
        :param with_visual: Set it to True if the model is expected to receive image input. Inner LLM models
                rely on this argument to decide whether to instantiate the visual encoder.
        :param max_seq_len: max context window size of the model
        :param mp_group:  If the parameters of the model are *not* split on multiple GPUs with model parallel,
                namely model parallel size == 1, then `mp_group` can be left to `None`. However, if model
                parallel is needed, `mp_group` should be an already initialized torch process group, ranks
                within which compose a logically complete model.
        :param dtype: parameter data type
        :param device: parameter device

        :return: An Accessory.model.MetaModel object with pretrained checkpoints loaded.
        """
        if isinstance(pretrained_path, str):
            pretrained_path = [pretrained_path]
        if pretrained_path is None or len(pretrained_path) == 0:
            raise ValueError("pretrained_path should be specified")

        if mp_group is None:
            print(f"mp_group not provided. Load model with model parallel size == 1")
            if dist.is_initialized():
                mp_group = dist.new_group(ranks=[dist.get_rank()])
            else:
                warnings.warn(
                    "\n\n********************************\n"
                    "Warning: Torch distributed not initialized when invoking `MetaModel.from_pretrained`.\n"
                    "trying to init distributed mode within `from_pretrained` with a world size of 1.\n"
                    "Note: Distrubuted functions like `get_world_size()` are used within Accessory's model implementations,\n"
                    "Therefore, distributed initialization is required even when using a single GPU.\n"
                    "This warning is normal if your program isn't designed for distributed computing.\n"
                    "However, if your program is intended for distributed use,\n"
                    "please initialize distributed mode before model creation"
                    "********************************\n")
                torch.distributed.init_process_group(
                    backend="nccl", rank=0, world_size=1,
                    init_method=f"tcp://127.0.0.1:{misc.find_free_port(9000, 10000)}")
                mp_group = dist.new_group(ranks=[dist.get_rank()])
        else:
            print(f"Load model with model parallel size == {dist.get_world_size(mp_group)}")

        fs_init._MODEL_PARALLEL_GROUP = mp_group

        # determine llama_type
        if llama_type is None:
            print(f"llama_type not specified, attempting to obtain from {Path(pretrained_path[-1])/'meta.json'}")
            if (Path(pretrained_path[-1])/'meta.json').exists():
                with open(Path(pretrained_path[-1])/'meta.json', 'r') as f:
                    llama_type = json.load(f)["llama_type"]
                    print(f"Obtained llama_type: {llama_type}")
            else:
                print(f"{Path(pretrained_path[-1])/'meta.json'} does not exist")
                raise ValueError("Cannot determine llama_type")


        # determine llama_config
        if llama_config is None:
            print(f"llama_config not specified, attempting to find {Path(pretrained_path[-1]) / 'config.json'}")
            if (Path(pretrained_path[-1])/'config.json').exists():
                llama_config = [str(Path(pretrained_path[-1])/'config.json')]
                print(f"Found llama_config: {str(Path(pretrained_path[-1])/'config.json')}")
            else:
                print(f"{str(Path(pretrained_path[-1]) / 'config.json')} does not exist\n"
                      f"will use the default config values (specified in the definition of ModelArgs in {llama_type}.py)")
                llama_config = []


        # determine tokenizer_path
        if tokenizer_path is None:  # first try setence-piece style
            print(f"tokenizer_path not specified.")

            print(f"trying to find sentencepiece-style tokenizer at {Path(pretrained_path[-1]) / 'tokenizer.model'}")
            if (Path(pretrained_path[-1])/'tokenizer.model').exists():
                print(f"Found {Path(pretrained_path[-1]) / 'tokenizer.model'}, use it.")
                tokenizer_path = str(Path(pretrained_path[-1]) / 'tokenizer.model')
            else:
                print("Not Found")
        if tokenizer_path is None:  # then try huggingface style
            print(f"trying to find huggingface-style tokenizer at "
                  f"{Path(pretrained_path[-1]) / '(tokenizer.json, tokenizer_config.json)'}")
            if (Path(pretrained_path[-1])/'tokenizer.json').exists() and (Path(pretrained_path[-1])/'tokenizer_config.json').exists():
                print(f"Found {Path(pretrained_path[-1]) / '(tokenizer.json, tokenizer_config.json)'}, use them.")
                tokenizer_path = pretrained_path[-1]
            else:
                print("Not Found")
        assert tokenizer_path is not None, "No usable tokenizer avaiable"


        with default_tensor_type(dtype=dtype, device=device):
            model = cls(llama_type, llama_config, tokenizer_path, with_visual, max_seq_len)
        print(f"Loading pretrained weights from {pretrained_path} ...")
        load_result = tensor_parallel.load_tensor_parallel_model_list(model, pretrained_path)
        assert load_result == {'missing_keys': [], 'unexpected_keys': []}, "checkpoint and model mismatch"
        model.eval()
        return model


    def get_trainable_params(self):
        llma_trainable = self.llma.get_trainable_params()
        return {"llma." + name: param for name, param in llma_trainable.items()}


    def _set_default_trainability(self):
        for key, value in self.named_parameters():
            value.requires_grad = False
        for key, value in self.get_trainable_params().items():
            value.requires_grad = True


    def forward(self, examples, labels, images=None):
        with torch.no_grad():
            non_zero_ = torch.count_nonzero(labels, dim=0)
            pos = non_zero_.shape[0] - 1
            while pos >= 0:
                if non_zero_[pos] == 0:
                    pos -= 1
                else:
                    break

            if pos == -1:  # nothing to predict in the whole batch
                print(f"[RANK {dist.get_rank()}] nothing to predict in the whole batch!", force=True)
                print(examples.cpu().tolist(), force=True)
                pos = 2
            examples = examples[:, :pos+1]
            labels = labels[:, :pos+1]

        output = self.llma(examples, images)
        output = output[:, :-1, :]
        labels = labels[:, 1:]

        if labels.sum() == 0:
           c_loss = output.mean() * 0
        else:
           c_loss = self.criterion(output.reshape(-1, self.tokenizer.n_words), labels.flatten())
        return c_loss


    @ torch.inference_mode()
    def generate(
        self,
        prompts: List[str],
        images: List,
        max_gen_len: int,
        temperature: float = 0.8,
        top_p: float = 0.95,
        return_logits: bool = False
    ) -> List[str]:
        bsz = len(prompts)
        args = self.llma.args
        assert bsz <= args.max_batch_size, (bsz, args.max_batch_size)

        prompt_tokens = [self.tokenizer.encode(
            x, bos=True, eos=False) for x in prompts]

        min_prompt_size = min([len(t) for t in prompt_tokens])
        max_prompt_size = max([len(t) for t in prompt_tokens])

        max_seq_len = args.max_seq_len
        if images is not None:
            max_seq_len -= self.llma.image_words

        total_len = min(max_seq_len, max_gen_len + max_prompt_size)

        tokens = torch.full((bsz, total_len), 0).cuda().long()
        input_text_mask = torch.full((bsz, total_len), False).cuda()
        for k, t in enumerate(prompt_tokens):
            tokens[k, : len(t)] = torch.tensor(t).long()
            input_text_mask[k, : len(t)] = True
        start_pos = min_prompt_size
        prev_pos = 0

        if return_logits:
            return self.llma.forward_inference(tokens[:, :start_pos], prev_pos, images if prev_pos == 0 else None)
    
        for cur_pos in range(start_pos, total_len):
            logits = self.llma.forward_inference(tokens[:, prev_pos:cur_pos], prev_pos, images if prev_pos == 0 else None)
            if temperature > 0:
                probs = torch.softmax(logits / temperature, dim=-1)
                next_token = self.sample_top_p(probs, top_p)
            else:
                next_token = torch.argmax(logits, dim=-1)
            next_token = next_token.reshape(-1)
            # only replace token if prompt has already been generated
            next_token = torch.where(
                input_text_mask[:, cur_pos], tokens[:, cur_pos], next_token
            )
            tokens[:, cur_pos] = next_token
            # trick: early stop if bsz==1
            if bsz == 1 and next_token[0] == self.tokenizer.eos_id:
                break
            prev_pos = cur_pos

        decoded = []
        for i, t in enumerate(tokens.tolist()):
            # cut to max gen len
            t = t[len(prompt_tokens[i]): len(prompt_tokens[i]) + max_gen_len]
            # cut to eos tok if any
            try:
                t = t[: t.index(self.tokenizer.eos_id)]
            except ValueError:
                pass
            decoded.append(self.tokenizer.decode(t))
        return decoded


    @ torch.inference_mode()
    def stream_generate(
        self,
        prompt: str,
        image: Optional[torch.Tensor],
        max_gen_len: int,
        temperature: float = 0.8,
        top_p: float = 0.95,
        additional_stop_symbols: Iterable[str] = ()
    ):
        args = self.llma.args

        prompt_tokens = self.tokenizer.encode(prompt, bos=True, eos=False)
        # truncate from the left. leave some space for generation.
        max_seq_len = args.max_seq_len
        if image is not None:
            max_seq_len -= self.llma.image_words

        max_prompt_size = max_seq_len - max_gen_len
        prompt_tokens = prompt_tokens[-max_prompt_size:]

        prompt_size = len(prompt_tokens)

        total_len = min(max_seq_len, max_gen_len + prompt_size)

        tokens = torch.full([total_len], 0).cuda().long()

        tokens[:len(prompt_tokens)] = torch.tensor(prompt_tokens).long()
        start_pos = prompt_size
        prev_pos = 0
        generate_until = start_pos
        for cur_pos in range(start_pos, total_len):
            logits = self.llma.forward_inference(tokens[None, prev_pos:cur_pos], prev_pos, image if prev_pos == 0 else None)
            if temperature > 0:
                probs = torch.softmax(logits / temperature, dim=-1)
                next_token = self.sample_top_p(probs, top_p)
            else:
                next_token = torch.argmax(logits, dim=-1)
            next_token = next_token.item()

            if next_token == self.tokenizer.eos_id:
                break

            tokens[cur_pos] = next_token
            prev_pos = cur_pos
            generate_until = cur_pos + 1

            generated = self.tokenizer.decode(tokens[start_pos:generate_until].tolist())
            for stop_symbol in additional_stop_symbols:
                stop_pos = generated.find(stop_symbol)
                if stop_pos != -1:
                    generated = generated[:stop_pos]
                    return {"text": generated, "end_of_content": True}

            yield {"text": generated, "end_of_content": False}

        generated = self.tokenizer.decode(tokens[start_pos:generate_until].tolist())
        return {"text": generated, "end_of_content": True}


    def sample_top_p(self, probs, p):
        probs_sort, probs_idx = torch.sort(probs, dim=-1, descending=True)
        probs_sum = torch.cumsum(probs_sort, dim=-1)
        mask = probs_sum - probs_sort > p
        probs_sort[mask] = 0.0
        probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))
        next_token = torch.multinomial(probs_sort, num_samples=1)
        next_token = torch.gather(probs_idx, -1, next_token)
        return next_token


    def get_image_words(self):
        return self.llma.image_words

    def get_quant_blocklist(self) -> List[str]:
        if hasattr(self.llma, "get_quant_blocklist"):
            return ["llma." + x for x in self.llma.get_quant_blocklist()]
        return []
