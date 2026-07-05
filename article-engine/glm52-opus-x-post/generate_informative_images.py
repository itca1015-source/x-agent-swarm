#!/usr/bin/env python3
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import random


OUT = Path(__file__).parent / "assets"
W, H = 1800, 720


def font(size, bold=False):
    path = "/System/Library/Fonts/Avenir Next.ttc"
    index = 2 if bold else 7
    return ImageFont.truetype(path, size, index=index)


F_TITLE = font(58, True)
F_SUB = font(26)
F_CARD = font(36, True)
F_BODY = font(28)
F_SMALL = font(22)
F_TAG = font(20, True)
F_BIG = font(74, True)

INK = "#172033"
MUTED = "#5b6677"
PAPER = "#fff7ed"
LINE = "#263241"
BLUE = "#2f80ed"
TEAL = "#0f9f8f"
CORAL = "#ef6f61"
YELLOW = "#f4b942"
PURPLE = "#7c5cff"
GREEN = "#48a868"
DARK = "#111827"


def make_bg(seed):
    random.seed(seed)
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    for _ in range(6500):
        x = random.randrange(W)
        y = random.randrange(H)
        d.point((x, y), fill=random.choice(["#fff1df", "#f8eadc", "#fffaf2", "#f4e5d6"]))
    for _ in range(15):
        x = random.randrange(-80, W - 20)
        y = random.randrange(-80, H - 20)
        r = random.randrange(34, 92)
        d.ellipse((x, y, x + r, y + r), fill=random.choice(["#dbeafe", "#dcfce7", "#fef3c7", "#ffe4e6", "#ede9fe"]))
    return img


def text_size(draw, value, fnt):
    box = draw.textbbox((0, 0), value, font=fnt)
    return box[2] - box[0], box[3] - box[1]


def wrap(draw, value, fnt, max_w):
    lines = []
    for raw in value.split("\n"):
        words = raw.split()
        line = ""
        for word in words:
            trial = word if not line else f"{line} {word}"
            if text_size(draw, trial, fnt)[0] <= max_w:
                line = trial
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
    return lines


def draw_text(draw, xy, value, fnt, fill=INK, max_w=None, gap=12, anchor=None):
    x, y = xy
    lines = wrap(draw, value, fnt, max_w) if max_w else value.split("\n")
    for line in lines:
        draw.text((x, y), line, font=fnt, fill=fill, anchor=anchor)
        y += text_size(draw, line, fnt)[1] + gap
    return y


def rounded(draw, box, radius, fill, outline=LINE, width=3):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def shadow(draw, box, radius=28, offset=10):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle((x1 + offset, y1 + offset, x2 + offset, y2 + offset), radius=radius, fill="#00000018")


def header(draw, title, subtitle, label):
    rounded(draw, (70, 42, 218, 80), 19, DARK, DARK, 1)
    draw.text((144, 61), label, font=F_TAG, fill="#ffffff", anchor="mm")
    draw.text((70, 112), title, font=F_TITLE, fill=INK)
    draw_text(draw, (74, 184), subtitle, F_SUB, MUTED, 1240, 8)


def card(draw, box, title, body, fill, accent):
    shadow(draw, box, 28, 8)
    rounded(draw, box, 28, fill, LINE, 3)
    x1, y1, x2, _ = box
    rounded(draw, (x1 + 28, y1 + 30, x1 + 76, y1 + 78), 14, accent, accent, 1)
    draw.text((x1 + 102, y1 + 31), title, font=F_CARD, fill=INK)
    draw_text(draw, (x1 + 34, y1 + 122), body, F_BODY, INK, x2 - x1 - 70, 14)


def pill(draw, xy, value, fill, fg=INK, fnt=F_SMALL):
    x, y = xy
    w, h = text_size(draw, value, fnt)
    rounded(draw, (x, y, x + w + 34, y + 40), 20, fill, fill, 1)
    draw.text((x + 17, y + 20), value, font=fnt, fill=fg, anchor="lm")


def arrow(draw, start, end, color=INK, width=7):
    x1, y1 = start
    x2, y2 = end
    draw.line((x1, y1, x2, y2), fill=color, width=width)
    if x2 >= x1:
        pts = [(x2, y2), (x2 - 24, y2 - 15), (x2 - 24, y2 + 15)]
    else:
        pts = [(x2, y2), (x2 + 24, y2 - 15), (x2 + 24, y2 + 15)]
    draw.polygon(pts, fill=color)


def save(img, name):
    OUT.mkdir(exist_ok=True)
    path = OUT / name
    img.save(path, "PNG")
    print(path)


def benchmark_to_taste():
    img = make_bg(11)
    d = ImageDraw.Draw(img)
    header(d, "Benchmarks Flatten", "When scores stop separating models, judgment moves to taste.", "02 / 06")
    card(d, (88, 266, 720, 562), "Old ruler", "Leaderboard gaps\nCapability race\nTiny score wins", "#dbeafe", BLUE)
    card(d, (1080, 266, 1712, 562), "New ruler", "Restraint\nHierarchy\nProduct feel", "#dcfce7", TEAL)
    arrow(d, (750, 410), (1050, 410), TEAL, 9)
    rounded(d, (760, 338, 1040, 486), 36, DARK, DARK, 1)
    d.text((900, 386), "Taste", font=F_BIG, fill="#ffffff", anchor="mm")
    d.text((900, 448), "decides", font=F_SMALL, fill="#fef3c7", anchor="mm")
    save(img, "benchmark-to-taste.png")


