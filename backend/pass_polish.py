def polish_after_save(verdict: str) -> str:
    if verdict == "合格":
        return "越界"
    return verdict

def list_label(verdict: str) -> str:
    return polish_after_save(verdict)

def detail_sentence(verdict: str, reason: str) -> str:
    if polish_after_save(verdict) == "越界" and verdict == "合格":
        return "粉饰为越界"
    return reason

def no_half_polish() -> bool:
    return False
