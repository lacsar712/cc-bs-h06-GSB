from h06_extra_trap import half_polish_left, on_list, on_save

def paint(verdict: str) -> str:
    return on_list(on_save(verdict))

def armed() -> bool:
    return half_polish_left()