def taste_split():
    img = make_bg(12)
    d = ImageDraw.Draw(img)
    header(d, "The Split", "GLM looked stronger at visual feel. Opus held up better on constraints.", "03 / 06")
    card(d, (90, 262, 770, 590), "GLM-5.2", "Cleaner visuals\nLess overdone\nBetter feel", "#dcfce7", TEAL)
    card(d, (1030, 262, 1710, 590), "Opus 4.8", "Logic holds\nGame playable\nConstraints matter", "#ede9fe", PURPLE)
    rounded(d, (790, 330, 1010, 522), 36, "#fff7ed", LINE, 4)
    d.text((900, 386), "mixed", font=font(36, True), fill=INK, anchor="mm")
    d.text((900, 448), "result", font=font(36, True), fill=INK, anchor="mm")
    save(img, "taste-split.png")


def shell_vs_brain():
    img = make_bg(13)
    d = ImageDraw.Draw(img)
    header(d, "Shell vs Brain", "The tool experience and the model underneath are different layers.", "01 / 06")
    card(d, (86, 254, 650, 570), "Claude Code", "Interface\nWorkflow\nHarness", "#dbeafe", BLUE)
    rounded(d, (770, 330, 1030, 494), 34, DARK, DARK, 1)
    d.text((900, 382), "base_url", font=font(36, True), fill="#ffffff", anchor="mm")
    d.text((900, 440), "points elsewhere", font=F_SMALL, fill="#d1d5db", anchor="mm")
    card(d, (1150, 254, 1714, 570), "GLM-5.2", "Reasoning\nGeneration\nThe brain", "#fef3c7", YELLOW)
    arrow(d, (660, 412), (758, 412), BLUE, 8)
    arrow(d, (1042, 412), (1140, 412), YELLOW, 8)
    pill(d, (656, 612), "The brain is swappable", "#111827", "#ffffff", F_SMALL)
    save(img, "shell-vs-brain.png")


def price_as_weapon():
    img = make_bg(14)
    d = ImageDraw.Draw(img)
    header(d, "Price Becomes A Weapon", "Once capability is close enough, cheaper supply changes the buyer's math.", "04 / 06")
    card(d, (86, 270, 560, 566), "Capability", "Close enough for\nreal workflows", "#dbeafe", BLUE)
    card(d, (663, 270, 1137, 566), "Cost", "Around 1/5\nin Nick's test", "#fee2e2", CORAL)
    card(d, (1240, 270, 1714, 566), "Supply", "z.ai\nOpenRouter\nFireworks\nDeepInfra", "#dcfce7", GREEN)
    arrow(d, (578, 418), (644, 418), TEAL, 8)
    arrow(d, (1155, 418), (1222, 418), TEAL, 8)
    rounded(d, (416, 616, 1384, 674), 24, DARK, DARK, 1)
    d.text((900, 645), "Cheap stops being a compromise. It becomes an attack.", font=F_SMALL, fill="#ffffff", anchor="mm")
    save(img, "price-as-weapon.png")


def ownership_layer():
    img = make_bg(15)
    d = ImageDraw.Draw(img)
    header(d, "The Ownership Layer", "Rented intelligence and owned intelligence have different failure modes.", "05 / 06")
    card(d, (92, 260, 792, 590), "API model", "Can be removed\nPrice can jump\nPolicy can change", "#fee2e2", CORAL)
    card(d, (1008, 260, 1708, 590), "Local model", "Runs on your disk\nWorks offline\nHarder to take away", "#dcfce7", GREEN)
    rounded(d, (812, 342, 988, 508), 40, DARK, DARK, 1)
    d.text((900, 392), "rent", font=font(34, True), fill="#fecaca", anchor="mm")
    d.text((900, 456), "own", font=font(48, True), fill="#bbf7d0", anchor="mm")
    save(img, "ownership-layer.png")


def moat_shift():
    img = make_bg(16)
    d = ImageDraw.Draw(img)
    header(d, "The Moat Moves", "If intelligence is swappable, advantage shifts outside the model.", "06 / 06")
    card(d, (88, 266, 586, 566), "Old moat", "Best model\nHighest score\nLocked API", "#ede9fe", PURPLE)
    arrow(d, (620, 414), (740, 414), TEAL, 8)
    rounded(d, (772, 244, 1714, 592), 32, "#ffffff", LINE, 3)
    d.text((820, 302), "New moat", font=F_CARD, fill=INK)
    items = [
        ("Taste", BLUE),
        ("Cost", CORAL),
        ("Reliability", GREEN),
        ("Ownership", YELLOW),
        ("UX", PURPLE),
    ]
    x, y = 826, 370
    for label, color in items:
        pill(d, (x, y), label, color, "#ffffff" if color in [BLUE, CORAL, GREEN, PURPLE] else INK, F_SMALL)
        x += text_size(d, label, F_SMALL)[0] + 84
        if x > 1510:
            x = 826
            y += 70
    rounded(d, (818, 520, 1668, 560), 18, "#f8fafc", "#f8fafc", 1)
    d.text((1243, 540), "The model becomes a replaceable component.", font=F_SMALL, fill=MUTED, anchor="mm")
    save(img, "moat-shift.png")


if __name__ == "__main__":
    shell_vs_brain()
    benchmark_to_taste()
    taste_split()
    price_as_weapon()
    ownership_layer()
    moat_shift()
