"""Offline model boundary for collection tests using the real relevance scorer."""

import re


class RelevanceModel:
    """Score explicit eligible indexes; material and result validation stay real."""

    def __init__(self):
        self.requests = []

    async def chat_json(self, messages, **kwargs):
        self.requests.append(messages)
        indexes = re.findall(r"^### 论文 #(\d+)$", messages[-1]["content"], re.MULTILINE)
        if not indexes:
            raise AssertionError("RelevanceModel only handles abstract screening requests")
        return [{"index": int(index), "status": "scored", "score": 8, "relevance": 8,
                 "best_direction": None, "reason": "Relevant portfolio research in the abstract"}
                for index in indexes]
