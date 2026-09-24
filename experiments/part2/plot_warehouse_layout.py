#!/usr/bin/env python3
"""Draw warehouse resources and Auth membership from warehouse_layout.json.

Usage:
    python3 plot_warehouse_layout.py warehouse_layout.json
    python3 plot_warehouse_layout.py warehouse_layout.json -o setup.svg
    python3 plot_warehouse_layout.py warehouse_layout.json -o setup.svg --png setup.png

SVG output uses only the Python standard library. Optional PNG output requires
Pillow (python3 -m pip install pillow). Robot positions are deliberately omitted:
they are not present in warehouse_layout.json. Shading denotes resource envelopes,
not exact rank-partition boundaries. Source coordinate units are left unchanged.
"""
import argparse
import html
import json
from collections import Counter
from pathlib import Path

COLORS = ['#2878B5', '#BA7B0C', '#24866D', '#9855A0', '#C4544E', '#596BB0', '#94713C', '#418E99']


class Canvas:
    def __init__(self, width, height, png=False):
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
                      '<title>Warehouse resource layout and Auth membership</title>',
                      '<desc>Resources plotted at their stored x and y coordinates, colored by Auth zone. Entity counts are listed per Auth.</desc>']
        self.image = None
        if png:
            try:
                from PIL import Image, ImageDraw, ImageFont
            except ImportError as exc:
                raise ValueError('PNG requires Pillow: python3 -m pip install pillow') from exc
            self.image = Image.new('RGB', (width * 2, height * 2), 'white')
            self.draw = ImageDraw.Draw(self.image)
            self.fonts = ImageFont
        self.rect(0, 0, width, height, 'white')

    def rect(self, x, y, width, height, fill, stroke=None):
        self.parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}" fill="{fill}" stroke="{stroke or fill}"/>')
        if self.image:
            self.draw.rectangle((x*2, y*2, (x+width)*2, (y+height)*2), fill=fill, outline=stroke)

    def line(self, x, y, xx, yy, color='#dddddd'):
        self.parts.append(f'<path d="M{x:.2f},{y:.2f} L{xx:.2f},{yy:.2f}" stroke="{color}" fill="none"/>')
        if self.image:
            self.draw.line((x*2, y*2, xx*2, yy*2), fill=color, width=2)

    def text(self, x, y, text, size=17, color='#222222'):
        self.parts.append(f'<text x="{x:.2f}" y="{y:.2f}" fill="{color}" font-family="Arial,sans-serif" font-size="{size}">{html.escape(str(text))}</text>')
        if self.image:
            font = None
            for name in ['Arial.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf', 'DejaVuSans.ttf']:
                try:
                    font = self.fonts.truetype(name, size*2)
                    break
                except OSError:
                    pass
            if font is None:
                font = self.fonts.load_default(size=size*2)
            self.draw.text((x*2, y*2), str(text), font=font, fill=color, anchor='ls')

    def save(self, svg_path, png_path=None):
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text('\n'.join(self.parts + ['</svg>']), encoding='utf-8')
        if png_path:
            png_path.parent.mkdir(parents=True, exist_ok=True)
            self.image.save(png_path)


def pale(color):
    rgb = [int(color[i:i+2], 16) for i in (1, 3, 5)]
    return '#' + ''.join(f'{round(v*.1 + 255*.9):02x}' for v in rgb)


