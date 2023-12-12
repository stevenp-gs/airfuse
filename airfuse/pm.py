from .parser import parse_args
from .mod import get_model
from .obs import pair_airnow, pair_purpleair
from .models import applyfusion, get_fusions
from .ensemble import distweight
from .util import df2nc
import numpy as np
import time
import pyproj
import os
import logging
from . import __version__

args = parse_args()
date = args.startdate


model = args.model.upper()
obskey = 'pm25'

vardescs = {
  'NAQFC': 'NOAA Forecast (NAQFC)',
  f'IDW_AN_{obskey}': f'NN weighted (n=10, d**-5) AirNow {obskey}',
  f'VNA_AN_{obskey}': f'VN weighted (n=nv, d**-2) AirNow {obskey}',
  'aIDW_AN': 'IDW of AirNow bias added to the NOAA NAQFC forecast',
  'aVNA_AN': 'VNA of AirNow bias added to the NOAA NAQFC forecast',
  'FUSED_aVNA': 'Fused surface from aVNA PurpleAir and aVNA AirNow',
  'FUSED_aIDW': 'Fused surface from aIDW PurpleAir and aIDW AirNow',
}
varattrs = {
    k: dict(description=v, units='micrograms/m**3')
    for k, v in vardescs.items()
}

fdesc = f"""title: AirFuse ({__version__}) {obskey}
author: Barron H. Henderson
institution: US Environmental Protection Agency
citation: AirFuse - a light weight data fusion system for
description:
    Fusion of observations (AirNow and PurpleAir) using residual
    interpolation and correction of the NOAA NAQFC forecast model. The bias is
    estimated in real-time using AirNow and PurpleAir measurements. It is
    interpolated using the average of either nearest neighbors (IDW) or the
    Voronoi/Delaunay neighbors (VNA). IDW uses 10 nearest neighbors with a
    weight equal to distance to the -5 power. VNA uses just the Delaunay
    neighbors and a weight equal to distnace to the -2 power. The aVNA and aIDW
    use an additive bias correction using these interpolations. Each algorithm
    is applied to both AirNow monitors and PurpleAir low-cost sensors. The
    "FUSED" surfaces combine both surfaces using weights based on distance to
    nearest obs.
"""

# edit outdir to change destination (e.g., %Y%m%d instead of %Y/%m/%d)
outdir = f'{date:%Y/%m/%d}'
os.makedirs(outdir, exist_ok=True)
stem = f'{outdir}/Fusion_PM25_{model}_{date:%Y-%m-%dT%H}Z'
logpath = f'{stem}.log'
pacvpath = f'{stem}_PurpleAir_CV.csv'
ancvpath = f'{stem}_AirNow_CV.csv'
fusepath = f'{stem}.{args.format}'


found = set()
for path in [pacvpath, ancvpath, fusepath]:
    if os.path.exists(path):
        found.add(path)

if len(found) > 0 and not args.overwrite:
    foundstr = ' '.join(found)
    raise IOError(f'Outputs exist; delete or use -O to continue:\n{foundstr}')

# Divert all logging during this script to the associated
# log file at the INFO level.
logging.basicConfig(filename=logpath, level=logging.INFO)
logging.info(f'AirFuse {__version__}')
bbox = args.bbox

pm = get_model(date, key=obskey, bbox=bbox, model=model)

# When merging fused surfaces, PurpleAir is treated as never being closer than
# half the diagonal distance. Thsi ensures that AirNow will be the preferred
# estimate within a grid cell if it exists. This is particularly reasonable
# given that the PA coordinates are averaged
dx = np.diff(pm.x).mean()
dy = np.diff(pm.y).mean()
pamindist = ((dx**2 + dy**2)**.5) / 2

proj = pyproj.Proj(pm.attrs['crs_proj4'], preserve_units=True)
logging.info(proj.srs)

andf = pair_airnow(date, bbox, proj, pm, obskey)
padf = pair_purpleair(date, bbox, proj, pm, obskey)

models = get_fusions()

if args.cv_only:
    tgtdf = None
