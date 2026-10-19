#!/usr/bin/env python3
import re, glob, json, os
D = os.path.dirname(os.path.abspath(__file__))
IDX = json.load(open(os.path.join(D, '..', 'episodes', 'index.json'), encoding='utf-8'))

STYLE = ("Hand-drawn illustration for an adult literary thriller, gouache and dry brush on textured cream paper, visible pencil sketch lines, "
 "realistic human proportions, small realistic eyes, no cartoon exaggeration, no rosy cheeks, restrained muted palette of grey-blue, ochre and faded brown "
 "with a single warm accent, deep shadows, one strong light source, melancholic tense mood, Soviet book illustration influence, cinematic composition, "
 "authentic 1990s provincial Russia, 16:9, no text, no letters, no watermark.")
VIS = (" VISION STYLE: the image fractures at the edges into grey analog TV static, cold blue-black desaturated tones, fragmented dreamlike composition, one burning detail.")
DOC = (" DOCUMENT STYLE: aged paper collage, yellowed newsprint, typewritten sheets, envelopes with postmarks, squared school notebook, warm lamp light, camera looking down, gouache texture, no readable text.")

CORE = {
 'MITYA':'Митя (взрослый; варианты по возрасту/одежде — словами в промпте)', 'MITYA_KID':'Митя в 6 лет (пролог)',
 'LIZA':'Лиза', 'STRELNIKOV':'Стрельников (в промпте указан возраст: 23 / 42 / седой)', 'SIVY':'Сивый', 'MUKHA':'Муха',
 'VADIM':'Вадим Шумский', 'NIKOLAI':'Отец, Николай Степанович', 'GALINA':'Мать, Галина Петровна', 'LURIE':'Лурье',
 'BARABASH':'Майор Барабаш', 'GUSEV':'Сержант Гусев', 'RAKITIN':'Ракитин', 'IRINA':'Ирина Ракитина', 'KIRILL':'Кирилл Ракитин',
 'VANYA_THANH':'Ваня-вьетнамец (Нгуен Ван Тхань)', 'VIKA':'Вика Орлова', 'LANSKOY':'Майор ФСБ Ланской', 'RYZHOV':'Рыжов',
 'LASKIN':'Ласкин', 'VERA':'Вера Михайловна (в промптах: VERA MIKHAILOVNA)', 'UNCLE_TOLYA':'Дядя Толя Карасёв',
 'BABY_VANYA':'Ваня, сын Лизы (разный возраст — словами)', 'VANYA_SHUMSKY':'Ваня Шумский, 18 лет (кода и видения)',
 'STEPA':'Стёпа Клочков', 'ANNA_STEPANOVNA':'Анна Степановна', 'STRAKHOV':'Страхов',
}
SECOND = ['ZHORA','TAMARA','BRUSNITSYN','TSAPKIN','GLYANTSEV','KURBATOV','KOROBOV','MARKELYCH','ZINAIDA','NINA SERGEEVNA','KOTYA','NAYDA','PETROVICH','TATYANA','KOCHETKOV','ANATOLY','TYOTYA MZIA','LUKICH','REX','IRAIDA','DED EFIM','KUDRYASHOV','ANNA_STEPANOVNA']

def parse():
    eps = {}; cur = None; ch = None
    for f in sorted(glob.glob(os.path.join(D, 'src_*.txt'))):
        for line in open(f, encoding='utf-8'):
            line = line.rstrip('\n')
            if not line.strip(): continue
            if line.startswith('@EP'):
                cur = int(line.split()[1]); eps[cur] = []
            elif line.startswith('@CH'):
                parts = [x.strip() for x in line[3:].split('|')]
                ch = dict(n=int(parts[0]), title=parts[1], note=parts[2], items=[]); eps[cur].append(ch)
            elif re.match(r'^[SVD]\|', line):
                ch['items'].append((line[0], line[2:].strip()))
    return eps

def mmss(sec):
    sec = int(round(sec)); h, r = divmod(sec, 3600); m, s = divmod(r, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'

def tosec(t):
    p = [int(x) for x in t.split(':')]; return p[0]*3600+p[1]*60+p[2] if len(p)==3 else p[0]*60+p[1]

def main():
    eps = parse(); total = 0
    outdir = D
    for n, chs in eps.items():
        info = next(i for i in IDX if i['n'] == n)
        marks = {c: t for t, c, h in info['marks']}
        tot_sec = info['words'] / 105 * 60
        words = {}
        for c in info['chs']:
            w = len(re.findall(r'\w+', open(os.path.join(D, '..', '..', 'tts', 'chapters', f'{c:02d}.txt'), encoding='utf-8').read().replace('́','')))
            words[c] = w
        used = []
        md = [f"# Эпизод {n}. «{info['title']}» — раскадровка и промпты\n"]
        md.append(f"**Главы:** {info['chs'][0]}–{info['chs'][-1]} · **озвучка:** {info['min110']:.0f}–{info['min100']:.0f} мин · **кадров всего:** {sum(len(c['items']) for c in chs)}\n")
        txt = []; k = 0; plain = []
        body = []
        for ch in chs:
            start = tosec(marks[ch['n']]) if ch['n'] in marks else 0
            dur = words[ch['n']] / 105 * 60
            cnt = len(ch['items'])
            step = dur / cnt
            body.append(f"## Глава {ch['n']}. {ch['title']}\n")
            body.append(f"*{ch['note']}*\n")
            body.append(f"Начало главы ≈ **{mmss(start)}**, длительность ≈ {mmss(dur)}, кадров: {cnt}, менять каждые ≈ {mmss(step)}.\n")
            for i, (typ, text) in enumerate(ch['items']):
                k += 1
                full = text + ' ' + STYLE + (VIS if typ == 'V' else DOC if typ == 'D' else '')
                tag = {'S':'', 'V':' · видение', 'D':' · документ'}[typ]
                t0 = start + i * step
                body.append(f"**{k:02d}**{tag} · ≈ {mmss(t0)}\n")
                body.append(f"{k:02d}. {full}\n")
                plain.append(f"{k:02d}. {full}\n")
                for name in re.findall(r'\b[A-Z][A-Z_]{3,}\b', text):
                    if name in CORE and name not in used: used.append(name)
        md.append("**Как использовать в Google Flow.** Создайте перечисленных ниже героев один раз (по листам из `character_sheets.md`) и называйте их в промптах так же, как здесь, заглавными латинскими буквами. Каждый пронумерованный абзац — отдельный готовый промпт со стилем в конце. Метка «видение» значит, что кадр сломан помехами телевизора, «документ» — коллаж из бумаг. Время у каждого кадра примерное (для озвучки 105 слов в минуту); после монтажа сдвиньте под реальный звук.\n")
        md.append("**Герои этого эпизода (создать в Flow):** " + ', '.join(f"`{u}`" for u in used) + "\n")
        md.append("**Второстепенные** (ZHORA, TAMARA, BRUSNITSYN и т. п.) описаны прямо в промптах; для них можно создать героев, если хотите единый облик.\n")
        md.append("---\n")
        md += body
        open(os.path.join(outdir, f'ep{n:02d}_storyboard.md'), 'w', encoding='utf-8').write('\n'.join(md) + '\n')
        open(os.path.join(outdir, f'ep{n:02d}_prompts_only.txt'), 'w', encoding='utf-8').write('\n'.join(plain))
        total += k
        print(n, k, len(' '.join(plain).split()))
    print('total prompts', total)
main()
