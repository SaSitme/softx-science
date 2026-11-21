"""Numerics using the local ASTM G173-03 global tilt reference spectrum."""
from bisect import bisect_left
import csv
from functools import lru_cache
import math
from pathlib import Path

Q = 1.602176634e-19
H = 6.62607015e-34
C = 299792458
K = 1.380649e-23
REFERENCE = "ASTM G173-03 AM1.5G global tilt, SMARTS 2.9.2"
SOURCE = "https://www.nlr.gov/grid/solar-resource/spectra-am1.5"


@lru_cache(maxsize=1)
def spectrum():
    with (Path(__file__).parent/"reference"/"ASTMG173.csv").open() as file:
        next(file)
        table=[(float(row["wavelength"]),float(row["global"])) for row in csv.DictReader(file)]
    return [pair[0] for pair in table],[pair[1] for pair in table]


def interpolate(x, y, query):
    index=bisect_left(x,query)
    if index==0:
        return y[0]
    if index==len(x):
        return y[-1]
    alpha=(query-x[index-1])/(x[index]-x[index-1])
    return y[index-1]+alpha*(y[index]-y[index-1])


def integral(grid, values):
    return sum((b-a)*(p+q)/2 for a,b,p,q in zip(grid,grid[1:],values,values[1:]))


def integrate_eqe(points, axes):
    x_unit=axes.get("x",{}).get("unit")
    y_unit=axes.get("y",{}).get("unit")
    if x_unit not in ("nm","µm","um") or y_unit not in ("%","1"):
        raise ValueError("Для расчёта нужны длина волны в nm или µm и EQE в % или долях (1)")
    xfactor=1 if x_unit=="nm" else 1000
    yfactor=.01 if y_unit=="%" else 1
    series=sorted((p["x"]*xfactor,p["y"]*yfactor) for p in points if p["x"] is not None and p["y"] is not None)
    if len(series)<2:
        raise ValueError("Для интегрирования нужны минимум две откалиброванные точки")
    xs,ys=zip(*series)
    if len(xs)!=len(set(xs)) or min(ys)<0:
        raise ValueError("Нужна неотрицательная EQE с уникальными длинами волн")
    wavelength,power=spectrum()
    if xs[0]<wavelength[0] or xs[-1]>wavelength[-1]:
        raise ValueError("Спектр ограничен диапазоном 280–4000 нм")
    grid=sorted({*xs,*(w for w in wavelength if xs[0]<w<xs[-1])})
    values=[interpolate(xs,ys,w)*interpolate(wavelength,power,w)*(w*1e-9)/(H*C) for w in grid]
    return .1*Q*integral(grid,values), [xs[0],xs[-1]]


@lru_cache(maxsize=256)
def detailed_balance(bandgap, temperature):
    """Ideal one-emitting-side device with unit absorption above Eg and AM1.5G."""
    wavelength,power=spectrum()
    cutoff=H*C/(bandgap*Q)*1e9
    if not wavelength[0]<cutoff<wavelength[-1] or not 200<=temperature<=450:
        return None
    grid=[w for w in wavelength if w<cutoff]+[cutoff]
    photon=[interpolate(wavelength,power,w)*w*1e-9/(H*C) for w in grid]
    jsc=Q*integral(grid,photon)
    thermal=K*temperature
    lower=bandgap*Q/thermal
    step=.05
    energies=[lower+i*step for i in range(1001)]
    emission=[u*u/math.expm1(u) if u<700 else 0 for u in energies]
    j0=Q*2*math.pi*thermal**3/(H**3*C**2)*integral(energies,emission)
    if j0<=0 or jsc<=0:
        return None
    upper=math.log1p(jsc/j0)
    def power_at(v):
        return thermal/Q*v*(jsc-j0*math.expm1(v))
    a,b=0,upper
    for _ in range(55):
        c,d=(2*a+b)/3,(a+2*b)/3
        if power_at(c)<power_at(d):
            a=c
        else:
            b=d
    return {"jsc_mA_cm2":jsc*.1,"pce_percent":100*power_at((a+b)/2)/integral(wavelength,power)}