def render(data, output, png=None):
    items = list(data['item_locations'].values())
    if not items:
        raise ValueError('item_locations is empty')
    counts = Counter(str(v['zone']) for v in items)
    zones = sorted(counts)
    colors = {z: COLORS[i % len(COLORS)] for i, z in enumerate(zones)}
    auths = data.get('entity_counts_by_auth', {})
    rows = sorted(auths.items(), key=lambda pair: str(pair[1]['zone']))
    if not rows:
        # Older layouts lack worker counts; do not invent them.
        rows = [(None, {'zone': z, 'resources': counts[z]}) for z in zones]
    height = max(800, 260 + 140*len(rows))
    c = Canvas(1200, height, png is not None)
    total = sum(int(v['total']) for _, v in rows) if all('total' in v for _, v in rows) else None
    title = f'Warehouse setup: {len(rows)} Auth zones'
    if total is not None:
        title += f', {total} registered entities'
    c.text(65, 48, title, 27)
    c.text(65, 100, '(a) Resource placement', 21)
    c.text(615, 100, '(b) Entities registered with each Auth', 21)
    b = data.get('warehouse_bounds') or {
        'xmin': min(float(v['x']) for v in items), 'xmax': max(float(v['x']) for v in items),
        'ymin': min(float(v['y']) for v in items), 'ymax': max(float(v['y']) for v in items)}
    xmin, xmax, ymin, ymax = [float(b[k]) for k in ('xmin', 'xmax', 'ymin', 'ymax')]
    if xmax <= xmin:
        xmin, xmax = xmin-1, xmax+1
    if ymax <= ymin:
        ymin, ymax = ymin-1, ymax+1
    top, bottom = 140, height-130
    X = lambda x: 100 + (float(x)-xmin)/(xmax-xmin)*410
    Y = lambda y: bottom-12 - (float(y)-ymin)/(ymax-ymin)*(bottom-top-24)
    c.rect(85, top, 445, bottom-top, 'white', '#888888')
    for z in zones:
        pts = [v for v in items if str(v['zone']) == z]
        # Derive envelopes directly from resources, preserving overlapping zones.
        x0,x1 = min(X(v['x']) for v in pts),max(X(v['x']) for v in pts)
        y0,y1 = min(Y(v['y']) for v in pts),max(Y(v['y']) for v in pts)
        c.rect(x0-4,y0-4,max(8,x1-x0+8),max(8,y1-y0+8),pale(colors[z]))
    for i in range(5):
        x=xmin+(xmax-xmin)*i/4
        y=ymin+(ymax-ymin)*i/4
        c.line(X(x),top,X(x),bottom,'#eeeeee')
        c.line(85,Y(y),530,Y(y),'#dddddd')
        c.text(X(x)-12,bottom+25,f'{x:g}',14)
        c.text(22,Y(y)+5,f'{y:g}',14)
    c.text(190,bottom+53,'Warehouse x coordinate',16)
    c.text(65,127,'y coordinate',14)
    for v in items:
        c.rect(X(v['x'])-3,Y(v['y'])-3,6,6,colors[str(v['zone'])])
    for i,(auth,row) in enumerate(rows):
        z=str(row['zone']); color=colors.get(z,COLORS[i % len(COLORS)]); y=140+i*140
        c.rect(615,y,520,122,pale(color));c.rect(615,y,5,122,color)
        label=f'Auth {auth} / Zone {z}' if auth is not None else f'Zone {z}'
        c.text(635,y+30,label,22,color)
        if 'total' in row:
            c.text(1005,y+30,f"{row['total']} entities",18)
        sup=f"{row['supervisors']} supervisor(s)  ·  " if 'supervisors' in row else ''
        c.text(635,y+67,f"{sup}{row.get('resources',counts.get(z,0))} protected resources",18)
        workers=[f"{row[k]} {k}" for k in ('robots','forklifts','drones') if k in row]
        c.text(635,y+99,'  ·  '.join(workers) if workers else 'Worker counts not provided in layout',18)
    c.rect(615,height-100,9,9,'#666666')
    c.text(635,height-89,'Resource; color indicates Auth membership',16)
    c.text(65,height-42,'Shading shows resource envelopes, not exact partition boundaries; colocated resources may overlap.',14,'#555555')
    c.text(65,height-19,'Mobile-entity positions are not stored in warehouse_layout.json and are not plotted.',14,'#555555')
    c.save(output,png)


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('layout',type=Path,help='Input warehouse_layout.json')
    parser.add_argument('-o','--output',type=Path,default=Path('warehouse-setup.svg'),help='Output SVG (default: warehouse-setup.svg)')
    parser.add_argument('--png',type=Path,help='Also save PNG; requires Pillow')
    args=parser.parse_args()
    try:
        render(json.loads(args.layout.read_text(encoding='utf-8')),args.output,args.png)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        parser.exit(1,f'Error: {exc}\n')
    print(f'Saved {args.output.resolve()}')
    if args.png:
        print(f'Saved {args.png.resolve()}')


if __name__ == '__main__':
    main()
