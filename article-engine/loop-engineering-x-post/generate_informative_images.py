#!/usr/bin/env python3
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


OUT = Path(__file__).parent / "assets"
W, H = 1800, 720


def font(size, bold=False):
    path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf"
    return ImageFont.truetype(path, size)


F_TITLE = font(54, True)
F_SUB = font(26)
F_H = font(31, True)
F_B = font(24)
F_S = font(21)
F_TAG = font(20, True)


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def text_size(draw, text, fnt):
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0], box[3] - box[1]


def wrap(draw, text, fnt, max_w):
    lines = []
    for para in text.split("\n"):
        words = para.split()
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


def draw_wrapped(draw, xy, text, fnt, fill, max_w, line_gap=8):
    x, y = xy
    for line in wrap(draw, text, fnt, max_w):
        draw.text((x, y), line, font=fnt, fill=fill)
        y += text_size(draw, line, fnt)[1] + line_gap
    return y


def gradient_bg():
    img = Image.new("RGB", (W, H), "#101827")
    px = img.load()
    for y in range(H):
        for x in range(W):
            t = (x / W) * 0.65 + (y / H) * 0.35
            r = int(15 + 13 * t)
            g = int(23 + 20 * t)
            b = int(39 + 30 * t)
            px[x, y] = (r, g, b)
    return img


def header(draw, title, subtitle):
    draw.text((70, 48), title, font=F_TITLE, fill="#f8fafc")
    draw.text((73, 116), subtitle, font=F_SUB, fill="#b8c7db")


def arrow(draw, start, end, color="#8dd3ff", width=6):
    x1, y1 = start
    x2, y2 = end
    draw.line((x1, y1, x2, y2), fill=color, width=width)
    draw.polygon([(x2, y2), (x2 - 18, y2 - 12), (x2 - 18, y2 + 12)], fill=color)


def save(img, name):
    path = OUT / name
    img.save(path, "PNG")
    print(path)


