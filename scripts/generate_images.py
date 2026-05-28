# -*- coding: utf-8 -*-
"""
占い系素材画像 一括生成スクリプト v2（時雨 / 龍脈命術）
テーマ別の本格シンボル描画版。

使い方:
  python scripts/generate_images.py
"""

import io, math, random, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SIZE = 1080
CX = CY = SIZE // 2
BASE_DIR = Path(__file__).parent.parent
ASSETS_DIR = BASE_DIR / "assets" / "images"


# ── ベース関数 ─────────────────────────────────────────────────────────

def hex_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def make_gradient(c1, c2, style='radial_in'):
    yi, xi = np.ogrid[:SIZE, :SIZE]
    c = SIZE / 2
    if style == 'radial_in':
        t = np.clip(np.sqrt((xi-c)**2+(yi-c)**2)/(c*1.42), 0, 1)
    elif style == 'radial_out':
        t = 1 - np.clip(np.sqrt((xi-c)**2+(yi-c)**2)/(c*1.42), 0, 1)
    elif style == 'diagonal':
        t = np.clip((xi+yi)/(SIZE*1.5), 0, 1)
    else:
        t = yi / SIZE
    arr = (np.array(c1,float)*(1-t[:,:,None]) + np.array(c2,float)*t[:,:,None]).astype(np.uint8)
    return Image.fromarray(arr, 'RGB')

def add_glow(img, color, radius=320, alpha=50):
    g = Image.new('RGBA', img.size, (0,0,0,0))
    ImageDraw.Draw(g).ellipse([CX-radius,CY-radius,CX+radius,CY+radius], fill=(*color,alpha))
    g = g.filter(ImageFilter.GaussianBlur(85))
    return Image.alpha_composite(img.convert('RGBA'), g).convert('RGB')

def add_vignette(img, strength=0.52):
    yi, xi = np.ogrid[:SIZE, :SIZE]
    d = np.sqrt((xi-SIZE/2)**2+(yi-SIZE/2)**2)
    v = np.clip(1 - strength*(d/math.sqrt((SIZE/2)**2*2))**1.8, 0, 1)
    arr = (np.array(img).astype(float)*v[:,:,None]).astype(np.uint8)
    return Image.fromarray(arr,'RGB')

def add_dots(img, color, count=60, r_min=1, r_max=3, alpha_max=200, seed=0):
    ov = Image.new('RGBA', img.size, (0,0,0,0))
    d  = ImageDraw.Draw(ov)
    rng = random.Random(seed)
    for _ in range(count):
        x,y = rng.randint(40,SIZE-40), rng.randint(40,SIZE-40)
        r   = rng.randint(r_min, r_max)
        a   = rng.randint(60, alpha_max)
        d.ellipse([x-r,y-r,x+r,y+r], fill=(*color,a))
    return Image.alpha_composite(img.convert('RGBA'), ov).convert('RGB')

def ov_apply(img, ov):
    return Image.alpha_composite(img.convert('RGBA'), ov).convert('RGB')


# ── 星座データ ─────────────────────────────────────────────────────────

