# -*- coding: utf-8 -*-
import json


def run_left_right_analysis(characters):
  total = 0
  right = 0
  left = 0
  for data in characters.values():
    if 'decomposition' not in data or 'etymology' not in data:
      continue
    (decomposition, etymology) = (data['decomposition'], data['etymology'])
    if etymology['type'] != 'pictophonetic':
      continue
    if decomposition[0] != '⿰' or len(decomposition) != 3:
      continue
    total += 1
    phonetic = etymology.get('phonetic')
    if phonetic == decomposition[1]:
      left += 1
    if phonetic == decomposition[2]:
      right += 1
  print('(total, left, right):', (total, left, right))


def run_stroke_count_analysis(characters):
  total = 0
  counts = [0, 0, 0]
  phonetic_stroke_total = 0
  semantic_stroke_total = 0
  for data in characters.values():
    etymology = data.get('etymology', {})
    if 'phonetic' not in etymology or 'semantic' not in etymology:
      continue
    (phonetic, semantic) = (etymology['phonetic'], etymology['semantic'])
    if phonetic not in characters or semantic not in characters:
      continue
    total += 1
    phonetic_strokes = len(characters[phonetic]['matches'])
    semantic_strokes = len(characters[semantic]['matches'])
    phonetic_stroke_total += phonetic_strokes
    semantic_stroke_total += semantic_strokes
    # Python3 doesn't have cmp(); map -1/0/1 -> indices 0/1/2
    cmp_val = (phonetic_strokes > semantic_strokes) - (phonetic_strokes < semantic_strokes)
    counts[cmp_val + 1] += 1
  mean = lambda x: 1.0 * x / total if total else 0.0
  print('(total, counts, phonetic_mean, semantic_mean):', (
    total, list(map(mean, counts)),
    mean(phonetic_stroke_total), mean(semantic_stroke_total))
  )


if __name__ == '__main__':
  characters = {}
  # dictionary.txt contains UTF-8 JSON lines; load it as text.
  with open('dictionary.txt', encoding='utf-8') as f:
    for line in f:
      line = line.strip()
      if not line:
        continue
      data = json.loads(line)
      characters[data['character']] = data
  run_left_right_analysis(characters)
  run_stroke_count_analysis(characters)
