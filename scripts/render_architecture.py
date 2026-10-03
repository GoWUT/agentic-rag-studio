"""Render the README architecture as SVG; --png also exports with Pillow.

Run from any directory: python scripts/render_architecture.py --png
SVG generation uses only the standard library. PNG uses the project's Pillow.
"""
from __future__ import annotations

import argparse
from html import escape
from pathlib import Path

WIDTH, HEIGHT = 1280, 990
BACKGROUND = '#f8fafc'
INK, MUTED = '#14253d', '#52647b'
BLUE, PURPLE, TEAL = '#2563eb', '#6d28d9', '#087f8c'


class Canvas:
    def __init__(self, png: bool):
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title description">',
            '<title id="title">Agentic RAG Studio system architecture</title>',
            '<desc id="description">Streamlit connects to FastAPI with live authentication and RBAC. PostgreSQL holds business state, durable queue jobs and checkpoints. An independent worker restores the original actor and executes bounded LangGraph workflows. Retrieval, analysis and governed MCP tools use shared workspace files. Coding Agent reads code and returns textual suggestions. API and Worker export optional redacted telemetry. SQLite inline mode is a local alternative.</desc>',
            f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{BACKGROUND}"/>',
            '<g font-family="Segoe UI, Arial, sans-serif">',
        ]
        self.png = png
        if png:
            from PIL import Image, ImageDraw
            self.im = Image.new('RGB', (WIDTH * 2, HEIGHT * 2), BACKGROUND)
            self.draw = ImageDraw.Draw(self.im)

    def rect(self, x, y, w, h, fill, stroke='#d9e2ef', radius=16):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>')
        if self.png:
            self.draw.rounded_rectangle((x*2, y*2, (x+w)*2, (y+h)*2), radius=radius*2, fill=fill, outline=stroke, width=2)

    def text(self, x, y, value, size=16, color=INK, bold=False):
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}">{escape(value)}</text>')
        if self.png:
            from PIL import ImageFont
            # Segoe UI on Windows, DejaVu Sans on Linux; no downloaded fonts.
            candidates = [Path('C:/Windows/Fonts') / ('segoeuib.ttf' if bold else 'segoeui.ttf'),
                          Path('/usr/share/fonts/truetype/dejavu') / ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')]
            font_path = next((p for p in candidates if p.exists()), None)
            if font_path is None:
                raise RuntimeError('PNG export requires Segoe UI or DejaVu Sans; SVG is font-portable')
            font = ImageFont.truetype(str(font_path), size*2)
            self.draw.text((x*2, y*2), value, fill=color, font=font, anchor='ls')

    def arrow(self, points, color=BLUE):
        coords = ' '.join(f'{x},{y}' for x,y in points)
        self.parts.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>')
        x,y = points[-1]; px,py = points[-2]
        if x > px: head = [(x,y), (x-9,y-5), (x-9,y+5)]
        elif x < px: head = [(x,y), (x+9,y-5), (x+9,y+5)]
        elif y > py: head = [(x,y), (x-5,y-9), (x+5,y-9)]
        else: head = [(x,y), (x-5,y+9), (x+5,y+9)]
        self.parts.append(f'<polygon points="{" ".join(f"{a},{b}" for a,b in head)}" fill="{color}"/>')
        if self.png:
            self.draw.line([(a*2,b*2) for a,b in points], fill=color, width=5)
            self.draw.polygon([(a*2,b*2) for a,b in head], fill=color)

    def save(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        (directory/'architecture.svg').write_text('\n'.join(self.parts + ['</g></svg>'])+'\n', encoding='utf-8')
        if self.png:
            self.im.save(directory/'architecture.png', optimize=True)


def render(png=False):
    c = Canvas(png)
    c.rect(44, 32, 182, 27, '#e8efff', '#e8efff', 8)
    c.text(56, 51, 'SYSTEM ARCHITECTURE', 13, BLUE, True)
    c.text(44, 103, 'Agentic RAG Studio', 36, INK, True)
    c.text(44, 133, 'Evidence-grounded research · durable execution · live workspace authority', 18, MUTED)

    c.rect(44, 177, 248, 179, '#ffffff')
    c.text(64, 209, '01  EXPERIENCE', 12, BLUE, True)
    c.text(64, 246, 'Streamlit workspace', 21, INK, True)
    c.text(64, 278, 'Chat · sources · datasets', 16, MUTED)
    c.text(64, 306, 'Tasks · agents · approvals', 16, MUTED)
    c.text(64, 335, 'Session-local auth tokens', 15, MUTED)

    c.rect(336, 177, 556, 179, '#ffffff')
    c.text(358, 209, '02  API & AUTHORITY', 12, BLUE, True)
    c.text(358, 246, 'FastAPI + scoped services', 23, INK, True)
    c.text(358, 278, 'JWT identity · live status · OWNER / EDITOR / VIEWER', 17, MUTED)
    c.text(358, 306, 'Private sessions · asset access · durable task control', 17, MUTED)
    c.text(358, 335, 'Argon2id · rotating refresh · membership & audit', 16, MUTED)

    c.rect(936, 177, 300, 179, '#eef5ff', '#bfcef0')
    c.text(958, 209, '03  DURABLE STATE', 12, BLUE, True)
    c.text(958, 246, 'PostgreSQL', 23, INK, True)
    c.text(958, 278, 'Business · accounts · audit', 17, MUTED)
    c.text(958, 306, 'Procrastinate queue', 17, MUTED)
    c.text(958, 335, 'LangGraph checkpoints', 17, MUTED)

    c.arrow([(292,266),(336,266)])
    c.arrow([(892,266),(936,266)])
    c.arrow([(1086,356),(1086,427)])
    c.text(1099,397, 'claim job', 14, BLUE)
    c.arrow([(614,356),(614,427)], PURPLE)
    c.text(629,397, 'inline alternative', 14, PURPLE)

    c.rect(44, 427, 848, 247, '#f5f1ff', '#d7c9f3')
    c.text(66, 461, '04  BOUNDED AGENT RUNTIME', 12, PURPLE, True)
    c.text(66, 498, 'ExecutionHarness + LangGraph', 24, INK, True)
    c.text(66, 530, 'Cost-aware supervisor: research workflow / single agent / delegation', 17, MUTED)
    c.rect(66, 550, 804, 47, '#ffffff', '#ded4f1', 9)
    c.text(88, 580, 'Research Agent', 17, PURPLE, True)
    c.text(307, 580, 'Data Agent', 17, PURPLE, True)
    c.text(484, 580, 'Coding Agent', 17, PURPLE, True)
    c.text(676, 580, 'Reviewer Agent', 17, PURPLE, True)
    c.text(66, 628, 'Private context · scoped memory · parallel delegation · selective review', 17, MUTED)
    c.text(66, 655, 'Call / token / time budgets · task events · persistent graph checkpoints', 17, MUTED)

    c.rect(936, 427, 300, 247, '#ffffff')
    c.text(958, 461, '05  INDEPENDENT WORKER', 12, BLUE, True)
    c.text(958, 498, 'Execute as task creator', 20, INK, True)
    c.text(958, 535, 'Live account + membership', 17, MUTED)
    c.text(958, 568, 'Expected-version guard', 17, MUTED)
    c.text(958, 601, 'Bounded retry · heartbeat', 17, MUTED)
    c.text(958, 638, 'Resume from durable state', 17, MUTED)
    c.arrow([(936,517),(892,517)], PURPLE)

    c.arrow([(236,674),(236,744)], TEAL)
    c.arrow([(676,674),(676,744)], TEAL)
    c.text(249,715, 'retrieve evidence', 14, TEAL)
    c.text(689,715, 'execute tools', 14, TEAL)

    c.rect(44, 744, 408, 165, '#ffffff')
    c.text(66, 776, '06  RESEARCH & SHARED FILES', 12, TEAL, True)
    c.text(66, 813, 'Workspace-scoped retrieval', 21, INK, True)
    c.text(66, 845, 'Dense + BM25 · RRF · optional reranker', 16, MUTED)
    c.text(66, 873, 'PDF / OCR · Chroma · page citations', 16, MUTED)
    c.text(66, 895, 'Shared files: datasets · artifacts · model cache', 14, MUTED)

    c.rect(484, 744, 408, 165, '#ffffff')
    c.text(506, 776, '07  GOVERNED ACTIONS', 12, TEAL, True)
    c.text(506, 813, 'Analysis + MCP tools', 21, INK, True)
    c.text(506, 845, 'MCP writes: RBAC → policy → approval', 16, MUTED)
    c.text(506, 873, 'Recheck authority → claim → receipt', 16, MUTED)
    c.text(506, 895, 'Constrained subprocess; not an OS sandbox', 14, MUTED)

    c.rect(936, 744, 300, 165, '#eff8f7', '#c6e3de')
    c.text(958, 776, '08  OPTIONAL TELEMETRY', 12, TEAL, True)
    c.text(958, 813, 'API + Worker export', 20, INK, True)
    c.text(958, 845, 'Redacted logs · OTel · LangSmith', 15, MUTED)
    c.text(958, 873, 'OTLP trace / metric export', 15, MUTED)
    c.text(958, 895, 'Prometheus metrics endpoint', 14, MUTED)

    c.text(44, 950, 'Shown: PostgreSQL worker mode. Local alternative: SQLite + inline execution.', 15, MUTED)
    c.text(44, 975, 'Implemented runtime: native API / Worker / UI processes; shared local workspace files.', 15, MUTED)
    c.save(Path(__file__).resolve().parents[1]/'docs'/'assets')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--png', action='store_true', help='Also export a 2x PNG using Pillow')
    render(parser.parse_args().png)
