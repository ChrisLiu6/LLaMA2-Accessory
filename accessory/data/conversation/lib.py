import dataclasses
from enum import auto, Enum
from typing import List, Tuple


class SeparatorStyle(Enum):
    """Different separator style."""
    SINGLE = auto()
    TWO = auto()


@dataclasses.dataclass
class Conversation:
    """A class that keeps all conversation history."""
    system: str
    roles: Tuple[str, str]
    messages: List | Tuple
    sep_style: SeparatorStyle = SeparatorStyle.SINGLE
    sep: str = "###"
    sep2: str = None
    version: str = "Unknown"

    skip_next: bool = False

    def process(self, space_part_of_next_word=True):
        l_to_predict = []
        if self.sep_style == SeparatorStyle.SINGLE:
            ret = self.system + '\n\n' + self.sep
            for i, (role, message) in enumerate(self.messages):
                if message is not None:
                    if type(message) is tuple:
                        message, _, _ = message
                    ret += " " + role + ": " + message + '\n' + self.sep
                    if role == self.roles[1]:
                        to_predict_value = message + '\n' + self.sep
                        if space_part_of_next_word:
                            to_predict_value = " " + to_predict_value
                        l_to_predict.append(to_predict_value)
                else:
                    assert i == len(self.messages) - 1, "only last message can be None"
                    ret += " " + role + ":"
                    if not space_part_of_next_word:
                        ret += " "
        elif self.sep_style == SeparatorStyle.TWO:
            seps = [self.sep, self.sep2]
            ret = self.system + seps[0]
            for i, (role, message) in enumerate(self.messages):
                if message:
                    if type(message) is tuple:
                        message, _, _ = message
                    ret += " " + role + ": " + message + seps[i % 2]
                    if role == self.roles[1]:
                        to_predict_value = message + seps[i % 2]
                        if space_part_of_next_word:
                            to_predict_value = " " + to_predict_value
                        l_to_predict.append(to_predict_value)
                else:
                    assert i == len(self.messages) - 1, "only last message can be None"
                    ret += " " + role + ":"
                    if not space_part_of_next_word:
                        ret += " "
        else:
            raise ValueError(f"Invalid style: {self.sep_style}")

        result = {
            "conv": ret,  # text involving the complete conversation
            "to_predict": l_to_predict  # the list of values that model should learn to predict during training
        }
        return result

    def get_prompt(self, space_part_of_next_word=True):
        return self.get_prompt(space_part_of_next_word)['conv']

    def append_message(self, role, message):
        self.messages.append([role, message])

    def copy(self):
        return Conversation(
            system=self.system,
            roles=self.roles,
            messages=[[x, y] for x, y in self.messages],
            sep_style=self.sep_style,
            sep=self.sep,
            sep2=self.sep2)


def conv_v1():
    conv = Conversation(
        system="A chat between a curious human and an artificial intelligence assistant. "
               "The assistant gives helpful, detailed, and polite answers to the human's questions.",
        roles=("Human", "Assistant"),
        messages=(),
        sep_style=SeparatorStyle.SINGLE,
        sep="###",
    )
    return conv


def conv_v1_2():
    conv = Conversation(
        system="A chat between a curious human and an artificial intelligence assistant. "
               "The assistant gives helpful, detailed, and polite answers to the human's questions.",
        roles=("Human", "Assistant"),
        messages=(),
        sep_style=SeparatorStyle.SINGLE,
        sep="###",
    )
    return conv


def conv_vicuna_v1_1():
    conv = Conversation(
        system="A chat between a curious user and an artificial intelligence assistant. "
               "The assistant gives helpful, detailed, and polite answers to the user's questions.",
        roles=("USER", "ASSISTANT"),
        version="v1",
        messages=(),
        sep_style=SeparatorStyle.TWO,
        sep=" ",
        sep2="</s>",
    )
    return conv


def conv_bair_v1():
    conv = Conversation(
        system="BEGINNING OF CONVERSATION:",
        roles=("USER", "GPT"),
        messages=(),
        sep_style=SeparatorStyle.TWO,
        sep=" ",
        sep2="</s>",
    )
    return conv


def simple_conv():
    conv = Conversation(
        system="A chat between a curious human and an artificial intelligence assistant. "
               "The assistant gives helpful, detailed, and polite answers to the human's questions.",
        roles=("Human", "Assistant"),
        messages=(),
        sep_style=SeparatorStyle.SINGLE,
        sep="###",
    )
    return conv


def simple_conv_multimodal():
    conv = Conversation(
        system="You are LLaVA, a large language and vision assistant trained by UW Madison WAIV Lab."
               "You are able to understand the visual content that the user provides, and assist the user with a variety of tasks using natural language."
               "Follow the instructions carefully and explain your answers in detail.",
        roles=("Human", "Assistant"),
        messages=(),
        sep_style=SeparatorStyle.SINGLE,
        sep="###",
    )
    return conv


def conv_llava_v1():
    conv = Conversation(
        system="You are LLaVA, a large language and vision assistant trained by UW Madison WAIV Lab."
               "You are able to understand the visual content that the user provides, and assist the user with a variety of tasks using natural language."
               "Follow the instructions carefully and explain your answers in detail.",
        roles=("USER", "ASSISTANT"),
        version="v1",
        messages=(),
        sep_style=SeparatorStyle.TWO,
        sep=" ",
        sep2="</s>",
    )
    return conv


default_conversation = conv_v1_2
conv_templates = {
    "default": conv_v1_2,
    "simple": simple_conv,
    "multimodal": simple_conv_multimodal,
    "llava_v1": conv_llava_v1,

    # fastchat
    "v1": conv_v1_2,
    "bair_v1": conv_bair_v1,
    "vicuna_v1_1": conv_vicuna_v1_1,
}

if __name__ == "__main__":
    print(default_conversation.get_prompt())
