"""Small shared helpers; never changes user source files."""
import json
import re
import unicodedata
from pathlib import Path

RADICALS = dict(zip('⻓⻅⻋⻜⻥⻰⻛⻔⻬⻢⻚⻆⻄', '长见车飞鱼龙风门齐马页角西'))


def normalize_glyphs(text):
    # Normalize compatibility Han only, preserving punctuation, case, and tone.
    return ''.join(RADICALS.get(c, unicodedata.normalize('NFKC', c)
                   if 0x2E80 <= ord(c) <= 0x2FFF or 0xF900 <= ord(c) <= 0xFAFF
                   else c) for c in text)


def compact(text):
    return re.sub(r'\s+', '', normalize_glyphs(text))


def output_dir(value):
    path = Path(value).expanduser().resolve()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError('Output directory must be new or empty; use a new work/ subdirectory.')
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