else:
    outdf = pm.to_dataframe().reset_index()
    tgtdf = outdf.query(f'{pm.name} == {pm.name}').copy()

# Apply all models to AirNow observations
for mkey, mod in models.items():
    logging.info(f'AN {mkey} begin')
    t0 = time.time()
    applyfusion(
        mod, f'{mkey}_AN', andf, tgtdf=tgtdf, obskey=obskey, modkey=pm.name,
        verbose=9
    )
    t1 = time.time()
    logging.info(f'AN {mkey} {t1 - t0:.0f}s')

# Apply all models to PurpleAir observations
for mkey, mod in models.items():
    logging.info(f'PA {mkey} begin')
    t0 = time.time()
    applyfusion(
        mod, f'{mkey}_PA', padf, tgtdf=tgtdf, loodf=andf,
        obskey=obskey, modkey=pm.name, verbose=9
    )
    t1 = time.time()
    logging.info(f'PA {mkey} finish: {t1 - t0:.0f}s')

# Force PA downweighting in same cell and neighboring cell.
# Has no effect on LOO because nearest (ie, same cell) is already removed.
andf['LOO_VNA_PA_DIST_ADJ'] = np.maximum(andf['LOO_VNA_PA_DIST'], pamindist)
# Perform fusions on LOO data for aVNA
distkeys = ['LOO_VNA_AN_DIST', 'LOO_VNA_PA_DIST_ADJ']
valkeys = ['LOO_aVNA_AN', 'LOO_aVNA_PA']
wgtdf = distweight(
    andf, distkeys, valkeys, modkey=model, ykey='FUSED_aVNA', power=-2,
    add=True, LOO_aVNA_PA=0.25
)
# Perform fusions on LOO data for eVNA
valkeys = ['LOO_eVNA_AN', 'LOO_eVNA_PA']
wgtdf = distweight(
    andf, distkeys, valkeys, modkey=model, ykey='FUSED_eVNA', power=-2,
    add=True, LOO_eVNA_PA=0.25
)
# Perform fusions on LOO data for eVNA
valkeys = ['LOO_aIDW_AN', 'LOO_aIDW_PA']
wgtdf = distweight(
    andf, distkeys, valkeys, modkey=model, ykey='FUSED_aIDW', power=-2,
    add=True, LOO_aIDW_PA=0.25
)
# Save results to disk as CSV files
andf.to_csv(ancvpath, index=False)
padf.to_csv(pacvpath, index=False)

if not args.cv_only:
    # Force PA downweighting in same cell and neighboring cell.
    tgtdf['VNA_PA_DIST_ADJ'] = np.maximum(tgtdf['VNA_PA_DIST'], pamindist)
    # Perform fusions on Target Dataset for aVNA
    distkeys = ['VNA_AN_DIST', 'VNA_PA_DIST_ADJ']
    valkeys = ['aVNA_AN', 'aVNA_PA']
    wgtdf = distweight(
        tgtdf, distkeys, valkeys, modkey=model, ykey='FUSED_aVNA', power=-2,
        add=True, aVNA_PA=0.25
    )
    # Perform fusions on Target Dataset for aIDW
    distkeys = ['VNA_AN_DIST', 'VNA_PA_DIST_ADJ']
    valkeys = ['aIDW_AN', 'aIDW_PA']
    wgtdf = distweight(
        tgtdf, distkeys, valkeys, modkey=model, ykey='FUSED_aIDW', power=-2,
        add=True, aIDW_PA=0.25
    )
    # Save final results to disk
    if fusepath.endswith('.nc'):
        metarow = tgtdf.iloc[0]
        fileattrs = {
            'description': fdesc, 'crs_proj4': proj.srs,
            'reftime': metarow['reftime'].strftime('%Y-%m-%dT%H:%M:%S%z'),
            'sigma': metarow['sigma']
        }
        tgtds = df2nc(tgtdf, varattrs, fileattrs)
        tgtds.to_netcdf(fusepath)
    else:
        # Defualt to csv
        tgtdf.to_csv(fusepath, index=False)


logging.info('Successful Completion')
