from typing import List, Optional
import torch
import numpy as np
from PIL import Image
from accessory.model.meta import MetaModel

from accessory.data.transform import get_transform
from accessory.data.conversation import default_conversation, ConversationGenerator

class SPHINXModel(MetaModel):
    def generate_reponse(self, qas: List[List[str]], image: Optional[Image.Image],
                         max_gen_len=512, temperature=0.1, top_p=0.5, seed=0) -> str:
        """

        Args:
            qas: A list of question answer pairs in the form of `[[q1, a1], [q2,a2], ... , [qn, None]]`.
                last answer should be None for generation.
            image: PIL Image for multi-modal understanding
            max_gen_len: generation hyper-param
            temperature: generation hyper-param
            top_p: generation hyper-param
            seed: random seed

        Returns:
            str: response
        """
        # to avoid sampling inconsistency among model parallel workers
        torch.manual_seed(seed)
        np.random.seed(seed)

        if image is not None:
            image = image.convert("RGB")
            target_size = getattr(self.llma, 'image_size', 224)  # 448 for SPHINX-1k, 224 for SPHINX
            image = get_transform("padded_resize", target_size)(image).unsqueeze(0).to(list(self.parameters())[0])

        conv_generator = ConversationGenerator(self.tokenizer, default_conversation)
        assert qas[-1][1] is None

        prompt = conv_generator.qas_to_prompt(qas)
        # print(prompt)

        # each turn of response ends with `conv_seq`
        conv_sep = conv_generator.response_end_signal

        for stream_response in self.stream_generate(
            prompt, image, max_gen_len=max_gen_len, temperature=temperature, top_p=top_p
        ):
            end_pos = stream_response["text"].find(conv_sep)
            if end_pos != -1:  # response ends
                stream_response["text"] = (
                        stream_response['text'][:end_pos].rstrip() + "\n"
                )
                break

        return stream_response['text']