CONSTELLATIONS = {
    'aries':       {'s':[(0.60,0.44),(0.53,0.49),(0.50,0.51),(0.44,0.48),(0.40,0.43)],
                    'l':[(0,1),(1,2),(2,3),(3,4)]},
    'taurus':      {'s':[(0.32,0.55),(0.40,0.52),(0.47,0.48),(0.54,0.46),
                          (0.57,0.52),(0.51,0.57),(0.43,0.58),(0.67,0.36)],
                    'l':[(0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,1),(2,7)]},
    'gemini':      {'s':[(0.38,0.33),(0.45,0.31),(0.39,0.42),(0.46,0.40),
                          (0.40,0.53),(0.47,0.51),(0.41,0.62),(0.48,0.60)],
                    'l':[(0,2),(2,4),(4,6),(1,3),(3,5),(5,7),(0,1),(6,7)]},
    'cancer':      {'s':[(0.43,0.42),(0.53,0.42),(0.46,0.51),(0.54,0.51),
                          (0.40,0.58),(0.59,0.58),(0.50,0.35)],
                    'l':[(0,2),(1,3),(2,3),(2,4),(3,5),(0,6),(1,6)]},
    'leo':         {'s':[(0.34,0.60),(0.41,0.55),(0.47,0.50),(0.50,0.42),
                          (0.56,0.38),(0.59,0.45),(0.62,0.55),(0.58,0.63),(0.38,0.43)],
                    'l':[(0,1),(1,2),(2,7),(7,6),(6,5),(5,4),(4,3),(3,2),(1,8),(8,3)]},
    'virgo':       {'s':[(0.35,0.38),(0.42,0.42),(0.48,0.40),(0.55,0.44),
                          (0.59,0.52),(0.53,0.58),(0.44,0.60),(0.37,0.55),(0.64,0.44)],
                    'l':[(0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,0),(3,8)]},
    'libra':       {'s':[(0.40,0.58),(0.50,0.56),(0.60,0.58),(0.45,0.46),
                          (0.55,0.46),(0.50,0.39)],
                    'l':[(0,1),(1,2),(0,3),(3,4),(4,2),(3,5),(5,4)]},
    'scorpio':     {'s':[(0.35,0.36),(0.40,0.42),(0.45,0.47),(0.50,0.50),
                          (0.55,0.53),(0.60,0.58),(0.63,0.64),(0.60,0.69),(0.56,0.66)],
                    'l':[(0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8)]},
    'sagittarius': {'s':[(0.37,0.62),(0.43,0.56),(0.49,0.50),(0.55,0.44),
                          (0.61,0.40),(0.58,0.54),(0.52,0.60),(0.44,0.44)],
                    'l':[(0,1),(1,2),(2,3),(3,4),(2,5),(5,6),(6,1),(3,7),(7,1)]},
    'capricorn':   {'s':[(0.34,0.42),(0.41,0.40),(0.49,0.42),(0.56,0.46),
                          (0.58,0.54),(0.52,0.60),(0.44,0.61),(0.37,0.55)],
                    'l':[(0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,0)]},
    'aquarius':    {'s':[(0.30,0.46),(0.38,0.44),(0.46,0.46),(0.50,0.42),
                          (0.56,0.44),(0.63,0.46),(0.42,0.55),(0.50,0.57),
                          (0.58,0.55),(0.64,0.57)],
                    'l':[(0,1),(1,2),(2,3),(3,4),(4,5),(1,6),(6,7),(7,8),(8,9),(2,7)]},
    'pisces':      {'s':[(0.37,0.35),(0.43,0.37),(0.49,0.42),(0.49,0.50),
                          (0.43,0.56),(0.37,0.58),(0.55,0.42),(0.61,0.44),
                          (0.63,0.50),(0.59,0.56)],
                    'l':[(0,1),(1,2),(2,3),(3,4),(4,5),(2,6),(6,7),(7,8),(8,9),(3,7)]},
}


# ── シンボル描画関数 ────────────────────────────────────────────────────

def draw_constellation(img, key, color, seed=0):
    const = CONSTELLATIONS.get(key, {})
    stars, lines = const.get('s',[]), const.get('l',[])
    if not stars: return img
    ov  = Image.new('RGBA', img.size, (0,0,0,0))
    d   = ImageDraw.Draw(ov)
    rng = random.Random(seed)
    # 結線
    for i,j in lines:
        x1,y1 = int(stars[i][0]*SIZE), int(stars[i][1]*SIZE)
        x2,y2 = int(stars[j][0]*SIZE), int(stars[j][1]*SIZE)
        d.line([x1,y1,x2,y2], fill=(*color,100), width=2)
    # 星
    sizes = [9,7,6,8,6,7,9,6,7,6,8,6,7,6]
    for idx,(sx,sy) in enumerate(stars):
        px,py = int(sx*SIZE), int(sy*SIZE)
        r = sizes[idx % len(sizes)]
        for gr in range(3,0,-1):
            d.ellipse([px-r-gr*4,py-r-gr*4,px+r+gr*4,py+r+gr*4], fill=(*color, 35*gr))
        d.ellipse([px-r,py-r,px+r,py+r], fill=(*color,235))
    # 背景の微小な星
    for _ in range(55):
        x,y = rng.randint(50,SIZE-50), rng.randint(50,SIZE-50)
        sr  = rng.randint(1,2)
        d.ellipse([x-sr,y-sr,x+sr,y+sr], fill=(*color, rng.randint(50,140)))
    return ov_apply(img, ov)