def stages():
    img = gradient_bg()
    d = ImageDraw.Draw(img)
    header(d, "Loop Engineering Maturity Curve", "The human role shifts from typing prompts to designing the system that prompts agents.")

    cards = [
        ("1", "Line-by-line driving", "You write code. The model autocompletes.\nHuman role: coder\nPattern: Tab, Tab, Tab"),
        ("2", "Manual parallelism", "You run many agent sessions at once.\nHuman role: dispatcher\nPattern: switch, review, nudge"),
        ("3", "Loop authoring", "The system reads repo, issues, and CI, then decides what to prompt.\nHuman role: system designer\nPattern: write the loop"),
    ]
    xs = [78, 632, 1186]
    y = 218
    cw, ch = 520, 330
    colors = [("#15345c", "#38bdf8"), ("#123f43", "#2dd4bf"), ("#4b2f12", "#f59e0b")]
    for i, (num, title, body) in enumerate(cards):
        fill, accent = colors[i]
        x = xs[i]
        rounded(d, (x, y, x + cw, y + ch), 26, fill, "#38506b", 2)
        rounded(d, (x + 28, y + 30, x + 88, y + 90), 18, accent)
        d.text((x + 49, y + 43), num, font=F_H, fill="#08111f", anchor="mm")
        d.text((x + 110, y + 30), title, font=F_H, fill="#f8fafc")
        draw_wrapped(d, (x + 34, y + 118), body, F_B, "#dbeafe", cw - 68, 12)
        if i < 2:
            arrow(d, (x + cw + 16, y + ch // 2), (xs[i + 1] - 34, y + ch // 2), "#7dd3fc")

    rounded(d, (230, 598, 1570, 656), 18, "#0f172a", "#334155", 2)
    d.text((W // 2, 628), "Prompt writer -> session dispatcher -> loop author", font=F_H, fill="#f8fafc", anchor="mm")
    save(img, "loop-stages.png")


def anatomy():
    img = gradient_bg()
    d = ImageDraw.Draw(img)
    header(d, "Anatomy of a Loop System", "Five working parts sit on top of one persistent memory spine.")

    spine = (160, 510, 1640, 596)
    rounded(d, spine, 32, "#1e293b", "#64748b", 2)
    d.text((W // 2, 537), "MEMORY SPINE", font=F_H, fill="#f8fafc", anchor="mm")
    d.text((W // 2, 572), "records what happened, what was tried, and what still needs attention", font=F_S, fill="#cbd5e1", anchor="mm")

    items = [
        ("Heartbeat", "Scheduled trigger\nfinds work to do", "#38bdf8"),
        ("Work trees", "Isolated branches\nprevent file collisions", "#34d399"),
        ("Skill", "Project rules in\nSKILL.md", "#a78bfa"),
        ("Connectors", "MCP links to issues,\nDBs, Slack, PRs", "#fbbf24"),
        ("Sub-agents", "Separate coding\nfrom review", "#fb7185"),
    ]
    gap = 26
    cw = (W - 2 * 92 - 4 * gap) // 5
    cy = 218
    for i, (title, body, accent) in enumerate(items):
        x = 92 + i * (cw + gap)
        rounded(d, (x, cy, x + cw, cy + 210), 24, "#162033", "#334155", 2)
        rounded(d, (x + 22, cy + 22, x + 74, cy + 74), 16, accent)
        d.text((x + 88, cy + 34), title, font=F_H, fill="#f8fafc")
        draw_wrapped(d, (x + 26, cy + 92), body, F_B, "#dbeafe", cw - 52, 10)
        cx = x + cw // 2
        d.line((cx, cy + 210, cx, spine[1]), fill=accent, width=5)
        d.ellipse((cx - 8, spine[1] - 8, cx + 8, spine[1] + 8), fill=accent)

    rounded(d, (560, 633, 1240, 674), 16, "#0f172a", "#334155", 2)
    d.text((W // 2, 654), "The loop keeps running because state lives outside the chat.", font=F_B, fill="#e2e8f0", anchor="mm")
    save(img, "loop-anatomy.png")


def goal_vs_loop():
    img = gradient_bg()
    d = ImageDraw.Draw(img)
    header(d, "Claude Code: /goal vs /loop", "One is completion-driven. The other is schedule-driven.")

    left = (86, 190, 858, 590)
    right = (942, 190, 1714, 590)
    rounded(d, left, 28, "#14213d", "#38bdf8", 3)
    rounded(d, right, 28, "#261b37", "#c084fc", 3)
    d.text((150, 232), "/goal", font=font(48, True), fill="#7dd3fc")
    d.text((1006, 232), "/loop", font=font(48, True), fill="#d8b4fe")
    d.text((150, 292), "Run now until the condition is true", font=F_H, fill="#f8fafc")
    d.text((1006, 292), "Run the prompt again on a schedule", font=F_H, fill="#f8fafc")

    left_steps = ["Start now", "Execute a round", "Check: is the goal true?", "If yes, stop. If no, continue."]
    right_steps = ["Scheduled time arrives", "Run the prompt", "No completion judgment", "Repeat at the next scheduled time."]
    for x0, y0, steps, accent in [(150, 360, left_steps, "#38bdf8"), (1006, 360, right_steps, "#c084fc")]:
        for i, step in enumerate(steps):
            y = y0 + i * 52
            d.ellipse((x0, y, x0 + 28, y + 28), fill=accent)
            d.text((x0 + 14, y + 14), str(i + 1), font=F_TAG, fill="#08111f", anchor="mm")
            d.text((x0 + 48, y - 2), step, font=F_B, fill="#e2e8f0")

    rounded(d, (280, 626, 1520, 675), 18, "#0f172a", "#334155", 2)
    d.text((W // 2, 651), "Use /goal for finishable objectives. Use /loop for recurring scans and inboxes.", font=F_B, fill="#f8fafc", anchor="mm")
    save(img, "goal-vs-loop.png")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    stages()
    anatomy()
    goal_vs_loop()
