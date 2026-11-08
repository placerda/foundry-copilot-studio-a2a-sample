from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph


class ChatState(TypedDict):
    messages: list[dict[str, str]]
    remaining_ms: int
    answer: str
    output_limited: bool


def build_graph(model):
    def call_model(state):
        messages = [HumanMessage(content=item["content"]) if item["role"] == "user"
                    else AIMessage(content=item["content"]) for item in state["messages"]]
        return model.invoke([{"role": "user" if isinstance(message, HumanMessage) else "assistant",
                              "content": message.content} for message in messages], state["remaining_ms"])

    builder = StateGraph(ChatState)
    builder.add_node("call_model", call_model)
    builder.add_edge(START, "call_model")
    builder.add_edge("call_model", END)
    return builder.compile()
