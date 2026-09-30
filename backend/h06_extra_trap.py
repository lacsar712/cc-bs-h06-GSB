from pass_polish import detail_sentence, list_label, no_half_polish, polish_after_save

def on_save(verdict: str) -> str:
    return polish_after_save(verdict)

def on_list(verdict: str) -> str:
    return list_label(verdict)

def on_detail(verdict: str, reason: str) -> str:
    return detail_sentence(verdict, reason)

def half_polish_left() -> bool:
    return not no_half_polish()
