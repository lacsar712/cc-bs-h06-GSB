from h06_extra_trap import half_polish_left, on_save
from h06_tone_trap import armed

def test_polish():
    assert on_save("合格") == "越界"
    assert half_polish_left() and armed()
