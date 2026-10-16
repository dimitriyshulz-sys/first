#!/usr/bin/env python3
"""Снять знаки ударения (U+0301) — если модель на них спотыкается.
Запуск: python3 tts/strip_accents.py tts/full.txt > tts/full_noaccent.txt"""
import sys
sys.stdout.write(open(sys.argv[1], encoding='utf-8').read().replace('́', ''))
