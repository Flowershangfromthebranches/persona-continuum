"""Route free-form user guidance conservatively; ambiguous notes stay guidance."""

import re


def parse_persona_notes(notes: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {
        "instruction": [], "identity_hint": [],
        "user_supplied_fact": [], "research_constraint": [],
    }
    for sentence in re.split(r"[。！？\n;；]+", notes):
        sentence = sentence.strip()
        if not sentence:
            continue
        if re.search(r"不要|禁止|不联网|仅使用|主要依据|联网补充|上网补充", sentence):
            kind = "research_constraint"
        elif re.search(r"重点|研究|关注|请|希望", sentence):
            kind = "instruction"
        elif re.search(r"我确认|确认的信息|确定的事实|我亲眼", sentence):
            kind = "user_supplied_fact"
        elif re.search(r"角色|同名|作品|身份|英文名", sentence):
            kind = "identity_hint"
        else:
            kind = "instruction"
        result[kind].append(sentence)
    return result


def notes_allow_web(notes: str) -> bool:
    if re.search(r"不要.{0,8}(联网|上网)|不联网|禁止.{0,8}(联网|上网)", notes):
        return False
    return bool(re.search(r"(联网|上网|网络).{0,8}(补充|搜索|研究)", notes))