def draw_compass_rose(img, color):
    ov = Image.new('RGBA', img.size, (0,0,0,0))
    d  = ImageDraw.Draw(ov)
    R  = int(SIZE*0.37)
    r  = int(SIZE*0.07)
    for rr,a in [(R+35,25),(R+65,12)]:
        d.ellipse([CX-rr,CY-rr,CX+rr,CY+rr], outline=(*color,a), width=2)
    for i in range(8):
        ang   = math.radians(i*45-90)
        card  = (i%2==0)
        length = R if card else int(R*0.65)
        wf    = 0.18 if card else 0.10
        perp  = ang + math.pi/2
        hw    = int(length*wf)
        tip   = (CX+int(length*math.cos(ang)), CY+int(length*math.sin(ang)))
        base  = (CX+int(r*math.cos(ang)),      CY+int(r*math.sin(ang)))
        mid_d = r + length*0.3
        lp    = (CX+int(mid_d*math.cos(ang))+int(hw*math.cos(perp)),
                 CY+int(mid_d*math.sin(ang))+int(hw*math.sin(perp)))
        rp    = (CX+int(mid_d*math.cos(ang))-int(hw*math.cos(perp)),
                 CY+int(mid_d*math.sin(ang))-int(hw*math.sin(perp)))
        d.polygon([tip,lp,base,rp], fill=(*color, 220 if card else 155))
    for rr,a in [(int(R*0.35),65),(int(R*0.65),50)]:
        d.ellipse([CX-rr,CY-rr,CX+rr,CY+rr], outline=(*color,a), width=2)
    cr = 22
    d.ellipse([CX-cr,CY-cr,CX+cr,CY+cr], fill=(*color,200))
    d.ellipse([CX-cr//2,CY-cr//2,CX+cr//2,CY+cr//2], fill=(0,0,0,180))
    return ov_apply(img, ov)


def draw_snowflake(img, color):
    ov = Image.new('RGBA', img.size, (0,0,0,0))
    d  = ImageDraw.Draw(ov)
    R  = int(SIZE*0.37)
    for arm in range(6):
        ang = math.radians(arm*60)
        ex  = CX+int(R*math.cos(ang)); ey = CY+int(R*math.sin(ang))
        d.line([CX,CY,ex,ey], fill=(*color,200), width=4)
        for frac in [0.35, 0.60, 0.82]:
            bx = CX+int(R*frac*math.cos(ang)); by = CY+int(R*frac*math.sin(ang))
            bl = int(R*(0.26-frac*0.14))
            for ba in [30,-30]:
                ba_r = ang+math.radians(ba)
                d.line([bx,by,bx+int(bl*math.cos(ba_r)),by+int(bl*math.sin(ba_r))],
                       fill=(*color,180), width=3)
        tr = 10
        d.ellipse([ex-tr,ey-tr,ex+tr,ey+tr], fill=(*color,180))
    hr = 28
    hp = [(CX+int(hr*math.cos(math.radians(i*60))),
           CY+int(hr*math.sin(math.radians(i*60)))) for i in range(6)]
    d.polygon(hp, fill=(*color,200))
    for rr,a in [(R+38,22),(R+68,11)]:
        d.ellipse([CX-rr,CY-rr,CX+rr,CY+rr], outline=(*color,a), width=2)
    return ov_apply(img, ov)


def draw_sun(img, color):
    ov  = Image.new('RGBA', img.size, (0,0,0,0))
    d   = ImageDraw.Draw(ov)
    RL  = int(SIZE*0.39)
    RS  = int(SIZE*0.27)
    cr  = int(SIZE*0.12)
    for i in range(16):
        ang   = math.radians(i*22.5-90)
        length = RL if i%2==0 else RS
        perp  = ang+math.pi/2
        hw    = int(cr*0.5)
        bl    = (CX+int(cr*0.8*math.cos(ang))+int(hw*math.cos(perp)),
                 CY+int(cr*0.8*math.sin(ang))+int(hw*math.sin(perp)))
        br    = (CX+int(cr*0.8*math.cos(ang))-int(hw*math.cos(perp)),
                 CY+int(cr*0.8*math.sin(ang))-int(hw*math.sin(perp)))
        tip   = (CX+int(length*math.cos(ang)), CY+int(length*math.sin(ang)))
        d.polygon([bl,tip,br], fill=(*color, 200 if i%2==0 else 140))
    for r2,a in [(cr+22,40),(cr+10,80)]:
        d.ellipse([CX-r2,CY-r2,CX+r2,CY+r2], fill=(*color,a))
    d.ellipse([CX-cr,CY-cr,CX+cr,CY+cr], fill=(*color,225))
    return ov_apply(img, ov)


def draw_spring_flowers(img, color):
    ov  = Image.new('RGBA', img.size, (0,0,0,0))
    d   = ImageDraw.Draw(ov)
    rng = random.Random(12345)
    bright = tuple(min(255,c+70) for c in color)
    configs = [
        (CX,CY,92),(CX-220,CY-185,58),(CX+210,CY-165,62),
        (CX-255,CY+155,52),(CX+235,CY+175,54),
        (CX,CY-305,42),(CX+305,CY+20,40),(CX-315,CY+30,38),
    ]
    for fx,fy,r in configs:
        for p in range(5):
            pa  = math.radians(p*72)
            off = r*0.55
            pcx = fx+int(off*math.cos(pa)); pcy = fy+int(off*math.sin(pa))
            pr  = int(r*0.58)
            d.ellipse([pcx-pr,pcy-pr,pcx+pr,pcy+pr], fill=(*color,115))
        cr2 = int(r*0.30)
        d.ellipse([fx-cr2,fy-cr2,fx+cr2,fy+cr2], fill=(*bright,205))
    for _ in range(28):
        x,y = rng.randint(80,SIZE-80), rng.randint(80,SIZE-80)
        pr  = rng.randint(7,16)
        d.ellipse([x-pr,y-pr,x+pr,y+pr], fill=(*color, rng.randint(45,110)))
    return ov_apply(img, ov)


def draw_autumn_leaves(img, color):
    ov  = Image.new('RGBA', img.size, (0,0,0,0))
    d   = ImageDraw.Draw(ov)
    def leaf(cx,cy,size,angle_deg,alpha=175):
        ang = math.radians(angle_deg)
        pts = []
        for t in range(0,361,8):
            tr = math.radians(t)
            lx = size*0.40*math.cos(tr); ly = size*math.sin(tr)
            rx = lx*math.cos(ang)-ly*math.sin(ang)
            ry = lx*math.sin(ang)+ly*math.cos(ang)
            pts.append((cx+rx, cy+ry))
        if len(pts)>=3:
            d.polygon(pts, fill=(*color,alpha))
        # 中心脈
        p_ang = ang+math.pi/2
        d.line([cx-int(size*0.75*math.cos(p_ang)), cy-int(size*0.75*math.sin(p_ang)),
                cx+int(size*0.95*math.cos(p_ang)), cy+int(size*0.95*math.sin(p_ang))],
               fill=(*tuple(min(255,c+50) for c in color), min(255,alpha+45)), width=2)
    configs = [
        (CX,CY,135,15,205),(CX-205,CY-205,88,-30,175),(CX+195,CY-195,92,45,165),
        (CX-225,CY+185,82,-45,155),(CX+205,CY+205,78,60,150),
        (CX+325,CY-85,62,20,130),(CX-305,CY+85,56,-60,120),(CX+65,CY-315,52,80,120),
    ]
    for cx,cy,s,a,al in configs:
        leaf(cx,cy,s,a,al)
    return ov_apply(img, ov)


def draw_yinyang(img, color_a, color_b):
    """numpy で精密描画"""
    arr  = np.zeros((SIZE,SIZE,4), dtype=np.uint8)
    R    = SIZE*0.30
    yi,xi = np.mgrid[:SIZE,:SIZE]
    dist = np.sqrt((xi-SIZE/2)**2+(yi-SIZE/2)**2)
    in_c = dist <= R
    d_upper = np.sqrt((xi-SIZE/2)**2+(yi-(SIZE/2-R/2))**2)
    d_lower = np.sqrt((xi-SIZE/2)**2+(yi-(SIZE/2+R/2))**2)
    yang = in_c & ((xi >= SIZE/2) | (d_upper <= R/2)) & (d_lower > R/2)
    yin  = in_c & ~yang
    dot_r = R/6
    d_ud  = np.sqrt((xi-SIZE/2)**2+(yi-(SIZE/2-R/2))**2) <= dot_r
    d_ld  = np.sqrt((xi-SIZE/2)**2+(yi-(SIZE/2+R/2))**2) <= dot_r
    al = 200
    arr[yin  & ~d_ld, :3] = color_b[:3]; arr[yin  & ~d_ld, 3] = al
    arr[yang & ~d_ud, :3] = color_a[:3]; arr[yang & ~d_ud, 3] = al
    arr[d_ud, :3] = color_b[:3]; arr[d_ud, 3] = al+20
    arr[d_ld, :3] = color_a[:3]; arr[d_ld, 3] = al+20
    ov = Image.fromarray(arr,'RGBA')
    dr = ImageDraw.Draw(ov)
    RI = int(R)
    for rr,a in [(RI+28,60),(RI+55,28)]:
        dr.ellipse([CX-rr,CY-rr,CX+rr,CY+rr], outline=(*color_a,a), width=2)
    return ov_apply(img, ov)


def draw_sacred_geometry(img, color):
    ov = Image.new('RGBA', img.size, (0,0,0,0))
    d  = ImageDraw.Draw(ov)
    r  = int(SIZE*0.135)
    positions = [(0,0)]
    for i in range(6):
        positions.append((int(r*math.cos(math.radians(i*60))),
                          int(r*math.sin(math.radians(i*60)))))
    for i in range(6):
        positions.append((int(r*2*math.cos(math.radians(i*60))),
                          int(r*2*math.sin(math.radians(i*60)))))
    for i in range(12):
        ox = int(r*2*math.cos(math.radians(i*30+15)))
        oy = int(r*2*math.sin(math.radians(i*30+15)))
        positions.append((ox,oy))
    for ox,oy in positions:
        pcx,pcy = CX+ox, CY+oy
        d.ellipse([pcx-r,pcy-r,pcx+r,pcy+r], outline=(*color,95), width=2)
    d.ellipse([CX-r,CY-r,CX+r,CY+r], fill=(*color,35))
    outer = int(r*3.15)
    d.ellipse([CX-outer,CY-outer,CX+outer,CY+outer], outline=(*color,55), width=3)
    return ov_apply(img, ov)


def draw_mandala(img, color):
    ov = Image.new('RGBA', img.size, (0,0,0,0))
    d  = ImageDraw.Draw(ov)
    for r2,w,a in [(385,3,22),(325,2,32),(265,2,38),(205,3,46),(145,2,52),(85,3,58)]:
        d.ellipse([CX-r2,CY-r2,CX+r2,CY+r2], outline=(*color,a), width=w)
    for ang in range(0,360,45):
        rad = math.radians(ang)
        d.line([CX,CY,CX+int(385*math.cos(rad)),CY+int(385*math.sin(rad))],
               fill=(*color,28), width=1)
    for i in range(8):
        ang = math.radians(i*45)
        pcx = CX+int(200*0.5*math.cos(ang)); pcy = CY+int(200*0.5*math.sin(ang))
        d.ellipse([pcx-68,pcy-68,pcx+68,pcy+68], outline=(*color,55), width=2)
    pts = []
    for i in range(16):
        ang = math.radians(i*22.5)
        r3 = 82 if i%2==0 else 46
        pts.append((CX+int(r3*math.cos(ang)), CY+int(r3*math.sin(ang))))
    d.polygon(pts, outline=(*color,80), width=2)
    return ov_apply(img, ov)


def draw_eye_triangle(img, color):
    ov = Image.new('RGBA', img.size, (0,0,0,0))
    d  = ImageDraw.Draw(ov)
    R  = int(SIZE*0.31)
    pts = [(CX, CY-R),
           (CX-int(R*0.87), CY+R//2),
           (CX+int(R*0.87), CY+R//2)]
    d.polygon(pts, outline=(*color,155), width=5)
    ew,eh  = int(R*0.55), int(R*0.29)
    eye_cy = CY+int(R*0.05)
    d.ellipse([CX-ew,eye_cy-eh,CX+ew,eye_cy+eh], outline=(*color,185), width=3)
    pr = int(R*0.12)
    d.ellipse([CX-pr,eye_cy-pr,CX+pr,eye_cy+pr], fill=(*color,205))
    ipr = int(pr*0.44)
    d.ellipse([CX-ipr,eye_cy-ipr,CX+ipr,eye_cy+ipr], fill=(0,0,0,205))
    for i in range(12):
        ang = math.radians(i*30)
        r1,r2 = int(R*0.40), int(R*0.56)
        d.line([CX+int(r1*math.cos(ang)), eye_cy+int(r1*math.sin(ang)),
                CX+int(r2*math.cos(ang)), eye_cy+int(r2*math.sin(ang))],
               fill=(*color,75), width=2)
    for rr,a in [(R+45,28),(R+78,13)]:
        d.ellipse([CX-rr,CY-rr,CX+rr,CY+rr], outline=(*color,a), width=2)
    return ov_apply(img, ov)


# ── 画像定義テーブル ───────────────────────────────────────────────────

IMAGES = {
    # 星座
    'zodiac/aries.jpg':        ('radial_in', '#0e0827','#2c1b68','constellation:aries',      (255,190, 90)),
    'zodiac/taurus.jpg':       ('diagonal',  '#081808','#1a4a2a','constellation:taurus',     (100,220,140)),
    'zodiac/gemini.jpg':       ('radial_in', '#180818','#4a1a78','constellation:gemini',     (200,140,255)),
    'zodiac/cancer.jpg':       ('linear',    '#08182e','#184a6e','constellation:cancer',     (140,195,255)),
    'zodiac/leo.jpg':          ('radial_out','#180800','#6a2800','constellation:leo',        (255,175, 45)),
    'zodiac/virgo.jpg':        ('diagonal',  '#081808','#385a28','constellation:virgo',      (140,215,170)),
    'zodiac/libra.jpg':        ('radial_in', '#18182e','#38387a','constellation:libra',      (175,175,255)),
    'zodiac/scorpio.jpg':      ('linear',    '#180000','#580a1a','constellation:scorpio',    (255, 95, 95)),
    'zodiac/sagittarius.jpg':  ('diagonal',  '#08081a','#1a1a58','constellation:sagittarius',(95,145,255)),
    'zodiac/capricorn.jpg':    ('radial_in', '#0a0a0a','#28283a','constellation:capricorn',  (175,180,200)),
    'zodiac/aquarius.jpg':     ('linear',    '#08182e','#086a8e','constellation:aquarius',   ( 85,215,255)),
    'zodiac/pisces.jpg':       ('radial_out','#080a2e','#182a7a','constellation:pisces',     (145,175,255)),
    'zodiac/general.jpg':      ('radial_in', '#080a2e','#281a5a','sacred_geometry',          (215,195,255)),
    # 方位
    'direction/north.jpg':     ('radial_in', '#08082e','#082a6e','compass',(90,195,255)),
    'direction/northeast.jpg': ('diagonal',  '#08082e','#183a6e','compass',(120,205,255)),
    'direction/east.jpg':      ('linear',    '#1a1a08','#4a4808','compass',(215,200, 95)),
    'direction/southeast.jpg': ('diagonal',  '#1a0808','#5a2808','compass',(255,165, 75)),
    'direction/south.jpg':     ('radial_out','#280808','#680808','compass',(255,115, 75)),
    'direction/southwest.jpg': ('diagonal',  '#281808','#5a3808','compass',(255,155, 55)),
    'direction/west.jpg':      ('linear',    '#081808','#1a4820','compass',( 95,200,145)),
    'direction/northwest.jpg': ('diagonal',  '#081a2e','#183a5e','compass',(115,200,220)),
    'direction/compass.jpg':   ('radial_in', '#08081a','#18184a','compass',(175,195,255)),
    # 季節
    'season/spring.jpg': ('diagonal', '#28083a','#e0609a','spring_flowers',(255,220,235)),
    'season/summer.jpg': ('radial_in','#08182e','#087a9e','sun',           (255,230,100)),
    'season/autumn.jpg': ('diagonal', '#3a1200','#c05800','autumn_leaves', (255,200,120)),
    'season/winter.jpg': ('radial_in','#08082e','#283a6e','snowflake',     (210,230,255)),
    # 風水
    'fengshui/general.jpg': ('diagonal', '#081808','#1a4828','yinyang',(90,200,150)),
    'fengshui/water.jpg':   ('radial_in','#08082e','#08285e','mandala', (75,180,220)),
    # 汎用
    'general/fortune_1.jpg': ('radial_in', '#08082e','#280a5a','mandala',        (200,175,255)),
    'general/fortune_2.jpg': ('diagonal',  '#081818','#083a4a','sacred_geometry',(95,200,220)),
    'general/fortune_3.jpg': ('linear',    '#180818','#481848','mandala',        (220,145,255)),
    'general/mystery_1.jpg': ('radial_out','#180808','#58081a','eye_triangle',   (255,125,125)),
    'general/mystery_2.jpg': ('radial_in', '#080808','#18183a','eye_triangle',   (145,145,255)),
    'general/ichi_gon.jpg':  ('diagonal',  '#181800','#484800','mandala',        (255,220, 95)),
    'general/menu.jpg':      ('linear',    '#180800','#482800','mandala',        (255,195, 95)),
}


# ── 生成メイン ─────────────────────────────────────────────────────────

def generate(filename, style, c1, c2, deco, accent):
    seed = abs(hash(filename)) % 100000
    img  = make_gradient(hex_rgb(c1), hex_rgb(c2), style)
    img  = add_glow(img, accent, radius=330, alpha=45)

    if deco.startswith('constellation:'):
        img = draw_constellation(img, deco.split(':')[1], accent, seed)
    elif deco == 'compass':
        img = draw_compass_rose(img, accent)
        img = add_dots(img, accent, 18, 1, 2, seed=seed)
    elif deco == 'snowflake':
        img = draw_snowflake(img, accent)
        img = add_dots(img, accent, 45, 1, 2, seed=seed)
    elif deco == 'sun':
        img = draw_sun(img, accent)
    elif deco == 'spring_flowers':
        img = draw_spring_flowers(img, accent)
        img = add_dots(img, accent, 25, 1, 2, seed=seed)
    elif deco == 'autumn_leaves':
        img = draw_autumn_leaves(img, accent)
    elif deco == 'yinyang':
        dark = tuple(max(0,c-80) for c in accent)
        img  = draw_yinyang(img, accent, dark)
    elif deco == 'sacred_geometry':
        img = draw_sacred_geometry(img, accent)
        img = add_dots(img, accent, 35, 1, 2, seed=seed)
    elif deco == 'mandala':
        img = draw_mandala(img, accent)
        img = add_dots(img, accent, 28, 1, 2, seed=seed)
    elif deco == 'eye_triangle':
        img = draw_eye_triangle(img, accent)
        img = add_dots(img, accent, 22, 1, 2, seed=seed)

    return add_vignette(img, 0.50)


def main():
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    total, done = len(IMAGES), 0
    for filename, (style,c1,c2,deco,accent) in IMAGES.items():
        out = ASSETS_DIR / filename
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            generate(filename,style,c1,c2,deco,accent).save(out,'JPEG',quality=92,optimize=True)
            done += 1
            print(f'  OK [{done:02d}/{total}] {filename}')
        except Exception as e:
            print(f'  NG {filename}: {e}')
            import traceback; traceback.print_exc()
    print(f'\n{done}/{total} 枚生成完了')

if __name__ == '__main__':
    main()
